"""Study saved Instagram reels locally and keep compact, structured summaries.

Per reel: fetch media through Apify (the only network step), then analyze on
this machine: ffmpeg scene cuts, Apple Vision OCR, near-duplicate frame
merging, ONE vision-model call on a labeled contact sheet, mlx-whisper
transcript with word timing, and giveaway ("comment X", "DM me X", "link in
bio") detection. One JSON per reel is written, plus one small hook frame. With
--archive DIR the media and every analyzed frame are kept in DIR/<code>/.

Use the `reel-watcher study` command. Default is a dry run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import media
from . import model as _model
from .model import Vision

APIFY_ACTOR = os.environ.get("APIFY_ACTOR", "apify~instagram-scraper")
APIFY_BASE = os.environ.get("APIFY_API_BASE", "https://api.apify.com/v2")
CDN_WORKERS = 6
FAST = False
TURBO = True  # default; --detailed restores the per-frame VLM path
DEDUPE_HASH_DIST = 9  # dhash bits (of 64) above which two frames count as different
MAX_UNIQUE_FRAMES = 9
TILE_PX = 512
SYNTH_TOKENS = 1100
HOOK_TIMES = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5]
FAST_HOOK_TIMES = [0.0, 1.0, 2.0]
FAST_LATER = 4
LATER_INTERVAL = 3.0
MAX_LATER_FRAMES = 10
WORKFLOW_COLLECTION_RE = re.compile(os.environ.get("REEL_WATCHER_WORKFLOW_RE", r"workflow|tutorial"), re.I)
VIEWER_CONTEXT = os.environ.get("REEL_WATCHER_CONTEXT", "a viewer who saved it to learn from it")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- input / state

def parse_input(path: Path) -> list[dict]:
    items, seen = [], set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("\t")
        url = parts[0].strip()
        code_id = media.id_from_url(url)
        if not code_id or not code_id.startswith("ig_"):
            log(f"skip non-instagram or unparseable url: {url[:80]}")
            continue
        code = code_id[3:]
        if code in seen:
            continue
        seen.add(code)
        # TSV is "url<TAB>type<TAB>collections" (collections comma separated); older inputs are "url<TAB>collections".
        raw = parts[-1] if len(parts) > 1 else ""
        if len(parts) == 2 and raw.strip() in ("reel", "post", "carousel", "photo", "video"):
            raw = ""
        colls = [c.strip() for c in re.split(r"[|;,]", raw) if c.strip()]
        items.append({"url": url, "code": code, "collections": colls, "kind": "carousel" if "/p/" in url else "reel"})
    return items


def is_done(study: Path, code: str, retry_failed: bool) -> bool:
    if (study / f"{code}.json").exists():
        return True
    return (not retry_failed) and code in failed_codes(study)


def failed_codes(study: Path) -> set[str]:
    f = study / "_failed.jsonl"
    if not f.exists():
        return set()
    out = set()
    for l in f.read_text().splitlines():
        try:
            out.add(json.loads(l)["code"])
        except (ValueError, KeyError):
            pass
    return out


def append_jsonl(path: Path, obj: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------- download

# ---------------------------------------------------------------- apify route (primary)

def apify_token() -> str | None:
    """The Apify token comes from the APIFY_TOKEN environment variable only."""
    return os.environ.get("APIFY_TOKEN", "").strip() or None


def http_json(method: str, url: str, body: dict | None = None, timeout: int = 120, token: str | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": "reel-watcher",
                                          **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "null")


def apify_fetch(urls: list[str], token: str, cap_usd: float, poll_s: float = 5.0, max_wait_s: float = 900.0) -> tuple[list[dict], dict]:
    """Run the actor on directUrls, wait, return (dataset items, run summary). One attempt, no retries."""
    q = f"?maxTotalChargeUsd={cap_usd}"
    run = http_json("POST", f"{APIFY_BASE}/acts/{APIFY_ACTOR}/runs{q}",
                    {"directUrls": urls, "resultsType": "posts", "resultsLimit": 1, "addParentData": False}, token=token)["data"]
    t0 = time.time()
    while run.get("status") in ("READY", "RUNNING"):
        if time.time() - t0 > max_wait_s:
            raise ApifyError(f"run {run.get('id')} still {run.get('status')} after {max_wait_s:.0f}s")
        time.sleep(poll_s)
        run = http_json("GET", f"{APIFY_BASE}/actor-runs/{run['id']}", token=token)["data"]
    if run.get("status") != "SUCCEEDED":
        raise ApifyError(f"run {run.get('id')} ended {run.get('status')}: {run.get('statusMessage')}")
    items = http_json("GET", f"{APIFY_BASE}/datasets/{run['defaultDatasetId']}/items?clean=true&format=json", token=token)
    return items or [], {"run_id": run.get("id"), "usd": run.get("usageTotalUsd"), "status": run.get("status"),
                         "items": len(items or [])}


class ApifyError(Exception):
    pass


def apify_meta(it: dict) -> dict:
    def pos(v):
        return v if isinstance(v, (int, float)) and v >= 0 else None
    ts = it.get("timestamp") or ""
    return {
        "caption": (it.get("caption") or "")[:2000],
        "author": it.get("ownerUsername"), "author_id": it.get("ownerId"),
        "likes": pos(it.get("likesCount")),
        "views": pos(it.get("videoViewCount")) if pos(it.get("videoViewCount")) is not None else pos(it.get("videoPlayCount")),
        "comments": pos(it.get("commentsCount")),
        "post_date": str(ts)[:10] or None, "duration_s": it.get("videoDuration"),
        "music_info": music_info_raw(it),
    }


# ---------------------------------------------------------------- music capture

MUSIC_LINE_RE = re.compile(r"(?:[\u266a\u266b\U0001F3B5\U0001F3B6]|\b(?:music|song|track|audio|sound)\s*[:\-])\s*(.{3,120})", re.I)
MUSIC_SPLIT_RE = re.compile(r"\s+(?:-|by|\u2013)\s+", re.I)


def music_info_raw(it: dict) -> dict | None:
    """Instagram audio attribution from the Apify item (musicInfo), minus signed CDN urls."""
    mi = it.get("musicInfo")
    if not isinstance(mi, dict) or not mi:
        return None
    keep = ("song_name", "artist_name", "uses_original_audio", "audio_id", "should_mute_audio")
    return {k: mi.get(k) for k in keep if k in mi}


def music_from_info(mi: dict | None) -> list[dict]:
    if not mi:
        return []
    title, artist = (mi.get("song_name") or "").strip(), (mi.get("artist_name") or "").strip()
    if not title and not artist:
        return []
    orig = bool(mi.get("uses_original_audio")) or title.lower() == "original audio"
    aid = mi.get("audio_id")
    return [{"title": title or None, "artist": artist or None, "source": "instagram_audio_metadata",
             "is_original_audio": orig, "audio_id": str(aid) if aid else None,
             "recommended_by_creator": False, "confidence": 0.95}]


def music_from_text(caption: str, analysis: dict | None) -> list[dict]:
    """Tracks named in the caption (note lines) or listed by the VLM as music_mentions."""
    out = []
    for line in (caption or "").splitlines():
        m = MUSIC_LINE_RE.search(line)
        if not m:
            continue
        txt = m.group(1).strip(" .*_")
        parts = MUSIC_SPLIT_RE.split(txt, maxsplit=1)
        title, artist = (parts[0], parts[1]) if len(parts) == 2 else (txt, None)
        out.append({"title": title.strip(), "artist": artist.strip() if artist else None, "source": "caption",
                    "is_original_audio": False, "audio_id": None, "recommended_by_creator": False, "confidence": 0.6})
    a = analysis if isinstance(analysis, dict) else {}
    for mm in a.get("music_mentions") or []:
        if not isinstance(mm, dict) or not (mm.get("title") or "").strip():
            continue
        where = str(mm.get("where") or "on_screen")
        out.append({"title": mm["title"].strip(), "artist": (mm.get("artist") or "").strip() or None,
                    "source": f"vlm_{where}", "is_original_audio": False, "audio_id": None,
                    "recommended_by_creator": bool(mm.get("recommended")), "confidence": 0.5})
    return out


def build_music(meta: dict, analysis: dict | None) -> list[dict]:
    """Per-reel music list. Order: platform metadata, then text-derived entries (deduped by title)."""
    res = music_from_info((meta or {}).get("music_info"))
    seen = {(m["title"] or "").lower() for m in res}
    for m in music_from_text((meta or {}).get("caption") or "", analysis):
        k = (m["title"] or "").lower()
        if k and k not in seen:
            seen.add(k)
            res.append(m)
    return res


def apify_media_urls(it: dict) -> tuple[str, list[str]]:
    """Returns ("video", [video_url]) or ("images", [image urls in slide order])."""
    if it.get("videoUrl"):
        return "video", [it["videoUrl"]]
    kids = it.get("childPosts") or []
    imgs = [k.get("displayUrl") for k in kids if k.get("displayUrl")]
    if not imgs:
        imgs = list(it.get("images") or [])
    if not imgs and it.get("displayUrl"):
        imgs = [it["displayUrl"]]
    return "images", imgs


def fetch_cdn(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=90) as r, open(dest, "wb") as f:
        shutil.copyfileobj(r, f)
    return dest


def prepare_apify_batch(batch: list[dict], out_root: Path, work_parent: Path, token: str, cap_usd: float) -> list[dict]:
    """Apify lookup for the batch, then parallel CDN downloads. Returns prepared units (one per item)."""
    from concurrent.futures import ThreadPoolExecutor
    items, run = apify_fetch([b["url"] for b in batch], token, cap_usd)
    append_jsonl(out_root / "apify_cost.jsonl", {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "urls": len(batch), **run})
    log(f"apify run {run['run_id']}: {run['items']} items, ${run['usd']}")
    by_code = {}
    for it in items:
        code = it.get("shortCode") or (media.id_from_url(it.get("url") or "") or "")[3:]
        if code:
            by_code[code] = it
    units, jobs = [], []
    for b in batch:
        it = by_code.get(b["code"])
        work = work_parent / b["code"]
        if not it or it.get("error"):
            units.append({"item": b, "error": f"apify_no_result: {(it or {}).get('errorDescription') or (it or {}).get('error') or 'missing'}"[:300]})
            continue
        work.mkdir(parents=True, exist_ok=True)
        kind, urls = apify_media_urls(it)
        if not urls:
            units.append({"item": b, "error": "apify_no_media_urls"})
            continue
        unit = {"item": {**b, "kind": "reel" if kind == "video" else "carousel"}, "meta": apify_meta(it), "work": work, "media": []}
        for i, u in enumerate(urls, 1):
            dest = work / (f"{b['code']}.mp4" if kind == "video" else f"slide_{i:02d}.jpg")
            jobs.append((unit, u, dest))
        units.append(unit)
    with ThreadPoolExecutor(max_workers=CDN_WORKERS) as ex:
        futs = [(unit, dest, ex.submit(fetch_cdn, u, dest)) for unit, u, dest in jobs]
        for unit, dest, f in futs:
            try:
                unit["media"].append(f.result())
            except Exception as e:  # noqa: BLE001
                unit["error"] = f"cdn_download: {type(e).__name__}: {e}"[:300]
    for u in units:
        if "work" in u and u.get("error"):
            shutil.rmtree(u["work"], ignore_errors=True)
        elif "work" in u:
            u["media"].sort()
    return units


# ---------------------------------------------------------------- local models



def vlm_ask(vision, label: str, prompt: str, image, max_tokens: int):
    """One vision-model call. `label` is only for debugging."""
    return vision.ask(prompt, image, max_tokens=max_tokens)


FRAME_PROMPT = """This is one frame from an Instagram reel or post slide (t={t}s). Return ONLY a JSON object with keys:
"on_screen_text": all text overlaid on the frame (captions, titles, stickers), max 200 chars, "" if none. Never copy passwords or codes.
"shot_type": one of ["talking head", "screen recording", "b-roll footage", "text card", "graphic or animation", "product or object", "photo", "other"]
"framing": one of ["close-up", "medium", "wide", "overhead", "screen", "n/a"]
"setting": short phrase (e.g. "home office", "car", "outdoors", "studio", "desktop app")
"person_speaking_to_camera": true or false
"caption_style": {{"present": true or false, "position": "top|middle|lower-third|bottom|n/a", "font_feel": "e.g. bold sans, serif, handwritten, clean minimal", "colors": "text and highlight colors", "highlight_active_word": true or false}}
"graphics": short phrase on overlays, arrows, stickers, zooms, lower thirds, UI callouts, "" if none
"design_notes": for slides or text cards, one sentence on layout and design, else an empty string."""

VISUAL_SPEC = (
    '"on_screen_text": all visible text verbatim incl. UI text and stickers, max 300 chars, "" if none (never copy passwords or keys); '
    '"font_feel": {{"family_guess": "e.g. Montserrat-like geometric sans, Impact, serif italic, handwritten, monospace terminal", "weight": "light|regular|bold|black", '
    '"case": "upper|lower|title|mixed", "color": "", "stroke_or_box": "outline|shadow|highlight box|none"}} or null if no text; '
    '"graphic_type": one of ["screenshot", "diagram", "chart", "UI demo", "meme", "text card", "b-roll", "talking head", "other"]; '
    '"what_it_shows": one concrete sentence (name the app, command, file or object, e.g. "Claude Code terminal running /impeccable"); '
    '"editing_technique": short list from [zoom punch, split screen, inset, green screen, cutout, overlay, screen record, speed ramp, text pop, none]; '
    '"worth_studying": {{"worth": true or false, "why": "short"}}')

FRAME_PROMPT += '\n"visual": {{' + VISUAL_SPEC + "}}"
VISUAL_ONLY_PROMPT = ("This is one frame from an Instagram reel or post slide (t={t}s). Return ONLY a JSON object with the single key "
                      '"visual": {{' + VISUAL_SPEC + "}}")

SYNTH_PROMPT = """You are analyzing one Instagram post saved by {viewer}. Learn two things from it: what makes it work as content, AND what practical technique, tool or habit it implies that the viewer could reuse.
Facts measured by tools (trust these):
{facts}

Per-frame vision notes (time, notes):
{frames}

Transcript with word timing (spoken):
{transcript}

Post caption by the author:
{caption}

Return ONLY a JSON object with keys:
"hook": {{"spoken": "first spoken words within 3s or \\"\\"", "on_screen": "text on screen within 3s or \\"\\"", "visual": "what is seen in the first 3s", "type": "e.g. question, bold claim, pattern interrupt, story, demo, list promise"}}
"format": one of ["talking head", "b-roll voiceover", "carousel", "skit", "screen recording", "text on screen", "montage", "interview or podcast clip", "tutorial", "other"]
"shot_types": list of distinct shot types seen
"setting": short phrase
"caption_style": {{"font_feel": "", "position": "", "word_by_word": true or false, "colors": ""}}
"graphics_motion": short phrase on graphics and motion design, "" if none
"music_sfx": {{"music": "yes|no|unclear", "sfx": "yes|no|unclear"}}
"music_mentions": list of {{"title": "", "artist": "", "where": "on_screen|caption|spoken", "recommended": true or false}} for every song or artist actually NAMED on screen, in the caption or in the transcript (recommended=true if the creator is recommending that track for others to use); [] if none named. Never guess a track from how it sounds
"cta": {{"spoken": "", "on_screen": "", "comment_word": "the WORD the viewer is asked to comment, or \\"\\"", "link_or_follow": ""}}
"structure_beats": list of {{"t": seconds, "beat": "hook|setup|proof|turn|payoff|cta|other", "what": "short"}}
"why_it_works": one line
"workflow": {{"tool": "", "technique": "", "setup_steps": []}} (concrete AI tool, technique or setup being shown; leave empty strings if the post is not about a tool or workflow)
"cta_asset": {{"offered": true or false, "word": "the WORD to comment, or \"\"", "asset_type": "repo|prompt library|template|guide|course|tool access|free trial|other|\"\"", "what_it_is": "what the viewer receives, as specific as the post allows", "delivery": "DM|link in bio|link in caption|other|\"\"", "value_for_viewer": "high|medium|low", "why": "why it would or would not be useful to the viewer"}} (offered=true whenever the post promises an external asset, repo, prompt pack, template or resource in exchange for commenting, DMing, or a link)
"takeaway": {{"relevant": true or false, "area": "short topic label", "insight": "what this implies for how the viewer could work, beyond content", "action": "one concrete thing to try, install or change"}}"""


# ---------------------------------------------------------------- turbo: OCR + dedupe + one contact-sheet VLM call

_OCR_LOCK = threading.Lock()
_SECRET_RE = re.compile(r"\b(?:sk|pk|ghp|xox[a-z]|AKIA|AIza)[-_A-Za-z0-9]{12,}\b|\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9+/_-]{40,}\b")


def ocr_image(path: Path) -> str:
    """Apple Vision text recognition (accurate), lines top to bottom, joined by spaces. '' when nothing or Vision unavailable."""
    try:
        import Vision
        from Foundation import NSURL, NSDictionary
    except Exception as e:  # pragma: no cover - depends on venv
        log(f"OCR unavailable ({e}); on_screen_text stays empty")
        return ""
    with _OCR_LOCK:
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        req.setUsesLanguageCorrection_(True)
        h = Vision.VNImageRequestHandler.alloc().initWithURL_options_(NSURL.fileURLWithPath_(str(path)), NSDictionary.dictionary())
        ok = h.performRequests_error_([req], None)
        if not (ok[0] if isinstance(ok, tuple) else ok):
            return ""
        obs = sorted(req.results() or [], key=lambda o: -o.boundingBox().origin.y)
        lines = []
        for o in obs:
            c = o.topCandidates_(1)
            if c:
                lines.append(str(c[0].string()))
    txt = _SECRET_RE.sub("[redacted]", " ".join(lines))
    return re.sub(r"\s+", " ", txt).strip()[:500]


def dhash_bits(path: Path, size: int = 8) -> int:
    from PIL import Image
    with Image.open(path) as im:
        g = im.convert("L").resize((size + 1, size), Image.LANCZOS)
        px = list(g.tobytes())
    bits = 0
    for r in range(size):
        for c in range(size):
            bits = (bits << 1) | (1 if px[r * (size + 1) + c] > px[r * (size + 1) + c + 1] else 0)
    return bits


def _norm_text(t: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (t or "").lower()).strip()


def text_changed(a: str, b: str) -> bool:
    import difflib
    na, nb = _norm_text(a), _norm_text(b)
    if na == nb:
        return False
    if not na or not nb:
        return True  # text appeared or disappeared
    return difflib.SequenceMatcher(None, na, nb).ratio() < 0.8


def dedupe_frames(hashes: list[int], texts: list[str], thr: int = DEDUPE_HASH_DIST, cap: int = MAX_UNIQUE_FRAMES) -> tuple[list[int], list[int]]:
    """Returns (unique indices in order, rep[i] = index of the unique frame that frame i inherits from).
    A frame is kept when it is far (hash distance > thr) from every kept frame, or its OCR text changed vs. the near one.
    Over `cap`, the most similar kept pair is merged repeatedly (first frame always survives); merged frames inherit the survivor."""
    uniq: list[int] = []
    rep = [0] * len(hashes)
    for i, h in enumerate(hashes):
        best, bd = None, 10 ** 9
        for u in uniq:
            d = bin(h ^ hashes[u]).count("1")
            if d < bd:
                best, bd = u, d
        if best is not None and bd <= thr and not text_changed(texts[best], texts[i]):
            rep[i] = best
        else:
            uniq.append(i)
            rep[i] = i
    while len(uniq) > cap:
        # drop the later frame of the most similar kept pair (text change counts as 32 extra bits), so diversity survives
        best = None
        for x in range(len(uniq)):
            for y in range(x + 1, len(uniq)):
                i, j = uniq[x], uniq[y]
                d = bin(hashes[i] ^ hashes[j]).count("1") + (32 if text_changed(texts[i], texts[j]) else 0)
                if best is None or d < best[0]:
                    best = (d, x, y)
        _, x, y = best
        gone, keep = uniq.pop(y), uniq[x]
        rep = [keep if r == gone else r for r in rep]
    return uniq, rep


def make_contact_sheet(tiles: list[tuple[Path, str]], dest: Path, tile_px: int = TILE_PX, cols: int = 3) -> Path:
    """Grid of <=9 tiles, each fit into tile_px, labeled '#n  caption' in the top-left corner."""
    from PIL import Image, ImageDraw, ImageFont
    ims = []
    for p, _ in tiles:
        with Image.open(p) as im:
            im = im.convert("RGB")
            im.thumbnail((tile_px, tile_px))
            ims.append(im.copy())
    cw, ch = max(i.width for i in ims), max(i.height for i in ims)
    rows = (len(ims) + cols - 1) // cols
    ncols = min(cols, len(ims))
    sheet = Image.new("RGB", (cw * ncols, ch * rows), (24, 24, 24))
    try:
        font = ImageFont.load_default(size=26)
    except TypeError:  # old Pillow
        font = ImageFont.load_default()
    d = ImageDraw.Draw(sheet)
    for n, (im, (_, cap)) in enumerate(zip(ims, tiles)):
        x, y = (n % cols) * cw, (n // cols) * ch
        sheet.paste(im, (x + (cw - im.width) // 2, y + (ch - im.height) // 2))
        label = f"#{n + 1}  {cap}"
        box = d.textbbox((x + 6, y + 6), label, font=font)
        d.rectangle((box[0] - 4, box[1] - 3, box[2] + 4, box[3] + 3), fill=(0, 0, 0))
        d.text((x + 6, y + 6), label, fill=(255, 255, 0), font=font)
    sheet.save(dest, "JPEG", quality=88)
    return dest


SHEET_PROMPT = """This contact sheet shows {n} distinct frames from one Instagram {what}. Each tile has its index (#1, #2, ...) and {when} burned into its top-left corner (ignore that label). The grid is only a contact sheet: never report split screen, collage or tiling unless a single tile itself shows it. Text in each frame was already read by OCR, so do not transcribe text.
Return ONLY compact JSON, no prose:
{{"tiles": [{{"i": 1, "graphic_type": one of ["screenshot","diagram","chart","UI demo","meme","text card","b-roll","talking head","other"], "what_it_shows": "one concrete sentence naming the app, command, file or object", "font_feel": {{"family_guess": "e.g. Montserrat-like geometric sans, Impact, serif italic, handwritten", "weight": "light|regular|bold|black", "case": "upper|lower|title|mixed", "color": "", "stroke_or_box": "outline|shadow|highlight box|none"}} or null if no text, "editing_technique": list from [zoom punch, split screen, inset, green screen, cutout, overlay, screen record, speed ramp, text pop, none], "worth_studying": true for at most the 2 most distinctive tiles, else false, "why": "max 8 words"}}, ... one object per tile],
"reel": {{"format": one of ["talking head","b-roll voiceover","carousel","skit","screen recording","text on screen","montage","interview or podcast clip","tutorial","other"], "hook_visual": "what is seen in tile #1", "caption_style": {{"font_feel": "", "position": "", "word_by_word": true or false, "colors": ""}}, "graphics_motion": "short phrase, \\"\\" if none", "cta": "on-screen call to action or \\"\\""}}}}
OCR text per tile (for reference):
{ocr}"""


def map_tiles(res, uniq_n: int) -> dict[int, dict]:
    """Sheet-call JSON -> {tile index (0-based): visual dict}. Uses each tile's "i" (1-based); falls back to list position."""
    out: dict[int, dict] = {}
    tiles = res.get("tiles") if isinstance(res, dict) else None
    if not isinstance(tiles, list):
        return out
    for pos, t in enumerate(tiles):
        if not isinstance(t, dict):
            continue
        try:
            k = int(t.get("i", pos + 1)) - 1
        except (TypeError, ValueError):
            k = pos
        if not 0 <= k < uniq_n or k in out:
            continue
        w = t.get("worth_studying")
        if isinstance(w, dict):
            worth = {"worth": bool(w.get("worth")), "why": str(w.get("why") or t.get("why") or "")}
        else:
            worth = {"worth": bool(w), "why": str(t.get("why") or "")}
        tech = t.get("editing_technique")
        out[k] = {"font_feel": t.get("font_feel") if isinstance(t.get("font_feel"), dict) else None,
                  "graphic_type": t.get("graphic_type") or "other", "what_it_shows": str(t.get("what_it_shows") or ""),
                  "editing_technique": tech if isinstance(tech, list) else ([tech] if tech else []),
                  "worth_studying": worth}
    return out


def turbo_visuals(vision, paths: list[Path], keys: list, captions: list[str], work: Path, what: str, when: str) -> tuple[list[dict], dict, dict]:
    """OCR every frame, dedupe, one VLM call on a contact sheet. Returns (per-frame rec dicts incl. visual, reel-level overview, timing).
    Raises RuntimeError if the sheet call returns nothing usable (caller falls back to the detailed path)."""
    t0 = time.time()
    texts = [ocr_image(p) for p in paths]
    hashes = [dhash_bits(p) for p in paths]
    t_ocr = time.time() - t0
    uniq, rep = dedupe_frames(hashes, texts)
    sheet = make_contact_sheet([(paths[i], captions[i]) for i in uniq], work / "_sheet.jpg")
    ocr_ref = "\n".join(f"#{n + 1}: {texts[i][:160] or '(none)'}" for n, i in enumerate(uniq))
    t1 = time.time()
    res = vlm_ask(vision, f"contact sheet ({len(uniq)} tiles)",
                  SHEET_PROMPT.format(n=len(uniq), what=what, when=when, ocr=ocr_ref), str(sheet), 140 + 100 * len(uniq))
    t_sheet = time.time() - t1
    vis = map_tiles(res, len(uniq))
    if not vis:
        raise RuntimeError("contact-sheet call returned no usable tiles")
    pos = {u: n for n, u in enumerate(uniq)}
    recs = []
    for i, k in enumerate(keys):
        v = dict(vis.get(pos[rep[i]], {}))
        v["on_screen_text"] = texts[i]
        gt = v.get("graphic_type")
        r = {"visual": v, "on_screen_text": texts[i], "shot_type": gt, "person_speaking_to_camera": gt == "talking head"}
        r["t" if isinstance(k, float) else "slide"] = k
        recs.append(r)
    overview = res.get("reel") if isinstance(res.get("reel"), dict) else {}
    return recs, overview, {"ocr": round(t_ocr, 1), "sheet_vlm": round(t_sheet, 1), "unique_frames": len(uniq), "frames_total": len(paths)}


def fmt_turbo_frames(frecs: list[dict], overview: dict) -> str:
    lines = []
    for f in frecs:
        v = f.get("visual") or {}
        k = f"{f['t']}s" if "t" in f else f"slide {f.get('slide')}"
        ff = v.get("font_feel") or {}
        lines.append(f"[{k}] {v.get('graphic_type')}; shows={v.get('what_it_shows')!r}; text={f.get('on_screen_text')!r}; "
                     f"font={json.dumps(ff, ensure_ascii=False)}; edit={v.get('editing_technique')}")
    if overview:
        lines.append("Overview from the contact sheet: " + json.dumps(overview, ensure_ascii=False))
    return "\n".join(lines) or "(none)"



def extract_frames_at(video: Path, times: list[float], dest: Path, prefix: str) -> list[tuple[float, Path]]:
    out = []
    for t in times:
        f = dest / f"{prefix}_{int(round(t * 100)):06d}.jpg"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", "scale='min(896,iw)':-2", "-q:v", "3", str(f)], check=False)
        if f.exists() and f.stat().st_size > 0:
            out.append((t, f))
    return out


def pick_later_times(cuts: list[float], duration: float) -> list[float]:
    """Scene-change sampled: one frame just after each cut (spaced >= LATER_INTERVAL), topped up by interval."""
    cand = [c + 0.15 for c in cuts if c > 3.0]
    times: list[float] = []
    for t in cand:
        if not times or t - times[-1] >= LATER_INTERVAL:
            times.append(t)
    t = 3.0 + LATER_INTERVAL
    while t < duration - 0.3:
        if all(abs(t - x) >= LATER_INTERVAL * 0.6 for x in times):
            times.append(t)
        t += LATER_INTERVAL
    times = sorted(x for x in times if x < duration - 0.1)
    if len(times) > MAX_LATER_FRAMES:  # evenly thin
        step = len(times) / MAX_LATER_FRAMES
        times = [times[int(i * step)] for i in range(MAX_LATER_FRAMES)]
    return times


def transcribe_words(video: Path) -> dict:
    import mlx_whisper
    r = mlx_whisper.transcribe(str(video), path_or_hf_repo=_model.WHISPER_MODEL, word_timestamps=True,
                               condition_on_previous_text=False, no_speech_threshold=0.6, verbose=None)
    words, segs = [], []
    for s in r.get("segments", []):
        if s.get("no_speech_prob", 0) >= 0.6 or not s["text"].strip():
            continue
        segs.append({"start": round(s["start"], 2), "end": round(s["end"], 2), "text": s["text"].strip()})
        for w in s.get("words", []) or []:
            words.append({"w": w["word"].strip(), "s": round(w["start"], 2), "e": round(w["end"], 2)})
    return {"language": r.get("language"), "text": " ".join(s["text"] for s in segs), "segments": segs, "words": words}


def non_speech_energy(video: Path, words: list[dict], duration: float) -> dict:
    """Heuristic only: share of non-speech time with audible audio (music or SFX bed likely)."""
    import numpy as np
    p = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "-"], capture_output=True)
    a = np.frombuffer(p.stdout, dtype=np.int16).astype("float32") / 32768.0
    if a.size < 16000:
        return {"has_audio": False}
    win = 4000  # 0.25 s
    n = a.size // win
    rms = np.sqrt((a[: n * win].reshape(n, win) ** 2).mean(axis=1) + 1e-12)
    db = 20 * np.log10(rms)
    speech = np.zeros(n, dtype=bool)
    for w in words:
        speech[int(w["s"] / 0.25): int(w["e"] / 0.25) + 1] = True
    non = ~speech
    loud_non = float(((db > -42) & non).sum() / max(1, non.sum())) if non.any() else 0.0
    return {"has_audio": True, "speech_share": round(float(speech.mean()), 2),
            "audible_non_speech_share": round(loud_non, 2), "music_or_sfx_likely": bool(loud_non > 0.5 and non.sum() > 8)}


def comment_words(*texts: str) -> list[str]:
    out = []
    for t in texts:
        for m in re.finditer(r"comment(?:ing)?\s+(?:the\s+word\s+|below\s+with\s+|with\s+)?[\"'“‘]?([A-Za-z0-9_#$]{2,24})", t or "", re.I):
            w = m.group(1).upper()
            if w not in ("BELOW", "THE", "YOUR", "AND", "ME", "IF", "ON", "FOR", "WORD", "WITH", "A", "I", "YES") and w not in out:
                out.append(w)
    return out


# ---------------------------------------------------------------- giveaway (CTA capture)

DM_RE = re.compile(r"\b(?:dm|message|text|send)\s+(?:me\s+)?(?:the\s+)?(?:word\s+|keyword\s+)?[\"'“‘]?([A-Za-z0-9_#$]{2,24})[\"'”’]?", re.I)
DM_STOP = {"ME", "US", "THE", "YOU", "YOUR", "AND", "A", "AN", "TO", "IT", "IF", "ON", "FOR", "WORD", "KEYWORD", "WITH", "I", "WE",
           "MY", "OUR", "THIS", "THAT", "NOW", "BACK", "OVER", "HI", "HEY", "ABOUT", "AT", "IN", "OF", "OR", "SO", "NOT", "MORE"}
LINK_RE = re.compile(r"\blink\s+(?:is\s+)?in\s+(?:my\s+|the\s+|our\s+)?(bio|profile|comments?|caption|description)\b", re.I)
GET_RE = re.compile(r"\b(?:to\s+get|and\s+(?:i'?ll|i\s+will)\s+(?:send|dm)(?:\s+you)?|i'?ll\s+(?:send|dm)\s+you|you'?ll\s+get|get\s+access\s+to|get)\s+"
                    r"(?:a\s+copy\s+of\s+)?((?:my|the|our|a|an|this|that|free)\s+[^.!?\n]{3,90})", re.I)


def _snip(text: str, m: re.Match, pad: int = 70) -> str:
    return re.sub(r"\s+", " ", text[max(0, m.start() - pad): m.end() + pad]).strip()


def parse_giveaway(sources: list[tuple[str, str]], cta: dict | None = None, cta_asset: dict | None = None) -> dict:
    """Pure: sources = [(where, text)] in priority order (e.g. transcript, caption, on-screen frames).
    cta / cta_asset are the VLM verdict's CTA outputs (optional). Returns the `giveaway` object:
    {offered, keyword, what_they_get, channel, evidence:[{quote, where}]}."""
    cta, cta_asset = cta or {}, cta_asset or {}
    hits: list[dict] = []
    for where, text in sources:
        text = text or ""
        for m in re.finditer(r"comment(?:ing)?\s+(?:the\s+word\s+|below\s+with\s+|with\s+)?[\"'“‘]?([A-Za-z0-9_#$]{2,24})", text, re.I):
            kw = comment_words(m.group(0))
            if kw:
                hits.append({"channel": "comment", "keyword": kw[0], "where": where, "quote": _snip(text, m), "text": text, "end": m.end()})
        for m in DM_RE.finditer(text):
            kw = m.group(1).upper()
            if kw not in DM_STOP and re.match(r"\s*(?:dm|message)", m.group(0), re.I):
                hits.append({"channel": "DM", "keyword": kw, "where": where, "quote": _snip(text, m), "text": text, "end": m.end()})
        for m in LINK_RE.finditer(text):
            ch = "link in bio" if m.group(1).lower() in ("bio", "profile") else "link in comments" if m.group(1).lower().startswith("comment") else "link in caption"
            hits.append({"channel": ch, "keyword": "", "where": where, "quote": _snip(text, m), "text": text, "end": m.end()})
    asset_offered = bool(cta_asset.get("offered"))
    vlm_word = (cta_asset.get("word") or cta.get("comment_word") or "").strip().upper()
    offered = bool(hits) or asset_offered or bool(vlm_word)
    if not offered:
        return {"offered": False, "keyword": "", "what_they_get": "", "channel": "", "evidence": []}
    rank = {"comment": 0, "DM": 1}
    hits.sort(key=lambda h: rank.get(h["channel"], 2))
    best = hits[0] if hits else None
    keyword = (best or {}).get("keyword") or vlm_word
    channel = (best or {}).get("channel") or {"DM": "DM", "link in bio": "link in bio"}.get((cta_asset.get("delivery") or "").strip(), "") \
        or ("comment" if vlm_word else (cta_asset.get("delivery") or ""))
    get = (cta_asset.get("what_it_is") or "").strip()
    if cta_asset.get("asset_type") and cta_asset["asset_type"] not in get:
        get = f"{cta_asset['asset_type']}: {get}" if get else cta_asset["asset_type"]
    if not get and best:
        g = GET_RE.search(best["text"][max(0, best["end"] - 200): best["end"] + 250])
        get = g.group(1).strip() if g else ""
    evidence, seen = [], set()
    for h in hits:
        k = (h["where"], h["quote"])
        if k not in seen:
            seen.add(k)
            evidence.append({"quote": h["quote"], "where": h["where"]})
    for where, q in (("vlm_cta.spoken", cta.get("spoken")), ("vlm_cta.on_screen", cta.get("on_screen"))):
        if q and not evidence:
            evidence.append({"quote": str(q)[:200], "where": where})
    return {"offered": True, "keyword": keyword, "what_they_get": get[:200], "channel": channel, "evidence": evidence[:6]}


def build_frames(frecs: list[dict], files: dict) -> list[dict]:
    """Per-frame records for the reel JSON: time/slide, archive filename (relative to DIR/<code>/), visual."""
    out = []
    for f in frecs:
        key = f.get("t") if "t" in f else f.get("slide")
        v = f.get("visual") if isinstance(f.get("visual"), dict) else {}
        rec = {("t" if "t" in f else "slide"): key, "file": files.get(key), "visual": v}
        if f.get("shot_type"):
            rec["shot_type"] = f["shot_type"]
        out.append(rec)
    return out


def giveaway_sources(transcript: str, caption: str, frecs: list[dict]) -> list[tuple[str, str]]:
    src = [("transcript", transcript or ""), ("caption", caption or "")]
    for f in frecs:
        v = f.get("visual") if isinstance(f.get("visual"), dict) else {}
        txt = " ".join(x for x in (f.get("on_screen_text"), v.get("on_screen_text")) if isinstance(x, str) and x)
        if txt:
            lab = f"on-screen@{f['t']}s" if "t" in f else f"on-screen@slide{f.get('slide')}"
            src.append((lab, txt))
    return src


# ---------------------------------------------------------------- archive

def archive_extract(video: Path, times: list[float], dest: Path) -> dict:
    """Native-res (<=1080px wide) q~85 jpgs for the analyzed times. Returns {t: filename}."""
    dest.mkdir(parents=True, exist_ok=True)
    files = {}
    for t in times:
        f = dest / f"f_{int(round(t * 100)):06d}.jpg"
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                        "-vf", "scale='min(1080,iw)':-2", "-q:v", "3", str(f)], check=False)
        if f.exists() and f.stat().st_size > 0:
            files[round(t, 2)] = f.name
    return files


def archive_commit(archive: Path, item: dict, meta: dict, rec: dict, work: Path, media: list[Path], keep_source: bool = False) -> Path:
    """Move/copy media, info/meta JSON and extracted frames into archive/<code>/ and write manifest.json. Never deletes the archive."""
    d = archive / item["code"]
    d.mkdir(parents=True, exist_ok=True)
    files = []

    def put(src: Path, rel: str, kind: str, t=None, copy=False):
        dst = d / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        (shutil.copy2 if copy else shutil.move)(str(src), str(dst))
        e = {"file": rel, "kind": kind}
        if t is not None:
            e["t"] = t
        files.append(e)

    for m in media:
        if m.exists():
            put(m, ("slides/" if item.get("kind") == "carousel" else "") + m.name, "slide" if item.get("kind") == "carousel" else "video", copy=keep_source)
    for j in sorted(work.glob("*.json")):
        put(j, "info.json" if "info" in j.name else j.name, "info")
    (d / "meta.json").write_text(json.dumps({"post": meta, "url": item.get("url"), "collections": item.get("collections")},
                                            ensure_ascii=False, indent=1), encoding="utf-8")
    files.append({"file": "meta.json", "kind": "meta"})
    arch = work / "_arch"
    for fr in rec.get("frames") or []:
        rel = fr.get("file")
        if rel and (arch / Path(rel).name).exists():
            put(arch / Path(rel).name, rel, "frame", t=fr.get("t", fr.get("slide")))
    manifest = {"code": item["code"], "url": item.get("url"), "kind": item.get("kind"), "archived_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "files": files}
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def prep_keyframe(src: Path, dest: Path) -> None:
    from PIL import Image
    with Image.open(src) as im:
        im = im.convert("RGB")
        im.thumbnail((480, 480 * 4))
        if im.width > 480:
            im = im.resize((480, round(im.height * 480 / im.width)))
        im.save(dest, "JPEG", quality=82)


def fmt_frames(frames: list[dict]) -> str:
    lines = []
    for f in frames:
        cs = f.get("caption_style") if isinstance(f.get("caption_style"), dict) else {}
        lines.append(f"[{f['t']}s] {f.get('shot_type')}/{f.get('framing')} in {f.get('setting')}; "
                     f"speaking_to_camera={f.get('person_speaking_to_camera')}; text={f.get('on_screen_text')!r}; "
                     f"caption_style={json.dumps(cs, ensure_ascii=False)}; graphics={f.get('graphics')!r}")
    return "\n".join(lines) or "(none)"


def ask_frames(vision, frames: list[tuple[float, Path]]) -> list[dict]:
    recs = []
    for i, (t, f) in enumerate(frames, 1):
        r = vlm_ask(vision, f"frame {i}/{len(frames)} at {t:.1f}s", FRAME_PROMPT.format(t=round(t, 2)), str(f),
                    380 if FAST else 560)
        if not isinstance(r, dict) or r.get("parse_error"):
            r = {"parse_error": True}
        r["t"] = round(t, 2)
        recs.append(r)
    return recs


ARCHIVE: Path | None = None  # set by --archive


def analyze_video(vision, video: Path, work: Path, collections: list[str], meta: dict) -> tuple[dict, Path | None]:
    pr = media.probe(str(video))
    dur = pr["duration"]
    cuts = media.detect_cuts(str(video), dur)
    hook_t = [t for t in (FAST_HOOK_TIMES if FAST else HOOK_TIMES) if t < max(0.3, dur - 0.1)]
    later_t = pick_later_times(cuts, dur)
    if FAST and len(later_t) > FAST_LATER:
        step = len(later_t) / FAST_LATER
        later_t = [later_t[int(i * step)] for i in range(FAST_LATER)]
    t_mark = time.time()
    tr_box: dict = {}
    wth = None
    if TURBO and pr["has_audio"]:  # whisper (mlx) overlaps frame extraction + OCR (CPU/ANE); joined before the VLM call
        def _whisper():
            try:
                tr_box["tr"] = transcribe_words(video)
            except Exception as e:  # noqa: BLE001
                tr_box["err"] = e
        wth = threading.Thread(target=_whisper, daemon=True)
        wth.start()
    times = hook_t + later_t
    frecs = overview = None
    turbo_t: dict = {}
    frames: list[tuple[float, Path]] = []
    afiles: dict = {}
    if TURBO:
        try:
            afiles = archive_extract(video, times, work / "_arch")
            frames = [(t, work / "_arch" / afiles[round(t, 2)]) for t in times if round(t, 2) in afiles]
            if frames:
                if wth:
                    wth.join()  # no concurrent mlx work
                    # whisper finished: its time is not part of the sheet call
                frecs, overview, turbo_t = turbo_visuals(vision, [p_ for _, p_ in frames], [round(t, 2) for t, _ in frames],
                                                         [f"{t:.1f}s" for t, _ in frames], work, "reel", "timestamp")
        except Exception as e:  # noqa: BLE001
            log(f"turbo failed ({type(e).__name__}: {e}); falling back to per-frame analysis")
            frecs = None
    if frecs is None:
        frames = extract_frames_at(video, hook_t, work, "h") + extract_frames_at(video, later_t, work, "s")
        frecs = ask_frames(vision, frames)
        afiles = archive_extract(video, [t for t, _ in frames], work / "_arch") if ARCHIVE else {}
    elif not ARCHIVE:
        afiles = {}
    t_vlm = time.time() - t_mark
    t_mark = time.time()
    if wth:
        wth.join()
        if "err" in tr_box:
            raise tr_box["err"]
        tr = tr_box["tr"]
    else:
        tr = transcribe_words(video) if pr["has_audio"] else {"language": None, "text": "", "segments": [], "words": []}
    audio = non_speech_energy(video, tr["words"], dur) if pr["has_audio"] else {"has_audio": False}

    # b-roll share: time-weighted share of sampled frames with no person speaking to camera
    broll_w = tot_w = 0.0
    for i, f in enumerate(frecs):
        nxt = frecs[i + 1]["t"] if i + 1 < len(frecs) else dur
        w = max(0.1, nxt - f["t"])
        tot_w += w
        if f.get("person_speaking_to_camera") is False:
            broll_w += w
    facts = {
        "duration_s": dur, "resolution": f"{pr['width']}x{pr['height']}", "fps": pr["fps"],
        "cuts": len(cuts), "cuts_per_10s": round(len(cuts) / dur * 10, 1) if dur else None, "cut_times": cuts[:40],
        "broll_share_est": round(broll_w / tot_w, 2) if tot_w else None, "audio": audio,
        "collections": collections,
    }
    t_whisper = time.time() - t_mark
    t_mark = time.time()
    wf = bool(any(WORKFLOW_COLLECTION_RE.search(c) for c in collections))
    tx_words = " ".join(f"{w['w']}@{w['s']}" for w in tr["words"])[:5000]
    synth = vlm_ask(vision, "final verdict", SYNTH_PROMPT.format(viewer=VIEWER_CONTEXT, facts=json.dumps(facts, ensure_ascii=False), frames=fmt_turbo_frames(frecs, overview) if overview is not None else fmt_frames(frecs),
                                           transcript=tx_words or "(no speech)", caption=meta.get("caption") or "(none)"),
                    None, SYNTH_TOKENS if TURBO else 1400)
    t_synth = time.time() - t_mark
    screen_text = " ".join(f.get("on_screen_text") or "" for f in frecs if isinstance(f.get("on_screen_text"), str))
    hook_frames = [f for f in frecs if f["t"] <= 3.0]
    rec = {
        "kind": "reel", "facts": facts,
        "hook_frame_text": [f.get("on_screen_text") for f in hook_frames if f.get("on_screen_text")],
        "analysis": synth,
        "comment_words_detected": comment_words(tr["text"], screen_text, meta.get("caption") or ""),
        "transcript": {"language": tr["language"], "text": tr["text"][:4000], "words": tr["words"][:600]},
        "frames_sampled": len(frames), "workflow_collection": wf, "fast_mode": FAST, "turbo": overview is not None,
        "frames": build_frames(frecs, {round(t, 2): f"frames/{afiles[round(t, 2)]}" for t in [x for x, _ in frames] if round(t, 2) in afiles}),
        "giveaway": parse_giveaway(giveaway_sources(tr["text"], meta.get("caption") or "", frecs),
                                   (synth or {}).get("cta") if isinstance(synth, dict) and isinstance(synth.get("cta"), dict) else None,
                                   (synth or {}).get("cta_asset") if isinstance(synth, dict) and isinstance(synth.get("cta_asset"), dict) else None),
        "timing_s": {"frames_vlm": round(t_vlm, 1), "whisper": round(t_whisper, 1), "synth": round(t_synth, 1), **turbo_t},
    }
    if not wf and isinstance(synth, dict):
        synth.pop("workflow", None)
    key = next((p for t, p in frames if t >= 0.9), frames[0][1] if frames else None)
    return rec, key


def analyze_carousel(vision, slides: list[Path], collections: list[str], meta: dict) -> tuple[dict, Path | None]:
    recs = None
    overview = None
    turbo_t: dict = {}
    if TURBO and slides:
        try:
            with tempfile.TemporaryDirectory() as td:
                recs, overview, turbo_t = turbo_visuals(vision, slides, list(range(1, len(slides) + 1)),
                                                        [f"slide {i}" for i in range(1, len(slides) + 1)], Path(td), "carousel", "slide number")
        except Exception as e:  # noqa: BLE001
            log(f"turbo failed ({type(e).__name__}: {e}); falling back to per-slide analysis")
            recs = None
    if recs is None:
        recs = []
        for i, s in enumerate(slides, 1):
            r = vlm_ask(vision, f"slide {i}/{len(slides)}", FRAME_PROMPT.format(t=f"slide {i}"), str(s), 600)
            if not isinstance(r, dict) or r.get("parse_error"):
                r = {"parse_error": True}
            r["slide"] = i
            r.pop("t", None)
            recs.append(r)
    lines = fmt_turbo_frames(recs, overview) if overview is not None else "\n".join(
        f"slide {r['slide']}: text={r.get('on_screen_text')!r} design={r.get('design_notes')!r} "
        f"graphics={r.get('graphics')!r} style={json.dumps(r.get('caption_style'), ensure_ascii=False)}" for r in recs)
    wf = bool(any(WORKFLOW_COLLECTION_RE.search(c) for c in collections))
    facts = {"slides": len(slides), "collections": collections}
    synth = vlm_ask(vision, "final verdict", SYNTH_PROMPT.format(viewer=VIEWER_CONTEXT, facts=json.dumps(facts), frames=lines, transcript="(carousel, no audio)",
                                           caption=meta.get("caption") or "(none)") +
                    '\nThis is a carousel: use format "carousel"; structure_beats t is the slide number; hook is slide 1.',
                    None, SYNTH_TOKENS if TURBO else 1400)
    if not wf and isinstance(synth, dict):
        synth.pop("workflow", None)
    alltext = " ".join(r.get("on_screen_text") or "" for r in recs if isinstance(r.get("on_screen_text"), str))
    rec = {"kind": "carousel", "facts": facts, "slides": [{k: v for k, v in r.items() if k != "parse_error"} for r in recs],
           "analysis": synth, "comment_words_detected": comment_words(alltext, meta.get("caption") or ""),
           "workflow_collection": wf, "turbo": overview is not None, "timing_s": turbo_t,
           "frames": build_frames(recs, {r["slide"]: f"slides/{slides[r['slide'] - 1].name}" for r in recs}),
           "giveaway": parse_giveaway(giveaway_sources("", meta.get("caption") or "", recs),
                                      (synth or {}).get("cta") if isinstance(synth, dict) and isinstance(synth.get("cta"), dict) else None,
                                      (synth or {}).get("cta_asset") if isinstance(synth, dict) and isinstance(synth.get("cta_asset"), dict) else None)}
    return rec, slides[0]


# ---------------------------------------------------------------- driver

def finalize(out_root: Path, item: dict, meta: dict, rec: dict, key: Path | None, secs: float) -> None:
    study = out_root / "study"
    kf = None
    if key is not None and not Path(key).exists() and rec.get("archive"):
        # archive_commit may already have moved the key image (carousel slides)
        key = next(Path(rec["archive"]).rglob(Path(key).name), None)
    if key is not None:
        prep_keyframe(key, study / f"{item['code']}.jpg")
        kf = f"{item['code']}.jpg"
    rec["music"] = build_music(meta, rec.get("analysis"))
    rec.update({"code": item["code"], "url": item["url"], "collections": item["collections"], "post": meta,
                "keyframe": kf, "analyzed_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "wall_s": round(secs, 1)})
    tmp = study / f".{item['code']}.json.tmp"
    tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.rename(study / f"{item['code']}.json")
    idx = {k: rec.get(k) for k in ("code", "url", "kind", "collections", "keyframe", "wall_s")}
    a = rec.get("analysis") if isinstance(rec.get("analysis"), dict) else {}
    idx.update(author=meta.get("author"), views=meta.get("views"), likes=meta.get("likes"), post_date=meta.get("post_date"),
               format=a.get("format"), hook=a.get("hook"), cta=a.get("cta"), why_it_works=a.get("why_it_works"),
               cuts_per_10s=(rec.get("facts") or {}).get("cuts_per_10s"), comment_words=rec.get("comment_words_detected"))
    append_jsonl(out_root / "study_index.jsonl", idx)


def rollup(out_root: Path) -> int:
    study = out_root / "study"
    n = 0
    lines = []
    for f in sorted(study.glob("*.json")):
        r = json.loads(f.read_text())
        a = r.get("analysis") if isinstance(r.get("analysis"), dict) else {}
        m = r.get("post") or {}
        lines.append(json.dumps({"code": r["code"], "url": r["url"], "kind": r.get("kind"), "collections": r.get("collections"),
                                 "keyframe": r.get("keyframe"), "wall_s": r.get("wall_s"), "author": m.get("author"),
                                 "views": m.get("views"), "likes": m.get("likes"), "post_date": m.get("post_date"),
                                 "format": a.get("format"), "hook": a.get("hook"), "cta": a.get("cta"),
                                 "why_it_works": a.get("why_it_works"), "cuts_per_10s": (r.get("facts") or {}).get("cuts_per_10s"),
                                 "comment_words": r.get("comment_words_detected")}, ensure_ascii=False))
        n += 1
    (out_root / "study_index.jsonl").write_text("\n".join(lines) + ("\n" if lines else ""))
    return n


def analyze_unit(vision, unit: dict, out_root: Path) -> float:
    """Analyze one prepared unit, write outputs, delete its media. Returns wall seconds."""
    item, meta, work = unit["item"], unit["meta"], unit["work"]
    t0 = time.time()
    try:
        if item["kind"] == "carousel":
            rec, key = analyze_carousel(vision, unit["media"], item["collections"], meta)
        else:
            rec, key = analyze_video(vision, unit["media"][0], work, item["collections"], meta)
        if ARCHIVE:
            rec["archive"] = str(archive_commit(ARCHIVE, item, meta, rec, work, unit["media"]))
        finalize(out_root, item, meta, rec, key, time.time() - t0)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return time.time() - t0


def prioritize(items: list[dict], terms: list[str], only: bool) -> list[dict]:
    if not terms:
        return items
    terms = [t.lower() for t in terms]

    def rank(it):
        for i, t in enumerate(terms):
            if any(t in c.lower() for c in it["collections"]):
                return i
        return len(terms)
    ordered = sorted(items, key=rank)  # stable: file order kept within a rank
    return [i for i in ordered if rank(i) < len(terms)] if only else ordered


def producer(todo, q, stop, out_root, work_parent, batch_n, cap_usd, token):
    """Download stage. Puts lists of prepared units, then a terminal sentinel."""
    try:
        for i in range(0, len(todo), batch_n):
            if stop.is_set():
                break
            q.put(("units", prepare_apify_batch(todo[i:i + batch_n], out_root, work_parent, token, cap_usd)))
        q.put(("done", None))
    except Exception as e:  # noqa: BLE001  (auth, quota, network): stop and never retry automatically
        q.put(("fatal", f"{type(e).__name__}: {e}"[:500]))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="reel-watcher study", description="Analyze saved reels locally. Dry run unless --run.")
    ap.add_argument("--input", help="TSV/TXT of url<TAB>collections (default: <out-root>/urls.tsv)")
    ap.add_argument("--out-root", default="./reel-watcher-out", help="where study/ JSON and the run logs go")
    ap.add_argument("--run", action="store_true", help="fetch via Apify (paid, small) and analyze")
    ap.add_argument("--limit", type=int, default=0, help="stop after N new reels")
    ap.add_argument("--local", nargs="+", metavar="MP4", help="analyze local video file(s); no download, no Apify, source untouched")
    ap.add_argument("--local-collections", default="", help="with --local: '|' separated labels")
    ap.add_argument("--retry-failed", action="store_true", help="retry reels that failed before (never automatic)")
    ap.add_argument("--rollup", action="store_true", help="rebuild study_index.jsonl from study/*.json and exit")
    ap.add_argument("--collections", default="", help="priority terms, comma separated, matched against collection names")
    ap.add_argument("--only-priority", action="store_true", help="with --collections: skip non-matching items")
    ap.add_argument("--model-size", choices=["small", "large"], help="small: 7B VLM + whisper-small for 16 GB Macs; large: 35B. Default auto by RAM (env REEL_WATCHER_MODEL)")
    ap.add_argument("--model", help="exact Hugging Face id of an MLX vision-language model (overrides --model-size)")
    ap.add_argument("--fast", action="store_true", help="fewer frames and tokens")
    ap.add_argument("--detailed", action="store_true", help="one VLM call per frame instead of one contact sheet")
    ap.add_argument("--archive", metavar="DIR", help="keep media, frames and manifest per reel in DIR/<code>/")
    ap.add_argument("--batch", type=int, default=15, help="Apify batch size")
    ap.add_argument("--apify-batch-cap-usd", type=float, default=2.0, help="hard spending cap per Apify batch")
    return ap


def main(argv: list[str] | None = None) -> int:
    global FAST, ARCHIVE, TURBO
    a = build_parser().parse_args(argv)
    FAST = a.fast
    _model.configure(a.model_size, a.model)
    TURBO = not a.detailed
    if a.archive:
        ARCHIVE = Path(a.archive).expanduser()
        ARCHIVE.mkdir(parents=True, exist_ok=True)

    out_root = Path(a.out_root).expanduser()
    study = out_root / "study"
    study.mkdir(parents=True, exist_ok=True)
    work_parent = out_root / ".work"

    if a.rollup:
        print(f"rolled up {rollup(out_root)} reels")
        return 0

    if a.local:
        missing = media.preflight()
        if missing:
            print(missing)
            return 2
        work_parent.mkdir(exist_ok=True)
        vision = Vision()
        try:
            for path in a.local:
                src = Path(path).expanduser()
                code = re.sub(r"[^A-Za-z0-9_-]", "_", src.stem)
                item = {"url": f"local:{src.name}", "code": code, "collections": [c for c in a.local_collections.split("|") if c], "kind": "reel"}
                t0 = time.time()
                work = Path(tempfile.mkdtemp(dir=work_parent))
                try:
                    rec, key = analyze_video(vision, src, work, item["collections"], {})
                    if ARCHIVE:
                        rec["archive"] = str(archive_commit(ARCHIVE, item, {}, rec, work, [src], keep_source=True))
                    finalize(out_root, item, {}, rec, key, time.time() - t0)
                finally:
                    shutil.rmtree(work, ignore_errors=True)
                print(f"{code}: {time.time() - t0:.1f}s")
        finally:
            shutil.rmtree(work_parent, ignore_errors=True)
        return 0

    inp = Path(a.input) if a.input else out_root / "urls.tsv"
    if not inp.exists():
        print(f"input not found: {inp}")
        return 2
    every = parse_input(inp)
    todo = [i for i in every if not is_done(study, i["code"], a.retry_failed)]
    todo = prioritize(todo, [t.strip() for t in a.collections.split(",") if t.strip()], a.only_priority)
    if a.limit:
        todo = todo[: a.limit]
    token = apify_token()
    print(f"{len(every)} urls, {len(todo)} to fetch via Apify")
    if not a.run:
        for i in todo:
            print(f"  would fetch [{i['kind']}] {i['code']}  {i['url']}  {'|'.join(i['collections'])}")
        print("dry run; pass --run to fetch and analyze (Apify is paid, about $0.002 per reel; per-batch cap $%.2f)" % a.apify_batch_cap_usd)
        return 0
    if not todo:
        return 0
    if not token:
        print("APIFY_TOKEN is not set. Export it in your shell (never put it in a file in this repo).")
        return 2
    missing = media.preflight()
    if missing:
        print(missing)
        return 2

    import queue
    work_parent.mkdir(exist_ok=True)
    vision = Vision()
    q: queue.Queue = queue.Queue(maxsize=1)  # bounded: at most ~2 batches of media on disk
    stop = threading.Event()
    th = threading.Thread(target=producer, daemon=True, args=(todo, q, stop, out_root, work_parent, a.batch, a.apify_batch_cap_usd, token))
    th.start()
    n = done_n = 0
    rc = 0
    try:
        while True:
            kind, payload = q.get()
            if kind == "units":
                for unit in payload:
                    n += 1
                    item = unit["item"]
                    if unit.get("error"):
                        append_jsonl(study / "_failed.jsonl", {"code": item["code"], "url": item["url"], "error": unit["error"],
                                                               "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                        print(f"[{n}/{len(todo)}] {item['code']} FAILED (not retried): {unit['error'][:140]}", flush=True)
                        continue
                    try:
                        secs = analyze_unit(vision, unit, out_root)
                    except Exception as e:  # one bad reel must not end an overnight run
                        append_jsonl(study / "_failed.jsonl", {"code": item["code"], "url": item["url"],
                                                               "error": f"{type(e).__name__}: {e}"[:300],
                                                               "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
                        print(f"[{n}/{len(todo)}] {item['code']} FAILED (not retried): {type(e).__name__}: {str(e)[:140]}", flush=True)
                        continue
                    done_n += 1
                    print(f"[{n}/{len(todo)}] {item['code']} analyzed in {secs:.0f}s", flush=True)
                continue
            if kind == "fatal":
                (out_root / "FATAL").write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {payload}\n")
                print(f"FATAL (no retry): {payload}")
                rc = 5
            break
    finally:
        stop.set()
        shutil.rmtree(work_parent, ignore_errors=True)
    print(f"analyzed {done_n}")
    return rc


if __name__ == "__main__":
    sys.exit(main())

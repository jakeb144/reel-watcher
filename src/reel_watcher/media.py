"""ffmpeg/ffprobe helpers and Instagram URL parsing."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.parse

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
SCENE_THRESHOLD = 0.30  # hard cuts on a 320px downscale; few false positives on fast motion
MIN_SHOT_GAP = 0.2  # merge cuts closer than this (flash frames, double triggers)


def preflight() -> str:
    """Empty string when ffmpeg and ffprobe are on PATH, else an install hint."""
    missing = [b for b in ("ffmpeg", "ffprobe") if not shutil.which(b)]
    return f"Missing on PATH: {', '.join(missing)}. Install with: brew install ffmpeg" if missing else ""


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({p.returncode}): {(p.stderr or p.stdout).strip()[-600:]}")
    return p


def id_from_url(url: str) -> str | None:
    """'ig_<shortcode>' parsed offline from an Instagram reel/post URL, else None."""
    u = urllib.parse.urlparse(url)
    host = (u.hostname or "").lower().removeprefix("www.")
    if host.endswith("instagram.com"):
        m = re.search(r"/(?:reels?|p|tv)/([A-Za-z0-9_-]+)", u.path)
        if m:
            return "ig_" + m.group(1)
    return None


def probe(path: str) -> dict:
    p = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path])
    j = json.loads(p.stdout)
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), None)
    if not v:
        raise RuntimeError(f"no video stream in {path}")
    a = any(s.get("codec_type") == "audio" for s in j["streams"])
    fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1"
    n, _, d = fr.partition("/")
    fps = float(n) / float(d or 1) if float(d or 1) else 0.0
    dur = float(j.get("format", {}).get("duration") or v.get("duration") or 0)
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = abs(int(sd["rotation"])) % 180
    w, h = int(v["width"]), int(v["height"])
    if rot == 90:
        w, h = h, w
    return {"duration": round(dur, 3), "width": w, "height": h, "fps": round(fps, 3), "has_audio": a}


def detect_cuts(video: str, duration: float, threshold: float = SCENE_THRESHOLD) -> list[float]:
    p = run(["ffmpeg", "-nostdin", "-i", video, "-an", "-vf",
             f"scale=320:-2,select='gt(scene,{threshold})',showinfo", "-f", "null", "-"])
    times = [float(x) for x in re.findall(r"pts_time:([\d.]+)", p.stderr)]
    cuts: list[float] = []
    for t in sorted(times):
        if t < MIN_SHOT_GAP or t > duration - MIN_SHOT_GAP:
            continue
        if cuts and t - cuts[-1] < MIN_SHOT_GAP:
            continue
        cuts.append(round(t, 3))
    return cuts

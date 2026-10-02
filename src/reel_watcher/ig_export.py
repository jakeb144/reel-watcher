"""Turn an Instagram "Download your information" export into a URL list for reel-watcher.

How to get the export (Instagram app or web, Accounts Center):
  Settings > Accounts Center > Your information and permissions > Download your information
  > Download or transfer information > choose your Instagram account > Some of your information
  > tick "Saved" (under Your Instagram activity) > Export to device > Format: JSON.
Unzip the download. The saved posts are in a JSON file such as
  your_instagram_activity/saved/saved_posts.json
and named collections, if you have any, in saved_collections.json next to it.

Shape this parser expects (as documented by users of the export; Instagram changes it
from time to time, so the parser is deliberately tolerant):
  {"saved_saved_media": [
      {"title": "<creator username>",
       "string_map_data": {"Saved on": {"href": "https://www.instagram.com/reel/<code>/", "timestamp": 1700000000}}}]}
Rather than trust one exact layout, it walks the whole JSON and collects every
instagram.com/reel|p|tv link, using the nearest "title" as the creator hint. HTML exports work
too (links are read with a regex). If your export looks different, any file with one URL per line
is accepted by `reel-watcher study` directly.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from . import media

URL_RE = re.compile(r"https?://(?:www\.)?instagram\.com/(?:reels?|p|tv)/[A-Za-z0-9_-]+/?[^\s\"'<>]*")


def _walk(node, title: str | None, out: list[tuple[str, str | None]]) -> None:
    if isinstance(node, dict):
        t = node.get("title") if isinstance(node.get("title"), str) and node.get("title") else title
        for k, v in node.items():
            if k == "href" and isinstance(v, str) and media.id_from_url(v):
                out.append((v, t))
            else:
                _walk(v, t, out)
    elif isinstance(node, list):
        for v in node:
            _walk(v, title, out)
    elif isinstance(node, str) and media.id_from_url(node) and node.startswith("http"):
        out.append((node, title))


def extract_urls(path: Path, collection: str = "") -> list[dict]:
    """Return [{url, creator, collection}] from a JSON or HTML export file, deduped by shortcode, order kept."""
    text = path.read_text(encoding="utf-8", errors="replace")
    pairs: list[tuple[str, str | None]] = []
    if path.suffix.lower() == ".json":
        _walk(json.loads(text), None, pairs)
    else:
        pairs = [(m.group(0).split("?")[0], None) for m in URL_RE.finditer(text)]
    seen, rows = set(), []
    for url, creator in pairs:
        code = media.id_from_url(url)
        if code in seen:
            continue
        seen.add(code)
        rows.append({"url": url.split("?")[0], "creator": creator, "collection": collection})
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="reel-watcher export", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", help="saved_posts.json (or other export JSON/HTML files)")
    ap.add_argument("-o", "--out", required=True, help="TSV to write (url<TAB>collection)")
    ap.add_argument("--collection", default="", help="label to attach to every URL from these files (e.g. the file's collection name)")
    a = ap.parse_args(argv)
    rows, seen = [], set()
    for f in a.files:
        for r in extract_urls(Path(f).expanduser(), a.collection):
            if r["url"] not in seen:
                seen.add(r["url"])
                rows.append(r)
    out = Path(a.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(f"{r['url']}\t{r['collection']}\n" for r in rows), encoding="utf-8")
    print(f"wrote {len(rows)} urls to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# Agent guide for reel-watcher

reel-watcher analyzes a user's saved Instagram reels with local models on an Apple Silicon Mac.
Read `README.md` first for behavior, options and the output schema.

## Run

```bash
uv sync
uv run pytest                                        # must pass before and after any change
uv run reel-watcher study --local clip.mp4 --out-root ./out    # no network, no cost
uv run reel-watcher study --input urls.tsv --out-root ./out    # dry run (default)
uv run reel-watcher study --input urls.tsv --out-root ./out --run --limit 3
```

## Rules

- Never spend money without the user's explicit OK. `--run` calls Apify (about $0.002 per reel). Always
  show the dry run and the count first, start with `--limit 3`, keep the per-batch cap.
- `APIFY_TOKEN` is read from the environment only. Never write it to a file, a log, a URL or a commit.
- Never log in to Instagram, use browser cookies, or automate the Instagram UI. Reel URLs come from the
  user's official data export (`reel-watcher export`) or a plain URL list.
- Do not commit media, frames, transcripts or study output (`.gitignore` covers `reel-watcher-out/`). No real
  reel data in tests or examples; use placeholders.
- Keep analysis local. Do not add a hosted-LLM or paid-API dependency.
- No em dash characters anywhere (a test enforces it in Python sources).

## Code map (`src/reel_watcher/`)

| File | Role |
|---|---|
| `cli.py` | `reel-watcher study|export|advice` dispatcher |
| `study.py` | The pipeline: Apify fetch, frame sampling, OCR, dedupe, contact-sheet VLM call, whisper, giveaway detection, archive, JSON writing, `main()` |
| `model.py` | Vision-model and whisper repo names (env overridable), `Vision.ask`, `parse_json` |
| `media.py` | ffprobe/ffmpeg helpers, scene cuts, Instagram URL parsing, preflight |
| `ig_export.py` | Instagram data-export (saved_posts JSON/HTML) to URL list |
| `advice_library.py` | Optional renderer: advice library JSON to one static HTML page |

Tests live in `tests/`; they use no network and no models (the vision model is faked).

## Extend

- New field in the verdict: edit `SYNTH_PROMPT` in `study.py`, then the schema block in `README.md`.
- Different model: set `REEL_WATCHER_VLM`; `Vision.ask` expects an `mlx-vlm` compatible repo.
- New input source: add a parser that yields Instagram URLs, write a TSV (`url<TAB>collection`).
- Add a test for every pure function you change (`dedupe_frames`, `parse_giveaway`, `map_tiles`, parsers).
- Do not publish or push anything without the owner's approval.

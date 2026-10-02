# reel-watcher

Made by Jacob Bruce ([Instagram](https://www.instagram.com/itsjakebruce/)).

You save reels on Instagram and never go back to them. reel-watcher has an AI
model on your own Mac watch them for you and write down what each one is: the
hook, the format, the on-screen text, the transcript, the editing tricks, any
"comment WORD and I'll send you X" giveaway, and a one-line verdict on why it
works. You end up with one searchable JSON file per reel.

Everything except the media download runs on your machine. No analysis data
leaves it.

## What it does per reel

1. Fetches the video (or carousel slides) through Apify, the only network step.
2. Cuts the video into key frames (hook frames plus one frame after each scene cut).
3. Reads on-screen text with Apple Vision OCR.
4. Merges near-duplicate frames, then asks a local vision model about all the
   unique frames at once, using one labeled contact sheet (this is the speed trick).
5. Transcribes speech with word timing (mlx-whisper, `large-v3-turbo`, or `whisper-small` on the small setup).
6. Detects giveaways: "comment X", "DM me X", "link in bio", and what you get.
7. Writes `study/<code>.json` and a small hook image. With `--archive DIR` it also
   keeps the video and every analyzed frame.

## Hardware

- Apple Silicon Mac (M1 or newer). It uses MLX and Apple Vision, so Intel, Linux and Windows are not supported.
- RAM decides the model. `--model-size auto` (the default) picks `small` below 32 GB and `large` otherwise.

  | Mac RAM | Setting | Vision model | Whisper | First download |
  |---|---|---|---|---|
  | 16 GB | `small` | `Qwen2.5-VL-7B-Instruct-4bit` | `whisper-small-mlx` | about 6 GB |
  | 32 GB or more | `large` | `Qwen3.6-35B-A3B-4bit` | `whisper-large-v3-turbo` | about 20 GB |

  Force one with `--model-size small|large` or `REEL_WATCHER_MODEL`, or pass any MLX vision model with `--model <hf id>`.
- Roughly a minute or two of compute per reel, depending on the Mac.
- `ffmpeg` on PATH (`brew install ffmpeg`) and [uv](https://docs.astral.sh/uv/) (`brew install uv`).

## Cost

- Apify (to download the media): about $0.002 per reel with the `apify/instagram-scraper` actor.
  The tool sends `maxTotalChargeUsd` with every batch (default $2, set with `--apify-batch-cap-usd`)
  and writes the actual cost of each run to `apify_cost.jsonl`. Check current Apify pricing yourself.
- All analysis (OCR, vision model, whisper) is local and free.
- Without an `APIFY_TOKEN` nothing is downloaded and nothing is charged. Dry run is the default.

## 60-second quickstart

```bash
git clone <this repo> reel-watcher && cd reel-watcher
brew install ffmpeg uv            # skip what you already have
uv sync                           # installs MLX, whisper, OCR bindings

# 1. Try it on any mp4 you own. No Apify, no account, no cost.
uv run reel-watcher study --local ~/Movies/some-clip.mp4 --out-root ./out
cat out/study/some-clip.json

# 2. Your saved reels: put a URL list in a file (see "Getting your saved reels"), then
export APIFY_TOKEN=...            # from console.apify.com, never commit it
uv run reel-watcher study --input urls.tsv --out-root ./out            # dry run, lists what it would fetch
uv run reel-watcher study --input urls.tsv --out-root ./out --run --limit 5
```

The first run downloads the models. Results are resumable: finished reels are skipped,
failed reels are logged to `out/study/_failed.jsonl` and retried only with `--retry-failed`.

## Getting your saved reels

Use Instagram's own data download, the safest route because it never touches your login.

1. Instagram (app or web) > Settings > Accounts Center > Your information and permissions >
   Download your information > Download or transfer information > your Instagram account >
   Some of your information > tick **Saved** > Export to device > format **JSON**.
2. Wait for the email, download and unzip. Look for `your_instagram_activity/saved/saved_posts.json`.
3. Convert it:

```bash
uv run reel-watcher export path/to/saved_posts.json -o urls.tsv
```

The parser walks the JSON and collects every `instagram.com/reel|p|tv/<code>` link, so it tolerates layout
changes. It was written from the documented shape (`saved_saved_media` entries with
`string_map_data["Saved on"].href`) and covered by tests on that shape; confirm against your own file and
open an issue if Instagram changed it. HTML exports work too. Any text file with one URL per line
(optionally `url<TAB>collection`) also works, see `examples/urls.tsv`.

## Common options

| Option | Meaning |
|---|---|
| `--run` | Actually fetch and analyze (default is a dry run) |
| `--limit N` | Stop after N new reels |
| `--local A.mp4 B.mp4` | Analyze local files, no network |
| `--archive DIR` | Keep video, frames and manifest per reel in `DIR/<code>/` |
| `--collections a,b` and `--only-priority` | Do reels from matching collections first (or only) |
| `--model-size small\|large` | Model preset (default auto by RAM, see Hardware) |
| `--model ID` | Exact Hugging Face MLX vision model id, overrides the preset |
| `--fast` | Fewer frames and tokens |
| `--detailed` | One model call per frame (slower, used automatically if the contact-sheet call fails) |
| `--rollup` | Rebuild `study_index.jsonl` from `study/*.json` |

Environment: `APIFY_TOKEN` (required for `--run`), `REEL_WATCHER_MODEL`, `REEL_WATCHER_VLM`, `REEL_WATCHER_WHISPER`,
`REEL_WATCHER_CONTEXT` (one phrase describing you, e.g. "a freelance video editor", so the verdict
speaks to your goals), `REEL_WATCHER_WORKFLOW_RE` (regex of collection names whose reels also get a
`workflow` field).

## Output: one study (`out/study/<code>.json`)

```json
{
  "code": "PLACEHOLDER",
  "url": "https://www.instagram.com/reel/PLACEHOLDER/",
  "kind": "reel",
  "collections": ["Example collection"],
  "post": {"caption": "...", "author": "creator", "likes": 0, "views": 0, "comments": 0,
           "post_date": "2026-01-01", "duration_s": 24.0, "music_info": null},
  "facts": {"duration_s": 24.0, "resolution": "720x1280", "fps": 30.0, "cuts": 9, "cuts_per_10s": 3.8,
            "cut_times": [1.2, 3.0], "broll_share_est": 0.4,
            "audio": {"has_audio": true, "speech_share": 0.8, "music_or_sfx_likely": false}},
  "hook_frame_text": ["text seen in the first 3 seconds"],
  "analysis": {
    "hook": {"spoken": "", "on_screen": "", "visual": "", "type": "bold claim"},
    "format": "talking head",
    "shot_types": [], "setting": "", "caption_style": {}, "graphics_motion": "",
    "music_sfx": {"music": "unclear", "sfx": "no"},
    "music_mentions": [],
    "cta": {"spoken": "", "on_screen": "", "comment_word": "", "link_or_follow": ""},
    "structure_beats": [{"t": 0.0, "beat": "hook", "what": ""}],
    "why_it_works": "one line",
    "cta_asset": {"offered": false, "word": "", "asset_type": "", "what_it_is": "", "delivery": "",
                  "value_for_viewer": "low", "why": ""},
    "takeaway": {"relevant": false, "area": "", "insight": "", "action": ""}
  },
  "giveaway": {"offered": true, "keyword": "GUIDE", "what_they_get": "free guide", "channel": "comment",
               "evidence": [{"quote": "comment GUIDE below", "where": "transcript"}]},
  "comment_words_detected": ["GUIDE"],
  "music": [],
  "transcript": {"language": "en", "text": "...", "words": [{"w": "hello", "s": 0.1, "e": 0.4}]},
  "frames": [{"t": 1.0, "file": "frames/f_000100.jpg", "shot_type": "talking head",
              "visual": {"on_screen_text": "", "graphic_type": "talking head", "what_it_shows": "",
                         "font_feel": null, "editing_technique": ["text pop"],
                         "worth_studying": {"worth": false, "why": ""}}}],
  "frames_sampled": 12, "turbo": true, "timing_s": {"whisper": 9.0, "synth": 6.0},
  "keyframe": "PLACEHOLDER.jpg", "analyzed_at": "2026-01-01T00:00:00", "wall_s": 41.2
}
```

Also written: `study_index.jsonl` (one summary line per reel) and `apify_cost.jsonl`.
Model output is not guaranteed to be valid or correct: treat `analysis` as a draft, and verify anything that matters.

## Optional: advice library

`reel-watcher advice --in library.json --out advice-library.html` renders a static page of
principles grouped by topic, each with its sources, an "implement?" checkbox and a copy-my-picks button.
It does not call any model. Have your AI assistant build `library.json` from your `study/*.json` using
`prompts/build_advice_library.md`; the format is shown in `examples/advice_library.json`.

## Ethics and terms

- Only analyze reels you saved yourself, for your own learning and research.
- Respect creators. Credit them, do not copy their work, and do not redistribute their videos, frames or transcripts.
  `--archive` is a private reference shelf, not a library to publish.
- Scraping can conflict with Instagram's Terms of Use. You are responsible for how you use Apify and for
  complying with the platform's terms and the law where you live. This tool does not log in to your account
  and does not use your cookies.
- Giveaway and offer detection is a convenience. Do not use it to mass-comment or spam creators.
- Model output can hallucinate, including names and quotes. Do not publish it as fact about a person.

## Development

```bash
uv run pytest
```

See `AGENTS.md` for the code map. MIT licensed, see `LICENSE`.

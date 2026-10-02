# Send this to your AI coding assistant

Copy everything below the line into Claude Code, Codex or a similar assistant that can run commands on your Mac.

---

Set up reel-watcher on this Mac and run it on a small sample. Work in the repo folder you were opened in
(clone it if needed). Follow `AGENTS.md` and `README.md`.

1. Check this is an Apple Silicon Mac with enough memory (`uname -m` must say arm64; `sysctl hw.memsize`).
   If it has under 32 GB of RAM, tell me, and set `REEL_WATCHER_VLM` to a smaller `mlx-community` vision
   model before the first run.
2. Install what is missing: `brew install ffmpeg uv`, then `uv sync`. Run `uv run pytest` and show me the result.
3. Smoke test with no network and no cost: ask me for one mp4 I own, run
   `uv run reel-watcher study --local <file> --out-root ./out`, and summarize `out/study/<name>.json`
   in five lines. The first run downloads the models, which is slow. Tell me before you start that it will.
4. Saved reels: explain how to download my Instagram data (README section "Getting your saved reels"),
   wait for me to put `saved_posts.json` somewhere, then run `uv run reel-watcher export <file> -o urls.tsv`
   and tell me how many reels it found.
5. Apify: I will create an account and an API token myself. Tell me to run `export APIFY_TOKEN=...` in my own
   shell. Never ask me to paste the token into chat, and never write it to a file.
6. Show the dry run (`uv run reel-watcher study --input urls.tsv --out-root ./out`) and the estimated cost
   at about $0.002 per reel. Do NOT pass `--run` until I say yes to that specific cost.
   Then run `--run --limit 5` first, and show me the results before doing the rest.
7. Rules: never log in to Instagram for me, never use my browser cookies, never commit my data, and keep all
   analysis local.

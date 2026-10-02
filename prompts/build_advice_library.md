# Build an advice library from your studies (optional)

Give this to your AI assistant after you have analyzed some reels. It only reads your local `out/study/*.json`.

---

Read every `out/study/*.json` (skip files starting with `_`). Extract each concrete piece of advice a creator
gives or demonstrates (look in `transcript.text`, `post.caption`, `analysis.why_it_works`, `analysis.takeaway`,
`analysis.workflow`). Group similar advice into principles, and write `library.json` in the format of
`examples/advice_library.json`:

- `buckets[]`: `bucket` (slug), `title`, `principles[]`.
- Each principle: `id`, `principle` (one sentence), `detail` (a few lines), `note` (what it means for me, optional),
  `strength`, `n_independent_creators`, `n_sources`, `best_evidence`
  (`creator_shows_results | cites_data | claims_results | opinion | selling`), `conflicts_with` (list of ids),
  and `sources[]` with `code`, `author`, `views`, `evidence`, `quote` (a short real quote from that reel).
- Strength is mechanical: strong = 3 or more independent creators, or 2 or more who show results or cite data;
  weak = one creator whose evidence is opinion or selling; everything else medium.
- Never invent a quote or a source. If you cannot find a quote, leave the principle out.
- Do not copy long passages from a creator; short quotes only.

Then run `uv run reel-watcher advice --in library.json --out advice-library.html` and open the page.

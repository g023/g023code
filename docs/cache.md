# `cache.py` — the per-project SQLite cache

`g023_code/cache.py` persists every expensive, repeatable artifact in one
per-project SQLite database (`.g023/cache.db`, path from
`config.get_cache_db()`). `get_cache()` returns a module-level singleton.

## Tables (`_init_schema`)

- **file_summaries** — ReadFile summary cache, keyed by content hash
  (`sha256` of file bytes, or of `bytes\x00focus` when a focus is given).
  Stores the summary JSON, symbol map, token/char counts, hit/miss stats,
  `accessed_at`. Serves `get_file_summary` / `put_file_summary`;
  `invalidate_file` (content changed) and `purge_old_files` (LRU trim).
- **vision** — image analysis cache, keyed by image hash + question +
  backend/model. `get_vision` / `put_vision`.
- **web_cache** — full web responses: `url_hash` (PK), `url`, `final_url`,
  `status`, `headers` (JSON), `body`, `engine`, `profile`, `fetched_at`,
  `accessed_at`, `hit_count`. `get_web(url, touch=True)` returns a cached
  response **regardless of age** — the docstring is explicit that
  freshness is the caller's business, not storage's; `touch_web` bumps
  access; `put_web` inserts/replaces; `list_web` / `clear_web` power the
  `/fetch` cache screens.
- **prefix_stats** — per-day, per-model cache-hit/miss token totals.
  `record_prefix_stats` feeds the hit-rate signal (`signals.md`);
  `prefix_history` reads it back for the verdict.
- **facts** — tiny key/value store (`set_fact` / `get_fact`) for session
  facts that should survive restarts.

## Behaviour notes

- Every table tracks `hit_count` and `accessed_at`, so the CLI can report
  what the cache actually saves.
- `_sha256` is the common key helper.
- `stats()` / `clear_all()` back the `/cache` command.
- The cache is deliberately dumb about freshness (web) and only about
  content identity (files) — eviction policy is driven by the callers and
  by `purge_old_files`, never by guesswork inside storage.

## Related

- `docs/subagents.md` — file_reader (summaries) and vision (image answers)
  are the main readers/writers.
- `docs/web_fetch.md` — `FetchUrl` results go through `get_web`/`put_web`.
- `docs/cli.md` — `/cache` and `/fetch` screens.
- `docs/config.md` — `get_cache_db()` / scratch dir.

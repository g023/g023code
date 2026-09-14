# `cache.py` — the per-project SQLite cache

`g023_code/cache.py` (~398 lines) persists every expensive, repeatable
artifact in one per-project SQLite database (`.g023/cache.db`, path from
`config.get_cache_db()`). `get_cache()` returns a module-level singleton.

## Tables (`_init_schema`)

- **file_cache** — ReadFile summary cache, keyed by `file_hash`
  (`sha256` of file bytes). Columns: `file_path`, `summary`, `metadata`
  (JSON), `raw_content`, `created_at`, `accessed_at`. Serves
  `get_file_summary` / `put_file_summary`; `invalidate_file` (content
  changed) and `purge_old_files` (LRU-by-access trim).
- **vision_cache** — image analysis cache, keyed by
  `(image_hash, question_hash, backend)`. The `backend` string includes
  model and, for DeepSeek, the endpoint (`deepseek:{model}:{endpoint}`).
  `get_vision` / `put_vision`.
- **web_cache** — full web responses: `url_hash` (PK), `url`, `final_url`,
  `status`, `headers` (JSON), `body`, `engine`, `profile`, `fetched_at`,
  `accessed_at`, `hit_count`. `get_web(url, touch=True)` returns a cached
  response **regardless of age** — the docstring is explicit that
  freshness is the caller's business, not storage's; `touch_web` bumps
  access; `put_web` inserts/replaces; `list_web` / `clear_web` power the
  `/fetch` cache screens.
- **prefix_stats** — per-day, per-model cache-hit/miss token totals.
  `record_prefix_stats` feeds the hit-rate signal (`signals.md`);
  `prefix_history` reads it back for the verdict. **`clear_all` does not
  erase this table** — it is a record of what happened, not cached data.
- **tool_vault** — content-addressed bulky tool results (`put_tool_body` /
  `get_tool_body` / `offload_tool_output`), keyed by `sha256` of the body.
  History holds a short `[g023_vault sha256:… chars=N]` pointer plus a
  head+tail excerpt so pytest failures at the end of a log stay visible.
  Same bytes are a no-op. `/cache clear` deletes this table. Prefix-stats
  are still preserved.
- **session_facts** — tiny key/value store (`set_fact` / `get_fact`).
  The working set lives here under the key `working_set`. `/cache clear`
  **does** delete session_facts (the working set resets); `/clear` also
  calls `working_set.reset()`.

## Behaviour notes

- `_sha256` is the common key helper.
- `stats()` / `clear_all()` back the `/cache` command. `stats()` reports
  row counts for files / vision / web / vault. `clear_all` does not erase
  `prefix_stats`.
- The cache is deliberately dumb about freshness (web) and only about
  content identity (files) — eviction policy is driven by the callers and
  by `purge_old_files`, never by guesswork inside storage.
- Learn guides (`learn.md`) and memory notes (`memory_notes.md`) are
  files, not rows. `/cache clear` does not touch them.

## Related

- `docs/subagents.md` — file_reader (summaries) and vision (image answers)
  are the main readers/writers.
- `docs/working_set.md` — the `session_facts` consumer.
- `docs/cost_policy.md` — when a tool result is vaulted vs kept in history.
- `docs/web_fetch.md` — `FetchUrl` results go through `get_web`/`put_web`.
- `docs/cli.md` — `/cache` and `/fetch` screens.
- `docs/config.md` — `get_cache_db()` / scratch dir.

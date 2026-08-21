# `config.py` — paths, settings, API keys, persistence

`g023_code/config.py` defines where everything lives, what every knob is,
and how persisted config is validated. Almost every other module imports
something from here.

## Paths

| Function | Returns | Notes |
|----------|---------|-------|
| `get_home()` | install dir | `$G023_HOME` else package parent. Holds `K.dat`, `OK.dat`, machine `config.json`, `ds_*.json`. |
| `get_project_root()` | working project | `$G023_PROJECT_ROOT` else `cwd`. |
| `get_scratch_dir()` | `<project>/.g023/` | created on demand; holds `cache.db`, project `config.json`, `backups/`. |
| `get_cache_db()` | `.g023/cache.db` | the SQLite cache (`cache.md`). |
| `get_config_file()` | machine `config.json` | next to `K.dat` — global defaults. Vision setup describes the local machine, so it lives here, not per-project. |
| `get_project_config_file()` | `.g023/config.json` | overrides the machine file for one project. |

## API keys

- `load_api_key()` — DeepSeek key from `K.dat` (first line). Recognises
  the placeholder values the installers leave behind
  (`YOUR_DEEPSEEK_API_KEY_HERE`, `sk-REPLACE-WITH-YOUR-DEEPSEEK-API-KEY`)
  and turns them into a clear instruction instead of a confusing 401.
- `load_openrouter_key()` — OpenRouter key from `OK.dat`, same pattern.

## `Settings` dataclass — every knob, one place

Groups of fields (all with defaults; `__post_init__` resolves `Path`s):

- **Models**: `orchestrator_model`, `subagent_model` (both
  `deepseek-v4-flash`), `reasoning_effort` (low/high/max), `thinking_enabled`.
- **API**: `base_url`, `max_tokens` (0 = don't send a budget → the model's
  own ceiling; the default), `temperature` (0.2).
- **Context**: `max_context_tokens` (900k, headroom under 1M),
  `compact_threshold` (0.85).
- **Agent loop**: `max_iterations` (255 in-code default, but 0/no-cap is
  the honoured "unlimited" and what the config file ships; see below).
- **Subagents/Tools**: `max_search_matches` (12),
  `file_summary_max_tokens` (400), `permission_default` (ask),
  `file_reader_raw` (env `G023_READFILE_RAW` — baseline mode that makes
  ReadFile return raw content instead of summaries),
  `write_backup` / `write_backup_keep` (env
  `G023_WRITE_BACKUP` / `G023_WRITE_BACKUP_KEEP`).
- **Vision**: `vision_backend` (none/ollama/deepseek/openrouter),
  `vision_api_key`, `vision_model`, `vision_host` (fallback `OLLAMA_HOST`,
  then localhost), `vision_max_image_dim` (1024 downscale),
  `vision_timeout` (180 s), `vision_num_ctx` (4096),
  `vision_keep_alive` (5m), `vision_endpoint` (responses/chat_completions),
  `deepseek_vision_model` (`deepseek-v4-flash-vision-exp`),
  `or_default_model`, `or_vision_models`, `or_credits`.
- **Surge**: `surge_notify` (true), `surge_pause` (false),
  `surge_pause_buffer_minutes` (5).
- **Runtime**: `stream` (true), `verbose` (low/mid/high), `auto_compact`
  (true), `show_tool_timing`, `show_context_bar`, `project_root`, `home`.

Properties: `show_answer_detail` (verbose ≥ mid), `show_thinking` (verbose
≥ high), `vision_enabled` (backend ≠ none), `effective_vision_model`
(configured model or the backend default).

## Constants

- `AVAILABLE_MODELS = ("deepseek-v4-flash",)` and `UNAVAILABLE_MODELS`
  — `deepseek-v4-pro` is *withheld* because the Responses API doesn't serve
  it yet; offering it would hand the user a model every call fails on.
- Loop bounds: `MIN_ITERATIONS=1`, `UNLIMITED_ITERATIONS=0`,
  `MIN_OUTPUT_TOKENS=1`, `UNLIMITED_OUTPUT_TOKENS=0`,
  `ITERATION_NOTICE_EVERY=10` (how often the loop prints a persistent
  progress line).
- OpenRouter: `OPENROUTER_DEFAULT_MODEL`,
  `OPENROUTER_FREE_DAILY_LIMIT=50`, `OPENROUTER_CREDITS_DAILY_LIMIT=1000`.
- Surge: `SURGE_PEAK_WINDOWS_BEIJING`, `SURGE_UTC_OFFSET` — loaded from
  `ds_times.json` by `_load_ds_times()` (home, then project root; falls
  back to DeepSeek's published sheet: 09–12 & 14–18 Beijing, UTC+8).

## Persisted config (two scopes)

Only `PERSISTED_KEYS` survive between sessions — everything else is
per-session. `_ALLOWED` constrains specific keys to a whitelist (models,
effort, verbose, backends, endpoints, permission_default).

- `load_config(target=settings)` — applies the **machine** file first,
  then the **project** file (project wins). Missing/broken files are a
  no-op. Returns the dict of applied keys.
- `save_config(target, scope)` — writes the current settings to the
  requested scope (`global` = machine file, `project` = project file).
- `_coerce(key, value, default)` — one validator per persisted key:
  bool→verbose mapping for old configs; any non-negative `max_iterations`
  / `max_tokens` honoured (0 = unlimited), only negatives/non-numbers are
  dropped; whitelist keys rejected unless they match; types checked
  against the current default. **A hand-edited or older config file can
  never put the app into a state its own commands cannot describe.**

## Display helpers

`describe_steps(n)` / `describe_max_tokens(n)` — one wording for
"unlimited" used by every command.

## Related

- `docs/data_files.md` — `config.json`, `K.dat`, `OK.dat`, `ds_times.json`
  on disk.
- `docs/entrypoints.md` — the env vars the launchers set.
- `docs/surge.md` — what consumes the surge windows.
- `tests/test_config_scopes.py` — pins the machine-vs-project split.

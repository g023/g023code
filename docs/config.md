# `config.py` — paths, settings, API keys, persistence

`g023_code/config.py` defines where everything lives, what
every knob is, and how persisted config is validated. Almost every other
module imports something from here.

## Paths

| Function | Returns | Notes |
|----------|---------|-------|
| `get_home()` | install dir | `$G023_HOME` else package parent. Holds `K.dat`, machine `config.json`, `ds_*.json`. |
| `get_project_root()` | working project | `$G023_PROJECT_ROOT` else `cwd`. |
| `get_scratch_dir()` | `<project>/.g023/` | created on demand; holds `cache.db`, project `config.json`, `backups/`, `sessions/`, `logs/`, `prompts/`, `memory/`, `learn/`. |
| `get_cache_db()` | `.g023/cache.db` | the SQLite cache (`cache.md`). |
| `get_config_file()` | machine `config.json` | next to `K.dat` — global defaults. |
| `get_project_config_file()` | `.g023/config.json` | overrides the machine file for one project. |

## API keys

- `load_api_key()` — DeepSeek key from `K.dat` (first line). Recognises
  the placeholder values the installers leave behind
  (`YOUR_DEEPSEEK_API_KEY_HERE`, `sk-REPLACE-WITH-YOUR-DEEPSEEK-API-KEY`)
  and turns them into a clear instruction instead of a confusing 401.
  It is the only credential the program reads.

## `Settings` dataclass — every knob, one place

Groups of fields (all with defaults; `__post_init__` resolves `Path`s):

- **Model**: not a setting — see `MODEL` below. `orchestrator_model` and
  `subagent_model` survive only as read-only properties returning `MODEL`.
  `reasoning_effort` (int 0–75, default **75**,
  ceiling **75**),
  `thinking_enabled`. String aliases (`high`, `max`, `off`, …) coerce on
  load and on `/thinking` via `effort.parse_effort_value` (100 / `max`
  cap to 75).
- **API**: `base_url`, `max_tokens` (0 = don't send a budget → the model's
  own ceiling; the default), `temperature` (0.2).
- **Context**: `max_context_tokens` (**1M**, DeepSeek's real limit; set with
  `/max-ctx`), `compact_at_tokens` (**200,000**; 0 or 1k–1M; set with
  `/compact-threshold 200k`), `compact_threshold` (0.85 of the window). Auto
  compaction fires at the lower of the two. `compact_policy` (`economic`
  default | `fixed`) and `compact_floor_tokens` (64k) let it fire earlier once
  re-reading the history has cost more than compacting (docs/economics.md).
- **Tools**: `subagents_enabled` (**false**; `/subagents on`). All persisted. `ownership` (**auto**; `/ownership auto|user|agent`) — see ownership.md. Persisted.
- **Agent loop**: `max_iterations` (**255** — the Settings default and what
  the installer writes; `0` is unlimited).
- **Subagents/Tools**: `max_search_matches` (12),
  `file_summary_max_tokens` (400 — FileReader's model-summary
  `max_output_tokens`; the local AST path does not call the model),
  `permission_default` (ask),
  `file_reader_raw` (env `G023_READFILE_RAW` — baseline mode that makes
  ReadFile return raw content instead of summaries),
  `write_backup` / `write_backup_keep` (env
  `G023_WRITE_BACKUP` / `G023_WRITE_BACKUP_KEEP`).
- **Write side**: `verify_replay` (true; env `G023_VERIFY_REPLAY=0` turns it
  off — re-run the check that failed after a successful write) and
  `failure_attribution` (true; env `G023_ATTRIBUTION=0` — re-run a failing
  check in a shadow copy without this turn's edits, `docs/attribution.md`).
- **Finish**: `finish_on_success` (true; env `G023_FINISH_ON_SUCCESS=0`
  turns it off) — Bash/Edit/WriteFile take an optional `final_answer` that ends
  the turn when that step succeeds, with no answer-only model call
  (`docs/finish.md`).
- **Pre-emptive reads**: `preread` (`ghost`; env `G023_PREREAD=off|prose|ghost`)
  — before call 1 the harness runs the ReadFile calls (and, for a rename, one
  SearchContent) the request makes predictable, recorded as the model's own
  tool exchange (`docs/preread.md`).
- **Closing calls** (`docs/finish.md`): `finish_licensed` (true;
  `G023_FINISH_LICENSED=0`) — an answer on a check whose failures are all
  pre-existing is delivered with a `⚠` line; `answer_escrow` (true;
  `G023_ANSWER_ESCROW=0`) — a held answer is kept and released by `[deliver]`;
  `claimed_write_guard` (true; `G023_CLAIMED_WRITE_GUARD=0`) — an answer that
  claims a change to a request-named file no call wrote is held. Together
  with the two below, measured live: $/solved
  −20 % [90 % CI −30, −9], calls −14 %, all solved.
- **Editing tools**: `multi_edit` (true; `G023_MULTI_EDIT=0`) — Edit's
  `more_edits` hunks, optionally in other files, all or nothing
  (`docs/multi_edit.md`); `rename_tool` (true; `G023_RENAME_TOOL=0`) — the
  `Rename` tool (`docs/rename.md`).
- **Round trips** (`docs/hops.md`): `step_budget` (true; `G023_STEP_BUDGET=0`)
  — the model declares `[budget MIN-MAX +OVER]` per step and each batch ends
  with a `[hop N · step k of ... · request $X]` tracker. With
  `finish_on_success`, measured live on 7 task shapes (n=35 each, all solved):
  $ −8 % [90 % CI −14, −1], calls −16 %, wall time −10 %. Opt-in, each
  measured neutral or worse: `ordered_batches` (`G023_ORDERED_BATCHES=1`,
  program-order prompt contract + skip later checks after a failed write),
  `write_check` (`G023_WRITE_CHECK=1`, `check` command carried by
  Edit/WriteFile; worse together with finish).
- **Defaults stamp**: every saved config.json carries `defaults_rev`. A file
  without the current stamp was written by an older build that wrote every
  key at its default, so its `false` for a default that has since become true
  (`finish_on_success`, shipped as false in the first 1.5.0 build) is read
  as unset.
- **Lean turns** (`docs/lean.md`): `closure_receipts`, `lean_codec`,
  `lean_prompt`, `orient_card` (all true; env `G023_CLOSURE_RECEIPTS`,
  `G023_LEAN_CODEC`, `G023_LEAN_PROMPT`, `G023_ORIENT_CARD` `=0` turn them
  off). Measured live together: $/solved −26%, solved unchanged.
  `scope_count` (**false**; env `G023_SCOPE_COUNT=1`): a scoped SearchContent
  also reports `elsewhere_in_project`.
- **Search**: zero-hit near-miss names are on unless env `G023_NEAR_MISS=0`
  (not a setting; `docs/near_miss.md`). `search_tiers` (**false**; env
  `G023_SEARCH_TIERS=1`) ranks hits by what the line defines (a def/class
  named by the query, then module-level names, then local assignments, then
  plain uses). `def_peek` (**false**; env `G023_DEF_PEEK=1`) adds the body of
  the one definition a search names unambiguously. Both measured neutral live,
  so they are opt-in (`docs/lean.md`).
- **View deltas**: `view_deltas` (true; env `G023_VIEW_DELTAS=0` turns it
  off) — a Bash call that changes a file the model has seen shows its current
  text (or a diff), and changes made between turns by something other than
  the agent are reported at turn start (`docs/views.md`).
- **Tests and requests**: `test_oracle` (true; env `G023_TEST_ORACLE=0`) adds
  `[tests]` rows to pytest runs (`docs/testoracle.md`); `vacuity_check`
  (true; `G023_VACUITY=0`) re-runs new passing tests with the turn's source
  edits reverted; `obligations` (true; `G023_OBLIGATIONS=0`) checks function
  signatures the request names before the final answer; `write_guards`
  (true; `G023_WRITE_GUARDS=0`) refuses the first write to a file the request
  protects (`docs/obligations.md`); `claim_check` (true; `G023_CLAIM_CHECK=0`)
  notes an unbacked "tests pass" (`docs/claims.md`); `callee_digest` (true;
  `G023_CALLEE_DIGEST=0`) adds `uses:` rows to ReadFile (`docs/callees.md`).
- **Long output**: `salience_excerpt` (true; `G023_SALIENCE=0`) picks vault
  excerpt lines by the step's intent (`docs/salience.md`).
- **Local model** (`docs/local_model.md`, `/local`): `local_model` (**auto** |
  on | off; `G023_LOCAL=1/0`; auto = on when `autotune.py` finds llama-server,
  a model and a GPU it fits; read it through the `local_enabled` property),
  `local_llama_dir` (`~/llamacpp/llama.cpp`), `local_models_dir` (`~/MODELS`),
  `local_model_file` (empty = best measured model found), `local_vision_file`
  (empty = local vision when the served model has an mmproj; `off` = DeepSeek
  only), `local_gpu_layers` (99, ≥ 0), `local_ctx` (16384, ≥ 1024), `local_port`
  (0 = pick a free port), `local_gpu_guard` (true), `scout_card` (`local`;
  `G023_SCOUT`; `local` | `always` | `off`, docs/scout.md). `local_model`
  also accepts true/false (on/off).
- Every saved key can be changed with `/settings set KEY VALUE`; the value is
  typed by the key's current value and checked by the same rules a loaded
  config.json goes through (integer floors: `write_backup_keep` ≥ 1,
  `vision_timeout` ≥ 1, `surge_pause_buffer_minutes` ≥ 0).
- **Vision**: `vision_enabled` (**true**; `/vision on|off`) and
  `vision_timeout` (180 s). Images go to `MODEL` as an Anthropic image block,
  always capped at 800 px (`images.py`).
- **Surge**: `surge_notify` (true), `surge_pause` (false),
  `surge_pause_buffer_minutes` (5).
- **Runtime**: `stream` (true), `verbose` (low/mid/high), `auto_compact`
  (true), `exploration_ledger` (false; opt-in exploration notes carried across
  compaction, see `ledger.md`), `show_tool_timing`, `show_context_bar`,
  `project_root`, `home`.

Properties: `show_answer_detail` (verbose ≥ mid), `show_thinking` (verbose
≥ high), `orchestrator_model` / `subagent_model` (both `MODEL`, read-only).

## Constants

- `MODEL = "deepseek-flash"` (DeepSeek V4.1 Flash; `MODEL_LABEL` is the
  display name) and `MESSAGES_PATH = "/anthropic/v1/messages"`. The model is a
  **constant, not a setting**: no config file can name a model the API will not
  serve, and `DeepSeekClient` overwrites any `model` a caller passes.
- `LEGACY_KEYS` — keys older versions wrote (`api_format`, the model fields,
  the Ollama/OpenRouter vision fields, `or_*`). Ignored on load, removed from
  the file on the next save. `vision_backend: "none"` is the one with intent
  worth keeping: `_migrate_legacy` turns it into `vision_enabled = false`.
- `DEFAULT_COMPACT_AT_TOKENS = 200_000`,
  `DEFAULT_CONTEXT_TOKENS = 1_000_000`.
- Loop bounds: `MIN_ITERATIONS=1`, `UNLIMITED_ITERATIONS=0`,
  `MIN_OUTPUT_TOKENS=1`, `UNLIMITED_OUTPUT_TOKENS=0`,
  `ITERATION_NOTICE_EVERY=10` (how often the loop prints a persistent
  progress line).
- Context bounds: `MIN_CONTEXT_TOKENS=1_000`,
  `MAX_CONTEXT_TOKENS=1_000_000` (DeepSeek's real ceiling),
  `MIN_COMPACT_THRESHOLD=0.01`, `MAX_COMPACT_THRESHOLD=1.0`.
- Surge: `SURGE_PEAK_WINDOWS_BEIJING`, `SURGE_UTC_OFFSET` — loaded from
  `ds_times.json` by `_load_ds_times()` (home, then project root; falls
  back to DeepSeek's published sheet: 09–12 & 14–18 Beijing, UTC+8).

## Persisted config (two scopes)

Only `PERSISTED_KEYS` survive between sessions — everything else is
per-session. `_ALLOWED` constrains specific keys to a whitelist (verbose,
permission_default, compact_policy). `web_search_max_uses` and `fetch_backend` are
`LEGACY_KEYS` (1.5.0): ignored on load, dropped on save.
`get_global_cache_dir()` → `$G023_GLOBAL_CACHE_DIR` or
`<install>/global_cache` (the machine-wide web cache, `docs/web.md`). `reasoning_effort`
is coerced through `parse_effort_value` rather than a string whitelist.

- `load_config(target=settings)` — applies the **machine** file first,
  then the **project** file (project wins), each through `_migrate_legacy`. Missing/broken files are a
  no-op. Returns the dict of applied keys.
- `save_config(target, scope, *, full=None)` — writes settings to the
  requested scope (`global` = machine file, `project` = project file).
  Unknown keys in the file are kept; `LEGACY_KEYS` are dropped.
  Per-key merge: `Settings` records the persisted keys assigned
  since the last load/save (`dirty_keys()`; transient overrides and
  `restore` are not dirty). An implicit save writes only those, so another
  process's change to a different key survives. `full=True` (explicit
  `/config save`, `/settings save`, the `global` / `project` scopes, or a
  file that does not exist yet) writes every persisted key.
- `_coerce(key, value, default)` — one validator per persisted key:
  bool→verbose mapping for old configs; any non-negative
  `max_iterations` / `max_tokens` honoured (0 = unlimited), only
  negatives/non-numbers are dropped; in-range `max_context_tokens`
  (1k–1M) and `compact_threshold` (0.01–1.0) honoured, out-of-range
  dropped; whitelist keys rejected unless they match; types checked
  against the current default. **A hand-edited or older config file can
  never put the app into a state its own commands cannot describe.**

## Display helpers

`describe_steps(n)` / `describe_max_tokens(n)` — one wording for
"unlimited" used by every command. `describe_context_window(n)` /
`describe_compact_threshold(f)` — the wording the `/max-ctx` and
`/compact-threshold` commands (and `/settings`) use.

## Related

- `docs/data_files.md` — `config.json`, `K.dat`, `ds_times.json`
  on disk.
- `docs/effort.md` — the 0–100 scale (capped at 75) `_coerce` calls.
- `docs/entrypoints.md` — the env vars the launchers set (`G023_LEARN` is
  read by `learn.enabled()`, not by this module).
- `docs/surge.md` — what consumes the surge windows.
- `tests/test_config_scopes.py` — pins the machine-vs-project split and the
  legacy-key migration.

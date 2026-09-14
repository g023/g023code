# `subagents/` — the subagent-first delegation layer

`g023_code/subagents/` implements the program's core design: the
orchestrator model never ingests raw data — heavy tools are executed by
**isolated workers** that return compact, structured results.

- `__init__.py` — re-exports `SubagentRouter` / `get_router`.
- `router.py` (~667 lines) — the dispatcher the orchestrator calls.
- `file_reader.py` (~473 lines) — the ReadFile subagent.
- `searcher.py` (~714 lines) — SearchContent and Glob.
- `vision.py` (~360 lines) — the AnalyzeImage subagent.

## `router.py` — `SubagentRouter`

`delegate(tool_name, arguments)` intercepts the four heavy tools
(`SUBAGENT_TOOLS` in the orchestrator):

- `ReadFile` → `run_file_reader(...)` with args normalised
  (`start_line`/`end_line` coerced to ints).
- `SearchContent` → `run_searcher(...)`.
- `AnalyzeImage` → `run_vision(...)`.
- `Agent` → `_run_agent(...)`: a **depth-1 nested loop** on an isolated
  `items` list. `kind` is `research` | `planning` | `todos` | `task`
  (`explore`/`plan` alias research/planning).

  Research/planning are offered ReadFile, SearchContent, and ListDir
  only. Task workers get those plus Glob, Edit, WriteFile, and Bash
  (`AGENT_TASK_TOOLS`). LearnPut and Agent are refused at every kind.
  After at most `AGENT_MAX_STEPS` (8) model calls the loop returns
  size-capped JSON harvested from tool results plus the model's JSON.
  Parent `state.items` never receive the nested `function_call` items.
  `prior` / `issues` / `tasks` are the bidirectional relay: todos
  orchestrates task workers, each receiving issues from the previous
  task; the parent never ingests a nested transcript.

  **Shared prefix.** When the orchestrator has published its frozen
  `instructions` + `tools` + `reasoning.effort` (`set_shared_prefix`),
  the nested `/responses` body reuses those three fields so the standing
  cache units hit. The worker brief (JSON shape, allowed tools) moves
  into the first user item. `max_output_tokens` is omitted on that path
  — adding it busts the prefix. Execution still refuses any tool not in
  `AGENT_READONLY_TOOLS` / `AGENT_TASK_TOOLS`. Without a published
  prefix (tests, standalone router) the old isolated schemas are used.

  **Branch journal.** Each nested run writes an outline to
  `.g023/scratch/agent/{kind}-{hash}.json` (kind, objective, payload,
  per-item type/name/chars — not the full transcript). The parent JSON
  carries `branch` as that relative path. Depth stays 1: a second hop
  is another miss on a new input prefix and is not worth it unless the
  grandchild work is huge.

  DeepSeek `reasoning` is clamped by `clamp_ds_reasoning` (`medium` is a
  real variant and is sent as `medium`; unset todos/todos_plan=`medium`,
  everything else=`high`). `max` / `xhigh` / 76–100 cap to `high`: `max`
  often burns the entire output budget on thinking and returns empty JSON.
  Numeric strings map through `effort_to_wire`. Sent via `reasoning_param`.
  When
  `settings.or_sub_enabled` is on (`/orsub on`), `_run_agent_or` still
  offloads the completion to OpenRouter (`or_sub_with_failover`) — that
  path has no tools wire, so a ListDir listing is injected into the
  prompt; reasoning is clamped as before.
- `get_router()` — module singleton.

## `file_reader.py` — ReadFile

One entry point, several modes; the output is always a JSON blob with a
`summary`, a `symbols` map (name → line span), and coverage notes.
Paths go through `paths.confine_or_error`.

- **Local index** (`_local_metadata`): unfocused ReadFile is summarised
  **with zero API calls** — AST for Python, regex/structure for JS/TS,
  Go, Rust, Java, C/C++, Markdown, JSON, YAML/TOML, HTML, shell. The
  symbol map is capped at `SYMBOL_MAP_MAX = 80`.
- **LLM path**: only a `focus` hint goes to the model
  (`LLM_INPUT_MAX_CHARS = 18_000` cap, `max_output_tokens` =
  `settings.file_summary_max_tokens`), with `_merge_metadata` joining
  local metadata onto the model summary. Model id is
  `settings.subagent_model` (`deepseek-flash`).
- **Ranges**: `start_line`/`end_line` → verbatim slice
  (`RANGE_MAX_CHARS = 20_000`) — the exact escalation path when a summary
  wasn't enough. Line numbers are ground truth from the file, not the
  model's guess.
- **Raw**: `raw=true` → full verbatim content (`FULL_READ_MAX_CHARS = 200_000`).
- **Cache**: summaries are keyed by content hash (+ focus) in
  `cache.get_cache()`; `invalidate_file` on change.
- Caps are enforced with explicit truncation flags so the orchestrator
  knows when it got a partial answer.

## `searcher.py` — SearchContent and Glob

Metadata-first grep over the project tree with **zero API calls**.
Backend, in order: `rg --json` if ripgrep is on PATH, else `grep -R`,
else a Python walk (`search_backend_name()`). Results report a **total
occurrence count** separately from a **sample list** so a sample cap is
not presented as a total cap. `.gitignore` is honoured (`build_ignorer`).
`.g023/` is in `IGNORE_DIRS` for a normal walk; an overlay still lets
SearchContent read `.g023/memory/`.

- `_walk_files` walks lazily, pruning `IGNORE_DIRS`.
- Hand-written globs are normalised (`_glob_patterns` — a bare
  extension becomes `*.ext`; comma-separated parts and brace expansion
  supported; `_matches_glob` tests each).
- Result JSON: query, path, `files_scanned`, `truncated`, a
  `metadata_summary`, total count, and a sample of matches — never full
  files. Default sample cap `settings.max_search_matches` = 12.
- `glob_sync` / `run_glob` back the Glob tool: `**` and brace expansion,
  paths relative to the project root, confined.

## `vision.py` — AnalyzeImage

`run_vision(path_or_url, question)` picks the backend from
`settings.vision_backend` and caches answers by image hash + question +
backend (+ model / endpoint):

- **ollama** (`_run_ollama`): `probe` the host, resolve the model
  (`OllamaModel.supports_vision`), `load_image` (downscale via
  `ollama_client`), then `vision_chat_detailed`.
- **deepseek** (`_run_deepseek`): `load_image` → `api.ResponsesClient`
  `vision_responses`/`vision_chat` (`chat_answer` for the text);
  honours `vision_endpoint` (responses vs chat_completions). Model id
  `deepseek-flash`.
- **openrouter** (`_run_openrouter`): `or_vision_with_failover` with the
  model list from `OR_VISION.json`; `OrRateLimitError` handled with a
  clear hint.
- Every send is constrained-resized to `VISION_SEND_MAX_SIDE = 800` on
  each edge. `settings.vision_max_image_dim` of 1024 or 0 does **not**
  change the send size (`tests/test_vision_resize.py`).
- Failures end in a `_failure_hint` that tells the user *what to change*
  (host, model, key, backend) rather than a bare exception.

## Related

- `docs/or_subagents.md` — the OpenRouter offload backend (`OR_SUB_MODELS.json`).
- `docs/orchestrator.md` — the caller and `SUBAGENT_TOOLS`.
- `docs/cost_policy.md` — why children reuse the frozen prefix.
- `docs/cache.md` — summary/vision caches.
- `docs/paths.md` — root confinement.
- `docs/tools.md` — the lightweight tools these are paired with.
- `tests/test_file_reader.py`, `tests/test_search.py`,
  `tests/test_openrouter_vision.py`, `tests/test_root_confine.py`.

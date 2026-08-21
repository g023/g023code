# `subagents/` — the subagent-first delegation layer

`g023_code/subagents/` implements the program's core design: the
orchestrator model never ingests raw data — heavy tools are executed by
**isolated workers** that return compact, structured results.

- `__init__.py` — re-exports `SubagentRouter` / `get_router`.
- `router.py` — the dispatcher the orchestrator calls.
- `file_reader.py` — the ReadFile subagent.
- `searcher.py` — the SearchContent subagent.
- `vision.py` — the AnalyzeImage subagent.

## `router.py` — `SubagentRouter`

`delegate(tool_name, arguments)` intercepts the four heavy tools
(`SUBAGENT_TOOLS` in the orchestrator):

- `ReadFile` → `run_file_reader(...)` with args normalised
  (`start_line`/`end_line` coerced to ints).
- `SearchContent` → `run_searcher(...)`.
- `AnalyzeImage` → `run_vision(...)`.
- `Agent` → `_run_agent(...)`: spawns a **fresh Flash-model call** with
  reasoning (`reasoning_param(enabled=True, effort=settings.reasoning_effort)`),
  a system prompt that casts it as Explore/Plan depending on the
  objective, `max_output_tokens=8192`, `temperature=0.3`, and returns a
  JSON payload (text + truncation/error info). Usage is recorded in the
  `subagent` scope.
- `get_router()` — module singleton.

## `file_reader.py` — ReadFile

One entry point, several modes; the output is always a JSON blob with a
`summary`, a `symbols` map (name → line span), and coverage notes.

- **Local AST path** (`_local_python_metadata`): Python files under
  `LOCAL_SUMMARY_MAX_CHARS = 12_000` are summarised **with zero API
  calls** — the AST walk extracts classes, functions, imports, spans.
- **LLM path**: non-Python or larger or focused files go to the model
  (`LLM_INPUT_MAX_CHARS = 18_000` cap), with `_merge_metadata` joining
  local metadata onto the model summary.
- **Ranges**: `start_line`/`end_line` → verbatim slice
  (`RANGE_MAX_CHARS = 20_000`) — the exact escalation path when a summary
  wasn't enough. Line numbers are ground truth from the file, not the
  model's guess.
- **Raw**: `raw=true` → full verbatim content (`FULL_READ_MAX_CHARS = 200_000`).
- **Cache**: summaries are keyed by content hash (+ focus) in
  `cache.get_cache()`; `invalidate_file` on change.
- Caps are enforced with explicit truncation flags so the orchestrator
  knows when it got a partial answer.

## `searcher.py` — SearchContent

Metadata-first grep over the project tree with **zero API calls**:

- `_walk_files` walks lazily, pruning `IGNORE_DIRS` (.git, node_modules,
  .venv, __pycache__, …).
- Hand-written globs are normalised (`_glob_patterns` — a bare
  extension becomes `*.ext`; comma-separated parts supported;
  `_matches_glob` tests each).
- `_search_file` returns per-file matches with context, capped by
  `max_matches` (default `settings.max_search_matches` = 12) and
  per-file limits.
- Result JSON: query, path, `files_scanned`, `truncated`, a
  `metadata_summary`, and the matches — never full files.

## `vision.py` — AnalyzeImage

`run_vision(path_or_url, question)` picks the backend from
`settings.vision_backend` and caches answers by image hash + question +
backend:

- **ollama** (`_run_ollama`): `probe` the host, resolve the model
  (`OllamaModel.supports_vision`), `load_image` (downscale via
  `ollama_client`), then `vision_chat_detailed`.
- **deepseek** (`_run_deepseek`): `load_image` → `api.ResponsesClient`
  `vision_responses`/`vision_chat` (`chat_answer` for the text);
  honours `vision_endpoint` (responses vs chat_completions).
- **openrouter** (`_run_openrouter`): `or_vision_with_failover` with the
  model list from `OR_VISION.json`; `OrRateLimitError` handled with a
  clear hint.
- Failures end in a `_failure_hint` that tells the user *what to change*
  (host, model, key, backend) rather than a bare exception.

## Related

- `docs/orchestrator.md` — the caller and `SUBAGENT_TOOLS`.
- `docs/cache.md` — summary/vision caches.
- `docs/file_reader` details → `tests/test_file_reader.py`;
  vision → `tests/test_openrouter_vision.py`.
- `docs/tools.md` — the lightweight tools these are paired with.

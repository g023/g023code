# `subagents/` — the delegation layer (local workers; model calls opt-in)

`g023_code/subagents/` implements the program's core design: the
orchestrator model never ingests raw data — heavy tools are executed by
**isolated workers** that return compact, structured results.

- `__init__.py` — re-exports `SubagentRouter` / `get_router`.
- `router.py` — the dispatcher the orchestrator calls.
- `file_reader.py` — the ReadFile subagent.
- `searcher.py` — SearchContent and Glob.
- `vision.py` — the AnalyzeImage subagent.

## `router.py` — `SubagentRouter`

`delegate(tool_name, arguments)` intercepts the four heavy tools
(`SUBAGENT_TOOLS` in the orchestrator):

- `ReadFile` → `run_file_reader(...)` with args normalised
  (`start_line`/`end_line` coerced to ints).
- `SearchContent` → `run_searcher(...)`.
- `AnalyzeImage` → `run_vision(...)`.
- `Agent` → refused with a JSON error unless `settings.subagents_enabled`
  (default **off**; the schema is also hidden by `registry.get_schemas`).
  When on, `_run_agent(...)`: a **depth-1 nested loop** on an isolated
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
  the nested request body reuses those three fields so the standing
  cache units hit. The worker brief (JSON shape, allowed tools) moves
  into the first user item. `max_output_tokens` is omitted on that path
  — adding it busts the prefix. Execution still refuses any tool not in
  `AGENT_READONLY_TOOLS` / `AGENT_TASK_TOOLS`. Without a published
  prefix (a standalone router) the old isolated schemas are used.

  **Branch journal.** Each nested run writes an outline to
  `.g023/scratch/agent/{kind}-{hash}.json` (kind, objective, payload,
  per-item type/name/chars — not the full transcript). The parent JSON
  carries `branch` as that relative path. Depth stays 1: a second hop
  is another miss on a new input prefix and is not worth it unless the
  grandchild work is huge.

  `reasoning` is clamped by `clamp_ds_reasoning` onto the real wire levels
  `none` / `low` / `high` (`medium` folds into `high`, as DeepSeek does
  server-side; unset defaults to `high` for every kind). `max` / `xhigh` /
  76–100 cap to `high`: `max` often burns the entire output budget on
  thinking and returns empty JSON. Numeric strings map through
  `effort_to_wire`. Sent via `reasoning_param`.
- `get_router()` — module singleton.

## `file_reader.py` — ReadFile

One entry point, several modes; the output is always a JSON blob with a
`summary`, a `symbols` map (name → line span), and coverage notes.
Paths go through `paths.confine_or_error`.

- **Small file whole** (1.5.0): an unfocused read of a file of at most
  `SMALL_FILE_WHOLE_MAX_CHARS` (4,000) chars returns `mode: small_whole`, the
  content verbatim. A summary there was followed by a raw re-read in most runs.
  Every verbatim payload (range, raw, small whole, summary) carries
  `line_endings: crlf|mixed` when the file is not LF.
- **Local index** (`_local_metadata`, see docindex.md): unfocused ReadFile is summarised
  **with zero API calls** — AST for Python, regex/structure for JS/TS,
  Go, Rust, Java/C# (methods), C/C++ (function spans), Markdown, plain text (induced outline), JSON, YAML/TOML, HTML, shell. Over
  budget, symbols fold by prefix (`_fold_symbols`), never dropped silently.
- **Local focus** (`_local_focus`, default): with subagents off, a
  `focus` is resolved locally: exact phrase → outline section path → word BM25 over symbol spans
  (or 40-line windows when a file has no symbols), 3× boost for terms in the
  symbol name, stop-words dropped. Matches carry section paths; the best one verbatim if
  it fits. `summary_source` is `local_focus`. Zero API calls.
- **LLM path** (only with `subagents_enabled`): a semantic `focus` goes to the model
  (`LLM_INPUT_MAX_CHARS = 18_000` cap, `max_output_tokens` =
  `settings.file_summary_max_tokens`), with `_merge_metadata` joining
  local metadata onto the model summary. Model id is
  `config.MODEL` (`deepseek-flash`).
- **Ranges**: `start_line`/`end_line` → verbatim slice
  (`RANGE_MAX_CHARS = 20_000`) — the exact escalation path when a summary
  wasn't enough. Line numbers are ground truth from the file, not the
  model's guess.
- **Raw**: `raw=true` → full verbatim content (`FULL_READ_MAX_CHARS = 200_000`).
- **Cache**: summaries are keyed by content hash (+ focus) in
  `cache.get_cache()`; `invalidate_file` on change.
- Caps are enforced with explicit truncation flags so the orchestrator
  knows when it got a partial answer.
- **Sniff before read**: the file is stat'd and its first 8 KB
  sniffed (`shapes.sniff`) before any full read. **Binary** files return
  `mode: binary` with a structural digest (`shapes.describe_binary`: image
  dims, archive members, SQLite tables and row counts, executable headers,
  PDF info, entropy/strings/hexdump). raw and range requests return the same
  digest, never bytes. UTF-16/32 text is decoded with its BOM encoding and
  says so (`encoding`). Text over `HUGE_TEXT_BYTES` (64 MB) is streamed:
  ranges via `islice`, unranged reads as head + log templates.
- **Long lines**: every returned line is clipped at `shapes.LINE_CLIP_CHARS`
  (2,000) with a `...[line n continues: K more chars]` tail and
  `verbatim: false`. A focus phrase inside a long line returns a 1,200-char
  window around the hit (`_long_line_window`).
- **Data digests** (`_data_digest`, unfocused reads only): CSV/TSV column
  profiles with exact row counts, JSONL record shape and count, a full JSON
  shape with array lengths, and log/text line templates where frequent lines
  collapse, rare lines (errors) keep line numbers and counts are exact over
  the whole file. `INDEX_VERSION` (3) rebuilds older cached indexes.
- **Line counts**: `_line_count` counts lines the way an editor does: a trailing
  newline does not start a new line. `count("\n") + 1` over-counted by one,
  and a model subtracted the header from 200,002 and gave a wrong row count.

## `searcher.py` — SearchContent and Glob

Metadata-first grep over the project tree with **zero API calls**.
Backend, in order: `rg --json` if ripgrep is on PATH, else `grep -R`,
else a Python walk (`search_backend_name()`). Results report a **total
occurrence count** separately from a **sample list** so a sample cap is
not presented as a total cap. `.gitignore` is honoured (`build_ignorer`).
`.g023/` is in `IGNORE_DIRS` for a normal walk; an overlay still lets
SearchContent read `.g023/memory/` and `.g023/bbs/` (`OVERLAY_G023_DIRS`).

- `_walk_files` walks lazily, pruning `IGNORE_DIRS`.
- Hand-written globs are normalised (`_glob_patterns` — a bare
  extension becomes `*.ext`; comma-separated parts and brace expansion
  supported; `_matches_glob` tests each).
- Every backend collects up to `CANDIDATE_CAP` (2 000) hits, then
  `_pick_samples` chooses `max_matches` (default 12, capped at
  `MAX_SAMPLES_CAP` = 60). Rank: the live tree before archive dirs
  (`releases/`, `backup/`, …), code before prose, and definitions first (the
  match is in the defined name: `def q(`, `class Q`, `Q = …`). It then
  round-robins across files so 12 samples cover up to 12 files. Each sample
  has 1 line of context before and 2 after on every backend.
- Result JSON: query, path, `files_scanned`, `truncated`, a
  `metadata_summary`, `total`, `samples`, and `unshown_files` (`{file: hits}`
  for matching files that got no sample, up to 20). The history keeps the
  `tool_codec` form, not this JSON (see `docs/tool_codec.md`). SearchContent
  is never vaulted: the sample cap bounds it.
- **Zero hits → near-miss names** (`_add_near_misses`, `near_miss.py`): when
  `total` is 0 the result gains `did_you_mean: [{name, path, line}]` — up to 5
  names the project defines that are spelled like the query (`count_words` →
  `word_count`), bounded to 1 s / 24 MB. `G023_NEAR_MISS=0` disables. See
  `docs/near_miss.md`.
- `glob_sync` / `run_glob` back the Glob tool: `**` and brace expansion,
  paths relative to the project root, confined.
- **Literal intent lifts the ignore list** (`literal_allowances`): a directory
  named literally in the pattern or path (`node_modules/**/*.js`,
  `path="vendor"`) is searched even though it is ignored by default, and so is
  an extension named literally (`*.png`). `.g023` stays private. The payload
  lists what was lifted under `included_ignored`. Before this, such globs
  returned `total: 0`.
- **More than `GLOB_FILES_CAP` (200) hits**: `digest` = `shapes.listing_lines`
  over *all* hits (`frames/frame_{00000..03999}.png  x4000`), plus the first
  `GLOB_FILES_WHEN_DIGEST` (20) paths and a note.

## `vision.py` — AnalyzeImage

`run_vision(path_or_url, question)` answers one question about one image
with one isolated request to `config.MODEL`; only a compact JSON answer
returns to the orchestrator.

- `images.load_image_for_vision` reads a project-confined local path (the
  same `paths.resolve_in_project` rule as ReadFile) or an http(s) URL,
  rejects anything that is not jpeg/png/gif/webp, caps the longest side at
  `VISION_SEND_MAX_SIDE = 800` (re-encoding to PNG only when it resizes),
  and sniffs the real media type from the bytes.
- `DeepSeekClient.vision` sends the image block + question with thinking
  off; the codec turns the data URI into an Anthropic `image` block.
- Answers are cached by image hash + question + `deepseek:<MODEL>`; a
  repeat costs nothing. Token usage is recorded under the `subagent` scope.
- `settings.vision_enabled` false (`/vision off`) returns an error JSON
  without touching the network; the AnalyzeImage tool is also hidden.

## Related

- `docs/orchestrator.md` — the caller and `SUBAGENT_TOOLS`.
- `docs/cost_policy.md` — why children reuse the frozen prefix.
- `docs/cache.md` — summary/vision caches.
- `docs/paths.md` — root confinement.
- `docs/tools.md` — the lightweight tools these are paired with.

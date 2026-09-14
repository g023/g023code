# `tools/` — tool schemas & the lightweight-tool registry

Two files define the tool surface the model can call:

- `tools/schemas.py` (~537 lines) — the **definitions shown to the API**.
- `tools/registry.py` (~728 lines) — permission policy + the **executors**
  for the lightweight tools. Heavy tools are not executed here; they are
  routed to subagents by the orchestrator (see `subagents.md`).

## `schemas.py`

DeepSeek's Responses API wants **flat** tool objects (`name`,
`description`, `parameters` directly on the tool — not the nested
OpenAI style). Exports:

- `TOOL_SCHEMAS` — function tools: `ReadFile`, `SearchContent`,
  `AnalyzeImage`, `Bash`, `WriteFile`, `Edit`, `ListDir`, `Glob`,
  `TodoWrite`, `Agent`, `FetchUrl`, `LearnSearch`, `LearnGet`,
  `LearnPut`, `SessionSearch`.
  Each pins `additionalProperties: False` and required fields, so
  malformed calls fail fast and can be reported back to the model.
- `WEB_SEARCH_TOOL = {"type": "web_search"}` — DeepSeek's **server-side**
  web search, offered alongside our tools on every orchestrator call (no
  schema; the server runs its own agentic loop).

The descriptions encode hard-won constraints (e.g. ReadFile summaries are
not full content; WriteFile must be preceded by a `raw=true` read and
must never be reconstructed from a summary; Edit fails closed on a
non-unique span; LearnPut is not for secrets or permission changes).
**These descriptions are user-visible model instructions — treat them as
documentation.**

Agent `kind` is `research` | `planning` | `todos` | `task` (aliases
`explore`/`plan`). Optional `reasoning` (none|low|medium|high) is sent
on the DeepSeek nested loop after clamping. Nested schemas are taken
from this same list; they are not duplicated onto the parent.

There is no `LearnForget` tool — forget is `/learn forget`.

## `registry.py`

### Permission model

`ALLOW` / `ASK` / `BLOCK` (strings), with `default_policy()`:

- `SAFE_TOOLS` = ReadFile, SearchContent, ListDir, Glob, TodoWrite,
  LearnSearch, LearnGet, LearnPut, SessionSearch → ALLOW.
- `DEFAULT_ASK_TOOLS` = AnalyzeImage, Bash, WriteFile, Edit, Agent → ASK
  (the model must be granted each one interactively).
- `FetchUrl` → ASK via `confirm_fetch` (which also offers the cached
  copy first), unless `permission_default` is `block`.
- `settings.permission_default` (allow/ask/block) scales the whole
  policy; `/tools` edits per-tool levels through `set_permission`.

`check_permission(tool_name, args)` is async and may prompt the human
(rich `Confirm`/`Prompt`), so the answer can vary per invocation. Write
and Edit prompts show a unified diff (`ui.permission_diff`) and skip
dumping `content` / `old_string` / `new_string` as raw args. Ctrl-C,
Ctrl-D, and a closed/non-TTY stdin decline the call (they do not hang
or crash the turn). One-shot and piped runs therefore cannot complete
ASK tools unless `permission_default` is `allow` or `/tools` has already
granted them.

`get_schemas()` hides `AnalyzeImage` while vision is disabled so the
model never proposes a call it cannot fulfil (and the static prefix
stays stable for as long as the config does).

### Executors (`execute` → `_exec_*`)

- `_exec_bash` — run a shell command in the project root, return
  stdout/stderr/exit code via `prepare_bash_payload` (strip ANSI, keep
  pytest failures, cap at the source). A small dangerous-string filter
  (`rm -rf /`, `mkfs`, fork bomb, `dd if=/dev/zero`).
- `_exec_write_file` — full-file write with **backup first**. Paths
  outside the project root are refused. Identical bytes are a no-op
  (no backup slot burned). Writing `.g023/memory/MEMORY.md` goes through
  `memory_notes.write_index` (200-line cap + overflow).
- `_exec_edit` — unique-span replace (`apply_unique_edit`). Fails closed
  if `old_string` is empty, missing, or not unique unless `replace_all`.
  Backup first. Confined to the project root.
- `_exec_list_dir` — directory listing with sizes, skipping ignored dirs.
  Recursive is a shallow tree (max depth 2, 80 entries); non-recursive
  200 entries. Confined.
- `_exec_glob` — delegates to `searcher.run_glob`.
- `_exec_todo_write` — merge working-set leaves; `status=done` requires
  evidence (`done_requires_evidence`). The error includes a `hint` with
  an example `path@sha256:<hash>` from `files[]` so the model can cite
  a real pointer instead of guessing the format.
- `_exec_fetch_url` — delegates to `web_fetch.fetch` + cache
  (`cache.get_web`/`put_web`), so re-fetches are served from disk unless
  the user asks for fresh.
- `_exec_learn_search` / `_exec_learn_get` / `_exec_learn_put` —
  `learn.py`. Put is orchestrator-only inside `learn.put`.
- `_exec_session_search` — `session_log.search_records`.

`restore_newest_backup(path)` is the `/undo` implementation: copy the
newest `.g023/backups/` snapshot back onto the file.

### Backup location

`.g023/backups/` per project — the safety net WriteFile and Edit rely
on; `tests/test_write_backup.py` pins it. Layout mirrors the file tree
(one folder per file, timestamped copies inside), newest
`settings.write_backup_keep` kept (default 5). Skipped for `.g023/`
paths, new files, unchanged content, and when `G023_WRITE_BACKUP=0`.

## Related

- `docs/orchestrator.md` — `SUBAGENT_TOOLS` split; who calls `execute`.
- `docs/subagents.md` — the heavy-tool handlers.
- `docs/paths.md` — root confinement.
- `docs/working_set.md` — TodoWrite.
- `docs/learn.md` — Learn*.
- `docs/web_fetch.md` — the FetchUrl engine behind `_exec_fetch_url`.
- `docs/cli.md` — `/tools` permission screens, `/undo`.

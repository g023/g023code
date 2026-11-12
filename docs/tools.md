# `tools/` — tool schemas & the lightweight-tool registry

Two files define the tool surface the model can call:

- `tools/schemas.py` — the **definitions shown to the API**.
- `tools/registry.py` — permission policy + the **executors**
  for the lightweight tools. Heavy tools are not executed here; they are
  routed to subagents by the orchestrator (see `subagents.md`).

## `schemas.py`

Schemas are written in the Responses **flat** style (`name`,
`description`, `parameters` directly on the tool); on the Anthropic wire
`anthropic_codec.tools_to_anthropic` renames `parameters` → `input_schema`
`registry.get_schemas` hides `Agent` unless `subagents_enabled`.
`WebSearch` and `FetchUrl` go through `g023_code/web.py` (DeepSeek
Search/Fetch APIs, machine-wide cache; see `docs/web.md`). Exports:

- `TOOL_SCHEMAS` — function tools: `ReadFile`, `SearchContent`,
  `AnalyzeImage`, `Bash`, `WriteFile`, `Edit`, `ListDir`, `Glob`,
  `TodoWrite`, `Agent`, `WebSearch`, `FetchUrl`, `LearnSearch`, `LearnGet`,
  `LearnPut`, `SessionSearch`, `LocalAsk`.
  Each pins `additionalProperties: False` and required fields, so
  malformed calls fail fast and can be reported back to the model.
  With `finish_on_success` on, the orchestrator's frozen copy of Bash, Edit
  and WriteFile also has an optional `final_answer` (`docs/finish.md`);
  `registry.execute` drops that key before a tool runs.
- The server-side `web_search` tool is no longer offered (since 1.5.0): it was
  opaque, uncacheable, and read the prefix twice per call.

The descriptions encode hard-won constraints (e.g. ReadFile summaries are
not full content; WriteFile must be preceded by a `raw=true` read and
must never be reconstructed from a summary; Edit fails closed on a
non-unique span; LearnPut is not for secrets or permission changes).

1.5.0: Edit and WriteFile run on `edit_engine.py` (see `edit_engine.md`):
CRLF/BOM/mode preserved, atomic writes, a tolerant match ladder, `line=N`
disambiguation, and misses that return the occurrences or the closest region.

### Bash (1.5.0)
- Runs `bash -o pipefail -c` in its own process group (`start_new_session`), stdin
  `/dev/null`. A timeout SIGKILLs the whole group and returns the partial
  stdout/stderr with the error.
- When `python` is absent but `python3` exists, a shim directory (machine-wide cache
  `shims/python -> python3`) is prepended to PATH.
- Exit 127 adds `hint`, naming an installed alternative (`python3`, `pip3`,
  `python3 -m pytest`, ...) when one exists.
**These descriptions are user-visible model instructions — treat them as
documentation.**

Agent `kind` is `research` | `planning` | `todos` | `task` (aliases
`explore`/`plan`). Optional `reasoning` (none|low|high) is sent
on the DeepSeek nested loop after clamping. Nested schemas are taken
from this same list; they are not duplicated onto the parent.

There is no `LearnForget` tool — forget is `/learn forget`.

## `registry.py`

### Permission model

`ALLOW` / `ASK` / `BLOCK` (strings), with `default_policy()`:

- `SAFE_TOOLS` = ReadFile, SearchContent, ListDir, Glob, TodoWrite,
  LearnSearch, LearnGet, LearnPut, SessionSearch, WebSearch → ALLOW.
- `DEFAULT_ASK_TOOLS` = AnalyzeImage, Bash, WriteFile, Edit, Agent, FetchUrl
  → `permission_default` (ASK by default). FetchUrl's prompt is
  `confirm_fetch`, which offers the cached copy first.
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
stays stable for as long as the config does). `LocalAsk` is offered only
while `local_model` is on; it is a SAFE (read-only) tool run by
`local_ask.run` (`docs/local_model.md`); without `paths` it searches the
whole project through the scout index (`docs/scout.md`).

### Executors (`execute` → `_exec_*`)

- `_exec_bash` — run a shell command in the project root, return
  stdout/stderr/exit code via `prepare_bash_payload` (strip ANSI, keep
  pytest failures, cap at the source). A small dangerous-string filter
  (`rm -rf /`, `mkfs`, fork bomb, `dd if=/dev/zero`). Before it runs,
  `local_llm.note_command` checks for GPU work; after it, `keep_full_output`
  keeps the raw streams in a `.g023/vault/` file when the cap dropped text
  (`docs/salience.md`).
- `_exec_write_file` — full-file write with **backup first**. Paths
  outside the project root are refused. Identical bytes are a no-op
  (no backup slot burned). Writing `.g023/memory/MEMORY.md` goes through
  `memory_notes.write_index` (200-line cap + overflow). Backups skip
  `.g023/` except `.g023/bbs/` (bulletin-board threads).
- `_exec_edit` — unique-span replace (`edit_engine.apply_edit`, a ladder of
  exact → normalised → affine-indent matching; `docs/edit_engine.md`). Fails closed
  if `old_string` is empty, missing, or not unique unless `replace_all` or `line=N`.
  Backup first. Confined to the project root.
- `_exec_list_dir` — directory listing with sizes, skipping ignored dirs. When the
  walk is truncated, or there are more than `DIGEST_MIN_ENTRIES` (40) entries and
  the digest is at least 2x more compact, it returns `dirmap.digest` (templated
  rows that account for every child; see `dirmap.md`) instead of the first N names.
  Recursive is a shallow tree (max depth 2, 80 entries); non-recursive
  200 entries. Confined.
- `_exec_glob` — delegates to `searcher.run_glob`.
- `_exec_todo_write` — merge working-set leaves; `status=done` requires
  evidence (`done_requires_evidence`). The error includes a `hint` with
  an example `path@sha256:<hash>` from `files[]` so the model can cite
  a real pointer instead of guessing the format.
- `_exec_fetch_url` / `_exec_web_search` — `web.fetch` + `web.render_page`
  and `web.search` + `web.render_search`, in a worker thread; errors come
  back as one-line JSON `{"error": …}`.
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
- `docs/web.md` — the web layer behind `_exec_web_search` / `_exec_fetch_url`.
- `docs/cli.md` — `/tools` permission screens, `/undo`.

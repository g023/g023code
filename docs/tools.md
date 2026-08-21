# `tools/` — tool schemas & the lightweight-tool registry

Two files define the tool surface the model can call:

- `tools/schemas.py` — the **definitions shown to the API**.
- `tools/registry.py` — permission policy + the **executors** for the
  lightweight tools. Heavy tools are not executed here; they are routed to
  subagents by the orchestrator (see `subagents.md`).

## `schemas.py`

DeepSeek's Responses API wants **flat** tool objects (`name`,
`description`, `parameters` directly on the tool — not the nested
OpenAI style). Exports:

- `TOOL_SCHEMAS` — eight function tools: `ReadFile`, `SearchContent`,
  `AnalyzeImage`, `Bash`, `WriteFile`, `ListDir`, `Agent`, `FetchUrl`.
  Each pins `additionalProperties: False` and required fields, so
  malformed calls fail fast and can be reported back to the model.
- `WEB_SEARCH_TOOL = {"type": "web_search"}` — DeepSeek's **server-side**
  web search, offered alongside our tools on every orchestrator call (no
  schema; the server runs its own agentic loop).

The descriptions encode hard-won constraints (e.g. ReadFile summaries are
not full content; WriteFile must be preceded by a `raw=true` read and
must never be reconstructed from a summary). **These descriptions are
user-visible model instructions — treat them as documentation.**

## `registry.py`

### Permission model

`ALLOW` / `ASK` / `BLOCK` (strings), with `default_policy()`:

- `SAFE_TOOLS = (ReadFile, SearchContent, ListDir)` → ALLOW.
- `DEFAULT_ASK_TOOLS = (AnalyzeImage, Bash, WriteFile, Agent)` → ASK
  (the model must be granted each one interactively).
- `FetchUrl` → ASK via `confirm_fetch` (which also offers the cached
  copy first).
- `settings.permission_default` (allow/ask/block) scales the whole
  policy; `/tools` edits per-tool levels through `set_permission`.

`check_permission(tool_name, args)` is async and may prompt the human
(rich `Confirm`/`Prompt`), so the answer can vary per invocation.

### Executors (`execute` → `_exec_*`)

- `_exec_bash` — run a shell command (blocked outside the project root by
  default), return stdout/stderr/exit code, truncated.
- `_exec_write_file` — full-file write with **backup first**:
  `backup_before_write(path, root)` snapshots the previous version to
  `.g023/backups/` (mirroring the tree, pruned to the newest few per
  file — retention `settings.write_backup_keep`; skipped for files inside
  `.g023/`, brand-new files, and unchanged content).
- `_exec_list_dir` — directory listing with sizes, skipping ignored dirs.
- `_exec_fetch_url` — delegates to `web_fetch.fetch` + cache
  (`cache.get_web`/`put_web`), so re-fetches are served from disk unless
  the user asks for fresh.

`get_schemas()` — the current schema list the orchestrator advertises.

### Backup location

`.g023/backups/` per project — the safety net the WriteFile flow relies
on; `tests/test_write_backup.py` pins it.

## Related

- `docs/orchestrator.md` — `SUBAGENT_TOOLS` split; who calls `execute`.
- `docs/subagents.md` — the heavy-tool handlers.
- `docs/web_fetch.md` — the FetchUrl engine behind `_exec_fetch_url`.
- `docs/cli.md` — `/tools` permission screens.

# `project_instructions.py` — g023.md / g023_compact.md / AGENTS.md

`g023_code/project_instructions.py` is this program's
AGENTS.md: optional standing instructions in the project root, appended
to the orchestrator's system prompt.

## Files

Looked up in `get_project_root()` (the launch cwd / `G023_PROJECT_ROOT`).
None is required. Unreadable or empty files are treated as absent.

| File | Role |
|------|------|
| `g023.md` | Full standing instructions. Used when no compact file exists. |
| `g023_compact.md` | Compacted copy. **Wins** whenever it is present. |
| `AGENTS.md` | Extra addendum, appended **in addition** when readable. |

`active_file()` returns the g023 path that would be used (`g023_compact.md`
else `g023.md`, or None). `system_addendum()` returns the text appended
to `_system`, each file headed `Project instructions (<filename>)`, or
`""`. `agents_addendum()` is the AGENTS.md half.

The addendum is frozen for the life of an Orchestrator (`_frozen_addendum`)
so a mid-session edit of these files does not bust the prefix cache until
the next process.

## Compaction (`/g023-compact`)

`compact(client, overwrite=False)` sends `g023.md` to `deepseek-flash`
with `reasoning={"effort": "none"}` and no tools, writes the reply to
`g023_compact.md`, and leaves `g023.md` untouched.

- Missing or empty `g023.md` → no API call.
- Existing compact file and `overwrite=False` → no API call.
- Empty model reply → original left, compact not written.
- A wrapping ` ```markdown ` fence around the whole reply is stripped;
  fences *inside* the file are kept.

The CLI handler (`cmd_g023_compact`) confirms before overwriting an
existing compact file, then calls `compact(..., overwrite=True)`. Cost is
recorded on the usage tracker as a subagent call.

Delete `g023_compact.md` to fall back to the full file on the next
process (`_system` re-reads each request, but the frozen addendum lasts
for the process).

## Related

- `docs/orchestrator.md` — `_system` appends the addendum.
- `docs/commands.md` — `/g023-compact` catalogue entry.
- `docs/cli.md` — the handler and the `/status` line.

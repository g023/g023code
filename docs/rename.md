# `rename.py` — Rename tool

One call renames an identifier across the project, so a rename does not need a
grep → sed → grep loop of Bash calls.

## Switch

`settings.rename_tool` (saved key, on by default), env `G023_RENAME_TOOL=0`, or
`/settings set rename_tool on|off`. When on, `Orchestrator._frozen_tools` adds
the `Rename` schema right after `Edit` (part of `_tools_key`, so a toggle is
one cache miss). Rename is in `DEFAULT_ASK_TOOLS`: it asks before it runs,
showing the files it will change, unless the permission default is `allow`.

## Arguments and result

Rename arguments: `old`, `new`, `path` (default `"."`), `include` (default `""`).

- `old`, `new`: identifiers (`[A-Za-z_][A-Za-z0-9_]*`); `new` must not be a
  Python keyword or equal `old`.
- `path`: a file or directory inside the project (confined like every path).
- `include`: optional glob matched against the file name or the
  project-relative path (`*.py`, `tests/*`).

Matches are whole identifiers only: `old` not preceded or followed by
`[A-Za-z0-9_]`, so `cart_total_eur` does not touch `cart_total_euro`. Every
text file under `path` is searched with the closure receipt's walker (ignore
rules, `.g023/`, binary, data and oversized files skipped). More than 200
files to change is refused (narrow `path` or `include`).

Result: `{ok, old, new, files: {rel: count}, total, refs_left: 0}` plus, when
they apply, `new_existed_in` (files that already used the new name — a
possible clash), `backups` (files backed up to `.g023/backups/`) and
`syntax_error` (`{rel: problem}` for a .py/.json file that stopped parsing).
Nothing to rename returns `ok, unchanged, total: 0` with a note.

All files or none: each file is read strictly as UTF-8 (BOM and line endings
kept, as `edit_engine` does), backed up, and written; if any write fails the
files already written get their original bytes back and the result is an
error.

## Orchestrator

Rename is a multi-file write without being in `WRITE_TOOLS` (those key on one
`path`). Before it runs, `planned_files()` lists the files it will change and
each is captured as a pre-image (failure attribution). After it lands, `_note_rename` records each file in the
working set and refreshes the model's view of files it had already seen
(`views.py`), and `_after_batch` counts it as a write (`_write_seq`, verify
replay). It carries no `final_answer`: the test run after it is the step that
finishes.

Tests: `tests/test_rename.py`.

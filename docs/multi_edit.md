# `multi_edit.py` — multi-hunk Edit

`Edit` takes an optional `more_edits` list: further replacements applied after
the first, all or nothing. An item may carry its own `path`, so one call can
change a constant in code and the line in the README that documents it. A
change that needs several separate spots (a constant, an import, a function,
the docs) is one call instead of one Edit per response.

## Switch

`settings.multi_edit` (saved key, on by default), env `G023_MULTI_EDIT=0`, or
`/settings set multi_edit on|off`. When on, `add_to_schemas` adds the
`more_edits` property to the Edit schema (a deep copy; the registry's schemas
are never mutated). The flag is part of `_tools_key`, so a toggle is one cache
miss. Off, the Edit schema is byte-identical to a build without this module.

## Mechanism

- Each item is `{old_string, new_string, replace_all?, path?}`; an item
  without `path` targets the Edit's own `path`.
- `multi_edit.groups` buckets the hunks per file, in first-seen order;
  `multi_edit.paths(args)` lists every file the call writes and
  `multi_edit.hunks(args)` returns `(path, old, new)` triples (path `""` =
  the Edit's own file) for the permission prompt.
- `edit_engine.apply_edits(text, hunks, symbols_for)` applies the first hunk
  (from Edit's own arguments, including `line`) and then each item, each one
  against the text the earlier ones produced, with the same matching rules as a
  single Edit (exact, then tolerant line endings / trailing spaces /
  indentation). The first hunk that fails stops the call with
  `edit i of n: <reason>` and nothing is written.
- The result's `lines` lists every hunk's span in the final text
  (`"3-4, 12-20"`) and `edits` gives the count. One backup, one syntax check,
  one closure receipt over the joined hunks.
- `ui.permission_diff` shows one diff per hunk at the ASK prompt, naming the
  file of each hunk that targets another file.

## Several files

When the hunks name more than one file, `ToolRegistry._exec_multi_file_edit`
handles the call: every path is confined to the project, paths that resolve
to the same file (`a.py`, `./a.py`) are merged, and each file's hunks are
applied in memory first. Any failure (path outside the project, missing file,
non-UTF-8 file, a hunk that does not match) returns an error naming the file
and `nothing written`. Then the files are written; if a write raises, the
files already written get their original bytes back (`no file changed`).
The result adds `files: {rel: spans}`, `backups` (count) and, if a .py/.json
file stopped parsing, `syntax_error: {rel: problem}`.

The orchestrator captures a pre-image (failure attribution) for every file in `multi_edit.paths(args)` before the call, records each file
in `result["files"]` as written this turn (working set, claimed-write guard),
and refreshes the model's view of each.

Tests: `tests/test_multi_edit.py`.

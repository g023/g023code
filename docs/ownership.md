# `ownership.py` — whose code the model may tidy

The setting `ownership` (`/ownership`, saved in config.json) controls one rule.
Under that rule, the model merges repeated logic in code it touches, fixes defects
it notices there, and names each change in its answer.

| mode | where the rule applies | standing prompt | tool results |
|---|---|---|---|
| `auto` (default) | files g023 created in this project | `AUTO_ADDENDUM` | `"yours":true` on those files |
| `user` | nowhere — minimal change everywhere | unchanged (byte-identical to before the setting) | unmarked |
| `agent` | the whole project | `AGENT_ADDENDUM` | unmarked |

## Provenance

`record_created(path)` is called by `ToolRegistry._exec_write_file` only when
the path did not exist before the write. It writes the project-relative path to
`.g023/provenance.json` (`{"created": {path: {"by": "WriteFile"}}}`), with the
write guarded by `fsutil.file_lock` and done through `atomic_write_text`. The
list is capped at 5,000 paths, and the oldest are dropped first. Some writes are
not recorded:

- overwriting an existing file (it was the user's before);
- files created by Bash (`cat > x`, generators, `git checkout`) — the program
  cannot prove who wrote them;
- paths under `.g023/`.

`forget(path)` (`/ownership forget <path>`) hands a file back to the user.
`created(path)` and `listing()` read the store. None of these raise.

## Marking

`Orchestrator._invoke_tool` passes every result through
`mark_result(tool, args, result)`. In `auto` mode only, a ReadFile, Edit or
WriteFile result gets `"yours": true` added. The result must be a JSON object
without `error`, and `args["path"]` must be a recorded file. The codec writes
compact JSON, so the model sees `"yours":true`. That is the exact string the
prompt names.

## Prompt and cache

`Orchestrator._system` appends `system_addendum()` after the subagent clause.
`ownership.mode()` is part of `_instructions_key`, so a mode change rebuilds the
frozen instructions once. Within one mode the prefix is stable. The marks live
in tool results, which are part of history, so the prefix does not change when a
new file is created.

## Why a directive, not "this codebase is yours"

A Claude prompting guide reports that one identity sentence makes Claude merge
repeated code and fix nearby problems. This was measured live on deepseek-flash.
Fixture: `shopkit/pricing.py`, with USD/EUR totals as duplicated loops and
an average function that raises on an empty cart despite its docstring. Task: add a GBP total. Six
runs per arm; every run solved the task and kept existing behaviour.

| arm | merged duplicate | fixed nearby bug | mean output tokens | cost / 6 runs |
|---|---|---|---|---|
| shipped prompt | 0/6 | 0/6 | 1,728 | $0.0092 |
| + identity sentence | 0/6 | 0/6 | 1,721 | $0.0104 |
| identity sentence replacing "Respect the user's codebase" | 0/6 | 1/6 | 1,664 | $0.0109 |
| + minimal-change sentence | 0/6 | 0/6 | 1,788 | $0.0103 |
| + explicit directive | 6/6 | 3/6 | 2,581 | $0.0140 |
| shipped `auto`, `pricing.py` created by g023 (`"yours":true`) | 6/6 | 3/6 | 2,574 | $0.0135 |
| shipped `auto`, `pricing.py` the user's (no mark) | 0/6 | 0/6 | 1,935 | $0.0114 |

The identity framing does not transfer to this model, but the directive does.
The user owns their code, so `auto` scopes the directive by provenance. In the
last two rows, the same file got the full directive effect when the mark was on
it, and no effect when the mark was absent. On user files, `auto` used about 12%
more output tokens than the shipped prompt (1,935 vs 1,728). That is within this
bench's run-to-run spread (n=6; single runs ranged from $0.0010 to $0.0028), but
`/ownership user` removes the addendum completely.
The bench and its raw rows are kept with the development research notes.

Tests: `tests/test_ownership.py`.

# `working_set.py` — prefix-stable remaining work

`g023_code/working_set.py` (~320 lines) is the session's live objective
object: remaining work, file pointers, and the last verify command. It is
small, merge-by-id, and serialized with a **fixed key order** so an
unchanged prefix stays byte-stable for DeepSeek's prompt cache.

The live copy lives in `cache.session_facts` under the key `working_set`.
An existing history item is **never rewritten in place** — rewriting
bytes after the sticky goal is a cache miss from that slot onward
(DeepSeek Example 2: A+B then A+C). Compact may place a fresh snapshot
after the sticky goal as a *new* prefix.

## Shape

```json
{
  "goal": "",
  "constraints": [],
  "open": [{"id": "...", "title": "...", "status": "pending", "evidence": []}],
  "files": [{"path": "rel/path", "hash": "...", "last_range": null}],
  "verify": {"command": "", "exit_code": null, "excerpt": "", "at": ""}
}
```

Caps: 8 open (non-done/cancelled) leaves, 32 file pointers (newest kept).
Statuses: `pending` | `in_progress` | `done` | `cancelled`.

The history mirror is a user-role item whose content starts with
`Working set:\n` plus the stable JSON (`WORKING_SET_LEADIN`).
`is_working_set_item` / `find_working_set_item` recognise it.
`mirror_working_set` saves session_facts and **appends** the item only
when none exists yet.

## Updates

| Function | Trigger |
|----------|---------|
| `update_todos` / `TodoWrite` | Merge leaves by id; optional live goal/constraints. `done` requires evidence. |
| `note_file` | After a WriteFile/Edit (relative path + content hash). |
| `note_verify` | After a Bash command that looks like a test runner. |
| `set_goal` | `/goal` seeds the goal string. |
| `reset` | `/clear` — empty object. |

`looks_like_test_command` matches pytest / unittest / cargo test / go
test / npm test / jest / make test / ctest / mvn / gradle, plus
`node --check`, `node …test…` (so `node test/headless.js` records
verify), and `python3 …test….py`.

`evidence_satisfies_done` accepts a working-set file path, the canonical
`path@sha256:<digest>` form, `path <hash>` (what models actually emit),
and a truncated/mismatched digest as long as the path is already in
`files[]`. Unknown paths still fail. TodoWrite's `done_requires_evidence`
error includes a `hint` with an example citation and the known files.

## Goal judge

`judge_goal_completion()` is the **only** completed/not-completed
verdict. A final assistant message is not success.

- Pending open leaves → not completed.
- Done leaves without evidence (file pointer `@sha256:` / path, or a
  passing verify command) → not completed.
- Empty leaf list → not completed.
- No open leaves, every done leaf cites file/verify evidence → completed.

That verdict is what `_should_post_turn_compact` uses when it decides an
objective is finished and writes `.g023/logs/`.

`assert_evidence` is a citation-lock helper: the path is in `files[]`
and `file_cache` still has that hash.

## Related

- `docs/orchestrator.md` — mirroring waits until a parallel tool batch
  is fully recorded; reseating if a working-set item splits a pair.
- `docs/session_log.md` — compact boundaries and objective logs carry
  the working set.
- `docs/tools.md` — `TodoWrite`.
- `tests/test_goal_judge.py`, `tests/test_history_integrity.py`.

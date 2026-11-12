# `session_log.py` — JSONL ancestry and objective handoffs

`g023_code/session_log.py` writes two on-disk records of a
session. Compact records a *view*; the JSONL keeps ancestry. Large write
bodies are stored as hash + byte count, not 200k-character contents.

## `.g023/sessions/{stamp}.jsonl`

Append-only. `SessionLog.open_new()` names the file
`YYYYMMDD-HHMMSS.jsonl` (UTC) and writes `CURRENT` pointing at it.

Record kinds:

| kind | Contents |
|------|----------|
| `user` | Sanitized user item |
| `output` | Sanitized output items |
| `tool_result` | `call_id` + sanitized output |
| `compact_boundary` | The compacted `view` plus working set |
| `working_set` | The object itself |
| `interrupt` | Items at Ctrl-C |
| `view` | Whole history after a non-append rewrite (rollback, repair) |
| `prefix_fingerprint` | sha256 of `instructions` and of the tools list |

`sanitize_item` omits only **re-derivable** bodies: WriteFile/Edit
`content` / large `old_string` / `new_string` (the file holds them) and large
tool outputs (`LARGE_LOG_CHARS = 4096`; re-run the tool). Message `content`
— user prompts, the compaction summary, the mid-turn restatement, assistant
text — is never elided: it has no other copy, and eliding it made
`/resume` restore a hash stub instead of the summary.

Torn tail: attaching to a log whose last byte is not `\n` (a
crash mid-append) writes the newline first, so the next record is not glued
onto the torn line and lost with it.

`replay_log` rebuilds the latest view: compact_boundary / interrupt / view
**replace** items rather than appending. Invariant: replay ==
memory. `SessionLog` keeps a shadow built with the same reducer (`_reduce`);
`checkpoint(items)` at the end of every turn appends an `output` tail or a
`view` record if memory diverged, and nothing otherwise. Each record is one
O_APPEND write; one live writer per file (resuming the same session from two
REPLs interleaves them). `load_latest_view` uses `CURRENT`, else the
newest jsonl. `/resume` (bare) loads that view and repairs pairing.

`search_records(query)` greps the JSONL without reconstructing omitted
bodies — the `SessionSearch` tool. Hits include the hash when a body was
omitted.

`prefix_hashes` / `load_prefix_fingerprint` let `/resume` warn when the
standing instructions or tool list no longer match the log (a prefix
cache miss from token 0).

## `.g023/logs/{YYYYMMDD_HHMMSS}.log`

Written by `_post_turn_compact` when a replace succeeds. This is the
handoff for a **new** orchestration: standing `instructions` stay in
the system prompt; this file is the next `input`.

```
G023_OBJECTIVE_LOG 1
ts: ...
completed: true|false|unknown
goal: ...
key: <key id>              (absent when no key could be created)
sig: hmac-sha256:<hex>

--- working_set ---
{...}

--- summary ---
...
```

`CURRENT` in the same folder points at the latest. `--continue` / `-c`
and `/resume log` seed from it (`seed_from_objective_log`). A file
without the header is treated as the summary body so a hand-edited log
still loads. `parse_objective_log` returns `provenance` (`verified` /
`modified` / `foreign` / `unsigned`; signing and the key are in `checkpoint.md`);
a non-verified log still loads, with a `[rehydrate]` caveat for the model.

## Related

- `docs/orchestrator.md` — who writes both records.
- `docs/working_set.md` — the object carried in both.
- `docs/cli.md` — `/resume`, `--continue`.
- `docs/learn.md` — SessionSearch is a tool, not a learn-store op.
- `tests/test_session_log.py`.

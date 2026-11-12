# `memory_notes.py` — project notebook under `.g023/memory/`

`g023_code/memory_notes.py` is a small file helper for a
project notebook that is **never injected into standing instructions**.
The orchestrator prompt tells the model that facts live in
`.g023/memory/` and that `SearchContent` can read that directory.

## Files

| Path | Role |
|------|------|
| `.g023/memory/MEMORY.md` | Index. Cap `INDEX_CAP = 200` lines. |
| `.g023/memory/topics/overflow.md` | Spill when the index is over the cap. |

`write_index(text)` is what WriteFile uses when the target **is**
`MEMORY.md` (`is_memory_index_path`). Lines past 200 go to
`topics/overflow.md`; the index keeps the first 200.

`status_line()` feeds `/status` (`none` / `MEMORY.md N line(s)` /
`unreadable`).

This is not the learn store (cross-project procedures), not the
working set (this-session remaining work), and not the bulletin board
(forum / message passing at `.g023/bbs/`).

## Related

- `docs/tools.md` — WriteFile special-cases the index path.
- `docs/learn.md` — the other, curated, store.
- `docs/working_set.md` — session remaining work.
- `docs/subagents.md` — SearchContent can overlay `.g023/memory/` and `.g023/bbs/`.
- `docs/bbs.md` — the forum, not the notebook.

`write_index` replaces `MEMORY.md` atomically; when it spills past the cap,
`topics/overflow.md` is written first so a crash never drops lines.

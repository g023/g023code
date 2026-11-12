# `bbs.py` — project bulletin board under `.g023/bbs/`

`g023_code/bbs.py` is the forum for this session and later ones.
Topic slugs are subdirectories; conversations are `.txt` files with
slug filenames. Subagents and the orchestrator write there to pass
insights both ways. **Post bodies are never injected into standing
instructions** — the same retrieve-don't-prepend rule as memory notes
and the learn store.

## Files

| Path | Role |
|------|------|
| `.g023/bbs/GUIDE.md` | How to use the board. Seeded if missing; never overwritten. |
| `.g023/bbs/tools_info.md` | What is in `bbs_tools/` and how to run it. |
| `.g023/bbs/INDEX.md` | Generated catalog. Cap 200 lines; spill to `index-overflow.txt`. |
| `.g023/bbs/bbs_tools/bbs` | Thin wrapper around `python -m g023_code.bbs`. |
| `.g023/bbs/<topic>/<slug>.txt` | One conversation thread. |

`ensure_layout()` creates the tree and seeds GUIDE / tools_info / the
wrapper if they are absent. The orchestrator calls it on init so a
fresh project has a board before the first turn.

`post(topic, slug, body)` validates slugs (`[a-z][a-z0-9-]{0,62}`),
appends by default, and rebuilds `INDEX.md`. Reserved topic: `bbs_tools`.

`status_line()` feeds `/status` (`none` / `empty` / `N topic(s), M post(s)`
/ `unreadable`).

## Search overlay

`.g023/` is in `IGNORE_DIRS`. SearchContent still reads `.g023/bbs/`
(and `.g023/memory/`) via the same overlay that skips `.g023/cache.db`.
See `docs/subagents.md`.

## Slash command

`/bbs` — status, `list`, `search`, `read`, `post`, `index`. Posts are
not a native function tool: that would change the frozen tools prefix.
Agents use SearchContent / ReadFile / WriteFile / Bash, or
`python -m g023_code.bbs`.

## Backups

WriteFile of a path under `.g023/bbs/` **does** snapshot
`.g023/backups/`. Other `.g023/` paths still skip backup.

## Not the other stores

| Store | Job |
|-------|-----|
| Working set | This session, remaining work |
| Learn | Cross-session procedures, retrieved |
| Memory notes | Project facts, searched |
| BBS | Forum / message passing / rejected approaches |

## Related

- `docs/memory_notes.md` — facts, not discussions
- `docs/learn.md` — procedures
- `docs/subagents.md` — SearchContent overlay
- `docs/orchestrator.md` — standing prompt names the path

## Concurrency and retries

`post` holds `fsutil.file_lock(thread)` while it decides between create and
append, so two writers cannot both "create" a thread. An append whose body
equals the thread's current last body is skipped and reported as
`"duplicate": true` (an idempotent retry after an interrupted call).
`INDEX.md` and `index-overflow.txt` are replaced atomically, overflow first.

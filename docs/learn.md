# `learn.py` — cross-session procedural guides

`g023_code/learn.py` is a file-backed store of reusable
procedures, pitfalls, routing notes, and facts. Guides travel across
sessions and (for `env`/`user` scope) across projects. They are **not**
injected into `Orchestrator._system` — the model must `LearnSearch` /
`LearnGet` them. `/cache clear` does not delete them (they are not
SQLite).

Disable the whole store with `G023_LEARN=0` (`false`/`off` also work).

## Layout

| Root | Scope | Path |
|------|-------|------|
| Home | `env` and `user` | `~/.g023/learn/` |
| Project | `project` | `<project>/.g023/learn/` |

Each root holds:

- `INDEX.md` — routing lines only (`# learn index — do not edit the body
  of guides here`). Cap `INDEX_CAP = 80` entries.
- `guides/{id}.md` — YAML front matter + body. Cap `GUIDE_FILE_CAP = 40`
  files per root. Body cap 80 lines / 4000 chars.
- `ledger.jsonl` — append-only put / forget / outcome / reject records.

Index line shape:

```
- [env] chrome-devtools-snapshot — Chrome snapshot. When: browser, snapshot
```

Ids are `^[a-z0-9-]{2,64}$`. Kinds: `procedure`, `pitfall`, `routing`,
`fact`. Scopes: `project`, `env`, `user`.

A project id shadows a home id of the same name. Search skips
`stale` and `tombstone` guides. Optional `requires:` probe commands
(`subprocess`, 5 s timeout) skip a guide when any probe fails.

## Tools vs slash command

| Surface | What it does |
|---------|----------------|
| `LearnSearch` | Routing hits only (id, title, when-tags). Cap 5. |
| `LearnGet` | One guide body. Cap 2 per turn (`TURN_GET_CAP`). |
| `LearnPut` | Orchestrator-only write. Cap 3 per session unless `source` is `user` / `user:explicit`. |
| `/learn` | Status, search, get, save (user-explicit, bypasses the session cap), forget, outcome. |

There is no `LearnForget` tool. Forget is `/learn forget <id>` — it
tombstones the file, removes the index line, and the id cannot be reused.

`/learn outcome <id> helped|harmed` increments counters; two+ `harmed`
with `harmful > helpful` marks the guide `stale` and drops it from the
index.

## Write guards

`put` rejects (and ledgers) rather than writing:

- missing required fields, invalid id/kind/scope
- `orchestrator_only` when `source` is `subagent` / `agent`
- `session_write_cap`, `body_too_large`, `index_full`, `guide_cap`
- `secret_reject` (API keys, Bearer tokens, PEM private keys)
- `auth_launder` (permission-change language in title/body)
- `untrusted_evidence` (`web:` / `fetch:` / `http(s):` unless the source
  is the user)
- `duplicate_when` (same scope + same when-tag set)
- `forgotten` (id is a tombstone)

Evidence must be a local tool success or `user:explicit`, not a URL.

## Catalog nudge

`catalog_nudge(tool_name)` returns a one-line hint when a tool error
matches a when-tag, so the orchestrator can `LearnGet` only if the error
matches. Prefixed `[learn] LearnSearch query=`.

## Related

- `docs/tools.md` — LearnSearch / LearnGet / LearnPut executors.
- `docs/cli.md` — `/learn` handler.
- `docs/orchestrator.md` — standing prompt tells the model to search;
  nothing from this store is concatenated into `instructions`.
- `tests/test_learn.py` — round-trip, cross-project env visibility,
  prefix pin, secret reject, root confine.

## Concurrency

Index upsert/remove run under `fsutil.file_lock(INDEX)` and replace the file
atomically. `outcome` re-reads the guide under the guide's lock, so concurrent
votes all count. There is no per-session vote dedupe by design (the repeat
detector already blocks same-turn duplicates).

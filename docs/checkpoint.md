# `checkpoint.py` — bounded carried state, signed handoffs, rehydration

`g023_code/checkpoint.py` owns what survives a post-turn compaction and what a later
process may trust when it resumes. It has three parts: **preserve** (deterministic, already
in the working set), **compress** (a bounded fold), and **resume** (verify, then reconcile with disk).

## Fold — `fold_summary(existing, exchanges)`

The post-turn *local fold* (no model call) runs when a compact-summary item already exists
and the finished turn did not produce a `[handoff]`. The carried summary is laid out by
volatility, so consecutive states share the longest byte-identical prefix (DeepSeek bills a
cached prefix at 1/50 of a miss):

```
<narrative>                                   rewritten only by Flash or a [handoff]
## Earlier requests (one line each, oldest first)   ≤ 12 lines × 160 chars, append at end
## Recent turns (newest last)                 ring of the last 3 exchanges
- [user] …                                    ≤ 600 chars, whitespace collapsed
  [assistant] …                               ≤ 900 chars
[recall] …                                    added by with_recall_pointer
```

A fold keeps the narrative bytes, pushes this turn's exchange into the ring, and moves the
ring's oldest request to a one-line digest. Every section has a cap (narrative 8,000 chars,
middle-elided), so repeated folds reach a fixed size. Before any summary exists the whole
conversation is folded the same way. `split_state` / `render_state` parse and print the layout;
text without the headings is all narrative, so Flash and hand-written summaries parse too.

Why not "keep the last N turns verbatim": a sliding window drops its head every turn, so
every retained token becomes a new prefix and re-bills at the miss rate. In a token model of a
40-turn project it cost more than keeping the whole history. An append-only N..2N ring
(fold N at a time) saved under 1% and is not used.

## Signature — `sign(fields)` / `verify(fields, key_id, sig)`

`session_log.write_objective_log` signs the fields a resume reads back (`ts`, `completed`,
`goal`, working-set JSON, summary) with HMAC-SHA256 and writes `key:` / `sig:` header lines.
The key is 32 random bytes in `<global cache dir>/handoff.key` (mode 0600, created on first
use; never inside a project or a release). `parse_objective_log` returns `provenance`:

| value | meaning |
|---|---|
| `verified` | signed by this install, bytes unchanged |
| `modified` | signed by this install, edited since |
| `foreign` | signed by another install (copied, or committed into a repo) |
| `unsigned` | no signature (hand-written, or written by an older g023) |

Every state still loads; only `verified` is presented without a caveat. The signature proves
where the bytes came from. It does not protect against code running as the same user, which
can read the key.

## Rehydration — `reconcile(ws)` / `rehydrate_note(drift, provenance)`

On `/resume log` / `--continue` (`seed_from_objective_log`) and bare `/resume`
(`resume_from_latest`), the working set's `files[]` sha256 values are recomputed:

- `changed` / `missing` — files that differ from, or no longer exist since, the saved state;
- `verify_stale` — a recorded passing verify that predates those changes.

When there is anything to say (non-verified provenance or drift), one user-role note is
appended: `[rehydrate] Checked the saved state against the project before continuing: …`.
A verified state that still matches the disk adds nothing. On `/resume` an identical repeat
of the latest note is skipped; older notes are not rewritten (that would bust the prefix).
`is_rehydrate_item` keeps the note out of the sticky goal and out of folded exchanges.
The CLI prints one `rehydrate · …` status line from `Orchestrator.last_rehydrate`.

Files larger than 32 MB and pointers without a hash are not judged.

## Related

- `docs/orchestrator.md` — post-turn compact, summary sources.
- `docs/session_log.md` — objective log format.
- `docs/working_set.md` — `files[]` hashes and the verify record.

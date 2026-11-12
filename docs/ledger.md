# `ledger.py` — exploration ledger (opt-in "session wiki")

A deterministic, in-memory record of what the session explored. It is built
from tool results with no API call. `Orchestrator.ledger.note()` runs after every
tool batch. At compaction, `Ledger.render()` adds one user item right after the
compact summary:

```
Exploration notes (recorded from tool results before compaction; ...):
- search 'ERROR' in big: 402 matches: big/app.log
- big/data.csv: csv file, 200001 lines. 4 columns x 200,000 rows; ...
- bin/pic.png [changed]: PNG image 2.0 KB width=1920 height=1080
- frames/: 0 dirs, 4,000 files: frame_{00000..03999}.png  x4000
```

- **Entries:** ReadFile (gist from the binary/data digest or summary line; ranges viewed; focus phrases), ListDir, Glob and SearchContent. Error results and non-JSON results are ignored.
- **Staleness:** each read file's `(mtime_ns, size)` is stored when it is read. At render time it is checked again: a file modified since gets `[changed]` and a deleted one gets `[gone]`.
- **Budget:** newest entries first, `LEDGER_BUDGET_CHARS=1500`, at most 200 entries kept. Older entries are counted as `(+N older entries omitted)`.

## Setting: `exploration_ledger` (default **off**)

This was A/B tested live (in the development repo): an explore phase,
then a forced `/compact`, then follow-up questions about what was seen. The
scenarios were survey, code, incidental facts never written into an answer, and a
3-turn session with unrelated work in between, with 1–2 reps each. With the
ledger off, every follow-up was answered in 1 call with no re-reads. The
compaction summary (up to 8k chars) already kept the per-file facts, partly
because the smart-read digests make those facts short. With the ledger on, each
post-compaction request carried about 1.5k extra chars and nothing improved. So
it stays opt-in for very long sessions where the summary may be squeezed, a
regime that was not measured.

Tests: `tests/test_ledger.py`.

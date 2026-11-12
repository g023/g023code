# `claims.py` — the final answer's test claim against the turn's own runs

The harness records every test command of the turn (exit code, oracle
summary, and how many writes came before it). When the final answer makes an
unqualified pass claim, it is compared with the last run:

| situation | note |
|---|---|
| last run failed | `... but the last test run (`cmd`) exited 1 (7 passed, 1 failed).` |
| files changed after the last run | `... but files changed after the last test run (`cmd`).` |
| no test run, files written | `... but no test command ran this turn after files changed.` |

A claim counts as backed when any test run after the last write passed, or
reported the passing count the claim states. An answer that quotes an earlier
green run (for example one with an environment variable set) is not flagged
because a later run without it failed.

The note is printed under the answer (`Note:`) and logged as trace event
`claim_check`. It does not change the answer, cost a hop, or enter history.

Which sentences count: "all tests pass", "the test suite passes", "the full
suite passes", "tests now pass", "N passed". Left alone: qualified sentences ("all other tests pass",
"except", "pre-existing", "fail"), and, when the last run failed, scoped
claims ("the 6 new tests pass", "the ledger/money tests pass", a `test_x`
name) and count claims that match the run's passing count. The scoping rule
came from live runs where the answer correctly reported its own new tests
next to a pre-existing failure.

Setting `claim_check` (env `G023_CLAIM_CHECK`).

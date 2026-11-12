# `obligations.py` — request obligations checked by the harness

What the user's message commits the turn to, compiled from the request text
with regular expressions and checked locally (ast, path rules). No model
call.

Settings `obligations` (env `G023_OBLIGATIONS`) and `write_guards`
(env `G023_WRITE_GUARDS`).

## Named callables (positive)

"Add a function average_entry(ledger, account) to ledgerkit/ledger.py" names a
module-level function with parameters `(ledger, account)`. A signature counts
only when:

- its arguments are bare identifiers (optionally annotated or defaulted);
  format_cents(-5) is an example call, not an obligation,
- a creation verb (add, implement, create, write, define, ...) comes before it
  in the same sentence,
- the name is not a builtin or keyword.

Owner.name(...) with a capitalised owner, or the word "method" nearby, asks
for a method.

At the commit point (the model gives a final answer and the turn wrote
files), `orchestrator._obligation_probe` looks for the definitions
(`find_definitions`, ast, at most 2,000 files). If one is missing or has
another shape (method instead of function, different parameters), the harness
adds one note and the turn continues:

    [harness check] Before you answer: the request asks for module-level function
    average_entry(ledger, account); the project has Ledger.average_entry(self,
    account) at <path>:<line>. If the request means exactly that, ...

At most once per turn. Trace event `obligations`.

## Protected files (negative)

"do not change the tests" and "don't modify config/app.py" become write
guards. The first Edit/WriteFile to a protected file that already existed is
refused with `{"error": "write_guard", "constraint": "<quote>", "hint": ...}`.
An identical repeat of the call goes through: the guard turns an accident
into a decision. New files and shell commands are not guarded. "tests" covers
`test_*.py`, `*_test.py`, `conftest.py` and anything in a directory named
`tests` or `test`.

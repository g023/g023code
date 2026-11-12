# `callees.py` — callee digests on ReadFile

Reading code is import-following: report.py calls `format_cents` and
`monthly_totals`, so the next hops read money.py, ledger.py, dates.py. Most of
those hops want one fact per name.

When ReadFile returns a Python file, `callees.attach` resolves its
module-level `from X import a, b` statements that point inside the project
(relative imports, absolute ones from the root, and `src/` layout) and adds a
`uses` list, rendered by `tool_codec.encode_read` as a `uses:` block after
the imports:

    uses:
      class Ledger -> ledgerkit/ledger.py:16 · __init__(), add(day, account, amount, memo=''), balance(account), monthly_totals(account)
      render_report(ledger, account) -> ledgerkit/report.py:5

(real output for the bench fixture's `ledgerkit/cli.py`; for `dates.py`'s
one-line helper the row is `month_key(day) = f'{day.year:04d}-{day.month:02d}' -> ledgerkit/dates.py:10`.)

Per name: the signature; for a one-statement function the returned expression
(≤ 90 chars) instead of the docstring line; for a class its public methods
(6 shown); for a constant its value; for `from pkg import module` the
module's public names. At most 10 rows of 180 characters; never the standard
library or site-packages. ast only, parse results cached by (path, mtime,
size).

Setting `callee_digest` (env `G023_CALLEE_DIGEST`).

# `views.py` — view-coherent Bash deltas and between-turn drift

The harness keeps, for each project file, the text as it was when the model
last saw it. A file is "seen" after a successful ReadFile (any mode) or after
the model's own Edit/WriteFile. Up to 64 files are kept, each up to 256 KB of
UTF-8, oldest dropped first.

Setting `view_deltas` (env `G023_VIEW_DELTAS`, **on**; `=0` turns it off).
Local only, no API calls.

## Bash deltas

Every Bash call already runs between two stat fingerprints
(`attribution.Preimages.snapshot`), so the files it changed are observed, not
guessed from the command text. After the call, `orchestrator._attach_view_deltas`
adds a `changed` field to the result. `tool_codec.encode_bash` renders it raw
under `[changed since you read]`:

- A seen file of at most 2,400 characters is shown whole:
  `## path (current text, N lines)` and then its text.
- A larger seen file is shown as a unified diff against the view, with one
  context line, at most 40 rows and 1,600 characters. If rows were cut, a
  `(+N more diff lines; ReadFile for the rest)` line says so.
- A seen file that was deleted shows `## path (deleted)`.
- Changed files the model never saw are listed by name on one
  `also changed:` line (at most 8).
- All entries together share a 3,200-character budget.

The view then moves forward to the new text. A file that was touched but has
the same text is not reported. Files under the install's `global_cache/`
(the Bash shims and web store) are excluded, because they are the harness's
own writes. That matters when g023 runs on its own checkout.

Small files are shown whole because, in live runs, the model re-read a file
after a hunk-only diff before editing it again. For small files the diff saved
nothing.

## Between-turn drift

At the start of a turn, `ViewStore.drift` compares every seen file with the
disk. Every write the agent makes (Edit, WriteFile, an observed Bash change)
moves the view forward. So any difference still there was made by something
else: the user's editor, a file watcher, a checkout, or `/undo`. The user
message gets one block, rendered the same way as a Bash delta:

    [files changed since you last saw them, not by your tool calls]
    ## src/app.py (current text, 12 lines)
    ...

The notice needs no guess about who wrote the change. It is exact by
construction: the agent's own changes have already been absorbed. `/clear`
empties the store.

## Measured (deepseek-flash, live)

Edit tasks on a small ledger library, fresh fixture each run, all other
features at their defaults, two batches pooled. The feature only acts when a
Bash command changes a file the model has seen, so the result is split by
whether that happens:

| task | deltas fire? | off: mean $ (sd), n | on: mean $ (sd), n |
|---|---|---|---|
| formatter rewrites a file between two edits | every run | 0.001901 (0.000816), 9 | 0.001454 (0.000255), 9 |
| root-cause bug fix | never | 0.001662, 9 | 0.001687, 9 |
| pre-existing helper | never | 0.002579, 9 | 0.002456, 9 |
| signature change, three files | 2 of 9 runs, last call | 0.002389, 9 | 0.002810, 9 |

On the formatter task the cost fell 24% and the spread shrank by two thirds,
the same direction in both batches. Where the deltas never fire, the arms
match within noise, as an A/A test should. In the signature task the
two expensive "on" runs got an 11-line delta in their final verification
call; the extra calls (exploring notes, undoing a quote style) came before
it, so the difference is run-to-run noise, not the feature. All runs were
solved except on the pre-existing-helper task, where the deltas never
fire (one miss off, two on). The first version showed only diffs and was
11% worse, because the model re-read the file anyway.

## Code

- `ViewStore.see(path)`: record the current text as the model's view.
- `ViewStore.deltas(changed, removed)`: render and advance the views.
- `ViewStore.drift()`: changes not made by the agent's tool calls.
- `changed_and_removed(before, after, root)`: diff two stat fingerprints,
  skipping the harness's own cache directory.

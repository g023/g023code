# attribution.py — counterfactual failure attribution (1.5.0)

**Question it answers:** a check failed after this turn's edits. Did the edits cause it?

The harness answers this without a model call. It re-runs the same command in a disposable shadow copy of the project where every file this turn wrote is restored to its pre-turn bytes (files the turn created are removed). Then it diffs the two failure sets.

| key | meaning |
|---|---|
| `new` | fails now, passes with the edits reverted (caused by this turn) |
| `pre_existing` | fails both ways (not caused by this turn) |
| `fixed_by_edits` | fails only with the edits reverted |
| `verdict` | one sentence; falls back to exit codes when the runner names no tests |

The result is attached as `attribution` to the failing Bash check's own result.

## When it runs (`Orchestrator._after_batch` → `_attribute_failure`)
- The command is check-class (`working_set.looks_like_test_command`) and exited non-zero.
- This turn wrote at least one file through Edit/WriteFile, and the bytes still differ from the pre-image (`Preimages.usable()`).
- No Bash command this turn changed a project file that has no pre-image. Every Bash call runs between two stat fingerprints (`Preimages.snapshot`, mtime+size, same skip rules as the shadow tree), and `note_bash` taints the turn on an unrecorded change. This is decided by effect, not command text: `cp -r . "$tmp"` stays clean, while `python fix.py` that writes taints.
- Bash permission is ALLOW and the original check took less than 30 s. The shadow timeout is `max(5, 3*elapsed + 5)` s.
- `settings.failure_attribution` is on. Disable it with `G023_ATTRIBUTION=0`.

## Truncated views
Checks often end in `| tail -5`. When the failure set changes size, the window shifts, and the same failure can be visible on one side and hidden on the other. When the command has a `| tail`/`| head` view, `strip_view_filters` removes it and the full command runs on both sides, the current tree in a second shadow (the two run concurrently). Under pipefail the exit code comes from the runner either way.

A runner that stopped early (pytest collection error, compile error) adds a `note`: the other tests did not run.

## Shadow tree
- Real copies (`copy2`), never hard links, so the shadow run cannot touch the project.
- `.git`, caches and `.g023` are skipped.
- `.venv`, `venv`, `node_modules` and `.gradle` are symlinked read-through, as are existing symlinks.
- Budget: 5,000 files and 64 MB. Over budget, attribution is skipped.
- The project's absolute path in the command is rewritten to the shadow path, and mapped back in the output.
- The run uses its own process group; the group is killed on timeout; the tree is always removed.

## Failure-id parsers
These runners are recognised:
- pytest `FAILED`/`ERROR` summary lines;
- unittest `FAIL: name (mod.Class)`;
- `go test --- FAIL:`;
- `cargo test ... FAILED`;
- jest/vitest `✕`.

## Caveats
Flaky tests read as `new` or `fixed`. The result is a measurement, not a judgement of intent.

Prior art: Agentless regression filtering (<https://dl.acm.org/doi/full/10.1145/3715754>) and SWE-bench's
`FAIL_TO_PASS` / `PASS_TO_PASS` sets compare test outcomes before and after a patch
offline. Here the comparison runs inside the agent loop, per check, with a
soundness guard; see the README's "For researchers" section.

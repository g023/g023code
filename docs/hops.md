# `hops.py` — round-trip economics: ordered batches, write checks, step budgets

A "hop" is one model call inside a request. Under prefix-cached pricing the
cached history is cheap; most of a hop's price is what the model writes and
the new input after the cached prefix. Measured over 543 earlier live runs,
the dollars of a hop were about 55 % output, 34 % uncached input and 11 %
cached prefix (about 6.5 k cached, 0.4 k uncached and 0.17 k output tokens per
call, 4.8 calls per run). So the cheapest hop is the one that never happens,
and this module saves hops, not context.

The same runs showed where the hops go: of 424 responses that wrote a file,
only 12 also ran the check (Bash) in that response. 253 spent the next round
trip on that Bash alone. Program-order execution
(`orchestrator._batch_segments` / `_execute_in_program_order`) already made
"edit, then test" safe inside one response; the model just was not told.

Three settings, each independent. When a setting is off, the prompt and the
tool schemas are byte-identical to a build without it. Toggling one changes
the cached prefix (`_instructions_key` / `_tools_key`), so the next request
after a toggle is a cache miss.

| setting | env | what it does |
|---|---|---|
| `ordered_batches` | `G023_ORDERED_BATCHES` | ordered-batch prompt contract + `&&` short-circuit |
| `step_budget` | `G023_STEP_BUDGET` | self-declared `[budget MIN-MAX +OVER]`, hop tracker on results |
| `write_check` | `G023_WRITE_CHECK` | Edit/WriteFile carry an optional `check` command, run `&&` after the response's writes |

All three can also be set with `/settings set <name> on|off`. Current defaults
and the measurements behind them are listed under "Defaults" below.

## Ordered batches (`ordered_batches`)

- The standing prompt's batching line (`BATCH_LINE`) is **replaced** by
  `ORDERED_BATCH_TEXT`, not appended to, so the prompt never says two different
  things. A test fails loudly if the base prompt drifts away from
  `BATCH_LINE`.
- The text says: calls in one response run in the order written; reads overlap;
  each Edit, WriteFile or Bash waits for every call before it; a call that only
  needs an earlier one to have *happened* (not to see its output) belongs in
  the same response — "all the edits you already know, then the command that
  checks them".
- `&&` short-circuit (in `_run_tool_calls`): when an Edit/WriteFile in a
  response fails (an `error`, `ok: false`, a denied permission, a write guard),
  every later Bash call in that response and every later write to the **same
  file** is not run. Each gets the typed result from `skipped_result()`:
  `{"error": "skipped", "tool", "after_failed": "Edit a.py", "hint"}`. Reads
  and writes to other files still run (they cannot be corrupted by the
  failure). A skipped call never ran, so it does not count toward the repeat
  guard. The batch summary line says "skipped: an earlier write in this
  response failed". Counted in `hop_stats["skipped"]`.

Why the short-circuit matters: without it, a test run after a failed edit
reports the *old* code's state, and the model reasons about a change that was
never applied.

## Write checks (`write_check`)

The live A/B showed that the ordered-batch *prose* did not change behaviour:
write responses still left the check to the next hop. A schema field did
(`final_answer` was used in about 40 % of runs), so the check moved into the
schema:

- `add_check_to_schemas` gives Edit and WriteFile an optional string `check`
  (deep copies; the registry's schemas are never mutated; `registry.execute`
  drops the key for the nested Agent loop). `_run_tool_calls` takes it off the
  arguments with `take_check` before display, permission and repeat keys.
- After the batch has run in program order, `_run_write_checks` runs each
  distinct command (`plan_checks`: at most `CHECK_MAX` = 2, each carried by
  the last call that offered it) **only if every write of the response
  succeeded** (`&&`). Otherwise the carrier gets
  `check: {command, skipped: "not run: Edit a.py failed"}`.
- The check goes through the ordinary Bash path: permission prompt, test
  oracle, view deltas, attribution. Its result is merged into
  the carrier's result as `"check": {...}` (`merge_check`), so the history
  keeps exactly one output per call. The codec renders it as the write's
  header, a `[check]` line, then the Bash rendering (raw output).
- `_after_batch` treats a carried check like a Bash test run: it is recorded
  in `_turn_checks` (claim check), counts as the batch's check (no
  verify-replay duplicate), and a failure is attributed as usual.
- With `finish_on_success`, `finish.step_ok` requires a carried check to have
  run and passed, and its evidence line names it, so "Edit + check +
  final_answer" is a one-hop, evidence-stamped commit.
- Counted in `hop_stats["checks"]`.

## Step budgets (`step_budget`)

- `BUDGET_ADDENDUM` asks the model to write `[budget MIN-MAX +OVER]` when it
  starts a step (locate, change, verify): the fewest and most model calls it
  expects, and how many more it would spend only if needed.
- `parse_budget()` reads the last such tag from the reasoning, commentary and
  reply text (`[budget 3]`, `[Budget: 2-4 +1]`, `[budget 5-2]` → 2-5; values
  clamped to 1..99). `BudgetState.observe()` starts a new step when the tag
  changes; restating the same tag is not a new step. Finished steps go to
  `history` as (label, hops used).
- Every batch's **last** result ends with one tracker line:
  `[hop N · step k of L-H +O · request $X]` (the step part only when a budget
  is declared; `request $X` is the turn's spend so far — the hop price made
  visible). Only the last result carries it, so the line costs a few tokens
  per hop, not per call.
- When a step has used more than `MAX + OVER` calls the tracker adds one
  `[over budget: ...]` note, once per step, suggesting to finish or re-declare
  with a reason. It is a signal, never a stop.
- The tracker lives only in the model's history (the stored tool result); the
  terminal shows the raw tool result without it.

## Defaults

| setting | default | why |
|---|---|---|
| `step_budget` | **on** | with `finish_on_success`: $ −7.8 % [−14.4, −0.6], calls −16 %, time −10 % |
| `finish_on_success` | **on** (docs/finish.md) | alone: $ −5 %, calls −15 %, time −7 % (n=84) |
| `ordered_batches` | off | prose did not change behaviour (0 % of write steps co-batched); cost neutral |
| `write_check` | off | used in ~half of write runs, cost neutral alone, worse with finish |

Saved config files carry `defaults_rev` (config.py). A file without the
current stamp was written by an older build that wrote every key at its
default, so `false` there for `finish_on_success` reads as unset; an
explicit off saved by this build sticks.

A fourth experiment, a `Rewind` tool (go back to hop N of the request,
restore the files written since, keep a one-line lesson), was measured and
removed before release: the model never called it, and offering it added
10 % calls (row below).

## Measurements

Live A/B, DeepSeek V4.1 Flash, the fixed fixture repos, tasks and judges of
the earlier benches (bugfix, feature, config, question, surge, effort,
rename); a warm-up batch per round is discarded. Three rounds, 546 runs, every
run solved. Task-stratified ratio vs base (mean over tasks of arm/base task
means), 90 % bootstrap CI from resampling runs within each task:

| arm | n | $ | calls | wall time |
|---|---|---|---|---|
| step_budget + finish | 35 | **−7.8 % [−14.4, −0.6]** | −16.2 % | −10.0 % |
| finish | 84 | −5.0 % [−10.2, +1.4] | −14.7 % | −7.2 % |
| write_check + step_budget | 63 | −4.4 % [−10.2, +3.5] | −10.2 % | −7.1 % |
| step_budget | 84 | −1.8 % [−7.0, +4.8] | −4.1 % | −3.7 % |
| write_check | 28 | +0.3 % | −3.6 % | −4.8 % |
| ordered_batches | 21 | +1.8 % | −2.1 % | −10.4 % |
| write_check + finish | 28 | +3.5 % | −10.0 % | −3.2 % |
| rewind (removed) | 21 | +5.6 % | **+10.0 %** | +5.2 % |
| write_check + step_budget + finish | 35 | +9.3 % | −13.7 % | 0.0 % |

The model declared a budget in roughly a third of runs; no write failed in a
batch, so the short-circuit never fired, and Rewind was never called. Bench
scripts and raw rows are kept with the project's research notes.

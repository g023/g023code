# `finish.py` — predicated finish (opt-in)

A tool turn usually ends with one more model call that only reads a passing
result and writes the answer. With `finish_on_success` on, that answer can ride
on the step that should complete the request, and the harness delivers it only
if that step succeeds.

## Switch

`settings.finish_on_success` (saved key, default **true** since the round-trip
bench in docs/hops.md), or env `G023_FINISH_ON_SUCCESS=0`, or
`/settings set finish_on_success off`. When it is
off, the tool schemas and the standing prompt are byte-identical to a build
without this module. Toggling it changes the cached prefix (schemas and prompt
are part of `_instructions_key` / `_tools_key`), so the first request after a
toggle is a cache miss.

## Mechanism

- `add_to_schemas` adds an optional string `final_answer` to Bash, Edit and
  WriteFile (deep copies; the registry's schemas are never mutated).
  `SYSTEM_ADDENDUM` tells the model when to use it.
- `_run_tool_calls` takes the answer out of the arguments with `take()`
  before display, the permission check and the repeat key. The tool never sees
  it, and `registry.execute` also drops the key (the nested Agent loop shares
  the schemas).
- After `_after_batch` (verify replay, attribution), `decide()` judges the
  batch. The last offered answer wins. Every call must have run and passed
  `step_ok`:
  - Bash: `exit_code == 0`, no `error` (a timeout is an error), and no
    test-failure summary in stdout/stderr (`1 failed`, `FAILED`, `FAIL:`,
    `ERROR:`, a Python traceback, cargo/go failure lines). A pipe or a `;`
    chain reports the last command's status, so `pytest | tail` can exit 0
    after a failing run (seen live).
  - Edit / WriteFile: `ok`, no `syntax_error`, and, if a verify replay ran,
    its exit 0 with no failure summary in its tail.
  - Any other call in the batch: no `error`.
- Run demanded by the request: when the user's message asks for a run
  (`run_demanded`: "... and run the tests", "make sure the tests pass",
  "verify the imports still work"), the batch must also have run something —
  a Bash call, or a write whose verify replay or write check ran. Otherwise
  the answer is held (`Decision.held_for_run`) and the carrier's result gets
  `RUN_HELD_NOTE` ("Run them; put the answer on that call"). With pre-emptive
  reads (`preread.md`) a bugfix can reach its Edit in call 1; without this
  rule a bare Edit carrying `final_answer` ended the turn unrun (seen live).
- Delivered: `_deliver_finish` parses `[handoff]` / `[next_effort]` trailers
  like a normal final reply, appends up to three `✓` evidence lines quoting the
  real result (command, exit code, last output line), and ends the turn. The
  carrier's stored tool result gets `DELIVERED_NOTE`. No synthetic assistant
  message is added: history ends `tool_use -> tool_result`, the same shape an
  interrupted turn leaves, so the reasoning-replay rule for tool turns is not
  touched.
- Discarded: the text is dropped, `finish_stats["discarded"]` counts it, a
  muted note says so, and the loop continues as usual.

## When a failed step may still finish

Bench rows showed 27% of offered answers discarded, most of them on a check
whose failure was not the turn's: a fixture test that already failed before
the turn (attribution labelled it `pre_existing`), or a confirming grep that
matched nothing (exit 1). Each discard cost an answer-only call that wrote the
same answer again. Two settings address this, both on by default and both
needing `finish_on_success`. A third, counting a no-match grep (exit 1) as a
pass, was measured (+4 %, no saving in any batch) and removed before release:
such a grep still does not pass a step.

- `finish_licensed` (on; env `G023_FINISH_LICENSED=0` turns it off): `licence(...)` lets a failed
  Bash check finish when its `attribution` lists only `pre_existing` failures
  (no `new`, no `note`), the request has no fix intent (`failing`, `broken`,
  `red`, ...) and shares no name token with those test ids, and the answer
  makes no clean-run claim (`claims.pass_claim`). The answer is delivered with
  a `⚠` line naming the pre-existing failures instead of a `✓` line;
  `finish_stats["licensed"]` counts it.
- `answer_escrow` (on; env `G023_ANSWER_ESCROW=0`): a discarded answer is kept
  (`Orchestrator._escrow`) and the carrier's result says so (`escrow_note`).
  If the model's next reply starts with `[deliver]`, the kept answer is the
  final answer (plus any line after the token), its trailers apply, and
  `finish_stats["released"]` counts it. The live panel hides the token
  (`effort.visible_reply_text`). Any other reply is used as usual.

## Claimed-write guard

A pre-written answer describes the end state before the turn reaches it. Seen
live: an answer carried by an Edit of `app/config.py` said the README was
updated too; that README edit never came and the turn ended with the task
half done. With `claimed_write_guard` (on; env `G023_CLAIMED_WRITE_GUARD=0`),
`unwritten_claims(request, answer, written, root)` lists the files the
request names (`request_files`: a path that exists, or a capitalised word that
is a top-level file's stem, `README` → `README.md`) that the answer mentions in
a sentence with a change word (`updated`, `added`, `now`, `documents`, ...,
not negated by `not`/`already`/`unchanged`) and that no call this turn wrote
(Edit/WriteFile, Rename, or a Bash run that changed it:
`Orchestrator._turn_written`). Any such file holds the answer back
(`Decision.held_note`, `finish_stats["claim_held"]`) and the carrier's result
says which file and to make the change or correct the answer. A turn that
wrote nothing is never checked (it is answering, not reporting changes). The
held answer is not escrowed: it is wrong.

## Measured: licensed finish, escrow, guard, multi-file Edit, Rename

Live A/B, deepseek-flash, held-out task prompts written after the code froze
(feature, rename, grep-confirmed config change, config + README, bug fix),
task-stratified cost per solved task vs the previous defaults, 90 % bootstrap
CI:

| arm | runs | solved | $/solved | calls |
|---|---|---|---|---|
| licensed finish alone | 27 | 26 | −13.6 % [−19.5, −6.0] | −13.6 % |
| answer escrow alone | 42 | 42 | −2.2 % [−8.3, +6.2] | −2.2 % |
| multi-hunk Edit alone | 47 | 47 | +0.9 % [−6.2, +10.3] | −8.5 % |
| grep absence alone (removed) | 42 | 42 | +4.0 % [−4.4, +15.1] | +0.1 % |
| the five shipped together (confirmation run) | 24 | 24 | −20.0 % [−29.5, −8.8] | −14.0 % |

Wall time of the shipped set: −15 %. The one miss of licensed finish alone was a
pre-written answer claiming a README edit that never happened — the case the
claimed-write guard now holds.

## Measured (live, deepseek-flash, 4 task shapes × 3)

| arm | solved | calls/task | output tok/task | $/task (warm) |
|---|---|---|---|---|
| off | 12/12 | 5.58 | 856 | 0.000830 |
| on  | 12/12 | 4.33 | 871 | 0.000845 |

8 of 12 turns ended by a finish (0/3 read-only questions, which have no
carrier step). Wall time fell about 10%. Cost did not fall: the answer-only
call's output is still written, only earlier, so the saving is one call's
cached input. Hence opt-in, for speed. The evidence stamp caught a pre-written
"2 passed" whose real result was "4 passed".

Prior art (developer-declared stop tools that return the raw tool output as
the result): AG2's ToolResult with final=True, the OpenAI Agents SDK's
StopAtTools / stop_on_first_tool, LangChain's return_direct, CrewAI's
force_tool_output_as_result, and the Vercel AI SDK's stopWhen with
hasToolCall (surveyed in pydantic-ai-harness issue 54). The difference here:
the model writes the answer, delivery depends on the step's success, and the
answer is stamped with evidence.

Tests: `tests/test_finish.py`.

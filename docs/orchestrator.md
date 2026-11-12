# `orchestrator.py` — the agent loop

`g023_code/orchestrator.py` is the reasoning engine. It
runs one user prompt through as many model→tools cycles as needed, keeps
the conversation in **Responses API items** (not chat messages) — the
Anthropic wire is a translation layer in `api.py` — runs local helpers for
heavy reads (subagent model calls only with `subagents_enabled`), and
enforces history integrity and the step cap.

The model id on every DeepSeek call from here is `settings.orchestrator_model`
(`deepseek-flash`).

## The conversation model

History is a flat list of typed items (sent as Anthropic `messages` by
`anthropic_codec`):

- `message` (user/assistant text), `function_call` (a tool request),
  `function_call_output` (the tool's result), `reasoning`, `web_search_call`.

`parse_reply(response, rendered_ids)` converts an API response into a
`Reply` dataclass: `content`, `commentary` (fallback text), `tool_calls`
(list of `ToolCall`), `searches` (server-side web searches), `reasoning`,
`usage`, `incomplete_reason`, `streamed`. Unknown item types are recorded
via `api.unknown_item_types` and `signals` (see `signals.md`).

- `ToolCall` wraps `{call_id, name, arguments}`; `parsed` = json.loads with
  a guarded error, `argument_error` returns the failure string when broken.
- `OrchestratorState` is the in-memory history: `add_user`, `add_output`,
  `add_tool_result`, plus `role_breakdown()` for the context gauge.
- `TurnRecord` snapshots one turn (tokens, cost, tool count).

Standing `instructions` and the tool list are **frozen** until the user
changes vision (`_frozen_instructions` / `_frozen_tools`). A prefix
fingerprint (sha256 of both) is written to the session JSONL on first use.

## The turn loop (`run_turn`)

1. `save_prompt(user_input)` → `.g023/prompts/{n}.txt`. `usage.start_turn()`;
   a fresh `TurnTrace` for the UI. Learn turn-get budget is reset.
2. **Auto-compact**: if `auto_compact`, Layer 1 blanks aged-out tool-result
   text in place **only under window pressure** (`context_fraction() >=
   compact_threshold` on a real gauge). Blanking a cached prefix is DeepSeek
   Example 2 and does not recoup at hit rates (see `cost_policy.md`). Fat
   Bash/FetchUrl/ListDir bodies are offloaded at append time into
   `tool_vault` instead, so they never enter a cache unit as raw text.
   Layer 3 `compact()` then runs only if the history is still over
   `compact_trigger_tokens()` (the lower of `compact_at_tokens`, default
   200k, and window × `compact_threshold`) on a real (not char/4-guessed)
   gauge, or earlier when `economic_compact_due()` says the rent paid
   re-reading history since the last compaction has reached the price of
   compacting (`CompactionMeter`, see `economics.md`; never below
   `compact_floor_tokens`, and the economic path never micro-blanks). **Mid-turn**: `_maybe_mid_turn_compact` re-checks the same trigger
   after every tool batch (all tool_use ids are answered at that point),
   compacts with "turn in progress" instructions, and re-appends the user's
   request as a `[continuing after mid-turn compaction]` item if it fell out
   of the kept tail. Every summary ends with a `[recall]` line
   (`with_recall_pointer`) naming `.g023/sessions/<id>.jsonl` and telling the
   model to use SessionSearch for exact details. After a tool-heavy micro,
   wait for the next API `input_tokens` rather than rewriting history on
   the pre-blank gauge.
3. Append the user message; step counter from `settings.max_iterations`
   (0 = `itertools.count(1)`, i.e. no cap; otherwise `range(1, cap+1)`).
4. For each iteration:
   - Print a persistent progress line every `ITERATION_NOTICE_EVERY`
     steps (spinner is transient; this stays in scrollback).
   - `_call_model(iteration, configured)` → blocking or streaming path
     (see below). On API error: print it, `_rollback_to_last_complete()`
     (drop the dangling exchange so the next turn starts clean), return.
   - `state.add_output(reply.output)`; record usage
     (`usage.record(..., scope="orchestrator")`) and drift signals.
   - Track `web_search_call` items (only present in sessions recorded before
     1.5.0, when the server-side tool was still sent) as `ToolEvent`s in the trace.
   - `_apply_reply_meta(reply)` — strip trailing `[handoff]...[/handoff]` and
     `[next_effort: N]` from the visible answer and from history items. A
     complete handoff on a final (no-tool) reply is stored as `_last_handoff`.
     A valid next-effort integer on that same final reply updates in-memory
     `settings.reasoning_effort` for the next user request, capped at 75.
     A trailer on a tool-call step is stripped, not applied.
   - If `show_thinking`: print reasoning excerpt panel + per-iteration
     token/cost line.
   - **Tool calls?** → `_run_tool_calls` → `continue` (next iteration).
   - **`incomplete_reason == "max_output_tokens"`?** → explain *which*
     limit cut it off: the model's own (when no budget was sent) vs the
     configured `/max-tokens` budget. If that reply had no tool calls,
     nudge ("continue, smaller payloads") and keep the turn going, up to
     `MAX_OUTPUT_CONTINUES` (3). A long `/goal` that thinks until the
     ceiling used to end mid-thought with nothing written.
   - Otherwise the turn is done: `final_content = reply.content or
     reply.commentary` (narration-only turns still get presented), break.
   - Loop exhaustion → "Stopped after N tool iterations" message.
5. **Post-turn compact**: after a finished turn (the model has answered and
   is waiting for the next user instruction), `_post_turn_compact()` may
   replace history with a compact summary so the next request starts a fresh
   orchestration: standing `instructions` + working-set item + summary, not
   the tool-call transcript. It fires when the working-set judge says the
   objective is complete, or leftover history (excluding an existing summary
   / working-set item) is at least `POST_TURN_COMPACT_MIN_CHARS` (4,000). A
   short chat-only reply, and a small tool turn under that floor, stay as-is
   — prefix cache still hits, no extra Flash call. Summary source, in order:
   a complete `[handoff]` on this turn's final answer (no second model call);
   a **local fold** of an existing compact-summary item (`checkpoint.fold_summary`:
   narrative bytes kept, this turn's exchange enters a ring of the last 3, the oldest
   leaves as a one-line digest, tool rows dropped; bounded, see `checkpoint.md`);
   otherwise Flash. On a successful
   replace the summary is written to `.g023/logs/{YYYYMMDD_HHMMSS}.log` for
   `--continue` / `/resume log`, and `release_prefix_effort()` runs so a
   stored `[next_effort]` can retune the wire on the new prefix.

   **Commit point.** Just before post-turn compaction the final
   answer is recorded as `committed_answer`. Turn state (`_turn_user_item`,
   `committed_answer`) is cleared at `run_turn` entry and in `reset()`.
   `handle_interrupt` rolls back only an uncommitted turn: Ctrl-C during
   post-turn compaction keeps the finished turn (compaction swaps history
   only after the summary returns), and `cli.run_prompt` still shows a
   non-streamed answer.

   `_call_blocking` owns its request task: if the caller is cancelled the
   request is cancelled too (`asyncio.wait` does not cancel what it waits
   on, so it used to finish, billed, in the background).

## Model calls (`_call_model`, `_call_blocking`, `_call_streaming`)

`_request_body` builds the request body: system prompt (`_system`),
items, `reasoning=reasoning_param(effort)`, `max_output_tokens`
(only when `settings.max_tokens > 0`), `temperature`, `tools` = the
frozen registry schemas (WebSearch and FetchUrl are ordinary client tools; the
server-side `web_search` tool is not sent). `anthropic_codec` turns the body into
an Anthropic Messages request.

`_system` is the static orchestrator prompt (Edit / Glob / TodoWrite /
LearnSearch / SessionSearch / memory notes / bulletin board at
`.g023/bbs/` / peer stance / same-response trailers, plus `A_Improve.txt`
communication markers) plus a vision clause, then the project-instruction
addendum from `project_instructions.system_addendum()`: `g023_compact.md`
if that file exists in the project root, otherwise `g023.md`, plus a
project-root `AGENTS.md` when readable. Neither file is required. See
`project_instructions.md`. Learn guides, memory-note bodies, and bbs
posts are **not** concatenated here. `Orchestrator.__init__` seeds
`.g023/bbs/` via `bbs.ensure_layout()` so the board exists before the
first turn.

- Streaming: consumes `client.stream(...)`, repaints a live spinner,
  records reasoning/items, and sets `_streamed_answer` when the final
  content arrived via the stream. The live panel shows `visible_reply_text`
  so an in-progress `[handoff]` / `[next_effort]` trailer is not painted.
- Blocking: `client.create(...)` — used when `settings.stream` is off or a
  stream can't be used.

## Tool execution (`_run_tool_calls`)

`SUBAGENT_TOOLS = ("ReadFile", "SearchContent", "AnalyzeImage", "Agent")`.

`READ_ONLY_PARALLEL` = ReadFile, SearchContent, ListDir, Glob,
LearnSearch, LearnGet, SessionSearch. Those ALLOW calls in one model
step may overlap. `WRITE_TOOLS` = WriteFile, Edit — same-path writes
stay serial. ASK tools stay serial.

- Heavy tools go to `subagents.router.get_router().delegate(...)` — an
  isolated worker (see `subagents.md`).
- Light tools go through `tools.registry.get_registry().execute(...)`
  with permission checks (see `tools.md`).
- Each tool result is appended as `function_call_output`; permission
  denials and errors become text results so the model can recover.
  A tool error may attach a learn `catalog_nudge`.
- After WriteFile/Edit, `working_set.note_file`. After a Bash command
  that looks like a test runner, `working_set.note_verify`.
- Repeat guard (1.5.0): read/run calls are keyed on `(write epoch, call)`,
  write calls on the call alone. A successful Edit/WriteFile, or a Bash command
  that plausibly mutates files (`bash_may_write`) with exit 0, advances the
  epoch, so test → edit → test → edit → test is never blocked; the 3rd identical
  call with no write in between is skipped, the 4th stops the turn.
- Verify replay (1.5.0, `_after_batch`): a check-class Bash command
  (`looks_like_test_command`) that failed this turn is remembered. After a batch
  with a successful write and no check of its own, it is re-run (Bash must be
  ALLOW, the check must have taken < 30 s) and `verify: {command, exit_code,
  output_tail, note}` is attached to the last write's result. Disable with
  `G023_VERIFY_REPLAY=0`.
- Failure attribution (1.5.0, `_attribute_failure`): before each Edit/WriteFile
  the path's pre-turn bytes are captured (`Preimages`). A failing check-class
  command after such writes is re-run in a shadow copy with them restored, and
  `attribution: {new, pre_existing, fixed_by_edits, verdict}` is attached to its
  result. A Bash call that changes an un-pre-imaged project file (stat
  fingerprint before/after) taints the turn (skip). See attribution.md.
- Predicated finish (on by default, `finish_on_success`): `_run_tool_calls` takes a
  `final_answer` argument off Bash/Edit/WriteFile calls before display,
  permission and repeat keys. After `_after_batch`, `finish.decide` delivers
  it only when every call of the batch ran and passed; `_deliver_finish`
  parses trailers via `_apply_final_meta`, sets `_finished_by_tool`, and the
  loop ends with no further model call. Otherwise the text is discarded. See
  finish.md.
- Round-trip economics (`hops.py`, see hops.md): `_system` applies
  `hops.system_text` (ordered-batch contract replaces the batching line; budget
  addendum), and `hops.prompt_key()` is part of `_instructions_key`.
  With `ordered_batches`, `_run_tool_calls` records failed/denied/guarded
  writes and answers later Bash calls (and later writes to the same file) of
  that response with `hops.skipped_result` without running them
  (`hop_stats["skipped"]`). With `step_budget`, the loop feeds each reply to
  `self._budget.observe` and the last result of each batch gets the
  `[hop N · step k of L-H +O · request $X]` tracker (history only; the
  terminal shows the raw result). The budget state is reset at each user turn.
- The frozen standing prompt ends with an Environment section — `environment_line()`:
  the working directory is the project root (relative paths, no `cd`), OS, shell, python.
  Wording that named Bash doubled denied Bash attempts in the long-doc bench (3 → 6).
  It deliberately carries **no path**: DeepSeek renders the tool schemas after the
  instructions, so any per-project byte there forks the cache before the tools
  (measured: call 1 hit 1.4k / miss 3.9k tokens in each new project; without the
  path, hit 5.1k / miss 168 — a live probe during development).
- Working-set mirroring waits until **every** output in the batch is
  recorded. Appending the user-role working-set item after the first
  WriteFile of a parallel group splits the remaining outputs from their
  calls; DeepSeek then 400s with `No tool output found for tool call …`.
- `_reseat_items_splitting_tool_pairs()` runs before each model call and
  parks any user/working-set item that still sits inside an open tool
  batch at the tail (move, not rewrite).

## History integrity

The API rejects any history where a `function_call` has no matching
`function_call_output` (and vice versa) — and the broken history is
re-sent on the next turn, so the session dies until repaired.

- `_rollback_to_last_complete()` — cut the item list back to the last
  state where every call is paired (used on API errors).
- `_repair_pairing()` — patch a history whose last call is unpaired.
- `_reseat_items_splitting_tool_pairs()` — move a user/working-set item
  that landed between a `function_call` batch and its outputs. Pairing
  can be complete and still unsendable if a user row splits the batch.

These plus `micro_compact` / `compact` are the history editors.
`tests/test_history_integrity.py` pins pairing repair; `tests/test_session_compact.py`
pins session compact; `tests/test_parallel_tools.py` pins overlap.

## Context management

- `context_fraction()` — last orchestrator `input_tokens` vs `max_context_tokens`
  (the API gauge, not tiktoken). A post-compact char/4 guess is marked estimated
  and is not an auto-compact trigger.
- `micro_compact()` — blank old tool-result text in place (items stay; already
  cleared placeholders stay the same string; most recent keep-window results
  and the latest verbatim/raw ReadFile are spared). The auto path only
  calls this under window pressure; the primitive itself is unchanged.
- `compact()` — prefers `_last_handoff` from the previous final answer when
  no focus instructions were passed (no API cost), then **consumes** it so
  a later pass cannot reuse a stale summary. Otherwise
  `_render_transcript()` feeds the summarizer the session head **and** tail
  (`max_output_tokens=2048`, reasoning off). History becomes sticky
  original-goal + middle summary + pairing-intact recent tail (original
  item dicts, not rebuilt). Empty/failed summarizer output leaves items
  unchanged. A focused `/compact <topic>` still calls Flash so the
  summary can follow that topic.
- `_post_turn_compact()` — after a finished turn, replace history with the
  summary plus a working-set item so the next request is system prompt +
  handoff, not the prior transcript. Skipped when
  `_should_post_turn_compact()` is false. That helper now goes through
  `cost_policy.should_post_turn_compact`: a **hot prefix** (last call hit
  rate ≥ 0.5) is kept even when leftover chars exceed 4 000, because those
  tokens will be hits at $0.003/M. Compact when the objective is complete,
  the window is full, or leftover bulk is large *and uncached*. Short chat,
  small tool rows, and Layer 3 having already shrunk a still-open objective
  still skip. Reasoning items are scratch work and do **not** count toward
  the leftover-bulk floor. `_needs_post_turn_summarizer()` is
  false when a same-response `[handoff]` is present, or a compact-summary
  item already exists — reuse or fold locally, do not pay for Flash again.
  Writes `.g023/logs/{dstamp_tstamp}.log`.
- `_request_body` publishes the frozen `instructions` / `tools` /
  `reasoning.effort` onto the subagent router so nested Agent calls reuse
  the standing prefix (cache hits from token 0 through tools; isolated
  input still misses).

Same-response trailers live in `effort.py` (`split_reply_meta`,
`visible_reply_text`, `strip_reply_meta_from_items`). Locked shapes:

```
[handoff]
## Goals / Files / Decisions / Done
[/handoff]
[next_effort: N]
```

The `[handoff]` block is solicited just in time. `run_turn` appends the
line `[handoff due]` to the user item only when `_handoff_due_forecast()`
predicts a compaction within ~2 turns: ctx + 2 x growth-EMA >= 0.9 x the
trigger (or 0.9 x the window threshold), or, under the economic policy,
rent + 2 x rent-EMA >= `meter.price(next_ctx, inline_handoff=True)` once
past `floor_tokens`. Estimated contexts (just after a compaction) never
forecast. `_observe_turn_growth()` updates both EMAs (alpha 0.5) after each
turn and skips turns that compacted. On other turns the model omits the
block, which saves the ~150-400 output tokens per turn the trailer used to cost.
The other trigger is also decided by the harness (1.5.0): a TodoWrite that
closes the last open todo returns `note: "[handoff due]"`. The old rule ("your
reply marks every open todo done") was vacuously true with zero todos and gave
unrequested ~1k-char blocks in 4 of 10 bench runs.

An unclosed `[handoff]` is hidden from the user but is not used as a
summary — compact then falls back to fold or Flash. `N` is 0–75 (75 is
the ceiling; values above 75 cap). `/goal` forces 75 for its turn
(releasing the pin first so a lower alias cannot stick) and keeps a
same-response `[next_effort]` in settings instead of restoring the
pre-goal dial. It does not release the pin again on the way out. The
stored dial is sent when the prefix is new.

## Resume

- `resume_from_latest()` — replay `.g023/sessions/` JSONL into
  `state.items` and restore the working set. Pairing is repaired. Working-set
  files that changed on disk since get a `[rehydrate]` note (`checkpoint.md`).
- `seed_from_objective_log()` — new orchestration from `.g023/logs/`:
  standing instructions + working-set item + summary, plus a `[rehydrate]` note
  when the log's signature is not `verified` or tracked files drifted.
  `last_rehydrate` keeps the verdict for the CLI.

## Status & cost reporting

- `status_line()` — the context gauge line after every turn.
- `turn_summary()` / `cost_lines()` — the `/cost` and
  per-turn numbers, formatted by `usage.format_cost`.

## Related

- `docs/api.md` — the client and response-parsing helpers it builds on.
- `docs/effort.md` — same-response `[handoff]` / `[next_effort]` trailers.
- `docs/cost_policy.md` — keep vs compact vs vault; what `_should_post_turn_compact` actually decides.
- `docs/working_set.md` — remaining work and the completion judge.
- `docs/session_log.md` — JSONL and objective logs.
- `docs/subagents.md`, `docs/tools.md` — the two tool-dispatch targets.
- `docs/signals.md` — `_record_signals`.
- `docs/data_files.md` — `.g023/sessions/` JSONL and `.g023/logs/` handoff files.
- `docs/ui.md` — `TurnTrace`, `ActivityPrinter` it feeds.
- `docs/project_instructions.md` — `g023.md` / `g023_compact.md` / `AGENTS.md`.

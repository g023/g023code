# `orchestrator.py` — the agent loop

`g023_code/orchestrator.py` (~2,060 lines) is the reasoning engine. It
runs one user prompt through as many model→tools cycles as needed, keeps
the conversation in **Responses API items** (not chat messages), delegates
heavy work to subagents, and enforces history integrity and the step cap.

The model id on every DeepSeek call from here is `settings.orchestrator_model`
(`deepseek-flash`).

## The conversation model

The orchestrator talks to DeepSeek's `/responses` endpoint, where history
is a flat list of typed items:

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
   threshold on a real (not char/4-guessed) gauge. After a tool-heavy micro,
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
   - Track server-side `web_search` items as `ToolEvent`s in the trace.
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
   a **local fold** of an existing compact-summary item (append this turn's
   user/assistant text, drop tool rows); otherwise Flash. On a successful
   replace the summary is written to `.g023/logs/{YYYYMMDD_HHMMSS}.log` for
   `--continue` / `/resume log`, and `release_prefix_effort()` runs so a
   stored `[next_effort]` can retune the wire on the new prefix.

## Model calls (`_call_model`, `_call_blocking`, `_call_streaming`)

`_request_body` builds the Responses body: system prompt (`_system`),
items, `reasoning=reasoning_param(effort)`, `max_output_tokens`
(only when `settings.max_tokens > 0`), `temperature`, `tools` = the
frozen registry schemas **plus** DeepSeek's server-side `web_search` tool.

`_system` is the static orchestrator prompt (Edit / Glob / TodoWrite /
LearnSearch / SessionSearch / memory notes / same-response trailers,
plus `A_Improve.txt` communication markers) plus a vision clause, then
the project-instruction addendum from `project_instructions.system_addendum()`:
`g023_compact.md` if that file exists in the project root, otherwise
`g023.md`, plus a project-root `AGENTS.md` when readable. Neither file
is required. See `project_instructions.md`. Learn guides are **not**
concatenated here.

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

An unclosed `[handoff]` is hidden from the user but is not used as a
summary — compact then falls back to fold or Flash. `N` is 0–75 (75 is
the ceiling; values above 75 cap). `/goal` forces 75 for its turn and
keeps a same-response `[next_effort]` for the next user request instead
of restoring the pre-goal dial.

## Resume

- `resume_from_latest()` — replay `.g023/sessions/` JSONL into
  `state.items` and restore the working set. Pairing is repaired.
- `seed_from_objective_log()` — new orchestration from `.g023/logs/`:
  standing instructions + working-set item + summary.

## Status & cost reporting

- `status_line()` — the context gauge line after every turn.
- `turn_summary()` / `cost_lines()` / `cost_summary()` — the `/cost` and
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

# `orchestrator.py` — the agent loop

`g023_code/orchestrator.py` (~1,020 lines) is the reasoning engine. It
runs one user prompt through as many model→tools cycles as needed, keeps
the conversation in **Responses API items** (not chat messages), delegates
heavy work to subagents, and enforces history integrity and the step cap.

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

## The turn loop (`run_turn`)

1. `usage.start_turn()`; a fresh `TurnTrace` for the UI.
2. **Auto-compact**: if `auto_compact` and `context_fraction() >=
   compact_threshold`, run `compact()` first.
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
   - If `show_thinking`: print reasoning excerpt panel + per-iteration
     token/cost line.
   - **Tool calls?** → `_run_tool_calls` → `continue` (next iteration).
   - **`incomplete_reason == "max_output_tokens"`?** → explain *which*
     limit cut it off: the model's own (when no budget was sent) vs the
     configured `/max-tokens` budget.
   - Otherwise the turn is done: `final_content = reply.content or
     reply.commentary` (narration-only turns still get presented), break.
   - Loop exhaustion → "Stopped after N tool iterations" message.

## Model calls (`_call_model`, `_call_blocking`, `_call_streaming`)

`_request_body` builds the Responses body: system prompt (`_system`),
items, `reasoning=reasoning_param(effort)`, `max_output_tokens`
(only when `settings.max_tokens > 0`), `temperature`, `tools` = the
registry schemas **plus** DeepSeek's server-side `web_search` tool.

- Streaming: consumes `client.stream(...)`, repaints a live spinner,
  records reasoning/items, and sets `_streamed_answer` when the final
  content arrived via the stream.
- Blocking: `client.create(...)` — used when `settings.stream` is off or a
  stream can't be used.

## Tool execution (`_run_tool_calls`)

`SUBAGENT_TOOLS = ("ReadFile", "SearchContent", "AnalyzeImage", "Agent")`.

- Heavy tools go to `subagents.router.get_router().delegate(...)` — an
  isolated worker (see `subagents.md`).
- Light tools (`Bash`, `WriteFile`, `ListDir`, `FetchUrl`) go through
  `tools.registry.get_registry().execute(...)` with permission checks
  (see `tools.md`).
- Each tool result is appended as `function_call_output`; permission
  denials and errors become text results so the model can recover.

## History integrity

The API rejects any history where a `function_call` has no matching
`function_call_output` (and vice versa) — and the broken history is
re-sent on the next turn, so the session dies until repaired.

- `_rollback_to_last_complete()` — cut the item list back to the last
  state where every call is paired (used on API errors).
- `_repair_pairing()` — patch a history whose last call is unpaired.

These are the only two history editors, and `tests/test_history_integrity.py`
pins both.

## Context management

- `context_fraction()` — estimated context used vs `max_context_tokens`
  (via `tiktoken`, reported by `usage`).
- `micro_compact()` — cheap trim for small wins.
- `compact()` — `_render_transcript()` turns the conversation into a
  single user message (the compressed summary), clears the item history,
  and reports what was dropped; the CLI prints the returned text.

## Status & cost reporting

- `status_line()` — the context gauge line after every turn.
- `turn_summary()` / `cost_lines()` / `cost_summary()` — the `/cost` and
  per-turn numbers, formatted by `usage.format_cost`.

## Related

- `docs/api.md` — the client and response-parsing helpers it builds on.
- `docs/subagents.md`, `docs/tools.md` — the two tool-dispatch targets.
- `docs/signals.md` — `_record_signals`.
- `docs/ui.md` — `TurnTrace`, `ActivityPrinter` it feeds.

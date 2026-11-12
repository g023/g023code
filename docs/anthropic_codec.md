# `anthropic_codec.py` — Responses items ↔ Anthropic messages

`g023_code/anthropic_codec.py` is a pure, deterministic translator. The rest of
the program keeps speaking the Responses item vocabulary (`message`,
`reasoning`, `function_call`, `function_call_output`, `web_search_call`);
`api.py` uses this module to talk to DeepSeek's Anthropic-format endpoint
(`POST {base}/anthropic/v1/messages`). No I/O happens here.

## Why deterministic

DeepSeek's prefix (KV) cache matches on identical leading bytes. The same item
list must always encode to the same body: fixed key order (`model, system,
tools, thinking, output_config, temperature, max_tokens, stop_sequences,
messages, stream`), no timestamps, no dict-order dependence. Live check
(2026-11-07): the call after a web search had 1,920 of 2,098 input tokens
cached.

## Request side

- `items_to_messages(items)`
  - `reasoning` with a `signature` → `thinking` block (required replay when
    tool calls continue); unsigned reasoning is dropped.
  - `function_call` → `tool_use` (`input` parsed from `arguments`; malformed
    JSON is kept as `{"_malformed_arguments": raw}` rather than lost).
  - `function_call_output` → `tool_result` (empty output → `"(empty)"`),
    always placed before text in its user message.
  - `web_search_call` → its stored `_anthropic` blocks (`server_tool_use` +
    `web_search_tool_result`) replayed verbatim.
  - User `input_text` / `input_image` (data URI → base64 image source).
  - Consecutive same-role messages are merged; thinking-only assistant
    messages are dropped; a `(continue)` user message is prepended when the
    list would otherwise start with an assistant turn.
- `tools_to_anthropic(tools, web_search_max_uses)` — function tools →
  `{name, description, input_schema}`; `{"type": "web_search"}` →
  `{"type": "web_search_20250305", "name": "web_search", "max_uses": N}`.
  Since 1.5.0 g023 no longer offers that tool (WebSearch is a client
  tool, `docs/web.md`); the mapping and the `web_search_call` replay remain
  so sessions recorded earlier still resume (verified live against the API
  during development).
- `thinking_params(reasoning)` — effort `none`/`off`/`disabled` →
  `thinking: disabled`; anything else → `thinking: enabled` +
  `output_config.effort`.
- `to_messages_body(body, *, web_search_max_uses)` — whole request.
  `temperature` is sent only when thinking is not enabled.

## Response side

- `from_message(msg)` → Responses-shaped dict:
  `thinking` → `reasoning` (`id rs_{msg}_{i}`, `signature` kept); `text` →
  `message` (phase `commentary` when it precedes a tool block or
  `stop_reason == "tool_use"`, else `final_answer`); `tool_use` →
  `function_call` (`call_id` = block id); `server_tool_use` + its
  `web_search_tool_result` → one `web_search_call` with
  `action {type: search, queries, sources}` and the raw blocks under
  `_anthropic`. `stop_reason == "max_tokens"` → `status: incomplete`,
  `incomplete_details.reason = max_output_tokens`.
- `convert_usage(usage)` — `input_tokens` = miss + `cache_read_input_tokens`,
  `input_tokens_details.cached_tokens` = hit, `server_tool_use` copied (the
  usage tracker bills `tool_search`, the count of successful searches).
- `StreamTranslator.feed(event)` — SSE events → Responses stream events:
  `response.reasoning_text.delta`, `response.output_text.delta`,
  `response.output_item.added` / `.done` (web_search_call; messages with
  their phase decided when the next block or the stop reason is known), and
  a terminal `response.completed` / `response.incomplete` carrying
  `from_message` of the assembled message. `error` events raise
  `StreamError`.

## Related

- `docs/api.md` — the client that calls this module.
- DeepSeek's Anthropic API compatibility guide: <https://api-docs.deepseek.com/guides/anthropic_api>.

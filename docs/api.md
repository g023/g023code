# `api.py` — the DeepSeek Responses API client

`g023_code/api.py` is the only module that talks to the DeepSeek HTTP API.
Everything above it (orchestrator, subagents, vision) goes through here.

## `ResponsesClient`

A thin `httpx.AsyncClient` wrapper for `https://api.deepseek.com`
(`settings.base_url`). The endpoint is the **Responses API** (`/responses`)
— the only DeepSeek surface that exposes the server-side `web_search` tool,
so the whole program is built on it.

- `_key()` / `_headers()` — `Authorization: Bearer <K.dat key>` +
  `Content-Type: application/json`.
- `create(model, instructions, input, tools, reasoning, max_output_tokens,
  temperature, ...)` — one non-streamed call; returns the parsed JSON.
- `stream(model, ...)` — async iterator over SSE chunks (the live UI path).
- `vision_responses(model, text_parts, images, ...)` /
  `vision_chat(model, ...)` — the two vision wire formats (native
  responses items vs chat-style); used by `subagents/vision.py` for the
  deepseek backend.
- `aclose()` — close the shared client.

The client timeout is deliberately generous because DeepSeek's
server-side web search can run a long agentic loop (5–20 searches plus
page opens) inside one response.

## `DeepSeekError`

Raised on transport/status failures; `DeepSeekError.from_response(r)`
builds a readable message from the HTTP response (status + body excerpt).

## Response-parsing helpers (pure functions)

These decode a Responses response dict. They are the layer that absorbs
schema drift, so the whole program can stay simple:

- `chat_answer(response)` — the plain text answer from a chat-shaped response.
- `reasoning_param(enabled, effort)` — the `reasoning` object sent to the
  API (`low`/`high`/`max`).
- `output_text(response)` — final assistant text; returns `""` on unknown
  shapes (this is the "silent model" failure the drift signals watch for).
- `incomplete_reason(response)` — why a response stopped
  (e.g. `max_output_tokens`), or `None`.
- `reasoning_text(response)` — the reasoning item's text, if any.
- `unknown_item_types(response)` — names of item types this client does
  not recognise (feeds `signals.note_unknown_types`).
- `silent_degradation(response)` — a single pass over the response
  checking the cheap "something moved" symptoms; returns a list of
  human-readable warnings the CLI can print.

## `get_client()`

Module-level singleton accessor for the shared `ResponsesClient`.

## Failure philosophy

The client does not validate responses — it assumes the API works and
**watches for quiet change** instead. A renamed content field must not
crash the session; it should make `output_text` return `""` and light up
`silent_degradation` / the signal log, so a human sees "something moved"
rather than a silently empty answer.

## Related

- `docs/orchestrator.md` — the consumer of `create`/`stream` and these
  parsers.
- `docs/subagents.md` — file_reader, router, and vision call
  `client.create(...)` through the same helper set.
- `docs/signals.md` — where `silent_degradation` results are recorded.

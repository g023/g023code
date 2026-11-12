# `api.py` — the DeepSeek client (one model, one wire)

`g023_code/api.py` is the only module that talks to the
DeepSeek HTTP API. Everything above it (orchestrator, subagents, vision,
`/g023-compact`) goes through here.

There is exactly one model (`config.MODEL = "deepseek-flash"`, DeepSeek V4.1
Flash) and exactly one wire: `POST {base_url}/anthropic/v1/messages`
(`config.MESSAGES_PATH`). Callers build bodies in the internal item shape
(`input` / `instructions` / `tools` / `reasoning` / `max_output_tokens`);
`anthropic_codec.py` is the only translator to and from the Anthropic format.

## `DeepSeekClient`

A thin `httpx.AsyncClient` wrapper for `settings.base_url`
(`https://api.deepseek.com`).

- `_key()` / `_headers()` — `Authorization: Bearer <K.dat key>` + JSON
  content type. The key is read lazily, so `--help` and `/settings` work
  without one.
- `url` — `base_url + MESSAGES_PATH` (a property, so a base-url change takes
  effect on the next call).
- `_wire_body(body)` — **forces `body["model"] = MODEL`** (a caller cannot
  pick another model) and calls `anthropic_codec.to_messages_body`. (DeepSeek's Search/Fetch
  tool endpoints are called by `web.py`, not by this client.)
- `create(*, timeout=None, **body)` — one blocking call; the reply goes
  through `anthropic_codec.from_message`. A `{"type": "error"}` body raises
  `DeepSeekError`.
- `stream(**body)` — async iterator of item-shaped events produced by
  `anthropic_codec.StreamTranslator` from the SSE stream. A stream `error`
  event raises `DeepSeekError(0, "stream error: …")`; a stream that closes
  without a terminal event yields `response.incomplete`
  (`incomplete_details.reason = "stream_closed"`).
- `vision(prompt, data_uri, system="", timeout=180, max_output_tokens=2048)`
  — one isolated image question: a single user turn with an `input_image`
  (the data URI carries the real media type) and the prompt text. Thinking is
  off. Used by `subagents/vision.py`; see `docs/images.md`.
- `aclose()` — close the shared HTTP client. `close_client()` closes the
  module-level one (the REPL calls it on exit).

**Retry.** `create()` and the opening of `stream()` retry up to
`MAX_ATTEMPTS = 3` on 408/409/429/500/502/503/504/529 and on connect /
protocol / read-write transport errors (not on `ReadTimeout`). Backoff: 1 s,
2 s, ... (cap 30 s) times 0.5–1.0 jitter; `Retry-After` (capped at 30 s) wins. A stream is only
retried before its first event — once output has been yielded a retry would
duplicate it. A stream that ends with `stream_closed` raises `StreamCutOff`
in the orchestrator, which makes one blocking `create()` call instead.

The default timeout (`DEFAULT_TIMEOUT = 300 s`) is generous because
DeepSeek's server-side web search can run a long loop inside one response.

## `DeepSeekError(status, message, body="")`

Raised on HTTP and API errors. `DeepSeekError.from_response(status, text)`
extracts the API's error message (falls back to a body excerpt).

## Request helper

- `reasoning_param(enabled=None, effort=None)` — the `reasoning` block,
  always `{"effort": <level>}`, defaulting to the current settings. The codec
  turns `none` into `thinking: {"type": "disabled"}` and `low` / `high` into
  `thinking: {"type": "enabled"}` + `output_config.effort`. Integer effort is
  mapped by `effort.effort_to_wire`: 0 → `none`, 1–50 → `low`, 51–75 →
  `high`. The harness never sends `max` (see `docs/effort.md`).

## Response-parsing helpers (pure functions)

They are the layer that absorbs schema drift, so the rest of the program can
stay simple:

- `output_text(response)` — final assistant text; `""` on unknown shapes
  (the "silent model" failure the drift signals watch for).
- `incomplete_reason(response)` — why a response stopped
  (e.g. `max_output_tokens`), or `""`.
- `reasoning_text(response)` — the reasoning item's text, if any.
- `unknown_item_types(response)` — item types this client does not
  recognise (feeds `signals.note_unknown_types`).
- `silent_degradation(response)` — one pass over the cheap "something moved"
  symptoms; returns a warning string or `None`.

## `get_client()`

Module-level singleton accessor for the shared `DeepSeekClient`.

## Failure philosophy

The client does not validate responses — it assumes the API works and
**watches for quiet change** instead. A renamed content field must not crash
the session; it should make `output_text` return `""` and light up
`silent_degradation` / the signal log.

## Related

- `docs/anthropic_codec.md` — item shape ↔ Anthropic messages translation.
- `docs/effort.md` — the mapping `reasoning_param` uses.
- `docs/images.md` — image loading and the vision call.
- `docs/orchestrator.md` — the consumer of `create` / `stream`.
- `docs/subagents.md` — FileReader, Agent and vision use the same client.
- `docs/signals.md` — where `silent_degradation` results are recorded.

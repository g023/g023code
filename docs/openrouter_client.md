# `openrouter_client.py` — OpenRouter vision + quota

`g023_code/openrouter_client.py` is the OpenRouter backend: a small
OpenAI-compatible `/chat/completions` client used for vision, with
per-model failover on rate limits and a daily-usage tracker for `/or`.

## Key & model list

- Key: `config.load_openrouter_key()` → `OK.dat` (machine level).
- `or_models_file()` → `OR_VISION.json` in the project root; it holds the
  ordered list of models to try (e.g. `["google/gemma-4-26b-a4b-it:free"]`).
- `load_or_models()` → that list, with clear errors when missing/broken.
  `settings.or_vision_models` overrides the file when set (via `/or
  models`).
- `rate_limit_tier()` / `active_daily_limit()` — free tier = 50 req/24h,
  credits tier ($10+ lifetime) = 1000 req/24h (from
  `config.OPENROUTER_*_DAILY_LIMIT`); `settings.or_credits` selects.

## Vision requests

- `or_vision_chat(model, prompt, image_b64, system, timeout, key)` — one
  request to `https://openrouter.ai/api/v1/chat/completions` with the
  image embedded as a `data:image/png;base64,…` URL (vision models accept
  data URLs even when the orchestrator's own backend doesn't). Hard 429 →
  `OrRateLimitError` carrying `retry_after`.
- `or_vision_with_failover(prompt, image_b64, system, timeout)` — tries
  models in order (the previously-used model first — it's most likely warm
  — then the rest), hopping on 429 until one works. When every model is
  rate-limited it raises one clear error naming the tier and its ceiling.

## Daily quota (`OrUsage`)

Persisted to `.g023/or_usage.json`: per-day request counts,
rate-limit events, and the last model used. `record_request`,
`record_rate_limit`, `remaining()` back the `/or status` screen. Daily
limits reset on date change.

## Related

- `docs/subagents.md` — `vision.py` calls `or_vision_with_failover` when
  the openrouter backend is selected.
- `docs/cli.md` — `/or` status/credits/models handlers.
- `docs/data_files.md` — `OK.dat`, `OR_VISION.json`.
- `tests/test_openrouter_vision.py` — pins the failover and quota logic.

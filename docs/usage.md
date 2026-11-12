# `usage.py` — token & cost accounting

`g023_code/usage.py` turns every API response into numbers
the user can trust: tokens, cache hit/miss split, and USD cost at
DeepSeek's published (tidal) rates.

## Pricing (`ds_pricing.json` → `PRICING`)

- `_load_ds_pricing()` reads `ds_pricing.json` from the home dir, then
  the project root (falling back to a built-in table) and builds
  `PRICING: dict[model, ModelPricing]`.
- `ModelPricing` holds the regular rates (`cache_hit`, `cache_miss`,
  `output`, per 1M tokens) and the surge rates (`surge_cache_hit`,
  `surge_cache_miss`, `surge_output`; 2× regular today).
- `pricing_for(model)` — lookup, defaulting to the flash table. There is
  one row, `deepseek-flash`; usage recorded under any older id (1.3.x
  session logs) bills as Flash.

Regular flash rates (effective 2026-09-10): hit $0.003/M, miss $0.15/M,
output $0.60/M. Surge is exactly 2×. Peak windows are Monday–Friday only.

## Accumulators

- `ModelUsage` — per-model totals: input tokens (cache hit vs miss),
  output tokens, calls, `web_searches`, `fetches`; `tool_cost` prices the
  last two at `SEARCH_REQUEST_USD` / `FETCH_REQUEST_USD` ($0.75 / 1,000
  successful requests); `cost()` adds `tool_cost` and applies the right price ×
  surge/regular; `hit_rate()`; `merge()` combines two records.
- `UsageTracker` (singleton via `get_usage()`):
  - `record(model, usage, scope="orchestrator")` — ingest one API
    response's `usage` object, normalising **both** usage spellings
    (`input_tokens` + `cached_tokens`, or flat `prompt_cache_hit/miss_tokens`),
    splitting cache hit/miss the same way either way, and **never assuming a hit
    when the provider didn't report one**. Returns the per-call
    `ModelUsage` delta. Scope is `orchestrator` vs `subagent`.
  - Server-side searches are read from `usage.server_tool_use.tool_search`
    (successful searches — the billable count; `web_search_requests`
    counts attempts).
  - `record_fetch(ok)` — one DeepSeek Fetch API call, billed only when
    `ok`; kept under the `deepseek-fetch-api` bucket (subagent scope).
  - `start_turn()` — snapshot for per-turn deltas (turn_cost / turn tokens).
  - Queries: `per_model()`, `totals()`, `cost(model, surge)`,
    `scope_cost(scope)`, `reset()` (backs `/clear` and `/cost`).

## Formatting

`format_cost(usd)` — money with sub-cent precision preserved.

## What the tests pin

`tests/test_usage_accounting.py` asserts the arithmetic: rates applied to
the right token counts, hit/miss derived identically from either
endpoint's field naming, and no invented cache hits. `tests/test_call_accounting.py`
counts API calls per operation (the delegation cost table).

## Related

- `docs/data_files.md` — `ds_pricing.json` on disk.
- `docs/orchestrator.md` — records usage every iteration.
- `docs/cost_policy.md` — the keep-vs-compact break-even that consumes these rates.
- `docs/surge.md` — where the surge flag comes from.
- `docs/cli.md` — `/cost`, `/status`, per-turn lines.

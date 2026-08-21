# `usage.py` — token & cost accounting

`g023_code/usage.py` turns every API response into numbers the user can
trust: tokens, cache hit/miss split, and USD cost at DeepSeek's published
(tidal) rates.

## Pricing (`ds_pricing.json` → `PRICING`)

- `_load_ds_pricing()` reads `ds_pricing.json` from the project root
  (falling back to a built-in table) and builds
  `PRICING: dict[model, ModelPricing]`.
- `ModelPricing` holds `regular` and `surge` rate sets, each with
  `cache_hit`, `cache_miss`, `output` (per 1M tokens) and a
  `surge_multiplier()` (2× today).
- `pricing_for(model)` — lookup, defaulting to the flash table for
  unknown models.

## Accumulators

- `ModelUsage` — per-model totals: input tokens (cache hit vs miss),
  output tokens, calls; `cost()` applies the right price ×
  surge/regular; `hit_rate()`; `merge()` combines two records.
- `UsageTracker` (singleton via `get_usage()`):
  - `record(model, usage, scope="orchestrator")` — ingest one API
    response's `usage` object, normalising **both** field-name styles
    (chat-completions and Responses API), splitting cache hit/miss the
    same way whichever endpoint reported it, and **never assuming a hit
    when the provider didn't report one**. Returns the per-call
    `ModelUsage` delta. Scope is `orchestrator` vs `subagent`.
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
- `docs/surge.md` — where the surge flag comes from.
- `docs/cli.md` — `/cost`, `/status`, per-turn lines.

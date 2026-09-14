# `cost_policy.py` — cache-aware keep vs compact vs vault

`g023_code/cost_policy.py` is the break-even layer between DeepSeek's
prefix cache and the history editors in `orchestrator.py`. It does not
call the API. It answers one question: **is rewriting this prefix cheaper
than keeping it?**

Flash regular rates (see `ds_pricing.json`): hit $0.003/M, miss $0.15/M
(~50×), output $0.60/M (~200× a hit). A later `/responses` request hits
only when it fully matches a persisted cache prefix unit from token 0
(Example 1: A+B then A+B+C hits; Example 2: A+B then A+C misses).

## The ratio that decides

Once a token sits in the cached prefix, keeping it costs the hit rate.
Rewriting it (Layer-1 blank, Layer 3, post-turn replace) turns the tail
from that point into misses.

Worked example, 500 cached tool-result tokens vs a 3 000-token tail bust:

- Keep: $0.0000015/turn as hits
- Bust: $0.000441 one-time
- Turns to recoup: ~294

Coding sessions do not last 294 turns. So the default is **keep a hot
prefix**. Compact when the window is full, the leftover is uncached (it
will be re-missed anyway), or the objective is done (next request is a
new orchestration).

## Decisions

| Helper | Used by | True when |
|---|---|---|
| `should_micro_cached_prefix` | `_maybe_auto_compact` | window pressure only |
| `should_post_turn_compact` | `_should_post_turn_compact` | objective done, window full, or large *uncached* leftover |
| `should_vault_tool_output` | `_run_tool_calls` | Bash/FetchUrl/ListDir/SearchContent/Glob/AnalyzeImage ≥ 2 500 chars, not a raw ReadFile, not already a pointer |
| `turns_to_recoup_rewrite` | tests / docs | arithmetic for the 183-turn example |

`HIT_RATE_KEEP = 0.5`: last orchestrator call at or above that hit rate
means leftover history is in the cached prefix.

Output is ~200× a cache hit. `[next_effort]` is stored on every final
reply but the wire alias stays pinned on a hot prefix (high→low misses
from token 0). A successful post-turn compact already replaces the input
list, so that is when the stored dial is applied.

Vaulting is **append-only**. The pointer is what is written the first
time, so there is no later middle rewrite of a cache unit. The full body
lives in `cache.tool_vault` (sha256). Re-run the original tool to hydrate;
file reads and URL fetches already have their own caches.

## Related

- `docs/orchestrator.md` — who calls these helpers
- `docs/cache.md` — `tool_vault` table
- `docs/subagents.md` — shared-prefix Agent (the other half of offload)
- `docs/usage.md` — the rates
- `tests/test_cost_efficiency.py`

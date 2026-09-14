# `or_subagents.py` — OpenRouter subagent offload

`g023_code/or_subagents.py` lets the orchestrator hand its `Agent`
(research / planning / todos / task; explore/plan aliases) turns to an
OpenRouter model instead of DeepSeek. The orchestrator keeps DeepSeek for
its own reasoning; the heavy subagent work rides the `OK.dat` key and the
`:free` model tiers, so it costs nothing and does not touch the K.dat quota.
The OpenRouter path has no tools wire — a ListDir listing is injected into
the prompt instead.

## The model file — `OR_SUB_MODELS.json`

Models, priorities, and per-model limits live in `OR_SUB_MODELS.json`. Two
copies exist, like `config.json`:

- **main** — `get_home() / "OR_SUB_MODELS.json"`, next to `OK.dat`, shared by
  every project;
- **project** — `get_project_root() / "OR_SUB_MODELS.json"`, optional; its
  entries override the main file for this project only.

Any change made through `/orsub` (add, remove, re-prioritise, switch primary,
change reasoning) is written to **both** files, so a model added in one
project becomes available in all of them.

Shape:

```json
{
  "primary": "stealth/ox-alpha",
  "models": [
    {
      "id": "stealth/ox-alpha",
      "priority": 1,
      "context_window": 1048576,
      "max_output_tokens": 131072,
      "reasoning_levels": ["low", "medium", "high"],
      "default_reasoning": "high",
      "notes": "Primary. Reasoning is mandatory on this endpoint."
    }
  ]
}
```

- `priority` — fallback order; 1 is tried first after the primary.
- `context_window` / `max_output_tokens` — the endpoint's ceilings, from the
  OpenRouter catalogue. Output budgets are clamped to `max_output_tokens`;
  the context number is informational for the status screen and for sizing
  prompts.
- `reasoning_levels` — the effort values the model accepts (`none` only when
  reasoning can actually be switched off — ox-alpha's endpoint makes it
  mandatory, so its list omits `none`).
- `default_reasoning` — used when the orchestrator does not name one. The
  orchestrator may override reasoning per `Agent` call; `or_sub_chat` clamps
  the request to the model's supported levels.

## Offload flow

- `or_sub_chat(model_id, system, prompt, reasoning, max_output_tokens,
  timeout, key)` — one request to
  `https://openrouter.ai/api/v1/chat/completions`. Hard 429 raises
  `OrRateLimitError`; 400/402/403 raise `RuntimeError` with the status.
- `or_sub_with_failover(system, prompt, reasoning, max_output_tokens,
  timeout)` — tries the primary model first, then the rest in priority
  order, hopping on 429/403/402/400 until one answers. Returns
  `(result, attempts)`; when every model fails it raises one error naming
  every model tried.

The `Agent` tool routes here from `subagents/router.py` whenever
`settings.or_sub_enabled` is on (`/orsub on`). The router builds the same
kind-aware system prompt it uses for DeepSeek, so a Plan vs Explore offload
behaves identically no matter which backend answers.

## `/orsub` command

`cli.cmd_orsub` drives the whole feature:

```
/orsub                    status: backend, primary, models, usage
/orsub on | off           enable/disable the offload
/orsub primary <model>    which model the offload starts with
/orsub add <model>        add a model (limits auto-fetched from the catalogue)
/orsub remove <model>     drop a model
/orsub models             list models, priorities, context, reasoning
/orsub priority <model> <n>   set fallback rank
/orsub reasoning <model> <level>  set default reasoning for a model
/orsub test [model]       run a real offload to check it works
```

`/orsub add` calls `fetch_model_info` against the OpenRouter catalogue
(`/api/v1/models/{id}/endpoints`) to fill in `context_window`,
`max_output_tokens` and `reasoning_levels` automatically — an unknown or
unreachable id is still added with conservative defaults so the command never
brick-walls on the network.

## Usage accounting

`.g023/or_sub_usage.json` tracks requests, rate-limit events, and failover
hops per day (rolling over at midnight, like the vision `or_usage.json`).
The token usage of each successful offload is also folded into the global
usage tracker under the subagent scope, so `/cost` sees it.

## Related

- `docs/openrouter_client.md` — the shared OK.dat key, `OrRateLimitError`,
  the vision failover this module mirrors.
- `docs/subagents.md` — `router.py` routes `Agent` here when enabled.
- `docs/data_files.md` — `OK.dat`, `OR_SUB_MODELS.json`, `or_sub_usage.json`.
- `tests/test_or_subagents.py` — pins the failover and file-management logic.

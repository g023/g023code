# Data files — what sits next to the code

The program is configured by a handful of small files. Two locations
matter: the **machine level** (`G023_HOME`, where the launcher lives)
and the **project level** (the `cwd` you launch from / `G023_PROJECT_ROOT`).

## Machine level (home dir)

| File | Content | Read by |
|------|---------|---------|
| `K.dat` | DeepSeek API key, first line. `K.dat.example` is the template. | `config.load_api_key()` |
| `OK.dat` | OpenRouter API key, first line. | `config.load_openrouter_key()` |
| `config.json` | Global defaults (persisted `Settings` keys). | `config.load_config()` — scope 1 |
| `ds_pricing.json` | DeepSeek price sheet: per-model `regular`/`surge` (`cache_hit`/`cache_miss`/`output` per 1M tokens), with `$schema`, source URL, `effective` date. Canonical model key is `deepseek-flash`; older names are aliases at the same rates. | `usage._load_ds_pricing()` |
| `ds_times.json` | Surge windows in Beijing time + `utc_offset_hours`, with the same metadata. | `config._load_ds_times()` |
| `OR_SUB_MODELS.json` | OpenRouter subagent offload: primary, models, priorities, context windows, reasoning levels. | `or_subagents.load_or_sub_config()` |
| `A_Improve.txt` | Optional communication/reasoning-style markers appended to the standing orchestrator prompt. Missing → a fallback with the four named sections. | `orchestrator._load_a_improve()` |

## Project level

| File | Content | Read by |
|------|---------|---------|
| `.g023/config.json` | Per-project overrides (wins over the machine file). | `config.load_config()` — scope 2 |
| `.g023/cache.db` | SQLite cache (file summaries, vision, web, prefix stats, session_facts). | `cache.Cache` |
| `.g023/backups/` | Pre-write snapshots from WriteFile and Edit. | `tools/registry.backup_before_write`; `/undo` |
| `.g023/sessions/*.jsonl` | Append-only item log (user, output, tool results, compact boundaries, prefix fingerprint). | `session_log.SessionLog`; `/resume` |
| `.g023/logs/{YYYYMMDD_HHMMSS}.log` | Finished-objective handoff: compact summary + working set. | `session_log.write_objective_log`; `--continue` / `/resume log` |
| `.g023/prompts/{n}.txt` | Each user prompt, incrementing. Ctrl+Up recalls the last. | `prompt_store` |
| `.g023/memory/MEMORY.md` | Project notebook. Never injected into instructions. | `memory_notes`; SearchContent overlay |
| `.g023/learn/` | Project-scope learn guides (`INDEX.md`, `guides/`, `ledger.jsonl`). | `learn.py` |
| `~/.g023/learn/` | Env/user-scope learn guides (machine-level, not project). | `learn.py` |
| `.g023/or_usage.json` | OpenRouter daily quota state. | `openrouter_client.OrUsage` |
| `.g023/or_sub_usage.json` | OpenRouter subagent daily usage (requests, rate limits, hops). | `or_subagents.OrSubUsage` |
| `.g023/cookies.json` | FetchUrl cookie jar. | `web_fetch.CookieStore` |
| `.g023/history` | prompt_toolkit input history. | `prompt.Reader` |
| `OR_VISION.json` | Ordered list of OpenRouter vision models to try. | `openrouter_client.load_or_models()` |
| `OR_SUB_MODELS.json` | Project override of the machine-level offload file. | `or_subagents.load_or_sub_config()` |
| `g023.md` | Optional standing project instructions (this program's AGENTS.md). Appended to the system prompt when no compact file exists. | `project_instructions.system_addendum()` |
| `g023_compact.md` | Optional compacted copy of `g023.md`. Wins over `g023.md` when present. Written by `/g023-compact`; the original is left in place. | `project_instructions.system_addendum()` |
| `AGENTS.md` | Optional extra standing instructions, appended when readable (in addition to g023.md / g023_compact.md). | `project_instructions.agents_addendum()` |

## Current example values

**Code defaults** (`Settings`): `orchestrator_model` / `subagent_model` =
`deepseek-flash`, `reasoning_effort` = 75, `verbose` = low,
`auto_compact` = true, `vision_backend` = none, `permission_default` =
ask, `max_iterations` = 255, `max_tokens` = 0, `surge_notify` = true.

The **installer-written** `config.json` is the full `PERSISTED_KEYS`
set matching those defaults (`deepseek-flash`, `reasoning_effort: 75`,
`max_iterations: 255`, `vision_max_image_dim: 800`, backups on). Older
files that still name `deepseek-v4-flash` and `"high"` are mapped onto
those values on load (`MODEL_ALIASES` for the retired ids; `"high"`
parses to 75).

- `ds_pricing.json` — deepseek-flash: regular hit 0.003 / miss 0.15 /
  output 0.60; surge is exactly 2×. Effective 2026-09-10. Older keys
  `deepseek-v4-flash` and `deepseek-v4-flash-vision-exp` share the same
  object.
- `ds_times.json` — peak 09:00–12:00 & 14:00–18:00 Beijing (01:00–04:00 &
  06:00–10:00 UTC), Monday–Friday (`peak_weekdays` 0–4). Weekends are off-peak.
- `OR_VISION.json` — `["google/gemma-4-26b-a4b-it:free"]`.
- `OR_SUB_MODELS.json` — primary `stealth/ox-alpha` with several models,
  their context windows, output ceilings, and reasoning levels.
- `K.dat.example` / `OK.dat` placeholders — the exact strings
  `config.py` recognises as "not a real key".

## Rules for editing these

- JSON must stay valid and type-correct: `config._coerce` drops broken
  keys silently, which is safe but confusing.
- `ds_*.json` may be updated when DeepSeek changes the sheet — the code
  falls back to defaults if a file is missing/unreadable, and the tests
  assert the *relationships* (surge = 2×, windows), not the literal numbers.
- Anything new in `config.json` must be added to `Settings` +
  `PERSISTED_KEYS` or it will be ignored on load.

## Related

- `docs/config.md` — the loader, `Settings`, scopes, coercion.
- `docs/usage.md`, `docs/surge.md` — the two `ds_*.json` consumers.
- `docs/installers.md` — how `K.dat`/`config.json` get created.
- `docs/openrouter_client.md` — `OR_VISION.json` + `or_usage.json`.
- `docs/or_subagents.md` — `OR_SUB_MODELS.json` + `or_sub_usage.json`.
- `docs/session_log.md`, `docs/learn.md`, `docs/memory_notes.md`,
  `docs/prompt_store.md` — the `.g023/` file stores.

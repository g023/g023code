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
| `ds_pricing.json` | DeepSeek price sheet: per-model `regular`/`surge` (`cache_hit`/`cache_miss`/`output` per 1M tokens), with `$schema`, source URL, `effective` date. | `usage._load_ds_pricing()` |
| `ds_times.json` | Surge windows in Beijing time + `utc_offset_hours`, with the same metadata. | `config._load_ds_times()` |

## Project level

| File | Content | Read by |
|------|---------|---------|
| `.g023/config.json` | Per-project overrides (wins over the machine file). | `config.load_config()` — scope 2 |
| `.g023/cache.db` | SQLite cache (file summaries, vision, web, prefix stats, facts). | `cache.Cache` |
| `.g023/backups/` | Pre-write snapshots from WriteFile. | `tools/registry.backup_before_write` |
| `.g023/or_usage.json` | OpenRouter daily quota state. | `openrouter_client.OrUsage` |
| `OR_VISION.json` | Ordered list of OpenRouter vision models to try. | `openrouter_client.load_or_models()` |

## Current example values (repo root)

- `config.json` — the shipped machine defaults: `orchestrator_model`
  / `subagent_model` = `deepseek-v4-flash`, `reasoning_effort` = high,
  `verbose` = mid, `auto_compact` = true, `vision_backend` = deepseek
  with `deepseek-v4-flash-vision-exp`, `permission_default` = ask,
  `max_iterations` = 0 (**unlimited**), `max_tokens` = 0 (**no client
  budget**), `surge_notify` = true, `surge_pause` = false.
- `ds_pricing.json` — deepseek-v4-flash: regular hit 0.007 / miss 0.22 /
  output 0.66; surge is exactly 2×. Effective 2026-08-17.
- `ds_times.json` — peak 09:00–12:00 & 14:00–18:00 Beijing (01:00–04:00 &
  06:00–10:00 UTC).
- `OR_VISION.json` — `["google/gemma-4-26b-a4b-it:free"]`.
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

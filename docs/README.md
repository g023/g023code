# g023 Code — Documentation Index

One page per part of the program. Every module, script, data file, and test
suite has a page here. If you edit a file, refresh its page.

| Part | File | Covers |
|------|------|--------|
| Architecture | [overview.md](overview.md) | module map, session flow, design decisions |
| Entry points & launchers | [entrypoints.md](entrypoints.md) | `__main__.py`, `__init__.py`, `g023.sh`, `g023.bat` |
| REPL / dashboard | [cli.md](cli.md) | `g023_code/cli.py` — the CLI class, main loop, slash handlers |
| Slash command catalogue | [commands.md](commands.md) | `g023_code/commands.py` — the `Command` data |
| Agent loop | [orchestrator.md](orchestrator.md) | `g023_code/orchestrator.py` |
| Reasoning effort | [effort.md](effort.md) | `g023_code/effort.py` — 0–100 scale capped at 75 / `high`, `[handoff]` / `[next_effort]` |
| Project instructions | [project_instructions.md](project_instructions.md) | `g023.md` / `g023_compact.md` / `AGENTS.md` |
| DeepSeek API client | [api.md](api.md) | `g023_code/api.py` |
| Configuration | [config.md](config.md) | `g023_code/config.py` — paths, settings, scopes |
| Path confinement | [paths.md](paths.md) | `g023_code/paths.py` |
| Working set | [working_set.md](working_set.md) | `g023_code/working_set.py` — remaining work, goal judge |
| Session log | [session_log.md](session_log.md) | `g023_code/session_log.py` — JSONL + objective handoffs |
| Learn store | [learn.md](learn.md) | `g023_code/learn.py` — cross-session procedures |
| Memory notes | [memory_notes.md](memory_notes.md) | `g023_code/memory_notes.py` — `.g023/memory/` |
| Prompt store | [prompt_store.md](prompt_store.md) | `g023_code/prompt_store.py` — `.g023/prompts/` |
| Ollama client | [ollama_client.md](ollama_client.md) | `g023_code/ollama_client.py` |
| OpenRouter client | [openrouter_client.md](openrouter_client.md) | `g023_code/openrouter_client.py` |
| OpenRouter subagent offload | [or_subagents.md](or_subagents.md) | `g023_code/or_subagents.py`, `OR_SUB_MODELS.json` |
| SQLite cache | [cache.md](cache.md) | `g023_code/cache.py` |
| Cost policy | [cost_policy.md](cost_policy.md) | `g023_code/cost_policy.py` — keep vs compact vs vault |
| Usage & pricing | [usage.md](usage.md) | `g023_code/usage.py` + `ds_pricing.json` |
| Drift signals | [signals.md](signals.md) | `g023_code/signals.py` |
| Surge pricing | [surge.md](surge.md) | `g023_code/surge.py` + `ds_times.json` |
| Terminal UI | [ui.md](ui.md) | `g023_code/ui.py` |
| Web fetching | [web_fetch.md](web_fetch.md) | `g023_code/web_fetch.py` |
| Input prompt | [prompt.md](prompt.md) | `g023_code/prompt.py` |
| Tools | [tools.md](tools.md) | `g023_code/tools/registry.py`, `tools/schemas.py` |
| Subagents | [subagents.md](subagents.md) | `g023_code/subagents/*` |
| Tests | [tests.md](tests.md) | `tests/` |
| Installers | [installers.md](installers.md) | `installer.sh`, `installer.bat`, `requirements.txt` |
| Data files | [data_files.md](data_files.md) | `K.dat`, `OK.dat`, `config.json`, `OR_*.json`, `ds_*.json`, `.g023/` |

Not documented here: `media/` (has its own `media/README.md`) and
`releases/` (packed archives).

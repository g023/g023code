# g023 Code — Documentation Index

Feature catalog (every feature, architecture, observations, benefits):
[`../FEATURES.md`](../FEATURES.md).

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
| Anthropic wire codec | [anthropic_codec.md](anthropic_codec.md) | `g023_code/anthropic_codec.py` — Responses items ↔ Anthropic messages |
| Configuration | [config.md](config.md) | `g023_code/config.py` — paths, settings, scopes |
| Path confinement | [paths.md](paths.md) | `g023_code/paths.py` |
| Working set | [working_set.md](working_set.md) | `g023_code/working_set.py` — remaining work, goal judge |
| Session log | [session_log.md](session_log.md) | `g023_code/session_log.py` — JSONL + objective handoffs |
| Learn store | [learn.md](learn.md) | `g023_code/learn.py` — cross-session procedures |
| Memory notes | [memory_notes.md](memory_notes.md) | `g023_code/memory_notes.py` — `.g023/memory/` |
| Bulletin board | [bbs.md](bbs.md) | `g023_code/bbs.py` — `.g023/bbs/` |
| Prompt store | [prompt_store.md](prompt_store.md) | `g023_code/prompt_store.py` — `.g023/prompts/` |
| Images (vision input) | [images.md](images.md) | `g023_code/images.py` |
| SQLite cache | [cache.md](cache.md) | `g023_code/cache.py` |
| Checkpoint | [checkpoint.md](checkpoint.md) | `g023_code/checkpoint.py` — bounded fold, signed handoffs, rehydration |
| Cost policy | [cost_policy.md](cost_policy.md) | `g023_code/cost_policy.py` — keep vs compact vs vault |
| Compaction economics | [economics.md](economics.md) | `g023_code/economics.py` — ski-rental compaction timing |
| Tool-result codec | [tool_codec.md](tool_codec.md) | `g023_code/tool_codec.py` — densest lossless form before history |
| Edit engine | [edit_engine.md](edit_engine.md) | `g023_code/edit_engine.py` — match ladder, byte-faithful atomic writes |
| View deltas | [views.md](views.md) | `g023_code/views.py` — what a Bash call changed in files the model has seen |
| Failure attribution | [attribution.md](attribution.md) | `g023_code/attribution.py` — new vs pre-existing failures |
| Predicated finish | [finish.md](finish.md) | `g023_code/finish.py` — answer on the final step, delivered only on success (opt-in) |
| Near-miss names | [near_miss.md](near_miss.md) | `g023_code/near_miss.py` — zero-hit search suggests the real spelling |
| Document outlines | [docindex.md](docindex.md) | `g023_code/docindex.py` — heading induction, BM25, folding |
| Shape digests | [shapes.md](shapes.md) | `g023_code/shapes.py` — name templates, binary sniff/decode, log templates, table/JSON shapes |
| Directory digests | [dirmap.md](dirmap.md) | `g023_code/dirmap.py` — templated, bounded ListDir for huge directories |
| Exploration ledger | [ledger.md](ledger.md) | `g023_code/ledger.py` — opt-in session wiki across compaction (A/B: no win by default) |
| Usage & pricing | [usage.md](usage.md) | `g023_code/usage.py` + `ds_pricing.json` |
| Drift signals | [signals.md](signals.md) | `g023_code/signals.py` |
| Surge pricing | [surge.md](surge.md) | `g023_code/surge.py` + `ds_times.json` |
| Salient excerpts & readable vault | [salience.md](salience.md) | `g023_code/salience.py` |
| Optional local model (llama.cpp) | [local_model.md](local_model.md) | `g023_code/local_llm.py`, `g023_code/local_ask.py` |
| Terminal UI | [ui.md](ui.md) | `g023_code/ui.py` |
| Web search + fetch | [web.md](web.md) | `g023_code/web.py` |
| Input prompt | [prompt.md](prompt.md) | `g023_code/prompt.py` |
| Tools | [tools.md](tools.md) | `g023_code/tools/registry.py`, `tools/schemas.py` |
| Subagents | [subagents.md](subagents.md) | `g023_code/subagents/*` |
| Code ownership (`/ownership`) | [ownership.md](ownership.md) | `g023_code/ownership.py` |
| Tests | [tests.md](tests.md) | `tests/` |
| Installers | [installers.md](installers.md) | `installer.sh`, `installer.bat`, `requirements.txt` |
| Data files | [data_files.md](data_files.md) | `K.dat`, `config.json`, `ds_*.json`, `.g023/` |

Not documented here: `media/` (has its own `media/README.md`). Release
history is in `CHANGELOG.md` at the project root.

# Overview — how g023 Code fits together

g023 Code is a pure-Python, DeepSeek-V4-powered terminal coding agent.
Architecture is inspired by Claude Code, with a **strict Subagent-First
design**: the orchestrator model never reads whole files into its context —
heavy data work is delegated to isolated subagents that return compact
summaries.

## Module map

```
g023.sh / g023.bat          launchers (set G023_HOME, G023_PROJECT_ROOT)
installer.sh / installer.bat  one-shot setup (venv, deps, K.dat, config.json)
        │
g023_code/__main__.py       python -m g023_code  →  cli.main()
g023_code/cli.py            CLI class: banner, slash commands, main REPL loop
        │  reads commands catalogue (commands.py), asks the user (prompt.py)
        ▼
g023_code/orchestrator.py   Orchestrator: the per-turn agent loop
        │  talks to DeepSeek through
        ▼
g023_code/api.py            ResponsesClient (create / stream / vision)
        │
        ├── server-side tools (web_search)  — offered natively by the API
        ├── heavy tools → g023_code/subagents/router.py
        │        ├── file_reader.py   ReadFile → JSON summary
        │        ├── searcher.py      SearchContent → JSON matches
        │        ├── vision.py        AnalyzeImage → ollama | deepseek | openrouter
        │        └── _run_agent       Agent (Explore / Plan) → Flash model call
        └── light tools → g023_code/tools/registry.py
                 ├── Bash, WriteFile, ListDir, FetchUrl (+ permission checks)
                 └── schemas.py  (the tool definitions shown to the model)
```

Supporting modules (used by all of the above):

| Module | Responsibility |
|--------|----------------|
| `config.py` | paths, `Settings` dataclass, API keys, persisted config scopes |
| `cache.py` | per-project SQLite cache: file summaries, vision, web fetches, prefix stats, facts |
| `usage.py` | token/cost accounting, per-model pricing (from `ds_pricing.json`) |
| `signals.py` | drift signals — quiet failure detection (unknown types, empty replies, hit-rate drops) |
| `surge.py` | DeepSeek peak/off-peak pricing windows (from `ds_times.json`) |
| `ui.py` | every pixel: rich console, glyphs, panels, gauges, activity printer |
| `prompt.py` | user input: prompt_toolkit reader with completion + fallbacks |
| `web_fetch.py` | the FetchUrl engine: browser-like HTTP, HTML→text extraction |
| `ollama_client.py` | local Ollama host: probe, models, image load, vision chat |
| `openrouter_client.py` | OpenRouter vision with model failover and daily quota |

## One session, end to end

1. `python -m g023_code` → `__main__` → `cli.main()` → `CLI.loop()`.
2. The API key is loaded from `K.dat` (machine-level, next to the launcher).
3. The banner prints; a surge monitor starts (notifies on peak/off-peak
   transitions, optionally pauses input around peak hours).
4. The REPL loop reads a line. Slash lines go to the command handlers;
   anything else becomes a prompt.
5. `run_prompt` calls `Orchestrator.run_turn`, which:
   - auto-compacts if context ≥ threshold,
   - iterates: model call → parse reply → record usage/signals → run tool
     calls (subagent or registry) → repeat until the model answers (or the
     step cap trips),
   - streams the answer and prints a per-turn summary.
6. Every model call is metered by `usage.py`; the context gauge and `/cost`
   read from it. The cache keeps file summaries, vision answers, and web
   fetches across sessions.

## The ideas the code is built around

- **Context is currency, subagents are the treasury.** ReadFile returns a
  summary, never a raw dump; escalation is a deliberate, exact re-read by
  line range. `G023_READFILE_RAW` flips this off for baseline measurements.
- **The client must not invent limits.** `max_iterations` and `max_tokens`
  default to "no client-side cap" (0); what replaces a ceiling is
  visibility (iteration notices, `/signals`, Ctrl-C).
- **History integrity is a hard invariant.** Responses items must keep
  `function_call` ⇄ `function_call_output` pairs intact; a dangling pair
  kills every following turn. The orchestrator repairs/rolls back rather
  than leave a broken history (see `orchestrator.md`).
- **Quiet failure is the real failure.** `api.py` and `signals.py` detect
  "something moved" (unknown item types, empty output, hit-rate drops)
  instead of crashing on schema drift.
- **Freshness policy belongs to the user.** The web cache stores
  everything and never evicts by age; the caller decides what is fresh.

See also: the user-facing `../README.md` (35 KB, everything the operator
needs) and `../SPEC.md` (60 KB, the design rationale and call-accounting
tables).

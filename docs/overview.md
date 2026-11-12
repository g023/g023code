# Overview — how g023 Code fits together

g023 Code is a pure-Python, DeepSeek-powered terminal coding agent.
Architecture is inspired by Claude Code. Since 2026-11-07 it runs
**single-agent by default**: the orchestrator still never reads whole files
into its context — local helpers (structural index, local focus ranking,
grep) return compact summaries without a model call; model-backed subagents
(`Agent`, LLM focus) are opt-in with `/subagents on`.

There is one model: **DeepSeek V4.1 Flash** (`config.MODEL =
"deepseek-flash"`), used for the orchestrator, FileReader's model path, the
Agent nested loop, `/g023-compact` and vision. It is a constant, not a setting.
There is one wire: `POST /anthropic/v1/messages`, translated by
`anthropic_codec.py`. `/model` shows the limits and prices.

## Module map

```
g023.sh / g023.bat          launchers (set G023_HOME, G023_PROJECT_ROOT)
installer.sh / installer.bat  one-shot setup (venv, deps, K.dat, config.json)
        │
g023_code/__main__.py       python -m g023_code  →  cli.main() (--help / --continue / one-shot / REPL)
g023_code/cli.py            CLI class: banner, slash commands, main REPL loop
        │  reads commands catalogue (commands.py), asks the user (prompt.py)
        ▼
g023_code/orchestrator.py   Orchestrator: the per-turn agent loop
        │  talks to DeepSeek through
        ▼
g023_code/api.py            DeepSeekClient (create / stream / vision; one model, one wire)
g023_code/anthropic_codec.py  internal items <-> Anthropic messages (deterministic)
        │
        ├── heavy tools → g023_code/subagents/router.py
        │        ├── file_reader.py   ReadFile → JSON summary
        │        ├── searcher.py      SearchContent / Glob → JSON (rg | grep | walk)
        │        ├── vision.py        AnalyzeImage → images.py → DeepSeekClient.vision
        │        └── _run_agent       Agent (research / planning / todos / task;
        │                             explore/plan aliases) → isolated tool loop
        └── light tools → g023_code/tools/registry.py
                 ├── Bash, WriteFile, Edit, ListDir, Glob, TodoWrite,
                 │   WebSearch, FetchUrl, LearnSearch, LearnGet, LearnPut, SessionSearch
                 └── schemas.py  (the tool definitions shown to the model)
```

Supporting modules (used by all of the above):

| Module | Responsibility |
|--------|----------------|
| `config.py` | paths, `Settings` dataclass, API keys, persisted config scopes |
| `paths.py` | resolve a tool path and refuse anything outside the project root |
| `cache.py` | per-project SQLite: file summaries, vision, web fetches, tool vault, prefix stats, session_facts |
| `cost_policy.py` | cache-aware keep vs compact vs vault (hit $0.003/M vs miss $0.15/M) |
| `usage.py` | token/cost accounting, per-model pricing (from `ds_pricing.json`) |
| `signals.py` | drift signals — quiet failure detection (unknown types, empty replies, hit-rate drops) |
| `surge.py` | DeepSeek peak/off-peak pricing windows (from `ds_times.json`) |
| `effort.py` | 0–100 effort ↔ wire aliases, capped at 75 / `high`; strip `[handoff]` / `[next_effort]` trailers |
| `working_set.py` | remaining-work object in session_facts; goal-completion judge |
| `session_log.py` | append-only `.g023/sessions/*.jsonl` and `.g023/logs/` handoffs |
| `learn.py` | cross-session procedural guides (`~/.g023/learn`, `<project>/.g023/learn`) |
| `memory_notes.py` | `.g023/memory/MEMORY.md` notebook (never injected into instructions) |
| `bbs.py` | `.g023/bbs/` bulletin board (never injected; SearchContent overlay) |
| `prompt_store.py` | `.g023/prompts/{n}.txt`; Ctrl+Up recalls the last one |
| `ui.py` | every pixel: rich console, glyphs, panels, gauges, activity printer |
| `prompt.py` | user input: prompt_toolkit reader with completion + fallbacks |
| `web.py` | WebSearch/FetchUrl: DeepSeek Search/Fetch APIs, machine-wide cache, page focus |
| `images.py` | load an image (project path or URL), sniff its media type, cap it at 800 px |
| `project_instructions.py` | `g023.md` / `g023_compact.md` plus a project-root `AGENTS.md` |

## One session, end to end

1. `python -m g023_code` → `__main__` → `cli.main()`: `--help` prints
   the catalogue and exits; `--version` prints the version; `--continue`
   (`-c`) seeds from the latest `.g023/logs/` handoff (else JSONL);
   remaining argv is one prompt; no argv starts `CLI.loop()`.
2. The API key is loaded from `K.dat` (machine-level, next to the launcher).
3. The banner prints; a surge monitor starts (notifies on peak/off-peak
   transitions, optionally pauses input around peak hours).
4. The REPL loop reads a line. Slash lines go to the command handlers;
   anything else becomes a prompt.
5. `run_prompt` calls `Orchestrator.run_turn`, which:
   - writes the user text to `.g023/prompts/{n}.txt`,
   - sends `g023_compact.md` (else `g023.md`) plus a project-root
     `AGENTS.md` when readable as a system-prompt addendum,
   - auto-compacts if context ≥ threshold (Layer 1 blanks stale tool
     results every turn; Layer 3 summarises at the threshold),
   - iterates: model call → parse reply → record usage/signals → run tool
     calls (subagent or registry, some ALLOW reads in parallel) → repeat
     until the model answers (or the step cap trips),
   - strips `[handoff]` / `[next_effort: N]` from the visible answer,
   - post-turn compact replaces the transcript with a summary when the
     working-set judge says the objective is complete or leftover history
     is large; a short reply or a small tool turn does not buy an extra
     model call. Prefer a `[handoff]` block on the same final answer over
     a second Flash call; otherwise fold an existing summary locally. The
     summary is also written to `.g023/logs/{dstamp_tstamp}.log` so
     `--continue` / `/resume log` can start the next orchestration from
     system prompt + that handoff,
   - streams the answer and prints a per-turn summary.
6. Every model call is metered by `usage.py`; the context gauge and `/cost`
   read from it. The cache keeps file summaries, vision answers, web
   fetches, and the working set across sessions. Learn guides and memory
   notes and the bulletin board are files, not SQLite.

## The ideas the code is built around

- **Context is currency, subagents are the treasury.** ReadFile returns a
  summary, never a raw dump; escalation is a deliberate, exact re-read by
  line range, or `raw=true` / Edit for a unique span. `G023_READFILE_RAW`
  flips this off for baseline measurements.
- **The client must not invent limits.** `max_tokens` defaults to "no
  client-side cap" (0). `max_iterations` defaults to 255, a loose backstop
  against a turn that calls tools forever (`/steps off` sets 0 = no cap).
  What replaces a tight ceiling is visibility (iteration notices,
  `/signals`, Ctrl-C).
- **History integrity is a hard invariant.** Internal items must keep
  `function_call` ⇄ `function_call_output` pairs intact; a dangling pair
  kills every following turn. A user-role working-set item must not land
  between a parallel tool batch and its outputs — that is also a 400.
  The orchestrator repairs/rolls back/reseats rather than leave a broken
  history (see `orchestrator.md`).
- **Prefix stability is a hard invariant.** Standing `instructions` and
  the tool list are frozen until the user changes vision. The working-set
  item is never rewritten in place. Compact may start a *new* prefix.
- **A hot prefix is cheaper to keep than to shrink.** Cache hits are ~50×
  cheaper than misses. History editors (Layer 1, post-turn replace) do not
  rewrite a prefix that is already hitting unless the window is full or
  the leftover is uncached. Bulky tool results are vaulted at append time
  so they never enter a cache unit as raw text. Nested Agent calls reuse
  the frozen instructions+tools+effort so they hit the same standing units.
- **Quiet failure is the real failure.** `api.py` and `signals.py` detect
  "something moved" (unknown item types, empty output, hit-rate drops)
  instead of crashing on schema drift.
- **Freshness policy belongs to the user.** The web cache stores
  everything and never evicts by age; the caller decides what is fresh.
- **Learned procedures are retrieved, not prepended.** The learn store is
  searched on demand so `/cache clear` and a growing INDEX cannot bust
  the prompt-cache prefix.

See also: the user-facing `../README.md` and the per-module pages in this
folder. Pricing lives in `../ds_pricing.json` (documented in `usage.md`).

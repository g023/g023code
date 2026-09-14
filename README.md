<div align="center">

# g023 Code

**A pure-Python AI coding agent for your terminal, powered by DeepSeek V4.**

*Subagent-First · Context is Currency · Terminal-native*

[![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-1.3.4-informational)](releases/1.3.4/RELEASE_NOTES.md)
[![License](https://img.shields.io/badge/license-MIT-green)](#license)
[![Model](https://img.shields.io/badge/model-DeepSeek%20V4-8A2BE2)](https://platform.deepseek.com/)

</div>

---

g023 Code reads your project, searches it, searches the web, runs commands,
fetches pages, and looks at images — while telling you exactly what it is doing
and what it just cost. Think of the orchestrator as a miser with a gold card:
it will spend, but it wants the receipt, and it would rather send a specialist
than haul the whole warehouse onto the table.

Data-heavy work is delegated by default. A subagent reads the file in an
isolated context and what comes back is a structural summary plus a
symbol → line-range map. When the summary is not enough, the orchestrator
asks for an exact line range and gets verbatim source — so "summary first,
raw on request" is the design, not "never raw".

That trade is a real trade, and it is worth being precise about which half of
it is measured. The call counts below are asserted by the test suite. Whether
delegation is *cheaper overall* is not: it depends on how many turns the
file's text would otherwise have sat in context, which no test here can know.
See [break-even](#what-delegation-costs) for the arithmetic and
[what we measured](#what-we-measured) for the numbers that *are* pinned.

It talks to DeepSeek exclusively through the **Responses API**, which is what
makes DeepSeek's own **native web search** a first-class tool of the loop
rather than a search provider bolted on the side. One model, one endpoint,
one price sheet — `deepseek-flash` on `POST /responses`.

<p align="center">
  <img src="media/screenshots/01-startup-banner.png" alt="g023 Code starting up and working through a request" width="880">
</p>

<p align="center"><sub>Startup banner, live tool trace, and a permission prompt before anything runs a command. Captured on 1.3.4 from sanitised paths — see <a href="media/README.md">media/README.md</a>.</sub></p>

## Table of contents

- [Quick start](#quick-start)
- [What you see while it runs](#what-you-see-while-it-runs)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [What we measured](#what-we-measured)
- [What delegation costs](#what-delegation-costs)
- [Drift signals](#drift-signals)
- [Project instructions (`g023.md`)](#project-instructions-g023md)
- [Slash commands](#slash-commands)
- [Verbosity, context & cost](#verbosity-context--cost)
- [Web search (native)](#web-search-native)
- [Vision](#vision)
- [Fetching web pages](#fetching-web-pages)
- [Design principles](#design-principles)
- [Tests](#tests)
- [Extending](#extending)
- [License](#license)

## Quick start

**Requirements:** Python 3.11 or newer, and a [DeepSeek API key](https://platform.deepseek.com/).

Drop the folder anywhere you like and run the installer inside it:

```bash
cd /path/to/g023-code
./installer.sh
```

<details>
<summary><b>Windows</b></summary>

```bat
cd C:\path\to\g023-code
installer.bat
```

</details>

It finds a Python 3.11+, builds a `.venv` in the folder, installs only the
dependencies you are actually missing, asks once for your API key, writes a
default `config.json`, and puts `g023` on your `PATH`. Every step checks what is
already true first, so running it again is safe — it is also how you repair a
half-finished setup or add the optional extras later.

<details>
<summary><b>Installer options</b></summary>

| Linux / macOS | Windows | Effect |
|---|---|---|
| `--yes` | `/y` | Non-interactive; take the recommended default everywhere |
| `--key sk-…` | `/key sk-…` | Write the key into `K.dat` instead of prompting (`$DEEPSEEK_API_KEY` is used if set) |
| `--with-optional` / `--no-optional` | `/optional` / `/nooptional` | Decide the extras (vision preprocessing, browser-grade fetching) up front |
| `--no-venv` | — | Install into the current interpreter instead of a venv |
| `--no-path` | `/nopath` | Do not touch `~/.local/bin`, your shell rc, or the user `PATH` |
| `--uninstall` | `/uninstall` | Remove the launcher shim and `PATH` entry (the folder, `.venv`, `K.dat` and `config.json` stay) |

</details>

Then launch from whatever project you want it to work on:

```bash
cd ~/some/project
g023
```

Doing it by hand instead is four commands — `pip install -r requirements.txt`,
your key on the first line of `K.dat`, then `/path/to/g023-code/g023.sh` (or
`g023.bat`) from a project folder. The launchers prefer `.venv/` in the install
folder when one exists, and set two environment variables for you:

| Variable | Meaning |
|---|---|
| `G023_HOME` | The installation folder — where `K.dat` and `config.json` live |
| `G023_PROJECT_ROOT` | The directory you launched from — your project |

A per-project `.g023/` folder holds the SQLite cache, your input history,
`backups/` (every WriteFile/Edit overwrite snapshots the previous version,
mirroring the file tree, newest few kept per file), `sessions/` (append-only
JSONL), `logs/` (objective handoffs for `--continue`), `prompts/` (each user
prompt, recalled with Ctrl+Up), and optional `memory/` / `learn/` stores.

`prompt_toolkit` is listed as a dependency but is not strictly required: with it
you get tab completion, persistent history, a live status bar pinned to the
bottom of the terminal, and Ctrl+Up to recall the last stored prompt from
`.g023/prompts/`. Without it the input line falls back to a plain prompt
and everything else behaves identically.

## What you see while it runs

The point is to never wonder what it is doing, or what it just cost.

<p align="center">
  <img src="media/screenshots/04-status.png" alt="The /status dashboard: model, vision, project path, context gauge, and spend" width="880">
</p>

Tool traces say what *happened* (`708 lines, 9 classes, cached`), not what was
sent. The status line after every turn carries the numbers that only mean
something together — context used, **cache hit rate**, cost of this turn, cost
of the session — because on V4 Flash a cached input token is billed at $0.003/M
off-peak against $0.15/M for an uncached one, and both double during surge
hours (peak times are 09:00–12:00 & 14:00–18:00 Beijing time, Monday–Friday). The hit/miss
gap is DeepSeek's price sheet, not something this harness achieves; what the
harness does is avoid breaking the prefix so more tokens fall on the cheaper
side.

Costs shown are computed locally from the token counts the API returns and the
price table in [`ds_pricing.json`](ds_pricing.json) (see [`docs/usage.md`](docs/usage.md)).
They are an estimate of your bill, not a reading of it.

Anything you type that does not start with `/` is a prompt. Everything else is
a slash command.

## Configuration

`/settings` shows every setting in one place and marks which ones persist;
`/tools` shows what the agent may do and what it has done this turn. Both are
editable from inside the session, and the `·saved` keys are written to
`config.json` next to `K.dat` — the **global** default that follows you across
projects — or, if the project has one, to the project's `.g023/config.json`.

### Per-project overrides and the global default

Settings live in two layers:

- **Global** — `config.json` next to `K.dat`. Applies to every project.
- **Project** — `.g023/config.json` in the project folder. When present it
  overrides the global file for *this* project only.

```
/config              show both files and which scope is active
/config global       promote the current settings to the global default
                     (new projects inherit them)
/config save project pin the current settings to this project
/config save         save to the active scope
/config clear        drop this project's override; global applies again
```

So you can tune settings while inside a project, then run `/config global` to
make exactly those settings the default for every future project — even when a
project file would otherwise mask them.

<p align="center">
  <img src="media/screenshots/03-tools-and-permissions.png" alt="The /tools panel: permission level and subagent isolation for every tool" width="430">
  <img src="media/screenshots/14-settings.png" alt="The /settings panel: every setting with its live value" width="430">
</p>

Defaults out of the box: **`deepseek-flash`** for the orchestrator and
subagents, thinking on at effort **75** (wire alias `high`), verbosity
**low**, auto-compaction **on** at 85% of the window (both numbers settable
— `/max-ctx`, `/compact-threshold`), acting **tools** set to **ask**, and
**vision disabled**. Nothing that writes, runs, or leaves your machine
happens without a prompt.

## Architecture

Everything — orchestrator, subagents, compaction — goes to a single endpoint:
`POST /responses`. That is a deliberate choice, not an implementation detail. It
is the only DeepSeek endpoint that exposes the server-side `web_search` tool, so
using it everywhere is what lets web search be part of the loop.

The Responses API does not take a list of chat messages. It takes **items** —
and the model's own output items go back into the next request *verbatim*:

- `message` items (your input; the model's narration and its final answer)
- `reasoning` items — echoed back unmodified so the model keeps its own chain
- `web_search_call` items — the server's record of a search it ran
- `function_call` / `function_call_output` pairs, one per tool call

Two rules fall out of that, and every part of the program respects them. A
`function_call` with no matching output is a hard 400 in either direction, so
compaction, truncation and error recovery all repair pairing before the next
request. And items are echoed byte-identical, because rewriting them both breaks
the prefix cache and desyncs the model's reasoning.

- **Orchestrator** (`deepseek-flash`) keeps only high-level reasoning, tool
  schemas, and compact summaries.
- **Subagents** handle data-heavy work in isolated contexts:
  - `FileReader` → local structural index + symbol → line-range map (zero API
    calls unless a *semantic* `focus` hint asks Flash; an exact / unique-suffix
    symbol name is local too); cached by content hash
  - `Searcher` → metadata-first grep (`rg` if present, else `grep`, else a
    Python walk) — **no API call**. Total occurrence count is reported
    separately from a sample list.
  - `Agent` → isolated reasoning: `research` / `planning` / `todos` / `task`
    (`explore`/`plan` still work as aliases). Task workers can Edit/Write/Bash;
    research/planning are read-only.
  - `Vision` → DeepSeek (`deepseek-flash`), OpenRouter, or an Ollama daemon
    (local or remote). The orchestrator itself is text-only.
- **Native `web_search`** runs server-side inside the model's turn — no client
  executor, no provider chain
- **Lightweight tools** on the orchestrator: Bash, WriteFile, **Edit**
  (unique-span replace, fails closed), ListDir, **Glob**, **TodoWrite**
  (working-set leaves), FetchUrl, LearnSearch/Get/Put, SessionSearch
- **SQLite cache** → a repeated read of unchanged bytes is a local lookup and
  costs no API call
- **Prefix-cache-friendly**: instructions and tool definitions stay frozen
  until you change vision. Effort and temperature are pinned to that prefix
  too. The working-set item is never rewritten in place. A hot prefix
  (last call mostly cache hits) is kept: rewriting it to "save tokens"
  turns hits into misses and does not recoup (~294 turns to break even on
  blanking 500 cached tokens). Nested Agent calls reuse that frozen prefix
  so they hit it too. Bulky Bash/FetchUrl/ListDir output is stored by hash
  and replaced with a pointer *before* it enters history.
- **Project-root confined**: ReadFile, ListDir, SearchContent, Glob, Edit,
  WriteFile, and `/undo` refuse paths that resolve outside the launch directory.
- **Learn store**: reusable procedures live in `~/.g023/learn` and
  `<project>/.g023/learn`. They are searched on demand (`LearnSearch` /
  `/learn`), never prepended to the system prompt.
- **Thinking mode** on a 0–100 dial (75 is the default). `/responses` rejects
  integers, so the client maps onto `none` / `minimal` / `low` / `medium` /
  `high` / `xhigh` / `max`. Off is `reasoning: {"effort": "none"}` — the only
  thing that actually disables thinking.

**One model.** `deepseek-flash` is the only model sent to the DeepSeek API, in
every role. `/model` still accepts the alias `flash`. It refuses
`deepseek-v4-pro` because calls to it on `/responses` returned an error when
this was last checked, and refusing with that reason attached beats handing you
a model every call fails on. If DeepSeek has since enabled it, that refusal is
stale — it is a hardcoded list in `config.py`, not a capability probe. Vision
on the DeepSeek backend uses the same `deepseek-flash` id; Ollama and
OpenRouter are the other backends, and they do not use the DeepSeek API.

The presentation and command layers are deliberately separate from the agent:

| Module | Responsibility |
|---|---|
| `commands.py` | The command catalogue as *data* — one source of truth shared by `/help`, tab completion, and "did you mean". A command cannot be completable but undocumented. |
| `ui.py` | Theme, glyphs, gauges, and the compact renderings of tool calls and their results. Falls back to ASCII on terminals that cannot draw box characters. |
| `prompt.py` | The input line and the shared numbered picker. Degrades cleanly without `prompt_toolkit`. |
| `ollama_client.py` | Host resolution, reachability probes, model discovery, and vision inference against any daemon. |

See [`docs/`](docs/README.md) for the per-module specification.

## What we measured

A handful of things that look like opinions from the outside and are
actually pinned by a test, a live probe, or DeepSeek's published sheet.
Dated 2026-09-10 (price sheet) and 2026-09-14 (the probes). The suite
that holds this to account is **421 tests**, all offline.

**The price sheet, off-peak Flash (effective 2026-09-10).** Cache hit
$0.003/M, cache miss $0.15/M (~50×), output $0.60/M (~200× a hit). Peak
is exactly 2×, **Monday–Friday only**. Weekend hours that *look* like
Beijing 09:00–12:00 / 14:00–18:00 are off-peak — billing them as surge
was a real overcharge, and `ds_times.json` now carries `peak_weekdays`.

**Keeping a hot prefix beats shrinking it.** DeepSeek hits only on a
full prefix-unit match from token 0. Blanking 500 cached tokens to
"save" them busts a ~3k-token tail: recoup is ~294 further turns.
Coding sessions do not last 294 turns. So the shipped policy
(`cost_policy.py`) is: do not rewrite a cached prefix unless the window
is actually full, the leftover is uncached, or the objective is done.
Layer-1 auto-micro of already-sent items waits for window pressure.
Bulky Bash / FetchUrl / ListDir bodies are vaulted *before* they enter
a cache unit — append-only pointers, not a later middle rewrite.

**Output is the remaining bill once the prefix is hot.** Reasoning
tokens are billed as output. Effort is pinned to the standing prefix
(changing it mid-stream is a cache miss); a stored `[next_effort: N]`
lands on the next request after `/thinking`, `/goal`, `/clear`, or a
post-turn compact that already replaced the input list. Temperature is
pinned the same way — it is a `/responses` cache-key field.

**FileReader buys Flash only for a semantic question.** Unfocused
reads of any language are a local index (AST for Python, structure for
JS/TS/Go/Rust/JSON/Markdown/…). A focus that is already a symbol in
that map — or a unique method suffix — is local too: 0 API calls, line
span returned. A sentence like "what does method_one do" still costs 1
Flash call, so the hint is honoured. Tests pin 0 vs 1.

**Nested Agent workers reuse the parent's frozen prefix.** Children
used to miss from token 0 on a different system prompt and a subset
tool list. The orchestrator now publishes its frozen instructions,
tools, and pinned effort; nested `create()` reuses them; the worker
brief moves into the first user item. Execution still refuses tools
the kind does not allow. Depth-2 was measured and not shipped: a
grandchild is another miss plus another hop of output tokens.

**Two things we tried and rejected, with numbers.** Sharing the parent
prefix for *focused FileReader* looked tempting. FileReader
instructions are 802 chars (miss ≈ $0.000030); the parent prefix is
5267 chars (miss ≈ $0.000197) plus high effort on a none-thinking
extract. Not worth it. And do not slice ReadFile / Agent JSON at 6000
chars to "save context": a 10505-char `orchestrator.py` summary became
an unparseable 5590-char stump and the symbol map vanished. Vault
already offloads Bash/ListDir/…; the compact form stays intact.

**Not measured, and labelled as such.** Nobody has run a controlled
A/B of end-to-end session cost. `G023_READFILE_RAW=1` is the flag;
`/cost` is the meter. Until someone does that on their own codebase,
the honest claim is smaller context, not a cheaper bill.

## What delegation costs

Delegating a read is not free — it is a second API call, and sometimes it is the
wrong trade. Here is what is actually true, separated by how well it is known.

**Measured.** These call counts are asserted in `tests/test_call_accounting.py`,
so they are checked rather than remembered:

| Operation | API calls |
|---|---|
| `SearchContent`, any size | 0 — `rg` / `grep` / Python walk, no API call |
| `ReadFile`, unfocused (any language) | 0 — local index (AST for Python, structure for JS/TS/Go/Rust/JSON/Markdown/…) |
| `ReadFile`, same bytes again (cache hit) | 0 |
| `ReadFile` with `start_line`/`end_line` | 0 — returns verbatim source |
| `ReadFile` with `raw=true` | 0 — returns the whole file verbatim (the FileReader never needs the API for this) |
| `ReadFile` with a `focus` that is an exact / unique-suffix symbol | 0 — looked up in the local map |
| `ReadFile` with a semantic `focus` hint | 1 — Flash honours the question |

**Arithmetic, given assumptions.** A 40 KB file is roughly 10k tokens. Inlined
into the orchestrator, it is re-sent on every subsequent turn — cheaply, if it
stays in the cached prefix, at $0.003/M (regular rate). An unfocused ReadFile
indexes it locally (zero API calls) and leaves a few hundred tokens of
structure in context instead of 10k. An exact-symbol focus is the same local
lookup. A *semantic* focused ReadFile still costs one Flash call (~10k in,
~300 out ≈ $0.0017 at regular miss rates). Which wins depends on how many
turns follow and whether the prefix survives them. **Delegation is not
unconditionally cheaper**; it is unconditionally *smaller*, and smaller
context is what keeps the model on task and the window from filling.

**Not measured.** Nobody has run a controlled A/B of end-to-end session cost.
The flag to do it exists:

```bash
G023_READFILE_RAW=1 g023      # ReadFile returns raw content, no subagent
```

With it set, whole-file reads return the file's bytes and cost zero API calls —
the baseline a plain agent loop would have. Run the same task script with and
without it and compare `/cost`. Until someone does that on their own codebase,
the honest claim is the one above: fewer tokens in the window; an extra call
only when a focus hint asks the model to read for a specific question.

**When the summary is not enough.** The known weakness of summary-first is that
a summary can silently drop the one detail that mattered, and nothing detects
that. What exists instead is a cheap way out: every Python summary carries

```json
"symbols": {"Orchestrator.run_turn": [412, 498], "parse_reply": [199, 262]}
```

with line numbers taken from the AST (decorators included), not from the model.
Escalating is then one targeted `ReadFile` with those two numbers, which returns
verbatim text and costs no API call. That turns "the summary was too thin" from
a re-read of the whole file into a precise second look. It does not turn it into
something the orchestrator notices automatically — that detector does not exist.

**For editing, a summary is never enough.** A summary omits code bodies, and
`WriteFile` *replaces the entire file* — so a write reconstructed from a summary
silently truncates the file. That is exactly how `config.py` got truncated once.
Three safeguards now cover it:

- Prefer **Edit**: `old_string` / `new_string` on a unique span. If the span is
  missing or not unique, the tool refuses and writes nothing.
- `ReadFile raw=true` returns the **whole file verbatim** (capped at 200k chars)
  when a full rewrite really is required. The tool schema and the system prompt
  both say to hold exact bytes before WriteFile.
- Every WriteFile/Edit overwrite first snapshots the previous version to
  `.g023/backups/<path-to-file>/<timestamp>-<hash>.bak`, keeping the newest few
  per file. `/undo` copies the newest snapshot back.

## Drift signals

The client does not validate responses on purpose: an unknown field has to reach
the model without a client release. The price is that a *renamed* field fails
quietly — `output_text` returns `""`, the model looks silent, and nothing
raises. Silent degradation, not a crash, is the realistic worst case here.

`/signals` (alias `/drift`) shows the three cheapest observations that would move
first:

| Signal | What it catches | What it cannot tell you |
|---|---|---|
| Unknown item `type` in a response | An additive API change, at the earliest moment it is visible | Whether it matters |
| Empty output with no `incomplete_reason` | What a renamed content field looks like from outside | Whether the model simply said nothing |
| Prefix hit rate diffed against previous days | Anything that started breaking the prefix | Whether the cause is your prompt, the tool list, or the server |

The hit rate is stored per day in `.g023/cache.db` and compared against the
trailing baseline, so the comparison survives restarts. It needs two prior days
before it will call anything a baseline, and says so until then. `/cache clear`
does not erase it — it is a record of what happened, not cached data.

None of these is a diagnosis. A model behaviour change, a schema change, and
drift in your own prompts all present identically from here: same call, worse
output, no error. These signals make the change visible and date it; they do not
separate the causes.

## Project instructions (`g023.md`)

Drop a `g023.md` in the project root — the same role `AGENTS.md` plays in other
agentic CLIs — and it is appended to the system prompt. `/g023-compact` writes a
denser `g023_compact.md` from it (`deepseek-flash`, reasoning off) and leaves
the original in place. If the compact file exists, that is what the prompt uses;
delete it to fall back to `g023.md`. A project-root `AGENTS.md`, when readable,
is appended as well. None of the files is required.

## Slash commands

Commands that take a fixed set of options **open a picker when typed bare**, so
`/vision` is as usable as `/vision qwen3.5:2b` — there is nothing to memorise.
`/help <command>` explains any one of them in full.

<details open>
<summary><b>Session</b></summary>

| Command | Action |
|---|---|
| `/help [command]` | List commands, or explain one in detail |
| `/status` | One-screen dashboard: model, context, cost, vision, cache |
| `/clear` | Reset the conversation, usage counters, and working set |
| `/resume` · `/resume log` | Load the latest JSONL session, or seed from `.g023/logs/` |
| `/learn [search\|get\|save\|forget\|outcome]` | Cross-session procedures (not in the system prompt) |
| `/undo [path]` | Restore a file from its newest `.g023/backups/` copy |
| `/exit` | Quit |
| `/config [status\|save\|global\|project\|clear]` *(alias `/cfg`)* | Show or manage the global vs per-project config scopes |

</details>

<details open>
<summary><b>Model</b></summary>

| Command | Action |
|---|---|
| `/model [flash]` | Show or set the orchestrator model — `deepseek-flash` is the only one the Responses API serves |
| `/thinking [1-100\|low\|high\|max\|off]` | Set reasoning effort (75 default and ceiling; max aliases to 75). Bare opens a picker: low / high / off |
| `/verbose [low\|mid\|high]` | How much detail to print while working |
| `/steps [n\|off\|default]` | Cap the tool steps one turn may take before it gives up (default 255; `off` for no cap, with progress printed every 10 steps) |
| `/max-tokens [n\|off\|default]` | Cap how long one reply may be (default `off` — no budget is sent, so the model's own maximum is the only ceiling) |

</details>

<details open>
<summary><b>Context & cost</b></summary>

| Command | Action |
|---|---|
| `/compact [focus]` | Summarise the history (`micro` = free local pass, `auto on\|off`) |
| `/g023-compact` | Compact `g023.md` → `g023_compact.md` (Flash, no reasoning; leaves the original) |
| `/max-ctx [n\|default]` | Set the context window ceiling (default 256k; DeepSeek's real limit is 1M) |
| `/compact-threshold [fraction\|default]` | Set when auto-compact fires, as a fraction of the window (default 0.85; e.g. `0.9` or `90%`) |
| `/context` | Break down what is occupying the context window, by role and size |
| `/cost` | Token usage and spend, split by cache hit/miss, with a per-turn sparkline |
| `/settings [save\|reset]` | Every setting, marking which persist; save or restore defaults |
| `/signals` *(alias `/drift`)* | Prefix hit rate against its own history, unknown item types, unexplained empty responses |

</details>

<details open>
<summary><b>Tools</b></summary>

| Command | Action |
|---|---|
| `/tools` | List tools, their permission level, and how often each ran |
| `/tools <tool> allow\|ask\|block` | Change one tool's permission |
| `/fetch <url>` | Fetch a URL yourself, with the cache/fresh prompt |
| `/fetch status` | Report how closely fetches imitate a real browser |
| `/cache [stats\|web\|clear]` | Inspect and purge the local SQLite caches |

</details>

<details>
<summary><b>Vision</b></summary>

| Command | Action |
|---|---|
| `/ollama` | Status: host, version, latency, installed model count |
| `/ollama host <addr>` | Point vision at another machine (tested before saving) |
| `/ollama host default` | Fall back to `OLLAMA_HOST`, then localhost |
| `/ollama models` | List installed models, sizes, quantisation, vision capability |
| `/ollama test [model]` | Run a real inference — proves the whole path, not just the port |
| `/ollama ps` | What the daemon currently holds in VRAM |
| `/vision` | Interactive picker of installed vision models |
| `/vision <model>` · `/vision off` | Enable a specific model, or disable |
| `/surge` | DeepSeek surge pricing — status, notify, auto-pause around peak |
| `/or` | OpenRouter vision — key, models, rate-limit tier, usage |
| `/orsub` | OpenRouter subagent offload — models, primary, failover, reasoning |

</details>

<details>
<summary><b>Work</b></summary>

| Command | Action |
|---|---|
| `/goal <text>` | State a high-level objective; runs one turn at effort 75 (the ceiling) with no step cap |

</details>

## Verbosity, context & cost

`/verbose` has three levels, persisted to `config.json`:

| Level | Shows |
|---|---|
| `low` *(default)* | The answer, plus a one-line trace of every tool call |
| `mid` | ↑ plus each tool's outcome and duration, and the per-turn token/cost line |
| `high` | ↑ plus the orchestrator's reasoning excerpts and per-iteration token counts |

`/context` answers the question `/cost` cannot: *what* is filling the window.
If tool results dominate, `/compact micro` clears the stale ones for free; if
it is real conversation, `/compact` summarises it on the standing prefix.
The command tells you which of the two applies. Auto-compact waits for
window pressure (default 85%): blanking a cached prefix every turn is a
cache miss, not a savings. A finished objective still hands off to a
summary so the next request is a new orchestration. Turn auto-compact off
with `/compact auto off`, move the trigger with `/compact-threshold`, or
resize the window with `/max-ctx`.

Every API call — orchestrator *and* subagents — reports its token usage to a
single tracker, so `/cost` and `/settings` show the cache-hit / cache-miss /
output split per model and price it from the SPEC §3.2 table.

## Web search (native)

Web search is **DeepSeek's own**, not a search API wired in behind it. There is
no provider chain, no key to supply, no rate limit to nurse, and no
`/websearch` command — because there is nothing on this side to configure.

The entire integration is one line in `tools/schemas.py`:

```python
WEB_SEARCH_TOOL = {"type": "web_search"}
```

That marker is appended to the tool list. No name, no parameters, no executor.
The server takes it from there.

**The search happens inside the model's turn**, on DeepSeek's infrastructure,
before the response comes back. g023 never sees a tool call for it and never
returns a result for it. What arrives instead is a record of what the server
did, which shows up in the trace like any other tool:

```
  ● searching the web…
  ✓ web_search  deepseek responses api web_search tool, deepseek v4 pricing
  ✓ web_search  read platform.deepseek.com/docs/…
```

A few consequences are worth stating plainly:

- **It never asks permission.** There is no moment at which g023 could
  interpose a prompt — the search is over by the time the response exists. So
  `web_search` has no entry in `/tools`. Giving the model the tool is the
  decision; each individual search is not. Everything that *does* leave your
  machine under g023's own control — `FetchUrl` — still asks every time.
- **It can search several times per turn**, following up on what it finds.
- **Findings persist.** The server's `web_search_call` items are carried in
  history like everything else, so what it learned survives across tool
  round-trips, across turns, and across `/compact`.
- **Slow searches are visible.** The server can be out on the web for a while
  without emitting anything, so g023 draws a live `searching the web…` line
  rather than letting it read as a hang.

Just ask — `what changed in the DeepSeek Responses API this month?` — and it
searches when it decides it needs to.

## Vision

The orchestrator does not see image bytes. Image analysis is delegated to a
*vision backend*. Three are supported, toggled with `/vision backend`:

| Backend | Key | Default model | Notes |
|---|---|---|---|
| `deepseek` | `K.dat` | `deepseek-flash` | Same model id as the orchestrator; billed at text prices. |
| `openrouter` | `OK.dat` | `google/gemma-4-26b-a4b-it:free` | Models from `./OR_VISION.json`, rotated on rate limits. |
| `ollama` | — | local daemon | A local/remote Ollama vision model (`/ollama host` points at it). |

**Vision is disabled by default** — enable it once and the choice persists.

```
/vision                      # configure the active backend (or pick an Ollama model)
/vision backend deepseek     # DeepSeek's deepseek-flash (K.dat key)
/vision backend openrouter   # OpenRouter (OK.dat key)
/vision backend ollama       # local/remote daemon
/vision endpoint responses   # deepseek wire format: responses | chat_completions
/vision off                  # disable (the default)
/vision status               # show the current setting
```

- **DeepSeek backend** — `deepseek-flash` on the same `K.dat` key, billed at
  exactly the token rates of the orchestrator model. The wire format is chosen
  per endpoint: `/responses` (the program's native format, default) or
  `/chat/completions`.
- **OpenRouter backend** — the key lives in `OK.dat` next to `K.dat`. Put a
  JSON array of model ids in `./OR_VISION.json` (project folder) to set the
  failover list; when one model returns HTTP 429 the next is tried. Free
  `:free` models are capped at **50 requests/24h** for accounts under $10 of
  lifetime credits, and up to **1000/24h** once you've bought $10+ credits —
  tell g023 which tier you're on with `/or tier free|credits`.
- **Ollama backend** — only models the daemon reports as `vision`-capable are
  listed; sizes are shown so you can stay inside your VRAM budget. The daemon
  may be local or on another machine (`/ollama host`).
- While vision is disabled the `AnalyzeImage` tool is not even offered to the
  orchestrator, so it never proposes a call it cannot fulfil.
- Images are always constrained-resized so each edge is ≤ 800px (needs
  `pillow`; oversized images are refused if it is not installed).
  `vision_max_image_dim` does not change the send size. Answers are cached by
  image hash + question + backend + model, so re-asking the *same* question
  about the same image is a local SQLite lookup rather than another inference.

Once enabled, just ask: `analyze screenshot.png — what's the error?`

### OpenRouter specifics

```
/or               status: key, tier, model list, today's usage
/or model <id>    set the vision model (or `default` to reset)
/or models        reload ./OR_VISION.json
/or tier free|credits   50/24h vs 1000/24h ceiling
/or usage         requests used today vs the ceiling
```

### Surge pricing (peak/off-peak)

DeepSeek moved to tidal pricing on 2026-08-17: peak hours are **09:00–12:00 and
14:00–18:00 Beijing time (UTC+8), Monday–Friday**; weekends are off-peak.
Off-peak is exactly half of peak. g023
converts the windows to **your local clock**, shows every time in **both
AM/PM and 24-hour format**, and can notify you and/or pause usage around them.
`/surge status` also counts down — **how many minutes to the next surge** and
**how long that surge will last** — and shows the regular vs surge (2×) prices
for each model, straight from `ds_pricing.json` / `ds_times.json`.

```
/surge               status: current state, local peak windows (AM/PM + 24h),
                     countdown to the next surge and its duration, prices
/surge notify on|off banner when surge pricing starts/ends (default on)
/surge on            pause usage from 5 min before peak until 5 min after it
                     ends, then resume automatically
/surge buffer <min>  widen the pause (default 5 min each side)
```

While a pause window is active the CLI waits it out instead of reading input, so
no requests are sent at double price; `Ctrl-C` bypasses it for that pass. The
toolbar shows `SURGE 2×` while peak hours are active.

### Running Ollama on another machine

The daemon does not have to be local — the usual reason to move it is that the
GPU is somewhere else.

```
/ollama host 192.168.1.50                 # bare IP: :11434 is appended for you
/ollama host gpu-box:11434
/ollama host https://ollama.example.com   # behind a TLS proxy, port left alone
/ollama host default                      # back to OLLAMA_HOST, then localhost
```

Addresses are forgiving: a missing scheme becomes `http://`, a missing port
becomes `:11434`. The host is **tested before it is saved**, and if it does not
answer you are told why — connection refused reads differently from a timeout —
and asked whether to save it anyway.

Precedence, highest first: the `vision_host` setting (what `/ollama host`
writes) → the `OLLAMA_HOST` environment variable → `http://localhost:11434`.
`/ollama` always shows which one is in effect.

Three commands make a remote setup diagnosable rather than a guessing game:

| Command | Answers |
|---|---|
| `/ollama` | Is anything listening, what version, how far away (latency), how many models |
| `/ollama models` | What that machine actually has — names, sizes, quantisation, vision capability |
| `/ollama test` | A real inference round-trip: g023 sends a generated image and checks the answer. This proves the whole path — encoding, transport, and the model genuinely being image-capable — rather than just that the port is open. |

> [!WARNING]
> For a remote daemon to accept connections it must be bound to the network
> (`OLLAMA_HOST=0.0.0.0:11434 ollama serve`) and the port must be open.
> **Ollama has no authentication.** Anyone who can reach that port can use the
> GPU and read whatever is sent to it — keep it on a trusted network, behind an
> SSH tunnel (`ssh -L 11434:localhost:11434 gpu-box`, then leave the host at
> default), or behind an authenticating reverse proxy.

## Fetching web pages

Distinct from [web search](#web-search-native): that runs on DeepSeek's side and
you never see the request. `FetchUrl` is g023 reaching out from *your* machine,
to a URL you or the model named — so it is held to a different standard.

`FetchUrl` reads a page and hands back readable text rather than raw HTML, in
keeping with the context budget. Two things make it different from a plain
`requests.get`.

**It asks first, every time.** A fetch leaves your machine and touches someone
else's server, so it is never on the `allow` list. When a cached copy exists the
prompt is a three-way choice rather than yes/no:

```
Permission required to fetch https://example.com/pricing
engine: curl_cffi · requested mode: auto
Cached copy available — fetched 12m ago, HTTP 200, 48,210 chars
Use cached copy, fetch fresh, or deny? [c/f/d] (c):
```

Your answer overrides whatever the model asked for. The model can also request
a mode itself — `auto` (cache if younger than `max_age`), `fresh`, or `cache`
(never touch the network) — and `cache` mode is the cheap way to re-read a page
at a larger `max_chars` without a second request.

**It presents itself as an ordinary browser.** Requests carry a real browser's
headers *in the order that browser sends them*, negotiate HTTP/2, and keep
cookies per domain in `.g023/cookies.json` so a second visit looks like a
returning user rather than a stranger. Requests to the same host are spaced out
instead of fired back-to-back. Nothing identifies g023.

The weak point is TLS: Python's handshake has a recognisable fingerprint, and a
UA claiming Chrome over a Python TLS stack is a contradiction that fingerprint
checks notice. Installing `curl_cffi` fixes it properly — it reproduces Chrome's
actual handshake:

```bash
pip install curl_cffi        # then: /fetch status
```

| | headers | HTTP/2 | TLS (JA4) |
|---|---|---|---|
| `curl_cffi` | Chrome order | yes | `t13d1516h2…` — matches Chrome |
| `httpx` + `h2` | Chrome order | yes | generic Python |
| `httpx` alone | Chrome order | no | generic Python |

`/fetch status` always tells you which of these you are on, and the JA4 column
reports what `curl_cffi` impersonates rather than a measurement — point `/fetch
https://tls.peet.ws/api/all` at it to see your own fingerprint on the wire.

What is *not* claimed: that this gets you past any particular bot defence. Those
systems weigh behaviour, IP reputation and history alongside the fingerprint, and
they change. A handshake that matches Chrome removes one obvious tell; it is not
a guarantee about any site, and any site may block you anyway.

> [!NOTE]
> **There is no JavaScript engine.** Pages that build their body client-side
> return the shell. Use a server-side check like `tls.peet.ws/api/all` to see
> what your requests actually look like on the wire.

## Design principles

1. **Context is currency** — keep raw files and search dumps out of the
   orchestrator unless they were asked for by line range.
2. **Subagents are the treasury** — heavy I/O happens in isolated, minimal
   contexts, and the escalation path back to verbatim source stays open.
3. **Cache everything cacheable** — file hashes, vision Q&A, fetched pages.
4. **Do it off-API where it is free** — search the filesystem locally, see with
   a local GPU, and spend tokens only on reasoning.
5. **Thinking mode is a dial** (0–100, default 75), not always-on max.
6. **Say what happened, not what was sent** — and always say what it cost.
7. **Claim only what is checked.** Numbers in this documentation are either
   asserted by a test, quoted from a published price sheet with attribution, or
   labelled as an estimate. Where something is not measured, it says so.
8. **Retrieve, don't prepend** — learned procedures live in files and are
   searched on demand, so they cannot bust the prompt-cache prefix.

## Tests

```bash
pip install pytest && python3 -m pytest tests/ -q
```

No plugins beyond `pytest` — async tests run through a small hook in
`tests/conftest.py`, and every test points `G023_PROJECT_ROOT` at a temporary
directory, so a run never reads your real cache and never touches the network.
**421 tests**, all passing, none of them on the network.

| File | What it holds to account |
|---|---|
| `test_call_accounting.py` | The API-call table above (unfocused ReadFile = 0, semantic focus = 1), counted against a stub client |
| `test_goal_cost_wins.py` | Exact-symbol focus = 0; no mid-JSON slice of ReadFile/Agent; nested Agent vaults bulky bodies; temperature pinned with the prefix |
| `test_cost_efficiency.py` | Rewriting a cached prefix does not recoup; post-turn compact keeps a hot prefix; Agent reuses parent instructions+tools+effort |
| `test_file_reader.py` | Symbol ranges are exact and in-bounds; truncation is declared; local AST facts outrank the model's |
| `test_drift_signals.py` | Each signal fires on the shape that matters and stays quiet on ordinary turns; hit-rate history survives restarts |
| `test_history_integrity.py` | Tool-call pairing survives rollback and repair; output items are echoed back byte-identical |
| `test_usage_accounting.py` | Cost arithmetic, both usage spellings, worst-case assumption when the split is unreported |
| `test_reasoning.py` | Effort 0–100 ↔ wire aliases, capped at 75 / `high`; `[handoff]` / `[next_effort]` stripped from the visible answer |
| `test_learn.py` | Learn store round-trip, prefix pin, secret reject, cross-project env visibility |
| `test_session_log.py` | JSONL `/resume` and `.g023/logs/` handoff round-trip |
| `test_goal_judge.py` | Working-set completed vs not-completed from evidence, not from a final message |
| `test_prefix_stable.py` | Standing instructions + tools stay byte-identical until vision changes |
| `test_root_confine.py` | ReadFile / ListDir / SearchContent refuse paths outside the project root |
| `test_search.py` | Total-vs-sample counts, gitignore, glob/brace, grep fallback |
| `test_vision_resize.py` | Every vision send is constrained to 800px |
| `test_write_backup.py` | Every overwrite snapshots `.g023/backups/`; skipped for `.g023/` paths, new files, unchanged content |

The full suite is listed in [`docs/tests.md`](docs/tests.md).

What these do not cover: anything requiring the real API, end-to-end session
cost, or whether a summary was good enough for the question asked.

## Extending

- **New slash command** — add a `Command(...)` to `COMMANDS` in `commands.py`
  and a matching `cmd_*` method on `CLI`. Help text, tab completion, and "did
  you mean" pick it up automatically; `check_handlers()` fails at startup if the
  method is missing, so the two halves cannot drift apart.
- **New tool** — add a schema in `tools/schemas.py` plus an executor or a
  subagent route. Give it an entry in `ui.TOOL_STYLES` and a case in
  `ui.describe_call` / `describe_result` so its trace reads like the others.
- **Vision backends** — `ollama_client.py` and `subagents/vision.py`; the router
  dispatches `AnalyzeImage` there.
- **Permissions** — `permission_default` (`allow` | `ask` | `block`) sets the
  starting level for the tools that act: `Bash`, `WriteFile`, `Edit`, `Agent`,
  `AnalyzeImage`. Read-only tools (including Glob, TodoWrite, Learn*,
  SessionSearch) stay `allow`, and `FetchUrl` still asks unless the default is
  `block`. Per-tool overrides live in `tools/registry.py` and `/tools`.

Environment overrides:

| Variable | Effect |
|---|---|
| `G023_HOME` | Installation folder — where `K.dat` and `config.json` live |
| `G023_PROJECT_ROOT` | The project being worked on |
| `OLLAMA_HOST` | Vision daemon, when `vision_host` is unset |
| `G023_ASCII=1` | Force plain-ASCII output on terminals that mangle box drawing |
| `G023_READFILE_RAW=1` | `ReadFile` returns raw content instead of delegating — the A/B baseline described in [What delegation costs](#what-delegation-costs) |
| `G023_WRITE_BACKUP=0` | Disable the automatic `.g023/backups/` snapshot before WriteFile/Edit overwrites |
| `G023_WRITE_BACKUP_KEEP=5` | How many backup versions to retain per file (minimum 1) |
| `G023_LEARN=0` | Disable the learn store (search/get/put become no-ops) |

## License

MIT — built for the g023 workflow.

<div align="center">

# g023 Code

**A pure-Python AI coding agent for your terminal, powered by DeepSeek V4.**

*Subagent-First · Context is Currency · Terminal-native*

[![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-1.2.0-informational)](releases/1/RELEASE_NOTES.md)
[![License](https://img.shields.io/badge/license-MIT-green)](#license)
[![Model](https://img.shields.io/badge/model-DeepSeek%20V4-8A2BE2)](https://platform.deepseek.com/)

</div>

---

g023 Code reads your project, searches it, searches the web, runs commands,
fetches pages, and looks at images — while telling you exactly what it is doing
and what it just cost. Data-heavy work is delegated by default: a subagent reads
the file in an isolated context and what comes back to the orchestrator is a
structural summary plus a symbol → line-range map. When the summary is not
enough, the orchestrator asks for an exact line range and gets verbatim source —
so "summary first, raw on request" is the design, not "never raw".

That trade is a real trade, and it is worth being precise about which half of it
is measured. The call counts below are asserted by the test suite. Whether
delegation is *cheaper overall* is not: it depends on how many turns the file's
text would otherwise have sat in context, which no test here can know. See
[break-even](#what-delegation-costs) for the arithmetic and its assumptions.

It talks to DeepSeek exclusively through the **Responses API**, which is what
makes DeepSeek's own **native web search** a first-class tool of the loop rather
than a search provider bolted on the side.

<p align="center">
  <img src="snapshots/snapshot.png" alt="g023 Code starting up and working through a request" width="880">
</p>

<p align="center"><sub>Startup banner, live tool trace, and a permission prompt before anything runs a command. Captured on 1.1.0 — see <a href="media/README.md">media/README.md</a> for what has changed since.</sub></p>

## Table of contents

- [Quick start](#quick-start)
- [What you see while it runs](#what-you-see-while-it-runs)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [What delegation costs](#what-delegation-costs)
- [Drift signals](#drift-signals)
- [Slash commands](#slash-commands)
- [Verbosity, context & cost](#verbosity-context--cost)
- [Web search (native)](#web-search-native)
- [Vision (via Ollama)](#vision-via-ollama)
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

A per-project `.g023/` folder holds the SQLite cache and your input history.

`prompt_toolkit` is listed as a dependency but is not strictly required: with it
you get tab completion, persistent history, and a live status bar pinned to the
bottom of the terminal. Without it the input line falls back to a plain prompt
and everything else behaves identically.

## What you see while it runs

The point is to never wonder what it is doing, or what it just cost.

<p align="center">
  <img src="snapshots/snapshot3.png" alt="A finished answer, the context and cost bar, and the /cost dashboard" width="880">
</p>

Tool traces say what *happened* (`708 lines, 9 classes, cached`), not what was
sent. The status line after every turn carries the numbers that only mean
something together — context used, **cache hit rate**, cost of this turn, cost
of the session — because on V4 Flash a cached input token is billed at $0.0028/M
against $0.14/M for an uncached one. That 50x is DeepSeek's price sheet, not
something this harness achieves; what the harness does is avoid breaking the
prefix so more tokens fall on the cheaper side.

Costs shown are computed locally from the token counts the API returns and the
price table in [`SPEC.md` §3.2](SPEC.md). They are an estimate of your bill, not
a reading of it.

Anything you type that does not start with `/` is a prompt. Everything else is
a slash command.

## Configuration

`/settings` shows every setting in one place and marks which ones persist;
`/tools` shows what the agent may do and what it has done this turn. Both are
editable from inside the session, and the `·saved` keys are written to
`config.json` next to `K.dat`, so they follow you across projects.

<p align="center">
  <img src="snapshots/snapshot2.png" alt="The /settings and /tools panels" width="880">
</p>

Defaults out of the box: **Flash** for the orchestrator and subagents, thinking
on at **high** effort, verbosity **low**, auto-compaction **on** at 85% of the
window, acting tools set to **ask**, and **vision disabled**. Nothing that
writes, runs, or leaves your machine happens without a prompt.

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

- **Orchestrator** (`deepseek-v4-flash`) keeps only high-level reasoning, tool
  schemas, and compact summaries.
- **Subagents** handle data-heavy work in isolated contexts:
  - `FileReader` → structural metadata, a symbol → line-range map, and a 2–4
    sentence summary, cached by content hash
  - `Searcher` → metadata-first grep results, **pure local Python — no API call**
  - `Explore` / `Plan` → isolated reasoning with thinking mode
  - `Vision` → an Ollama daemon, local or remote (DeepSeek V4 Flash is text-only)
- **Native `web_search`** runs server-side inside the model's turn — no client
  executor, no provider chain
- **SQLite cache** → a repeated read of unchanged bytes is a local lookup and
  costs no API call
- **Prefix-cache-friendly**: instructions and tool definitions stay static
- **Thinking mode** with a `reasoning_effort` dial (low / high / max, or off —
  which maps to `reasoning: {"effort": "none"}`, the only thing `/responses`
  actually honours)

**One model.** `deepseek-v4-flash` is the only model sent to the API, in every
role. `/model` refuses `deepseek-v4-pro` because calls to it on `/responses`
returned an error when this was last checked, and refusing with that reason
attached beats handing you a model every call fails on. If DeepSeek has since
enabled it, that refusal is stale — it is a hardcoded list in `config.py`, not a
capability probe. Vision is the only other exception, and it does not use the
DeepSeek API at all.

The presentation and command layers are deliberately separate from the agent:

| Module | Responsibility |
|---|---|
| `commands.py` | The command catalogue as *data* — one source of truth shared by `/help`, tab completion, and "did you mean". A command cannot be completable but undocumented. |
| `ui.py` | Theme, glyphs, gauges, and the compact renderings of tool calls and their results. Falls back to ASCII on terminals that cannot draw box characters. |
| `prompt.py` | The input line and the shared numbered picker. Degrades cleanly without `prompt_toolkit`. |
| `ollama_client.py` | Host resolution, reachability probes, model discovery, and vision inference against any daemon. |

See [`SPEC.md`](SPEC.md) for the full specification.

## What delegation costs

Delegating a read is not free — it is a second API call, and sometimes it is the
wrong trade. Here is what is actually true, separated by how well it is known.

**Measured.** These call counts are asserted in `tests/test_call_accounting.py`,
so they are checked rather than remembered:

| Operation | API calls |
|---|---|
| `SearchContent`, any size | 0 — pure local regex |
| `ReadFile`, Python under 12k chars, no focus | 0 — summarised from the local AST |
| `ReadFile`, same bytes again (cache hit) | 0 |
| `ReadFile` with `start_line`/`end_line` | 0 — returns verbatim source |
| `ReadFile`, first read of a non-Python file, a file over 12k chars, or any focused read | 1 |

**Arithmetic, given assumptions.** A 40 KB file is roughly 10k tokens. Inlined
into the orchestrator, it is re-sent on every subsequent turn — cheaply, if it
stays in the cached prefix, at $0.0028/M. Summarising it costs one Flash call
(~10k in, ~300 out ≈ $0.0023 at miss rates) and leaves ~300 tokens in context
instead of 10k. Which wins depends on how many turns follow and whether the
prefix survives them, and those depend on your session. **Delegation is not
unconditionally cheaper**; it is unconditionally *smaller*, and smaller context
is what keeps the model on task and the window from filling.

**Not measured.** Nobody has run a controlled A/B of end-to-end session cost.
The flag to do it exists:

```bash
G023_READFILE_RAW=1 g023      # ReadFile returns raw content, no subagent
```

With it set, whole-file reads return the file's bytes and cost zero API calls —
the baseline a plain agent loop would have. Run the same task script with and
without it and compare `/cost`. Until someone does that on their own codebase,
the honest claim is the one above: fewer tokens in the window, an extra call to
get there.

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
| `/clear` | Reset the conversation and usage counters |
| `/exit` | Quit |

</details>

<details open>
<summary><b>Model</b></summary>

| Command | Action |
|---|---|
| `/model [flash]` | Show or set the orchestrator model — `flash` is the only one the Responses API serves |
| `/thinking [low\|high\|max\|off]` | Set reasoning effort, or turn thinking off |
| `/verbose [low\|mid\|high]` | How much detail to print while working |

</details>

<details open>
<summary><b>Context & cost</b></summary>

| Command | Action |
|---|---|
| `/compact [focus]` | Summarise the history (`micro` = free local pass, `auto on\|off`) |
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

</details>

<details>
<summary><b>Work</b></summary>

| Command | Action |
|---|---|
| `/goal <text>` | State a high-level objective; runs one turn at max reasoning effort |

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
it is real conversation, `/compact` summarises it with the cheap model in an
isolated context. The command tells you which of the two applies. Compaction
also runs automatically once the context passes `compact_threshold` (85%) —
turn that off with `/compact auto off`.

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

## Vision (via Ollama)

DeepSeek V4 Flash is text-only, so image analysis is delegated to an
[Ollama](https://ollama.com) vision model. **Vision is disabled by default** —
enable it once and the choice persists.

```
/vision              # interactive: lists installed models, pick one or disable
/vision qwen3.5:2b   # enable a specific model directly
/vision off          # disable (the default)
/vision status       # show the current setting
```

- Only models Ollama reports as `vision`-capable are listed (if none are, every
  model is listed so you can force a choice). Sizes are shown so you can stay
  inside your VRAM budget — on a 12 GB GPU anything up to ~8 GB is comfortable.
- While vision is disabled the `AnalyzeImage` tool is not even offered to the
  orchestrator, so it never proposes a call it cannot fulfil.
- Images are downscaled to 1024 px on the longest edge (needs `pillow`; skipped
  if not installed) and answers are cached by image hash + question, so re-asking
  the *same* question about the same image is a local SQLite lookup rather than
  another inference. A different question about the same image is a new
  inference — the question is part of the key.

Once enabled, just ask: `analyze screenshot.png — what's the error?`

### Running the model on another machine

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
5. **Thinking mode is a dial**, not always-on max.
6. **Say what happened, not what was sent** — and always say what it cost.
7. **Claim only what is checked.** Numbers in this documentation are either
   asserted by a test, quoted from a published price sheet with attribution, or
   labelled as an estimate. Where something is not measured, it says so.

## Tests

```bash
pip install pytest && python3 -m pytest tests/ -q
```

No plugins beyond `pytest` — async tests run through a small hook in
`tests/conftest.py`, and every test points `G023_PROJECT_ROOT` at a temporary
directory, so a run never reads your real cache and never touches the network.

| File | What it holds to account |
|---|---|
| `test_call_accounting.py` | The API-call table above, counted against a stub client |
| `test_file_reader.py` | Symbol ranges are exact and in-bounds; truncation is declared; local AST facts outrank the model's |
| `test_drift_signals.py` | Each signal fires on the shape that matters and stays quiet on ordinary turns; hit-rate history survives restarts |
| `test_history_integrity.py` | Tool-call pairing survives rollback and repair; output items are echoed back byte-identical |
| `test_usage_accounting.py` | Cost arithmetic, both usage spellings, worst-case assumption when the split is unreported, command/handler contract |

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
  starting level for the tools that act: `Bash`, `WriteFile`, `Agent`,
  `AnalyzeImage`. Read-only tools stay `allow`, and `FetchUrl` still asks unless
  the default is `block`. Per-tool overrides live in `tools/registry.py` and
  `/tools`.

Environment overrides:

| Variable | Effect |
|---|---|
| `G023_HOME` | Installation folder — where `K.dat` and `config.json` live |
| `G023_PROJECT_ROOT` | The project being worked on |
| `OLLAMA_HOST` | Vision daemon, when `vision_host` is unset |
| `G023_ASCII=1` | Force plain-ASCII output on terminals that mangle box drawing |
| `G023_READFILE_RAW=1` | `ReadFile` returns raw content instead of delegating — the A/B baseline described in [What delegation costs](#what-delegation-costs) |

## License

MIT — built for the g023 workflow.

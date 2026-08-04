<div align="center">

# g023 Code

**A pure-Python AI coding agent for your terminal, powered by DeepSeek V4.**

*Subagent-First · Context is Currency · Terminal-native*

[![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
![Version](https://img.shields.io/badge/version-1.1.0-informational)
[![License](https://img.shields.io/badge/license-MIT-green)](#license)
[![Model](https://img.shields.io/badge/model-DeepSeek%20V4-8A2BE2)](https://platform.deepseek.com/)

</div>

---

g023 Code reads your project, searches it, runs commands, fetches pages, and
looks at images — while telling you exactly what it is doing and what it just
cost. The orchestrator never sees a raw file: every data-heavy operation is
delegated to a subagent with an isolated, minimal context, and the answer that
comes back is a summary. That is the whole design, and it is why a real session
costs fractions of a cent.

<p align="center">
  <img src="docs/snapshot.png" alt="g023 Code starting up and working through a request" width="880">
</p>

<p align="center"><sub>Startup banner, live tool trace, and a permission prompt before anything runs a command.</sub></p>

## Table of contents

- [Quick start](#quick-start)
- [What you see while it runs](#what-you-see-while-it-runs)
- [Configuration](#configuration)
- [Architecture](#architecture)
- [Slash commands](#slash-commands)
- [Verbosity, context & cost](#verbosity-context--cost)
- [Vision (via Ollama)](#vision-via-ollama)
- [Fetching web pages](#fetching-web-pages)
- [Design principles](#design-principles)
- [Extending](#extending)
- [License](#license)

## Quick start

**Requirements:** Python 3.11 or newer, and a [DeepSeek API key](https://platform.deepseek.com/).

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Put your API key in K.dat (single line, no quotes)
cp K.dat.example K.dat && $EDITOR K.dat

# 3. Launch from any project folder
/path/to/g023-code/g023.sh
```

<details>
<summary><b>Windows</b></summary>

```bat
C:\path\to\g023-code\g023.bat
```

</details>

Add the folder to your `PATH`, or alias it to `g023`, and it is available
everywhere. The launchers set two environment variables for you:

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
  <img src="docs/snapshot3.png" alt="A finished answer, the context and cost bar, and the /cost dashboard" width="880">
</p>

Tool traces say what *happened* (`708 lines, 9 classes, cached`), not what was
sent. The status line after every turn carries the numbers that only mean
something together — context used, **cache hit rate**, cost of this turn, cost
of the session — because a cache hit is roughly fifty times cheaper than a miss.

Anything you type that does not start with `/` is a prompt. Everything else is
a slash command.

## Configuration

`/settings` shows every setting in one place and marks which ones persist;
`/tools` shows what the agent may do and what it has done this turn. Both are
editable from inside the session, and the `·saved` keys are written to
`config.json` next to `K.dat`, so they follow you across projects.

<p align="center">
  <img src="docs/snapshot2.png" alt="The /settings and /tools panels" width="880">
</p>

Defaults out of the box: **Flash** for the orchestrator and subagents, thinking
on at **high** effort, verbosity **low**, auto-compaction **on** at 85% of the
window, acting tools set to **ask**, and **vision disabled**. Nothing that
writes, runs, or leaves your machine happens without a prompt.

## Architecture

- **Orchestrator** (`deepseek-v4-flash` by default) keeps only high-level
  reasoning, tool schemas, and compact summaries.
- **Subagents** handle all data-heavy work in isolated contexts:
  - `FileReader` → structural metadata + a 2–4 sentence summary, cached by content hash
  - `Searcher` → metadata-first grep results
  - `Explore` / `Plan` → isolated reasoning with thinking mode
  - `Vision` → an Ollama daemon, local or remote (DeepSeek V4 is text-only)
- **Aggressive SQLite cache** → repeated file reads cost $0.00
- **Prefix-cache-friendly** system prompt and tool definitions stay static
- **Thinking mode** with a controllable `reasoning_effort` dial (low / high / max)

The presentation and command layers are deliberately separate from the agent:

| Module | Responsibility |
|---|---|
| `commands.py` | The command catalogue as *data* — one source of truth shared by `/help`, tab completion, and "did you mean". A command cannot be completable but undocumented. |
| `ui.py` | Theme, glyphs, gauges, and the compact renderings of tool calls and their results. Falls back to ASCII on terminals that cannot draw box characters. |
| `prompt.py` | The input line and the shared numbered picker. Degrades cleanly without `prompt_toolkit`. |
| `ollama_client.py` | Host resolution, reachability probes, model discovery, and vision inference against any daemon. |

See [`SPEC.md`](SPEC.md) for the full specification.

## Slash commands

Commands that take a fixed set of options **open a picker when typed bare**, so
`/model` is as usable as `/model pro` — there is nothing to memorise.
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
| `/model [flash\|pro]` | Choose the orchestrator model — the picker shows both prices |
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

## Vision (via Ollama)

DeepSeek V4 is text-only, so image analysis is delegated to an
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
  if not installed) and answers are cached by image hash + question, so
  re-asking about the same screenshot is free and instant.

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

`/fetch status` always tells you which of these you are on. Verified against
`tls.peet.ws`: with `curl_cffi` the JA4 and the HTTP/2 fingerprint both match
real Chrome, and Cloudflare- and PerimeterX-protected pages return content
rather than a challenge.

> [!NOTE]
> **There is no JavaScript engine.** Pages that build their body client-side
> return the shell. Use a server-side check like `tls.peet.ws/api/all` to see
> what your requests actually look like on the wire.

## Design principles

1. **Context is currency** — never pollute the orchestrator with raw files or search dumps.
2. **Subagents are the treasury** — all heavy I/O happens in isolated, minimal contexts.
3. **Cache everything cacheable** — file hashes, vision Q&A, fetched pages.
4. **Prefer Flash for cost**; use Pro only when quality clearly demands it.
5. **Thinking mode is a dial**, not always-on max.
6. **Say what happened, not what was sent** — and always say what it cost.

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

Environment overrides: `G023_HOME`, `G023_PROJECT_ROOT`, `OLLAMA_HOST`, and
`G023_ASCII=1` to force plain-ASCII output on terminals that mangle box drawing.

## License

MIT — built for the g023 workflow.

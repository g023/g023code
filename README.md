<div align="center">

# g023 Code

**A pure-Python AI coding agent for your terminal, powered by DeepSeek V4.**

*Subagent-First · Context is Currency · Terminal-native*

[![Python](https://img.shields.io/badge/python-3.11%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-1.3.2-informational)](../RELEASE_NOTES.md)
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
  <img src="docs/session.png" alt="One question answered: the live tool trace, the boxed answer, and the accounting line under it" width="880">
</p>

<p align="center"><sub>One read-only question, answered. Every tool call is named as it runs, and the turn ends with what it took: 3 iterations, 4 tool calls, 5.5 s of model time, 11 ms of tool time, $0.00044. Captured on 1.3.2 — <a href="docs/README.md">docs/README.md</a> indexes every screenshot and states how they were taken.</sub></p>

## Table of contents

- [Quick start](#quick-start)
- [What you see while it runs](#what-you-see-while-it-runs)
- [Configuration](#configuration)
- [The tools](#the-tools)
- [Permissions](#permissions)
- [Architecture](#architecture)
- [What delegation costs](#what-delegation-costs)
- [How compaction works](#how-compaction-works)
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

All screenshots below are real captures from a 1.3.2 session — see
[docs/README.md](docs/README.md) for the full index and the capture conditions.

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

Everything g023 writes about your project lives in one hidden folder beside it,
created on launch:

```
<project>/
└── .g023/
    ├── cache.db       # one SQLite file — file summaries, vision answers,
    │                  #   fetched pages, session facts, prefix-cache history
    ├── cookies.json   # per-host cookies, created on the first FetchUrl
    └── history        # your input history for the REPL
```

Nothing in it is required to run: delete the folder and the next session rebuilds
it, having lost only cached work. `cache.db` holds five tables — file summaries,
vision answers, fetched pages, session facts and the daily prefix-cache history —
and `/cache` reports what is in each of them, how large the file is and where it
sits.

<p align="center">
  <img src="docs/cache.png" alt="The /cache panel: per-table counts for the local SQLite caches, with the database size and path" width="880">
</p>

<p align="center"><sub><code>/cache</code>. <code>/cache clear</code> empties the caches but deliberately leaves the prefix-cache history alone — that is a record of what happened, not cached work.</sub></p>

`prompt_toolkit` is listed as a dependency but is not strictly required: with it
you get tab completion, persistent history, and a live status bar pinned to the
bottom of the terminal. Without it the input line falls back to a plain prompt
and everything else behaves identically.

At the prompt: **Tab** completes commands, **↑/↓** walks your history,
**Esc-Enter** starts a new line inside the same prompt, and **Ctrl-C** cancels
the turn in flight and hands the prompt back rather than killing the process.

<p align="center">
  <img src="docs/startup.png" alt="The startup banner: version, project, home, model, and a live probe of the vision daemon" width="880">
</p>

<p align="center"><sub>Startup states where it is and what it is configured with, and probes the vision daemon rather than assuming it is there.</sub></p>

## What you see while it runs

The point is to never wonder what it is doing, or what it just cost.

<p align="center">
  <img src="docs/cost.png" alt="The /cost dashboard: calls, cache hit, cache miss, output and cost per model" width="880">
</p>

<p align="center"><sub><code>/cost</code>. Cache hit and cache miss are separate columns because they are priced an order of magnitude apart, and the price line is printed underneath so the arithmetic is checkable rather than trusted.</sub></p>

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

Answers **stream** by default (`stream`, on), so text appears as it is written
rather than after the turn completes. A *transport* failure mid-stream does not
cost you the turn: it is printed (`streaming unavailable (…); falling back`) and
the same request is re-issued unstreamed. An error the API itself returned is
raised as-is — retrying a rejected request would only get it rejected again.

`/status` (alias `/dash`) is the same information from the other direction: not
what the last turn cost, but where the session stands overall.

<p align="center">
  <img src="docs/status.png" alt="The /status dashboard: model, session counters, vision, project, cache size, context and spend" width="880">
</p>

Anything you type that does not start with `/` is a prompt. Everything else is
a slash command.

## Configuration

`/settings` shows every setting in one place and marks which ones persist. It is
editable from inside the session, and the `·saved` keys are written to
`config.json` next to `K.dat`, so they follow you across projects.

<p align="center">
  <img src="docs/settings.png" alt="The /settings panel: every setting, its live value, and which ones persist" width="880">
</p>

Defaults out of the box: **Flash** for the orchestrator and subagents, thinking
on at **high** effort, verbosity **low**, auto-compaction **on** at 85% of the
window, up to **100** tool steps per turn, **12** search matches returned, acting
tools set to **ask**, and **vision disabled**. Nothing that writes, runs, or
leaves your machine happens without a prompt.

`/settings save` writes the persisted subset now; `/settings reset` restores the
built-in defaults. A hand-edited `config.json` cannot put the session into a
state its own commands cannot describe — a value outside the allowed set (an
unavailable model, a verbosity that is not `low`/`mid`/`high`, a negative step
count) is dropped and the default kept, rather than obeyed.

## The tools

Eight function tools, plus DeepSeek's server-side `web_search`. `/tools` lists
them with their current permission and how many times each has run this turn.

| Tool | Takes | Returns |
|---|---|---|
| `ReadFile` | `path`, and optionally `raw`, `focus`, `start_line`, `end_line` | A structural summary plus a symbol → line-range map — **never** the whole file unless you ask. `start_line`/`end_line` returns that range as verbatim source instead; `focus` steers the summary; `raw` adds a truncated excerpt. |
| `SearchContent` | `query`, `path`, `max_matches` (12), `file_glob` | File, line, the matching text and a little context — metadata, not file dumps. Pure local Python: no API call at any size. |
| `ListDir` | `path`, `recursive` | Names, types and sizes. `recursive` gives a shallow tree, capped at depth 2, so listing a large repo cannot flood the window. |
| `Bash` | `command`, `timeout` (60 s) | Stdout/stderr from the project root. Asks first. |
| `WriteFile` | `path`, `content` | Writes or overwrites. Asks first, showing the path and an excerpt of what would be written. |
| `Agent` | `kind` (`explore` \| `plan`), `objective` | An isolated subagent's conclusion, reasoned out in its own context rather than in the orchestrator's. |
| `AnalyzeImage` | `path_or_url`, `question` | A textual answer from a local vision model. Not offered at all while vision is disabled. |
| `FetchUrl` | `url`, `cache_mode`, `max_age` (3600 s), `extract` (`text`/`markdown`/`links`/`raw`), `max_chars` (20000) | Readable text from a page, not raw HTML. Always asks. |
| `web_search` | — | Nothing on this side: DeepSeek runs it server-side inside the turn. See [Web search](#web-search-native). |

Whatever a tool returns, the orchestrator's copy is capped: a result over 6,000
characters is cut to 5,500 and labelled *"truncated for orchestrator context;
re-run the tool with a narrower scope for the rest"*. A single tool call cannot
fill the window by accident.

<p align="center">
  <img src="docs/tools.png" alt="The /tools panel: each tool, its permission, and whether it runs in an isolated subagent context" width="880">
</p>

The **subagent-isolated** column is the architecture made visible: `ReadFile`,
`SearchContent`, `AnalyzeImage` and `Agent` do their work in a context that is
thrown away, and only their conclusion comes back.

## Permissions

Every tool sits at one of three levels — `allow`, `ask`, `block`. The ones that
write, run, spend or leave the machine (`Bash`, `WriteFile`, `Agent`,
`AnalyzeImage`) start at `permission_default`, which is `ask`. The read-only,
local, free ones (`ReadFile`, `SearchContent`, `ListDir`) start at `allow` and
stay there whatever the default is — there is nothing to approve about reading a
file you already own. Change any of them with `/tools <tool> allow|ask|block`.

When something needs permission, the prompt shows what would actually happen —
the path, and an excerpt of the exact content — before you answer:

<p align="center">
  <img src="docs/permission.png" alt="A WriteFile permission prompt showing the path and content excerpt" width="880">
</p>

`y` allows this one call, **`a` promotes the tool to `allow` for the rest of the
session**, and `n` refuses — the refusal goes back to the model as the tool's
result, so it can try another way instead of stalling. Ctrl-C or Ctrl-D at the
prompt counts as a refusal of that one call, not as a crash. `FetchUrl` is the
exception to the default: it asks every time unless you have blocked it outright,
because every fetch touches a different third party.

That capture also shows the escalation path working unprompted: `read
app/schemas.py` returned a summary (25 lines, 3 classes), and the model then
asked for `app/schemas.py:1-8` to see the head verbatim before overwriting it.

<p align="center">
  <img src="docs/write.png" alt="The same turn after approving: wrote 197 B in 7 ms, then the answer" width="880">
</p>

<p align="center"><sub>After <code>y</code>: <code>wrote 197 B · 7 ms</code>, then the answer. The trace says what happened to the file, not what was sent to the model.</sub></p>

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

## How compaction works

Delegation keeps a *turn* small. Compaction is what keeps a long *session* small.
Both exist because the same history is re-sent on every single request: an item
that entered the transcript twenty turns ago is still being paid for now.

There are three places where history gets smaller, and they are worth keeping
apart, because two of them are free and one of them costs money.

### 0 · On the way in — tool results are capped

Before a tool result is ever appended to the transcript, the orchestrator
truncates it:

```python
if len(result) > 6000:
    result = result[:5500] + "…[truncated for orchestrator context; " \
                             "re-run the tool with a narrower scope for the rest]"
```

This is not compaction so much as its precondition. It puts a hard ceiling on
what one call can cost you, and the wording tells the model what to do about it —
narrow the scope and call again — rather than leaving it to guess that something
was cut.

### 1 · `/compact micro` — free, local, manual

Tool results are almost always the largest thing in a long history, and almost
always the least useful part of it once they have been acted on. `/compact micro`
(aliases `local`, `free`) walks the history, keeps the **six most recent** tool
results intact, and replaces the `output` text of every older one with:

```
[Old tool result content cleared]
```

Two properties matter. First, **no API call happens** — this is string surgery on
a local list, so it costs nothing and takes no measurable time. Second, the items
themselves are *not removed*. A `function_call` and its `function_call_output`
must both be echoed back on the next request; deleting one would orphan the other
and the next request would be a hard 400. So the shape of the conversation
survives exactly, and only the bytes inside it go.

<p align="center">
  <img src="docs/compact-before.png" alt="/context before compacting: 33 items, ~3,922 tokens, tool results 46%" width="880">
</p>

<p align="center"><sub><code>/context</code> after three read-heavy turns: 33 items, ~3,922 tokens, and tool results are 46% of it — the largest slice, and the one that has already been read.</sub></p>

<p align="center">
  <img src="docs/compact-micro.png" alt="/compact micro clearing old tool results for ~949 tokens at no API cost" width="880">
</p>

<p align="center"><sub>The same session, one command later: <code>~949 tokens</code> gone for nothing. The session cost on the status bar does not move, because nothing was sent anywhere.</sub></p>

It is **manual on purpose** and never runs by itself. It is worth doing when the
history is mostly finished tool output and you want to keep reading — it is the
cheapest thing in the program.

### 2 · `/compact` — one summary, paid for once

When the session itself has become long, blanking results is not enough: the
turns, the reasoning and the decisions are the bulk. `/compact` replaces the
whole history with a written summary of it.

The order of operations is deliberate. The transcript is rendered **before**
anything is blanked — micro-compacting first would hand the summariser a page of
`[Old tool result content cleared]` placeholders and it would faithfully
summarise nothing. Rendering caps each piece so a single monster item cannot
dominate: tool results to 1,500 characters, call arguments to 160, message text
to 4,000, and the whole transcript to 60,000. Over that cap it keeps the **tail**,
marked `…[earlier transcript elided]…`, because the recent end of a session is
the part still being worked on. `reasoning` items are dropped outright — they are
the model's scratch work, not the record of what happened.

That rendered text goes to the **subagent model** in an isolated call —
`temperature 0.1`, reasoning off, 2,048 output tokens — with a system prompt that
asks for five things in order:

1. goals and constraints still in force
2. files read or modified, with the key facts about them
3. decisions taken, and why
4. work completed
5. what remains, and the next step

The result replaces the history with exactly **two** items: a user item carrying
the summary, and an assistant item acknowledging it. The session continues from
there as if that summary were what you had said all along.

<p align="center">
  <img src="docs/compact-summary.png" alt="/compact reporting 33 items compacted to 2, ~1,448 tokens reclaimed, summary cost $0.00086" width="880">
</p>

<p align="center"><sub>Both sides of the trade on one line: <code>Compacted 33 items → 2 (~1,448 tokens reclaimed · summary cost $0.00086)</code>. The summary is a real API call and it is billed as one — that call is charged to the subagent scope, so <code>/cost</code> shows it separately from your turns.</sub></p>

<p align="center">
  <img src="docs/compact-after.png" alt="/context after compaction: two items, ~1,348 tokens" width="880">
</p>

<p align="center"><sub>Afterwards: two items, ~1,348 tokens. Everything that was thirty-three items of reads, calls and replies is now one paragraph the model wrote about itself.</sub></p>

Compaction can fail to pay for itself, and when it does it says so. If the
summary comes out larger than what it replaced — a short session with a long
answer in it — the report reads `no reduction — the summary is ~N tokens larger`
rather than claiming a win. Nothing is rolled back; you are told.

Two useful forms:

```
/compact                      # summarise everything
/compact the auth refactor    # anything else is a focus hint for the summary
```

The focus form appends *"Focus the summary on: …"* to the request, which is worth
using when the session covered several unrelated things and only one of them is
still live.

### Automatic compaction

`/compact` also runs by itself. Before each turn the orchestrator compares the
context against the window, and if `auto_compact` is on and the fraction has
reached `compact_threshold` — **85% of 900,000 tokens** by default — it prints

```
Context at 86% → auto-compacting.
```

and compacts before continuing. That is the *only* automatic mechanism: there is
no sliding window and nothing silently drops the oldest items behind your back.
Either you compact, or auto-compact does it at 85% and tells you, or the history
stays as it is.

```
/compact auto off        # never compact on its own
/compact auto on         # restore (saved to config.json)
```

Turning it off does not raise the ceiling; it hands you the responsibility for
staying under it.

### What is not here

The on-disk cache has a `session_facts` table, and helpers to read and write it,
intended as a middle layer that would carry durable facts across compactions.
**Nothing in the shipped code calls them.** The two layers described above are
the two that run. It is listed here so that finding the table in `cache.db` does
not read as a feature you failed to switch on.

### Watching it

`/context` is the instrument for all of this — items, estimated tokens, and the
share taken by each kind of content, with a hint naming the cheapest thing to do
about it. Check it before compacting and after; the numbers in the four captures
above are from one continuous session and nothing else.

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

<p align="center">
  <img src="docs/signals.png" alt="The /signals panel: prefix hit rate against baseline, unknown item types, unexplained empty responses" width="880">
</p>

<p align="center"><sub>What the loop noticed and nobody raised an error about. A clean panel is the normal case — the point is that it is checkable rather than assumed.</sub></p>

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
`/help <command>` explains any one of them in full. Every command has short
aliases, listed in italics below — you never have to type `/permissions` when
`/perm` will do.

<p align="center">
  <img src="docs/help.png" alt="The /help panel: the whole command surface on one screen, grouped by what it touches" width="880">
</p>

<details open>
<summary><b>Session</b></summary>

| Command | Action |
|---|---|
| `/help [command]` *(`/?`, `/h`, `/commands`)* | List commands, or explain one in detail |
| `/status` *(`/dash`)* | One-screen dashboard: model, context, cost, vision, cache |
| `/clear` *(`/reset`)* | Reset the conversation and usage counters |
| `/exit` *(`/quit`, `/q`)* | Quit |

</details>

<details open>
<summary><b>Model</b></summary>

| Command | Action |
|---|---|
| `/model [flash]` | Show or set the orchestrator model — `flash` is the only one the Responses API serves |
| `/thinking [low\|high\|max\|off]` *(`/effort`)* | Set reasoning effort, or turn thinking off |
| `/verbose [low\|mid\|high]` *(`/v`)* | How much detail to print while working |
| `/steps [n\|off\|default]` *(`/iterations`, `/maxsteps`)* | Cap the tool steps one turn may take before it gives up (default 100; `off` for no cap, with progress printed every 10 steps) |

</details>

<details open>
<summary><b>Context & cost</b></summary>

| Command | Action |
|---|---|
| `/compact [focus]` | Summarise the history — see [How compaction works](#how-compaction-works) |
| `/compact micro` *(`local`, `free`)* | Blank old tool results locally; no API call, no cost |
| `/compact auto on\|off` | Toggle automatic compaction at the threshold, and save it |
| `/context` | Break down what is occupying the context window, by role and size |
| `/cost` *(`/usage`)* | Token usage and spend, split by cache hit/miss, with a per-turn sparkline |
| `/settings [save\|reset]` *(`/config`)* | Every setting, marking which persist; save or restore defaults |
| `/signals` *(`/drift`)* | Prefix hit rate against its own history, unknown item types, unexplained empty responses |

</details>

<details open>
<summary><b>Tools</b></summary>

| Command | Action |
|---|---|
| `/tools` *(`/permissions`, `/perm`)* | List tools, their permission level, and how often each ran |
| `/tools <tool> allow\|ask\|block` | Change one tool's permission |
| `/fetch <url>` | Fetch a URL yourself, with the cache/fresh prompt |
| `/fetch status` | Report how closely fetches imitate a real browser |
| `/fetch cookies clear` | Drop every stored cookie and start the next fetch cold |
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
| `/vision` *(`/config_vision`)* | Interactive picker of installed vision models |
| `/vision <model>` · `/vision off` | Enable a specific model, or disable |
| `/vision backend <name>` | Choose the vision provider. The setting accepts `none`, `ollama`, `glm`, `openai` and `local`; **only `ollama` is implemented**, and the command says so rather than failing later. |

</details>

<details>
<summary><b>Work</b></summary>

| Command | Action |
|---|---|
| `/goal <text>` | State a high-level objective; runs one turn at max reasoning effort |

</details>

Two keys are worth knowing and are not commands. **Ctrl-C** cancels the turn in
flight and gives the prompt back — it does not kill the process, so the session
and its history survive an answer you did not want to wait for. **Esc-Enter**
adds a line to the prompt you are typing instead of submitting it.

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

<p align="center">
  <img src="docs/context.png" alt="/context: items, estimated tokens, and the share taken by each kind of content" width="880">
</p>

<p align="center"><sub><code>/context</code> after a single read-heavy turn: tool results are 76% of a ~1,752-token history, and the hint under the table names the cheapest fix rather than leaving you to work it out.</sub></p>

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

<p align="center">
  <img src="docs/vision.png" alt="/vision with no argument: a picker listing the vision models the daemon actually reports" width="880">
</p>

<p align="center"><sub><code>/vision</code> bare opens a picker built from what the daemon reports, not from a hard-coded list — so the models offered are the ones you can actually run.</sub></p>

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

<p align="center">
  <img src="docs/ollama.png" alt="/ollama: which daemon vision is pointed at, whether it answers, its version and latency" width="880">
</p>

<p align="center"><sub><code>/ollama</code> reports the host actually in effect and whether it answered, so "vision is not working" resolves to a specific step rather than a shrug.</sub></p>

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
| `test_step_limit.py` | A turn spends exactly `max_iterations` model calls and no more; the cap is read from the setting on every turn rather than baked in; `0` means uncapped, prints progress as it goes, and reads back as a word; a meaningless value in `config.json` is ignored while any deliberate large one is honoured |

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

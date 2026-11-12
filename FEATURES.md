# g023 Code — Complete Feature Catalog

**Product:** g023 Code 1.5.0
**What it is:** a pure-Python, terminal-native coding agent that talks to one model — DeepSeek V4.1 Flash (`deepseek-flash`) — through `POST /anthropic/v1/messages`.

> **Current state (1.5.0, 2026-11-10) — this block summarises the release; release-by-release changes are in [`CHANGELOG.md`](CHANGELOG.md).**
> Wire: Anthropic messages endpoint via `g023_code/anthropic_codec.py` (internal history is item-shaped; see `docs/anthropic_codec.md`).
> Web: client tools WebSearch + FetchUrl on DeepSeek's Search/Fetch APIs ($0.75 / 1,000 each, shown in `/cost`) through one machine-wide cache with near-duplicate query reuse and search-primed pages.
> FetchUrl: bounded page view (outline, char window, BM25 `focus`); `/fetch <url>`, `/search <query>`, `/cache web`.
> Subagents: **off by default** — no `Agent` tool, ReadFile `focus` ranked locally; `/subagents on` restores both.
> Context: 1M window, auto-compact at **200k tokens**, also checked mid-turn after every tool batch; summaries carry a `[recall]` pointer to the session transcript.
> Long docs/codebases (1.5.0): plain-text files get an induced section outline (acts/chapters/works); C/C++ functions and Java/C# methods are indexed; ReadFile `focus` resolves an exact phrase, a section path or a symbol locally, and every hit carries its section/function breadcrumb (`docs/docindex.md`).
> One model (`config.MODEL`, a constant), one wire, one key (`K.dat`); OpenRouter, Ollama, the `/responses` wire and `deepseek-v4-pro` were removed in 1.4.0. Vision is one isolated call on the same model.
> Cost economics (1.5.0): ski-rental compaction timing (`economics.py`), a lossless tool-result codec at the history boundary (`tool_codec.py`), and the `[handoff]` summary requested only when compaction is forecast.
> Write side (1.5.0): `edit_engine.py` (match ladder incl. affine indentation, byte-faithful atomic writes), verify replay, and counterfactual failure attribution (`attribution.py`). Paths under the home directory print as `~`.
> Reliability (1.5.0): Ctrl-C cancels only the running work (`interrupts.py`); a turn rolls back as a transaction and its final answer is the commit point; `/resume` replays exactly what the model saw; stores are crash- and race-safe (`fsutil.py`); `/undo` is a stack; bounded API retries. See [Interruption, retries and crash safety](#interruption-retries-and-crash-safety).
> Checks on the model (1.5.0): a test oracle behind filtered pytest output, request obligations and write guards, a claim check on "tests pass", callee digests on ReadFile. Lean turns, view deltas, smart reads of binaries/data/logs, near-miss names on a zero-hit search, code ownership, step budgets and predicated finish are on by default; search tiers, definition peek, scope count, the exploration ledger, ordered batches and write checks are opt-in (measured neutral or cost-neutral).
**Who this page is for:** a mid-level engineer who wants to understand every feature, how it is wired, why it exists, and what is actually measured versus what is a design claim.

This is the feature catalog, not a substitute for the per-module pages in [`docs/`](docs/README.md). Those pages are the source of truth for file-level behaviour. This page is the map of *what the harness can do*, *how the pieces fit*, and *what you should believe about each piece*.

---

## How to read this document

Every feature below is written in five layers on purpose:

1. **In simple terms** — what a user experiences, without the internals.
2. **Architecture / how it works** — the call path, the data it stores, the invariants it protects.
3. **For a mid-level engineer** — the details you need to change it without breaking prefix cache, history pairing, or the bill.
4. **Observations** — what was measured, what was tried and rejected, and what is *not* known.
5. **Benefits** — why the feature is in the product.

Numbers in this document are one of three things: checked offline during development, quoted from DeepSeek's published price sheet (`ds_pricing.json`, effective 2026-09-10), or labelled as an estimate / unmeasured. Where something looks like a cost win but has not been A/B'd end-to-end, this page says so.

---

## Table of contents

1. [What the harness is](#1-what-the-harness-is)
2. [The ideas it is built around](#2-the-ideas-it-is-built-around)
3. [System architecture](#3-system-architecture)
4. [Feature catalog](#4-feature-catalog)
   - [4.1 Launch, install, and process model](#41-launch-install-and-process-model)
   - [4.2 Orchestrator (the agent loop)](#42-orchestrator-the-agent-loop)
   - [4.3 DeepSeek API client](#43-deepseek-api-client)
   - [4.4 Summary-first delegation](#44-summary-first-delegation)
   - [4.5 FileReader](#45-filereader)
   - [4.6 Searcher and Glob](#46-searcher-and-glob)
   - [4.7 Nested Agent workers](#47-nested-agent-workers)
   - [4.8 Vision](#48-vision)
   - [4.9 WebSearch and the machine-wide web cache](#49-websearch-and-the-machine-wide-web-cache)
   - [4.10 FetchUrl (DeepSeek Fetch API, bounded page view)](#410-fetchurl-deepseek-fetch-api-bounded-page-view)
   - [4.11 Lightweight acting tools](#411-lightweight-acting-tools)
   - [4.12 Permission model](#412-permission-model)
   - [4.13 Project-root confinement](#413-project-root-confinement)
   - [4.14 Prefix cache and cost policy](#414-prefix-cache-and-cost-policy)
   - [4.15 History integrity](#415-history-integrity)
   - [4.16 Compaction (Layer 1, Layer 3, post-turn)](#416-compaction-layer-1-layer-3-post-turn)
   - [4.17 Tool vault](#417-tool-vault)
   - [4.18 SQLite cache](#418-sqlite-cache)
   - [4.19 Usage and cost accounting](#419-usage-and-cost-accounting)
   - [4.20 Surge (tidal) pricing](#420-surge-tidal-pricing)
   - [4.21 Drift signals](#421-drift-signals)
   - [4.22 Reasoning effort and same-response trailers](#422-reasoning-effort-and-same-response-trailers)
   - [4.23 Working set and goal judge](#423-working-set-and-goal-judge)
   - [4.24 Session log, `/resume`, `--continue`](#424-session-log-resume---continue)
   - [4.25 Learn store](#425-learn-store)
   - [4.26 Memory notes](#426-memory-notes)
   - [4.27 Prompt store](#427-prompt-store)
   - [4.28 Project instructions (`g023.md` / `AGENTS.md`)](#428-project-instructions-g023md--agentsmd)
   - [4.29 Configuration scopes](#429-configuration-scopes)
   - [4.30 CLI, slash commands, and the REPL](#430-cli-slash-commands-and-the-repl)
   - [4.31 Terminal UI](#431-terminal-ui)
   - [4.32 Input line](#432-input-line)
   - [4.33 Write backups and `/undo`](#433-write-backups-and-undo)
   - [4.34 Parallel tool execution](#434-parallel-tool-execution)
   - [4.35 Streaming](#435-streaming)
   - [4.36 Output-limit auto-continue](#436-output-limit-auto-continue)
   - [4.37 Step cap, max-tokens, context window](#437-step-cap-max-tokens-context-window)
   - [4.38 Verbosity](#438-verbosity)
   - [4.39 Release media and capture tooling](#439-release-media-and-capture-tooling)
   - [4.40 Communication markers (`A_Improve.txt`)](#440-communication-markers-a_improvetxt)
   - [4.41 Data files and on-disk layout](#441-data-files-and-on-disk-layout)
   - [4.42 Bulletin board (`.g023/bbs/`)](#442-bulletin-board-g023bbs)
   - [4.43 Code ownership (`/ownership`)](#443-code-ownership-ownership)
   - [4.44 Near-miss names on a zero-hit search](#444-near-miss-names-on-a-zero-hit-search)
   - [4.45 Predicated finish (on by default since 4.52) and `/settings set`](#445-predicated-finish-on-by-default-since-452-and-settings-set)
   - [4.46 Lean turns: closure receipts, lean codec, lean prompt, orientation card](#446-lean-turns-closure-receipts-lean-codec-lean-prompt-orientation-card)
   - [4.47 View deltas: what a shell command changed, against what the model last saw](#447-view-deltas-what-a-shell-command-changed-against-what-the-model-last-saw)
   - [4.48 Test oracle, request obligations, claim check and callee digests](#448-test-oracle-request-obligations-claim-check-and-callee-digests)
   - [4.49 Salient excerpts, readable vault, and the optional local model](#449-salient-excerpts-readable-vault-and-the-optional-local-model)
   - [4.50 Scout card: the local model does the first digging hop](#450-scout-card-the-local-model-does-the-first-digging-hop)
   - [4.51 Local model auto-tune](#451-local-model-auto-tune)
   - [4.52 Round-trip economics: step budgets on, predicated finish on, two opt-ins](#452-round-trip-economics-step-budgets-on-predicated-finish-on-two-opt-ins)
   - [4.53 Pre-emptive reads: the harness does the predictable first hop](#453-pre-emptive-reads-the-harness-does-the-predictable-first-hop)
   - [4.54 Closing calls: licensed finish, answer escrow, multi-file Edit, Rename](#454-closing-calls-licensed-finish-answer-escrow-multi-file-edit-rename)
5. [Cross-cutting observations](#5-cross-cutting-observations)
6. [What is not claimed](#6-what-is-not-claimed)
7. [Module map](#7-module-map)
8. [Environment variables](#8-environment-variables)
9. [Slash-command index](#9-slash-command-index)

---

## 1. What the harness is

g023 Code is an interactive coding agent you run from a project directory. You type a request; it reads the tree, searches, fetches pages, runs commands, edits files, and tells you what it did and what that just cost.

It is **not** a chat wrapper around a completion API. The whole program is built on DeepSeek's **Anthropic-format messages API**, which also serves DeepSeek's Search and Fetch tool APIs. Conversation history is a list of typed **items** (`message`, `reasoning`, `function_call` / `function_call_output`, `web_search_call`), not a list of chat-role messages.

Every call — orchestrator, FileReader, nested Agent, `/g023-compact`, vision — goes to **`deepseek-flash`** (DeepSeek V4.1 Flash, 1M context). The model is a constant, not a setting.

Shipped defaults (what a fresh install writes): thinking on at effort **75** (wire alias `high`), verbosity **low**, auto-compact **on** at 200k tokens of a 1M window, subagents **off**, acting tools set to **ask**, vision **on**.

---

## 2. The ideas it is built around

These are not slogans. They are load-bearing constraints.

**Context is currency, subagents are the treasury.** The orchestrator never ingests a whole file unless it asked for a line range or `raw=true`. Heavy I/O happens in isolated workers that return compact JSON.

**A hot prefix is cheaper to keep than to shrink.** DeepSeek's prompt cache hits only on a full prefix-unit match from token 0 (their Example 1: A+B then A+B+C hits; Example 2: A+B then A+C misses). Off-peak Flash: cache hit $0.003/M, miss $0.15/M (~50×), output $0.60/M (~200× a hit). Blanking 500 cached tokens to "save" them busts a ~3k tail; recoup is ~294 further turns. Coding sessions do not last 294 turns.

**History integrity is a hard invariant.** A `function_call` without a matching `function_call_output` is a 400, and the broken history is re-sent on the next turn, so the session dies until repaired. A user-role working-set item sitting between a parallel tool batch and its outputs is also a 400.

**Prefix stability is a hard invariant.** Standing `instructions`, the tool list, reasoning effort, and temperature are frozen until the user changes vision, or effort is retuned on a prefix that is already new (`/thinking`, `/clear`, or a successful post-turn compact). `/goal` retunes only for its own turn, then leaves the pin. The working-set item is never rewritten in place.

**The client must not invent limits.** `max_tokens` defaults to "send no budget" (0). `max_iterations` can be 0 (unlimited). What replaces a ceiling is visibility: iteration notices, `/signals`, Ctrl-C.

**Quiet failure is the real failure.** The client does not schema-validate responses. A renamed content field returns `""` and lights up `/signals` instead of crashing.

**Retrieve, don't prepend.** Learned procedures, memory notes, and bulletin-board posts live in files and are searched on demand. Prepending them to the system prompt would bust the prefix cache every time INDEX grew.

**Claim only what is checked.** Call counts are checked offline. End-to-end session cost is not. `G023_READFILE_RAW=1` is the A/B flag; `/cost` is the meter.

---

## 3. System architecture

```
g023.sh / g023.bat                 launchers (G023_HOME, G023_PROJECT_ROOT)
installer.sh / installer.bat       venv, deps, K.dat, config.json, PATH shim
        │
g023_code/__main__.py              python -m g023_code → cli.main()
g023_code/cli.py                   REPL, slash commands, one-shot, --continue
g023_code/commands.py              command catalogue as data (help, tab, "did you mean")
g023_code/prompt.py                input line, pickers, Ctrl+Up
        │
        ▼
g023_code/orchestrator.py          per-turn agent loop (items, not chat messages)
        │  talks to DeepSeek through
        ▼
g023_code/api.py                   DeepSeekClient: create / stream / vision
        │
        ├── heavy tools            subagents/router.py
        │        ├── file_reader   ReadFile → JSON summary / range / raw
        │        ├── searcher      SearchContent / Glob  (rg | grep | walk)
        │        ├── vision        AnalyzeImage → images.py → DeepSeekClient.vision
        │        └── _run_agent    Agent (research/planning/todos/task), depth 1
        └── light tools            tools/registry.py
                 Bash, WriteFile, Edit, ListDir, Glob, TodoWrite,
                 WebSearch, FetchUrl, LearnSearch, LearnGet, LearnPut, SessionSearch
```

Supporting modules sit under all of the above: `config`, `paths`, `cache`, `cost_policy`, `usage`, `signals`, `surge`, `effort`, `working_set`, `session_log`, `learn`, `memory_notes`, `bbs`, `prompt_store`, `project_instructions`, `ui`, `web`, `images`.

**One session, end to end:**

1. Launch sets `G023_HOME` (install folder: `K.dat`, machine `config.json`) and `G023_PROJECT_ROOT` (the project you launched from).
2. `cli.main()` handles `--help` / `--version` / `--continue` / a one-shot prompt / the REPL.
3. The API key is loaded from `K.dat`. A surge monitor starts.
4. A non-slash line becomes a prompt. `Orchestrator.run_turn`:
   - writes the prompt to `.g023/prompts/{n}.txt`
   - sends frozen instructions (system prompt + `g023_compact.md` else `g023.md` + optional `AGENTS.md`) plus frozen tools
   - auto-compacts only under window pressure
   - iterates: model call → parse → record usage/signals → run tools (some ALLOW reads in parallel) → repeat
   - strips `[handoff]` / `[next_effort: N]` from the visible answer
   - may post-turn compact into a handoff written to `.g023/logs/`
5. Every model call is metered. The cache keeps file summaries, vision answers, web fetches, vaulted tool bodies, prefix-hit stats, and the working set.

---

## 4. Feature catalog

### 4.1 Launch, install, and process model

**In simple terms:** drop the folder somewhere, run the installer, type `g023` from any project. It finds Python, builds a venv, asks for a DeepSeek key, and puts `g023` on your PATH.

**Architecture / how it works:**

- `installer.sh` / `installer.bat` are idempotent. Re-running repairs a half-finished setup. They check Python 3.11+, create `.venv/`, install dependencies in three tiers (required / recommended / optional), write `K.dat` and a default machine `config.json` if missing, and install a launcher shim.
- `g023.sh` / `g023.bat` export `G023_HOME` and `G023_PROJECT_ROOT`, prefer the bundled venv, and `exec python -m g023_code` with remaining argv.
- `g023_code/__main__.py` calls `cli.main()`. `--help` prints the slash-command catalogue and exits 0 without building a Reader. `--version` prints `g023 Code 1.5.0`. `--continue` / `-c` seeds from the latest `.g023/logs/` handoff (else JSONL). Remaining argv is one prompt then exit. No argv starts the REPL.

Dependency tiers (`requirements.txt`):

| Tier | Packages | If missing |
|------|----------|------------|
| Required | `rich`, `httpx`, `pygments` | will not start |
| Recommended | `prompt_toolkit` | input degrades to a plain prompt |
| Optional (vision) | `pillow` | oversized images are refused rather than downscaled |

**For a mid-level engineer:**

- Installer options: `--yes` / `/y` (non-interactive), `--key`, `--with-optional` / `--no-optional`, `--no-venv`, `--no-path`, `--uninstall` (removes the shim, leaves the folder / venv / `K.dat` / `config.json`).
- Placeholder keys (`YOUR_DEEPSEEK_API_KEY_HERE`, `sk-REPLACE-WITH-YOUR-DEEPSEEK-API-KEY`) are recognised and turned into a clear instruction instead of a confusing 401.
- There is no local tokenizer. The context gauge is last API `input_tokens` / `settings.max_context_tokens`. `tiktoken` was removed.
- `__init__.py` must not reassign `__name__` — that breaks `from g023_code import <submodule>`.

**Observations:**

- The installer-written `config.json` is the full `PERSISTED_KEYS` set matching `Settings` defaults. Older files that still name `deepseek-v4-flash` or `"high"` coerce on load.
- One-shot and piped runs cannot complete ASK tools unless `permission_default` is `allow` (Ctrl-D / closed stdin decline). Unattended evals need a project override; this fired on the G023 Legends run and was fixed with a clearer decline hint.

**Benefits:**

- Zero-ceremony setup that is safe to re-run.
- Optional deps degrade instead of blocking install.
- Launchers split "where the program lives" from "which project it is working on", which is what lets one install serve many repos.

---

### 4.2 Orchestrator (the agent loop)

**In simple terms:** one user prompt becomes as many model→tools cycles as needed. The model thinks, calls tools, sees compact results, and eventually answers. You see a live trace and a cost line.

**Architecture / how it works:**

`g023_code/orchestrator.py` is the reasoning engine. History is `OrchestratorState.items`: a flat list of items (message / reasoning / function_call / function_call_output; `web_search_call` only in sessions recorded before 1.5.0).

`run_turn(user_input)`:

1. `save_prompt` → `.g023/prompts/{n}.txt`. Usage turn snapshot. Learn get-budget reset.
2. Auto-compact if enabled and the real (API) context gauge is over threshold. Layer 1 blanks aged tool results in place **only under window pressure**. Layer 3 summarises only if still over threshold on a real gauge.
3. Append the user message. Iterate until the model answers or the step cap trips.
4. Each iteration: `_call_model` (stream or block) → `parse_reply` → record usage and drift signals → apply `[handoff]` / `[next_effort]` trailers → if tool calls, `_run_tool_calls` and continue → if `incomplete_reason == max_output_tokens` with no tools, auto-continue up to 3 times → else done.
5. Post-turn compact may replace history with standing instructions + working-set item + summary, and write `.g023/logs/{stamp}.log`.

Heavy tools (`ReadFile`, `SearchContent`, `AnalyzeImage`, `Agent`) go to `subagents.router`. Light tools go to `tools.registry`. WebSearch and FetchUrl run in `tools.registry` through `web.py`. Replayed `web_search_call` items from old sessions are never executed.

**For a mid-level engineer:**

- Standing `instructions` and `tools` are frozen (`_frozen_instructions`, `_frozen_tools`) until vision changes. A sha256 fingerprint of both is written to the session JSONL on first use so `/resume` can warn on mismatch.
- Effort and temperature are pinned to that prefix (`_prefix_effort_wire`, `_prefix_temperature`). `release_prefix_effort()` runs on `/thinking`, `/clear`, the start of `/goal` (so that turn can send the ceiling), and a successful post-turn compact — not when `/goal` returns, and not on a hot continuation. The goal transcript stays a hit; a stored `[next_effort]` is sent once that input list has been replaced.
- `_request_body` publishes the frozen prefix onto the subagent router so nested Agent calls reuse it.
- `max_output_tokens` is omitted when unlimited. Sending 0 would ask for an empty reply. Adding the field at all is a cache-key change, which is why nested Agent also omits it.
- Streaming deltas are for the eye. The authoritative result is the terminal `response` object in the SSE stream, so reassembly bugs cannot poison history.
- Live panels paint `visible_reply_text`, so an in-progress `[handoff]` trailer does not flash on screen.
- After WriteFile/Edit, `working_set.note_file`. After a Bash command that looks like a test runner, `working_set.note_verify`. Mirroring waits until **every** output in a parallel batch is recorded.
- Write side (1.5.0): repeat guard keyed on (write epoch, call), so test → edit → test is never blocked; verify replay attaches the re-run of the last failing check to a fixing write; Bash kills the whole process group on timeout (partial output kept), shims `python` → `python3`, and hints on exit 127; small files (≤ 4k chars) are read whole; the standing prompt names the project root, OS, shell and python.
- Stop discipline (1.5.0): counterfactual failure attribution — a failing check after this turn's edits is re-run in a shadow copy with the edits reverted, and its result gains `attribution: {new, pre_existing, fixed_by_edits, verdict}`, so the model no longer spends hops asking "did I break this?"; check-command detection covers venv/uv/poetry/timeout/VAR=x/yarn/pnpm/bun/npx forms; the handoff trailer no longer fires on turns with no todos.

**Observations:**

- Live probe 2026-09-14: dropping reasoning items from the wire cut hit rate from 96.1% to 48.2% on the next turn (~7× cost). Echoing reasoning is how the next request matches the previous output-end cache unit.
- Flattening compact onto a different `COMPACT_SYSTEM` + `effort none` + `max_output_tokens` missed from token 0. Shipped compact appends a user prompt to the live list instead.
- A `/goal` that thought until the output ceiling used to end mid-thought with nothing written. Auto-continue (cap 3) was added after that fired on a real project.

**Benefits:**

- One loop, one endpoint, one price sheet.
- The model can search the web *inside* a turn and keep going.
- Failures roll back to a paired history instead of poisoning the next turn.
- The user always sees what happened and what it cost.

---

### 4.3 DeepSeek API client

**In simple terms:** one HTTP client talks to DeepSeek, about one model. Everything else — orchestrator, FileReader LLM path, nested Agent, vision — goes through it.

**Architecture / how it works:**

`g023_code/api.py` wraps `httpx.AsyncClient` against `https://api.deepseek.com` (`settings.base_url`) + `/anthropic/v1/messages`.

- `create(...)` — one blocking call; the body is translated by `anthropic_codec.to_messages_body`, the reply by `from_message`.
- `stream(...)` — async iterator of item-shaped events from `StreamTranslator` (the live UI path).
- `vision(prompt, data_uri, ...)` — one isolated image question, thinking off.
- `_wire_body` forces `model = config.MODEL`; a caller cannot choose another model.
- Timeouts are generous because server-side web search can run a 5–20 search loop inside one response.

Parsing helpers absorb schema drift: `output_text`, `reasoning_text`, `incomplete_reason`, `unknown_item_types`, `silent_degradation`. `reasoning_param` always sends `{"effort": <level>}`; the codec turns `none` into `thinking: {"type": "disabled"}` and `low` / `high` into `thinking: enabled` + `output_config.effort`.

**For a mid-level engineer:**

- The client does **not** validate responses. A renamed field must not crash the session; it should return `""` and light up signals.
- `DeepSeekError.from_response` extracts the API's error message (or a body excerpt).
- `get_client()` is a module singleton.
- The endpoint's real effort levels are `low` / `high` / `max` (it folds `minimal` → low, `medium` / `xhigh` → high). The harness sends only `none` / `low` / `high`; `MAX_EFFORT = 75` → `high`. `max` often burns the entire output budget on thinking and returns an empty answer.

**Observations:**

- Numbers for effort are rejected; only the level strings are accepted.
- Hits land on a 128-token grain. Tiny chat-only reasoning (14–76 tokens) is below that grain; the tool-loop is the agentic case that matters.

**Benefits:**

- One place to change auth, timeouts, and drift detection.
- No config file can name a model the API will not serve.
- Quiet change becomes visible instead of looking like a silent model.

---

### 4.4 Summary-first delegation

**In simple terms:** the main model never reads a warehouse of bytes. It sends a specialist, and the specialist brings back a one-page briefing.

**Architecture / how it works:**

`SUBAGENT_TOOLS = ("ReadFile", "SearchContent", "AnalyzeImage", "Agent")`. The orchestrator calls `subagents.router.get_router().delegate(...)`. Parent `state.items` never receive nested `function_call` items from Agent.

This is the product's core design, inspired by Claude Code, taken as a hard rule: data-heavy work happens in an isolated context; the orchestrator keeps high-level reasoning, tool schemas, and compact summaries.

**For a mid-level engineer:**

- Delegation is **not free**. A semantic focused ReadFile is a second Flash call. An unfocused ReadFile is local (0 calls). Search is always local. The honest claim is smaller context, not an unconditionally cheaper bill.
- `G023_READFILE_RAW=1` makes ReadFile return raw content with zero API calls — the baseline a plain agent loop would have. Use it to A/B `/cost` on a real codebase.
- Nested Agent reuses the parent's frozen instructions + tools + effort. Isolated input still misses from the first user row; that is the point of the branch.
- Depth-2 (grandchild Agent) was measured and not shipped: another miss on a new input prefix plus another hop of output tokens.

**Observations (measured call counts, against a stub client that counts every API call):**

| Operation | API calls |
|-----------|-----------|
| SearchContent, any size | 0 |
| ReadFile, unfocused (any language) | 0 — local index |
| ReadFile, same bytes again (cache hit) | 0 |
| ReadFile with `start_line`/`end_line` | 0 — verbatim |
| ReadFile with `raw=true` | 0 — whole file, capped 200k |
| ReadFile, focus is exact / unique-suffix symbol | 0 |
| ReadFile, semantic focus sentence | 1 Flash |

**Benefits:**

- The orchestrator window stays small enough to stay on task.
- Escalation to verbatim source is cheap and exact (line ranges from the AST, not from the model).
- Specialists can fail without corrupting parent history.

---

### 4.5 FileReader

**In simple terms:** "read this file" returns a map of what is in it, not the file. If you then ask for lines 412–498, you get those lines word for word.

**Architecture / how it works:**

`g023_code/subagents/file_reader.py`. Paths go through `paths.confine_or_error`. Output is always JSON with a `summary`, a `symbols` map (name → `[start_line, end_line]`), and coverage notes.

Modes:

- **Local index** — unfocused ReadFile. AST for Python; regex/structure for JS/TS, Go, Rust, Java/C# (methods), C/C++ (function spans), Markdown, plain text (induced outline), JSON, YAML/TOML, HTML, shell. Zero API calls. Over a 2.4k budget the outline folds (symbols grouped by prefix, sections with +N), never silently. Scan cap `LOCAL_INDEX_MAX_CHARS = 2_000_000` for code; text outlines cover the whole file.
- **Exact / unique-suffix focus** — if the focus is already a symbol in that map (or a unique method suffix, e.g. `login` matching `User.login`), local lookup, 0 API. Small bodies (`LOCAL_SYMBOL_BODY_MAX_CHARS = 2_000`) may be included.
- **Semantic focus** — a sentence like "what does method_one do" goes to Flash (`LLM_INPUT_MAX_CHARS = 18_000`, `max_output_tokens = settings.file_summary_max_tokens` default 400). `_merge_metadata` joins local AST facts onto the model summary so the model's opinion cannot outrank line numbers.
- **Range** — `start_line`/`end_line` → verbatim slice, cap 20_000 chars. Line numbers are ground truth from the file.
- **Raw** — `raw=true` → full verbatim, cap 200_000 chars. Above that the read is refused and the caller is pointed at ranges.
- **Cache** — keyed by content hash (+ focus). `invalidate_file` on change.
- **Binary / huge / data files (`shapes.py`)** — the first 8 KB is sniffed before any full read. Binaries return `mode: binary` with a structural digest: image dims, archive members, SQLite tables and row counts, ELF/PE/Mach-O headers, PDF info, entropy/strings/hexdump. Text over 64 MB is streamed. Lines over 2,000 chars are clipped with a "continues" pointer, and focus hits inside them get a 1,200-char window. Unfocused CSV/TSV, JSON, JSONL and log reads carry column profiles with exact row counts, the full JSON shape with array lengths, the record shape, and line templates with exact counts of rare lines. See `docs/shapes.md`.

**For a mid-level engineer:**

- Summaries **omit code bodies**. Reconstructing a file from a summary silently truncates it. That is how `config.py` got truncated once. The tool schema, the system prompt, and WriteFile's description all say: hold exact bytes (`raw=true` or every range) before a full rewrite; prefer Edit for a unique span.
- Do **not** slice ReadFile JSON at 6000 chars to "save context". A 10,505-char `orchestrator.py` summary became an unparseable 5,590-char stump and the symbol map vanished. Vault already offloads Bash/ListDir; the compact form stays intact.
- Sharing the parent prefix for focused FileReader was measured and rejected. FileReader instructions are 802 chars (miss ≈ $0.000030); the parent prefix is 5,267 chars (miss ≈ $0.000197) plus high effort on a none-thinking extract.
- Caps set explicit truncation flags so the orchestrator knows when it got a partial answer.
- `end_line`-only range reads are spared from micro-compact the same way `start_line` is — the model still needs those bytes to Edit.

**Observations:**

- The known weakness of summary-first: a summary can silently drop the one detail that mattered, and nothing detects that. The mitigation is the symbol map plus a cheap range re-read, not an automatic detector. That detector does not exist.
- Local AST facts outrank the model.
- Live bench, 9 read tasks on huge dirs, binaries and big data files: Bash blocked went from 6/9 solved, 71 calls, $0.1824 to 8/9, 30 calls, $0.0055. Bash allowed went from 9/9, 31 calls, $0.1572 to 9/9, 23 calls, $0.0026. Most of the old cost was a 1.6M-char minified line read as a "range". The last nobash miss (CSV rows) was fixed by correcting the line count and pointing the schema at the digest; on re-run the task solves in one read.

**Benefits:**

- Unfocused reads of any supported language cost 0 API calls.
- Escalation is precise: one targeted ReadFile with two line numbers, no API call.
- Cache makes a repeated read of unchanged bytes a local lookup.

---

### 4.6 Searcher and Glob

**In simple terms:** grep the project. You get "47 matches across 12 files, here are 12 samples" — not 47 full files dumped into the chat.

**Architecture / how it works:**

`g023_code/subagents/searcher.py`. Zero API calls. Backend, in order: `rg --json` if ripgrep is on PATH, else `grep -R`, else a Python walk (`search_backend_name()`).

- Total occurrence count is reported separately from a sample list. Default sample cap `settings.max_search_matches = 12`. Count ceiling 50,000.
- `.gitignore` is honoured even when the folder is not a git repo.
- `IGNORE_DIRS` prunes `.git`, `node_modules`, `.venv`, `dist`, `build`, `.g023`, etc. An overlay still lets SearchContent read `.g023/memory/` and `.g023/bbs/`.
- Globs: a bare extension becomes `*.ext`; comma-separated parts and brace expansion (`*.{ts,js}`) are supported.
- `glob_sync` / `run_glob` back the Glob tool: `**` and brace expansion, paths relative to the project root, confined. Glob does not use ListDir's depth-2 / 80-entry listing.
- **Literal intent:** a directory or extension named literally in the pattern or path (`node_modules/**/*.js`, `*.png`) lifts the ignore list for that call and is reported under `included_ignored`. Previously those globs returned `total: 0`. More than 200 hits return a templated `digest` over *all* hits plus the first 20 paths.
- **ListDir digests (`dirmap.py`):** a truncated or large listing becomes a templated map in which every child is counted (`frame_{00000..03999}.png  x4000`), sibling dirs collapse with their shared layout, and recursive listings give each subdirectory a fair share of rows. See `docs/dirmap.md`.

**For a mid-level engineer:**

- Presenting a sample cap as a total cap would be a lie; the total is always reported separately from the samples.
- Search results are vaulted when ≥ 2,500 chars, same as Bash/ListDir.
- The Python walk is the last resort; prefer having `rg` on PATH for speed, not for correctness.

**Observations:**

- Search is the cheapest "where is this symbol" tool in the harness. FileReader's local symbol map is the cheapest "where is this symbol *in this file*". Using Flash for either is the expensive path.

**Benefits:**

- Metadata-first results keep the orchestrator on the map, not in the haystack.
- Honouring gitignore and ignore-dirs means the model is not grepping `node_modules`.
- Glob is a real find, not a shallow listing with a glob-shaped description.

---

### 4.7 Nested Agent workers

**In simple terms:** the main model can hire a researcher, a planner, a todo-lister, or a task worker. Those workers talk to each other through short JSON relays. The parent never reads their homework.

**Architecture / how it works:**

`Agent` kind is `research` | `planning` | `todos` | `task` (`explore`/`plan` still alias research/planning). Depth 1: the nested loop cannot spawn another Agent, and LearnPut is refused at every kind.

| Kind | Tools offered | Returns |
|------|----------------|---------|
| research / explore | ReadFile, SearchContent, ListDir | JSON findings relay |
| planning / plan | same, read-only | JSON plan relay |
| todos | read-only; then orchestrates task workers | todo list + final summary |
| task | those plus Glob, Edit, WriteFile, Bash | `{result, issues[]}` |

- Cap `AGENT_MAX_STEPS = 8` model calls. Result cap 12,000 chars.
- Bidirectional relay: `prior` / `issues` / `tasks`. Each task worker receives issues from the previous task.
- **Shared prefix:** when the orchestrator has published frozen instructions + tools + effort, nested `create()` reuses them. The worker brief (JSON shape, allowed names) moves into the first user item. Execution still refuses tools the kind does not allow. Without a published prefix (tests, standalone router) the old isolated schemas are used.
- **Branch journal:** each run writes `.g023/scratch/agent/{kind}-{hash}.json` (kind, objective, payload, per-item type/name/chars — not the full transcript). Parent JSON carries `branch` as that relative path.
- Nested Agent vaults bulky Bash/ListDir bodies the same way the parent does.

**For a mid-level engineer:**

- Reasoning is clamped: unset = `high` for every kind; `none` / `low` / `high` are honoured, `minimal` → `low`, `medium` / `max` / `xhigh` / 76–100 → `high`.
- Do not add `max_output_tokens` on the shared-prefix path — it busts the prefix.
- Depth-2 is not "more hierarchy, more savings". A grandchild is another miss plus another hop of output. Add depth only when a single worker's window is the constraint.
- The client-side analogue of a server radix tree is: freeze the standing prefix, fork isolated input lists, merge compact JSON. That is shared-prefix Agent.

**Observations:**

- Children used to miss from token 0 on a different system prompt and a subset tool list. Shared prefix was the cache win; extra hops usually are not.
- Head-slicing nested Agent JSON at 8,000 chars dropped pytest `AssertionError` tails. Vault + tail-heavier excerpts fixed that.

**Benefits:**

- Parent context stays a briefing, not a nested transcript.
- Task workers can edit; research/planning cannot — kind is a real permission boundary, not a prompt suggestion.
- Relays (`issues`) let a pipeline of workers communicate without the parent ingesting any of it.
- The branch journal makes offload inspectable after the fact.

---

### 4.8 Vision

**In simple terms:** the orchestrator never holds image bytes. If you ask about a screenshot, one isolated call to the same model looks at it and sends back text. Vision is on by default; `/vision off` removes the tool.

**Architecture / how it works:**

`AnalyzeImage` is offered to the model only while `vision_enabled` is true — so it never proposes a call it cannot fulfil, and the static prefix stays stable until you change vision.

`images.load_image_for_vision` reads a project-confined path (same `resolve_in_project` as ReadFile) or an http(s) URL, sniffs the real media type (jpeg / png / gif / webp — anything else is refused), and caps each edge at **800 px** (`VISION_SEND_MAX_SIDE`). An image that already fits is sent unchanged; a resized one is re-encoded as PNG. Pillow is required to downscale; without it, oversized images are refused.

`subagents/vision.run_vision` then calls `DeepSeekClient.vision` (`deepseek-flash`, thinking off, 2,048 output tokens), records usage under the subagent scope so `/cost` includes it, and caches the answer by image hash + question. Re-asking the same question about the same image is a SQLite lookup.

**For a mid-level engineer:**

- Enabling or disabling vision **is** a prefix change: instructions gain/lose the vision clause, and `AnalyzeImage` appears/disappears from the tool list. That is intentional and the one user action allowed to bust the standing prefix.
- Same `K.dat` key, same model, same token prices.

**Observations:**

- 1.3.x labelled every image `image/png` and did not record vision usage; both are fixed.

**Benefits:**

- The orchestrator never burns context on image bytes.
- No second provider, daemon or key to configure.

---

### 4.9 WebSearch and the machine-wide web cache

**In simple terms:** the model searches the web through DeepSeek's Search API. Every result is kept in one cache shared by all g023 instances on the machine. The same search, a reworded search, or a page a search already returned costs nothing the second time.

**Architecture / how it works:** `g023_code/web.py` (see [docs/web.md](docs/web.md)), executed by `tools/registry._exec_web_search`. It is a SAFE tool with no prompt.

- `POST /v1/tools/search {query, count: max(count,10), need_content: true}`. The fee is $0.75 / 1k requests, independent of `count`. 408/429/5xx are retried.
- `WebStore` lives at `<install>/global_cache/web.db` (or `$G023_GLOBAL_CACHE_DIR`). It uses SQLite WAL, so instances share it safely, and keeps zlib page bodies and a ledger of paid / cached / primed serves.
- `canonical_query` ignores order, case and trailing punctuation. `query_similarity` reuses a fresh near-duplicate:
  - stemmed-term Jaccard ≥ 0.75, or ≥ 0.6 for near-subsets;
  - `site:`/`-x`/`lang:`/dates must agree, and differing versions veto.
- `ttl_for_query` keeps volatile queries (latest/news/release/price/years) 6 h, others 7 days.
- **Search-primed pages:** each hit's full Markdown is filed in the page table, so a later `FetchUrl` on that hit is free.

```
{"query": "asyncio taskgroup", "results": 5, "source": "cache 3m old", "more": 5}
[1] Coroutines and Tasks — Python 3.14 documentation
    https://docs.python.org/3/library/asyncio-task.html · Python documentation · page cached (44k chars)
    ~700-char abstract…
```

**For a mid-level engineer:**

- **Why not the server-side `web_search` tool any more?** In a live probe, one server search step cost $0.001398, because the server reads the prefix twice. It then replayed about 2.2k opaque tokens every later turn, and nothing could be cached. The client path renders 5 hits in about 1.2k tokens, and repeats cost $0.
- Old sessions that contain `web_search_call` items still replay. The codec emits their stored blocks verbatim, and the API accepts them without the tool declared (verified live).
- Billing happens only when a search returns results. Network failure serves a stale cached copy with a note.

**Benefits:** fees are paid once per machine; reworded queries (3 of 5 in the live replay) are free; pages of search hits are free.

---

### 4.10 FetchUrl (DeepSeek Fetch API, bounded page view)

**In simple terms:** "read this URL as Markdown." Long pages come back as a window plus an outline, or as just the sections that match `focus`. A page is never pasted whole into context.

**Architecture / how it works:** `web.fetch` + `web.render_page`, executed by `tools/registry._exec_fetch_url`. The permission follows `permission_default` (ASK by default), and `confirm_fetch` offers the cached copy.

- `POST /v1/tools/fetch {url}`. The cache key is `canonical_url` (no fragment or tracking params). The TTL comes from `ttl_for_url`: volatile 1 h, pinned 30 d (commit SHAs, arXiv vN, DOIs, PDFs, versioned paths), otherwise 1 d. `max_age` overrides it.
- `cache_mode`: `auto` | `fresh` | `cache`. 403/404/empty responses are negatively cached for 1 h, but never over a good copy. Failures fall back to the cached copy (`stale`).
- How a page is rendered:

| page | output |
|---|---|
| ≤ 8k chars | whole |
| `focus="topic"` | top-3 sections by BM25 (headings 3x), cut to `max_chars` (default 8k, max 40k), plus ranked list and outline |
| otherwise | `max_chars` window from `offset`, `next_offset`, and the outline `@offset heading (size)` |

**Observations:**

- Before 1.5.0, FetchUrl output was in `VAULT_TOOLS`. A 20k page reached the model as 400 head + 800 tail chars: the fee was paid and the model saw almost nothing. It is now bounded at the source and never vaulted.
- The forced ASK used to make unattended runs (`permission_default=allow`) unable to read any page. That is fixed.
- There is no local browser, cookie jar or JavaScript. DeepSeek fetches the page. URLs with credentials are refused before any request is made.
- **Service cut-off.** DeepSeek's Fetch API and Search `need_content` return some sources cut at exactly 20,480 UTF-8 bytes, mid-sentence, with no flag (seen on a docs site, GitHub blob pages and a blog; an arXiv page of 58,541 bytes came back whole). `web.source_truncated` detects the exact size. `_recover_cut` then fetches one plain-text twin (`<url>.md` for extension-less doc paths, `raw.githubusercontent.com` for GitHub blobs), checks with `same_document` that it is the same text, and stores the full body under the original URL. The twin's answer is cached, refusals included, so it is tried once. A page that stays cut renders with `"complete": false` and a note. Live: a Claude docs page went from 20,336 to 30,890 chars, and a GitHub blob from 20,480 to 35,037 bytes.

**Benefits:** a 54k-char PEP page answered from about 10k chars of focused sections; re-reads, other offsets and other focus terms are free (all cached).

---

### 4.11 Lightweight acting tools

**In simple terms:** the main model can list directories, run a shell command, edit a unique span of a file, or overwrite a file — after you say yes (by default).

**Architecture / how it works:**

Executed in `tools/registry.py`. Schemas in `tools/schemas.py` are the definitions shown to the API (`additionalProperties: False`, required fields pinned).

**Bash.** Runs in the project root. Returns stdout/stderr/exit code via `prepare_bash_payload`: strip ANSI, keep pytest FAILURES/ERRORS blocks, elide passing-test chatter, cap stdout 6,000 / stderr 1,500. A small dangerous-string filter (`rm -rf /`, `mkfs`, fork bomb, `dd if=/dev/zero`). Default timeout 60s. Vaulted when large. After a command that looks like a test runner, `working_set.note_verify`.

**WriteFile.** Overwrites the entire file. Backup first (see [4.33](#433-write-backups-and-undo)). Paths outside the project root refused. Identical bytes are a no-op (no backup slot burned). Writing `.g023/memory/MEMORY.md` goes through `memory_notes.write_index` (200-line cap + overflow). Paths under `.g023/bbs/` take a write backup (other `.g023/` paths still skip). Permission prompt shows a unified diff, not a dump of `content`.

**Edit.** Unique-span replace (`edit_engine.apply_edit`: exact → normalised → affine-indent rungs, the rung used is reported). Fails closed if `old_string` is empty, missing, or not unique, unless `replace_all`. Backup first. Confined. Prefer this over WriteFile for small changes.

**ListDir.** Sizes, skipping ignored dirs. Recursive is a shallow tree (max depth 2, 80 entries); non-recursive 200 entries. Confined. Vaulted when large.

**Glob, TodoWrite, Learn\*, SessionSearch, FetchUrl** are documented in their own sections.

**For a mid-level engineer:**

- Tool descriptions are user-visible model instructions. Treat them as documentation. Changing a description changes model behaviour *and* the frozen prefix (tools are part of the cache key).
- WriteFile reconstructed from a summary silently truncates. Three safeguards: prefer Edit; `raw=true` before a full rewrite; automatic backups + `/undo`.
- Bash hygiene happens at the source, before the vault. Vault excerpts are tail-heavier (400/800) so a pytest failure at the end of a log stays in the pointer the model actually sees.

**Observations:**

- `looks_like_test_command` matches pytest / unittest / cargo test / go test / npm test / jest / make test / ctest / mvn / gradle, plus `node --check`, `node …test…` (so `node test/headless.js` records verify), and `python3 …test….py`. The G023 Legends eval needed the `node …test…` form.

**Benefits:**

- Acting tools are small, confined, and reversible.
- Fail-closed Edit is the difference between "unique span replace" and "accidentally rewrite the third copy of a helper".
- Bash output is decision-ready instead of a 40k ANSI dump.

---

### 4.12 Permission model

**In simple terms:** reading is free. Writing, running, seeing images, spawning agents, and fetching URLs ask you first — unless you change that.

**Architecture / how it works:**

Three levels: `ALLOW` / `ASK` / `BLOCK`. `default_policy()`:

- `SAFE_TOOLS` (ReadFile, SearchContent, ListDir, Glob, TodoWrite, LearnSearch, LearnGet, LearnPut, SessionSearch) → always ALLOW.
- `DEFAULT_ASK_TOOLS` (AnalyzeImage, Bash, WriteFile, Edit, Agent) → follow `settings.permission_default` (default `ask`).
- `FetchUrl` → follows `permission_default` (ASK by default). `WebSearch` → allow (SAFE).

`/tools` shows every tool's level and how often it ran this turn. `/tools <tool> allow|ask|block` edits one tool. Write/Edit prompts show `ui.permission_diff`. Ctrl-C, Ctrl-D, and a closed/non-TTY stdin decline the call (they do not hang or crash the turn).

**For a mid-level engineer:**

- `check_permission` is async and may prompt, so the answer can vary per invocation.
- ASK tools stay **serial**. ALLOW read-only tools in one model step may overlap.
- One-shot / piped / unattended runs need `permission_default=allow` (typically a project `.g023/config.json`) or the ASK tools will all decline on EOF.
- See [4.9](#49-websearch-and-the-machine-wide-web-cache) for the web tools.

**Observations:**

- The G023 Legends unattended run silently declined every write until the project pinned `permission_default=allow`. The harness now prints that unattended runs need that setting.

**Benefits:**

- Default-ask means nothing that writes, runs, or leaves the machine happens without a prompt.
- Per-tool overrides let you allow Bash in a trusted repo without allowing WriteFile, or the reverse.
- Declining is a tool result the model can recover from, not a crashed turn.

---

### 4.13 Project-root confinement

**In simple terms:** the agent can only touch files inside the folder you launched from. `../secrets` is refused.

**Architecture / how it works:**

`g023_code/paths.py`. `resolve_in_project` runs `Path.resolve()` (so symlink escapes are included) and raises `PathOutsideProject` if the result is outside `G023_PROJECT_ROOT`. `confine_or_error` returns `(resolved, None)` or `(None, error message)` without reading the path. `rel_to_root` produces POSIX-ish relative paths for working-set / search results.

ReadFile, ListDir, SearchContent, Glob, Edit, WriteFile, and `/undo` all go through this. WriteFile still has an inline `relative_to` check as well. `.g023/` itself is inside the project, so backups, logs, and memory notes are reachable on purpose.

**For a mid-level engineer:**

- Empty / `.` / `None` means the project root.
- Absolute paths are accepted only when they stay inside the root after resolve.
- Learn-store writes have their own root confine.

**Observations:**

- This is a client-side fence, not an OS sandbox. Bash still runs a shell in the project root; a command that `cd`s elsewhere is a different class of escape. The dangerous-string filter is a tripwire, not a sandbox.

**Benefits:**

- A model that hallucinates `/etc/passwd` or a sibling repo gets a clean error instead of a leak.
- Relative paths in tool results stay meaningful.

---

### 4.14 Prefix cache and cost policy

**In simple terms:** DeepSeek will remember the start of the conversation and charge almost nothing to send it again — but only if you send the *exact same bytes from the start*. If you rewrite the middle, you pay full price for everything after the rewrite. So the harness would rather keep a slightly fat history that is already cheap than "clean it up" and make it expensive.

**Architecture / how it works:**

`g023_code/cost_policy.py` does not call the API. It answers one question: **is rewriting this prefix cheaper than keeping it?**

Flash regular rates (`ds_pricing.json`, 2026-09-10):

| | Regular (off-peak) | Surge (peak, 2×) |
|--|--------------------|------------------|
| Cache hit | $0.003 / MTok | $0.006 / MTok |
| Cache miss | $0.15 / MTok | $0.30 / MTok |
| Output | $0.60 / MTok | $1.20 / MTok |

Worked example: 500 cached tool-result tokens vs a 3,000-token tail bust.

- Keep: $0.0000015/turn as hits.
- Bust: $0.000441 one-time.
- Turns to recoup: ~294.

Decisions:

| Helper | Used by | True when |
|--------|---------|-----------|
| `should_micro_cached_prefix` | auto Layer 1 | window pressure only |
| `should_post_turn_compact` | post-turn replace | objective done, window full, or large *uncached* leftover |
| `should_vault_tool_output` | tool append | Bash/ListDir/Glob/AnalyzeImage ≥ 2,500 chars, not a raw ReadFile, not already a pointer |

`HIT_RATE_KEEP = 0.5`: last orchestrator call at or above that hit rate means leftover history is in the cached prefix.

What is frozen as the standing prefix:

- `instructions` (system prompt + project addendum)
- `tools` (registry schemas)
- `reasoning.effort` (wire alias)
- `temperature` (only sent when thinking is off)

What is *not* rewritten in place: the working-set item. Compact may start a *new* prefix.

**For a mid-level engineer:**

- DeepSeek Example 1 vs Example 2 is the whole game. Appending is cheap. Splicing the middle is a miss from that slot onward.
- Effort alias is in the prefix. `high` then `low` → 0 hits. Pin the wire; apply a stored `[next_effort]` only when the prefix is already new.
- Temperature is pinned the same way, same release points.
- Nested Agent reuses the frozen three (instructions, tools, effort) so children hit the standing units. Isolated input still misses — that is the branch.
- Live compact that appends a summarize user item to the live list hit 84.5% (same as a normal follow-up). Flattened `COMPACT_SYSTEM` missed from token 0.

**Observations:**

- Naive "always compact to save tokens" loses to "keep the hot prefix". That is the single most expensive wrong instinct in this codebase.
- Output is the remaining bill once the prefix is hot. Reasoning tokens are billed as output. That is why effort 75 is the ceiling, not 100, and why `[next_effort]` exists.
- Weekend hours that *look* like Beijing 09:00–12:00 / 14:00–18:00 are off-peak. Billing them as surge was a real overcharge; `ds_times.json` now carries `peak_weekdays`.

**Benefits:**

- The harness spends on the cheap side of a 50× gap by default.
- Policy is arithmetic in one module, tested, instead of folklore in comments.
- Vaulting at append time means bulky output never enters a cache unit as raw text, so there is no later middle rewrite.

---

### 4.15 History integrity

**In simple terms:** every "please run this tool" must be followed by "here is what happened". If that pairing breaks, DeepSeek rejects the next request and the session is stuck until someone repairs it. The harness repairs it.

**Architecture / how it works:**

Three editors, all in the orchestrator:

- `_rollback_to_last_complete()` — on API error, cut the item list back to the last state where every call is paired.
- `_repair_pairing()` — patch a history whose last call is unpaired.
- `_reseat_items_splitting_tool_pairs()` — **move** (not rewrite) a user/working-set item that landed between a `function_call` batch and its outputs. Pairing can be complete and still unsendable if a user row splits the batch.

`micro_compact` / `compact` are also history editors and must preserve pairing. Layer 3 keeps a sticky original-goal item, a middle summary, and a pairing-intact recent tail (original item dicts, not rebuilt).

**For a mid-level engineer:**

- Working-set mirroring waits until every output in a parallel WriteFile batch is recorded. Appending the user-role working-set item after the first WriteFile of a group was a real 400 (`No tool output found for tool call …`).
- Output items from the model are echoed back **byte-identical**. Rewriting them breaks the prefix cache *and* desyncs the model's reasoning.
- `/resume` repairs pairing after replay.

**Observations:**

- This is the class of bug that looks like "the API is down" from the user's chair. The session is sending a history the server will never accept. Repair-or-rollback is the difference between a recoverable turn and a dead REPL.

**Benefits:**

- A failed tool or a Ctrl-C does not kill the rest of the session.
- Parallel writes cannot wedge the history.
- Compaction cannot produce an unsendable item list.

---

### 4.16 Compaction (Layer 1, Layer 3, post-turn)

**In simple terms:** when the conversation gets long, the harness has three ways to make room — blank old tool dumps (free, local), summarise the middle (one model call, or reuse a handoff the model already wrote), or, after a finished job, replace the whole transcript with a one-page brief for the next job.

**Architecture / how it works:**

**Layer 1 — `micro_compact()`.** Blank old tool-result text in place. Items stay (pairing stays). Already-cleared placeholders stay the same string. Most recent `MICRO_KEEP_RECENT = 6` results and the latest verbatim/raw ReadFile (including `end_line`-only ranges) are spared. Auto path only runs under window pressure (`context_fraction() >= compact_threshold` on a **real** API gauge, not a char/4 guess). After a tool-heavy micro, wait for the next API `input_tokens` rather than rewriting again on the pre-blank gauge.

**Layer 3 — `compact()`.** Prefers `_last_handoff` from the previous final answer when no focus instructions were passed (no API cost), then **consumes** it so a later pass cannot reuse a stale summary. Otherwise `_render_transcript()` feeds the summarizer the session head **and** tail (cap 60k chars). Stored summary clipped to 8,000 chars. History becomes sticky original-goal + middle summary + pairing-intact recent tail. Empty/failed summarizer output leaves items unchanged. Focused `/compact <topic>` still calls Flash so the summary can follow that topic. Prefix-stable compact appends `COMPACT_USER_PROMPT` to the live list; it does not flatten onto `COMPACT_SYSTEM`.

**Post-turn — `_post_turn_compact()`.** After a finished turn (model answered, waiting for the next user instruction), may replace history with summary + working-set item. Fires when the working-set judge says the objective is complete, or leftover history is large *and uncached*, or the window is full. A hot prefix (hit rate ≥ 0.5) is kept even past the 4,000-char leftover floor. Short chat, small tool turns, and think-heavy short chats (reasoning scratch does not count toward leftover) skip. Summary source, in order: same-response `[handoff]` (no second Flash call); local fold of an existing compact-summary item (bounded: narrative kept byte-for-byte, a ring of the last 3 exchanges, older requests as ≤12 one-line digests); otherwise Flash. On success, writes `.g023/logs/{YYYYMMDD_HHMMSS}.log` and `release_prefix_effort()` so a stored `[next_effort]` can retune the wire on the new prefix.

Manual: `/compact`, `/compact micro`, `/compact auto on|off`. `/context` tells you which of the two (stale tool results vs real conversation) is filling the window.

**For a mid-level engineer:**

- `context_fraction()` uses last orchestrator `input_tokens` vs `max_context_tokens`. A post-compact char/4 guess is marked estimated and is **not** an auto-compact trigger.
- Layer 3 must not call `release_prefix_effort()` — instructions+tools+early items can still hit.
- An unclosed `[handoff]` is hidden from the user but is **not** used as a summary.
- Reasoning items are scratch work and do not count toward the leftover-bulk floor.

**Observations:**

- Auto-micro every turn used to look like "being a good citizen" and was a cache miss every turn. Window pressure is the constraint; incremental blanking of a cached prefix is Example 2.
- Prefer a `[handoff]` on the same final answer over a second Flash call. Output is ~200× a hit; a summarizer call is a real bill.

**Benefits:**

- The window can actually be reclaimed when it is full.
- Finished objectives hand off to a new orchestration instead of dragging a tool transcript into the next job.
- Manual `/compact micro` is still there for "I know these dumps are junk" without waiting for 85%.

---

### 4.17 Tool vault

**In simple terms:** a huge command log is stored in a locker. History keeps a ticket (`[g023_vault sha256:… chars=N]`) plus the head, the lines that match what the model said it was looking for, and the tail. The full raw output is kept as a readable file under `.g023/vault/`, so the model reads a range or greps it instead of running the command again (see 4.50).

**Architecture / how it works:**

`cache.tool_vault` is content-addressed (`sha256` of the body). `put_tool_body` / `get_tool_body` / `offload_tool_output`. Same bytes are a no-op. Vaulting is **append-only**: the pointer is what is written the first time, so there is no later middle rewrite of a cache unit.

Vaulted tools: Bash, ListDir, Glob, AnalyzeImage (FetchUrl left in 1.5.0: it bounds its own output). SearchContent is not vaulted (1.5.0: the excerpt hid the definition hits; its sample cap of 60 bounds it instead). Every result first goes through `tool_codec.encode_tool_result` (see `docs/tool_codec.md`). **Not** vaulted: ReadFile summaries and Agent payloads (they *are* the compact form), raw ReadFile (the model needs those bytes to Edit/WriteFile), anything already a pointer, bodies under 2,500 chars.

`/cache clear` deletes the vault. Prefix-stats are still preserved.

**For a mid-level engineer:**

- Nested Agent vaults like the parent. Head-slicing nested results at 8,000 chars was the rejected alternative.
- A re-run returns the same body, so the model is told to narrow the command, scope, or range. File reads and URL fetches already have their own caches.
- Excerpt shape: head 240 + up to 14 numbered lines picked by the step's intent (`salience.py`) + tail 500, so pytest failures still survive. `G023_SALIENCE=0` restores the old head 400 + tail 800.

**Observations:**

- Vault is the thing that makes "keep a hot prefix" viable. Without it, the only lever is rewriting history, which is the expensive lever.

**Benefits:**

- Bulky output never enters a cache unit as raw text.
- The model still sees the decision-relevant tail.
- Clearing the vault does not erase the hit-rate history used by `/signals`.

---

### 4.18 SQLite cache

**In simple terms:** a per-project notebook of "we already did this." Same file bytes → same summary. Same image + question → same vision answer. Same URL → same fetched body, until you ask for fresh.

**Architecture / how it works:**

`.g023/cache.db`, path from `config.get_cache_db()`. `get_cache()` is a module singleton.

| Table | Key | What it stores | Freshness |
|-------|-----|----------------|-----------|
| `file_cache` | `sha256(file bytes)` | ReadFile summary + metadata | content identity; `invalidate_file` on change; LRU-by-access trim |
| `vision_cache` | `(image_hash, question_hash, backend)` | vision answer | backend string includes model and DeepSeek endpoint |
| `prefix_stats` | per day, per model | cache-hit/miss token totals | **survives `/cache clear`** |
| `tool_vault` | `sha256(body)` | bulky tool results | deleted by `/cache clear` |
| `session_facts` | key/value | working set lives here | deleted by `/cache clear`; `/clear` also `working_set.reset()` |

Learn guides and memory notes are files, not rows. `/cache clear` does not touch them. Web searches and pages are not here either: they live in the machine-wide `global_cache/web.db` ([4.9](#49-websearch-and-the-machine-wide-web-cache)); a project's old `web_cache` rows are migrated there once and the table dropped.

**For a mid-level engineer:**

- The project cache keys on content identity (files). A changed file is a new key, so stale rows are never served; there is no automatic eviction, `/cache clear` empties it. Web freshness is per query/URL in `web.py`.
- `stats()` reports row counts for files / vision / vault. `/cache [stats|web|web clear|web prune|clear]` is the UI; it adds the web store's counts and the fees saved.

**Observations:**

- "Cache everything cacheable" is a design rule, not an LRU of the prompt. Prompt-cache (DeepSeek, prefix units) and this SQLite cache are different layers. Confusing them is how you end up rewriting a hot prefix to "use the cache".

**Benefits:**

- Repeated reads of unchanged bytes cost no API call.
- Web freshness is a human decision, which is the only honest policy when "is this page stale?" depends on the page.
- Hit-rate history survives a cache purge, so `/signals` still has a baseline.

---

### 4.19 Usage and cost accounting

**In simple terms:** every API call — orchestrator and subagents — reports tokens. The harness prices them from DeepSeek's published sheet, splits cache hit vs miss, and shows you the bill. It is an estimate of your bill, not a reading of it.

**Architecture / how it works:**

`g023_code/usage.py` + `ds_pricing.json`.

- `ModelPricing` holds the regular rates (`cache_hit`, `cache_miss`, `output` per 1M tokens) and the surge rates (`surge_cache_hit`, `surge_cache_miss`, `surge_output`; 2× today).
- `pricing_for(model)` defaults to the flash table. One row, `deepseek-flash`; usage recorded under an older id (1.3.x session logs) bills as Flash.
- `UsageTracker.record(model, usage, scope=...)` normalises **both** usage spellings (`input_tokens` + `cached_tokens`, or flat `prompt_cache_hit/miss_tokens`), splits hit/miss the same way either way, and **never assumes a hit when the provider didn't report one**. Scope is `orchestrator` vs `subagent`.
- `/cost` shows per-model totals, hit/miss/output split, a per-turn sparkline, and `format_cost` with sub-cent precision.
- Vision, FileReader and nested Agent calls are recorded under subagent scope.

**For a mid-level engineer:**

- Costs shown are `token_counts × ds_pricing.json × surge flag`. They will diverge from the provider invoice if the sheet is stale or if a billed line item is not in the usage object.
- Checked offline: rates × right token counts, hit/miss derived identically from either endpoint, no invented cache hits.

**Observations:**

- The 50× hit/miss gap and the 200× output/hit gap are DeepSeek's price sheet, not something this harness achieves. What the harness does is avoid breaking the prefix so more tokens fall on the cheaper side.

**Benefits:**

- `/cost`, the status line, and high-verbosity per-iteration lines all read from one tracker.
- You can see whether you are paying for hits, misses, or output — three different problems with three different fixes.

---

### 4.20 Surge (tidal) pricing

**In simple terms:** DeepSeek costs twice as much during Beijing weekday peak hours. The toolbar shows `SURGE 2×`. You can ask to be notified, or to pause input until peak is over.

**Architecture / how it works:**

`g023_code/surge.py` + `ds_times.json`. Windows are **never** hard-coded in `surge.py`; they load from the JSON (Beijing 09:00–12:00 and 14:00–18:00, UTC+8, Monday–Friday) with a published-sheet fallback.

- `beijing_now` / `is_surge` / `is_peak_weekday` decide on/off.
- `peak_windows_local` / `next_peak_window` / `describe` report in the **machine's local timezone**, both AM/PM and 24-hour.
- `/surge` shows countdown to the next surge, how long it lasts, and regular vs surge prices from `ds_pricing.json`.
- `/surge notify on|off` — banner on transitions (default on).
- `/surge on` — pause usage from `buffer` minutes before peak until `buffer` after it ends (default 5). CLI sleeps in 5 s slices. Ctrl-C bypasses for the rest of the session.
- `/surge now` is the current-instant panel (same body as status).

`usage.cost()` prices the tokens recorded during a peak window at the surge rates and the rest at the regular rates (each call is tagged when it is recorded).

**For a mid-level engineer:**

- Weekends are off-peak even during hours that look like the weekday windows. `peak_weekdays` is `[0,1,2,3,4]` (Python Monday=0).
- Pause ranges merge overlaps after widening by the buffer.
- The guarantees are about the arithmetic, not the literal clock.

**Observations:**

- Billing weekend "peak-looking" hours as surge was a real overcharge. The weekday flag is load-bearing.

**Benefits:**

- You can see 2× coming in local time instead of discovering it on the invoice.
- Optional pause is a real money control, not a banner you ignore.
- Prices in the panel come from the same JSON `/cost` uses.

---

### 4.21 Drift signals

**In simple terms:** if DeepSeek quietly changes a field name, the model looks like it went silent and nothing crashes. `/signals` is the smoke alarm: unknown item types, empty replies with no reason, and a drop in cache-hit rate against previous days.

**Architecture / how it works:**

`g023_code/signals.py` + helpers in `api.py`.

| Signal | What it catches | What it cannot tell you |
|--------|-----------------|-------------------------|
| Unknown item `type` | An additive API change, at the earliest moment it is visible | Whether it matters |
| Empty output with no `incomplete_reason` | What a renamed content field looks like from outside | Whether the model simply said nothing |
| Prefix hit rate vs prior days | Anything that started breaking the prefix | Whether the cause is your prompt, the tool list, or the server |

Hit rate is stored per day in `prefix_stats` and compared against a trailing baseline. Needs two prior days before it will call anything a baseline. `HIT_RATE_ALERT_DROP = 0.15` (15 points). `/cache clear` does not erase it.

`/signals` (alias `/drift`) renders the log and the verdict (`no history | insufficient baseline | down | up | steady`).

**For a mid-level engineer:**

- Signals must fire on shapes that matter **and stay quiet on shapes that are merely unusual**. A signal that fires on ordinary turns gets ignored and stops being a signal.
- None of these detect a schema change *as such*; they detect "something moved". A model behaviour change, a schema change, and drift in your own prompts all present identically from here.

**Observations:**

- Silent degradation, not a crash, is the realistic worst case for an unvalidated client. This feature exists because that was an explicit design choice in `api.py`.

**Benefits:**

- You get a dated "something moved" instead of a week of "the model feels worse".
- Hit-rate collapse is visible across restarts.

---

### 4.22 Reasoning effort and same-response trailers

**In simple terms:** thinking is a 0–100 dial, default and ceiling 75. The model can also, on the same final answer, write a short handoff for the next turn and suggest how hard the *next* request looks. You never see those control blocks; the harness strips them.

**Architecture / how it works:**

`g023_code/effort.py`. Stored effort is an integer 0–75. Wire value is always a level the endpoint really distinguishes:

| Stored | Wire |
|--------|------|
| 0 / off | `none` (thinking disabled) |
| 1–50 | `low` |
| 51–75 | `high` (75 is default and ceiling) |

DeepSeek folds `minimal` into `low` and `medium` into `high`, so those are never sent.

`/thinking` accepts `1-100` or `low`/`high`/`max`/`off` (max aliases to 75). Bare opens a picker. `/goal` forces 75 for its turn.

Locked trailer shapes, trailing-only:

```
[handoff]
## Goals / Files / Decisions / Done
[/handoff]
[next_effort: N]
```

- Complete `[handoff]` on a **final** (no-tool) reply is stored as `_last_handoff` and reused by compact (no second Flash call), then consumed.
- Valid `N` on that same final reply updates in-memory `settings.reasoning_effort` for the following **user request**, capped at 75. The wire alias stays pinned on a hot prefix; the stored dial is applied on `/thinking`, `/clear`, or a post-turn compact that replaced the input list. `/goal` does not apply it on the way out.
- A trailer on a tool-call step is stripped from display and history but is **not** applied — it is not this turn's remaining steps.
- HTML-comment and `<meta>` forms of `next_effort` are accepted if they appear as the last thing in the text.
- Recognised trailers are always stripped, even when the effort value is invalid.

The standing prompt uses `N` as a placeholder so Flash does not copy 75 on every turn. 25–50 is for routine follow-up; 0 disables thinking.

**For a mid-level engineer:**

- `parse_effort_value` never raises: invalid input returns `None` ("leave current unchanged"); 76–100 return 75.
- Old configs that say `"high"` or `"max"` coerce to 75 via `ALIAS_TO_INT`.
- `/goal` keeps a same-response `[next_effort]` in settings instead of restoring the pre-goal dial. If omitted, the pre-goal dial is restored. The wire stays on the alias that turn warmed until the transcript is replaced.

**Observations:**

- `max` / 100 / `xhigh` often burn the entire `max_output_tokens` budget on thinking and return an empty answer. That is why 75 is the ceiling, not a taste preference.
- high→low mid-prefix misses from token 0. Pin, then release.

**Benefits:**

- Thinking is a dial, not always-on max, which matters because reasoning tokens are billed as output (~200× a cache hit).
- Same-response handoff avoids a second summarizer call.
- The user never sees the control protocol.

---

### 4.23 Working set and goal judge

**In simple terms:** the session keeps a small, structured "what is still open" object — not a paragraph the model paraphrases every turn. A job is done when the open list is empty *and* every done item cites a real file hash or a passing test, not when the model says "done".

**Architecture / how it works:**

`g023_code/working_set.py`. Live copy in `cache.session_facts` under `working_set`. Serialized with a **fixed key order** so an unchanged prefix stays byte-stable.

```json
{
  "goal": "",
  "constraints": [],
  "open": [{"id": "...", "title": "...", "status": "pending", "evidence": []}],
  "files": [{"path": "rel/path", "hash": "...", "last_range": null}],
  "verify": {"command": "", "exit_code": null, "excerpt": "", "at": ""}
}
```

Caps: 8 open (non-done/cancelled) leaves, enforced by TodoWrite with an explicit `too_many_open_leaves` reject listing the ids not recorded (never a silent drop; closes earlier in the same batch free slots), 32 file pointers (newest kept). Statuses: `pending` | `in_progress` | `done` | `cancelled`.

History mirror: a user-role item starting with `Working set:\n` plus the stable JSON. `mirror_working_set` **appends** the item only when none exists yet — never rewritten in place (DeepSeek Example 2). Compact may place a fresh snapshot after the sticky goal as a *new* prefix.

Updates: `TodoWrite` merges leaves by id (`done` requires evidence); `note_file` after WriteFile/Edit; `note_verify` after a test-like Bash; `set_goal` from `/goal`; `reset` from `/clear`.

`judge_goal_completion()` is the **only** completed/not-completed verdict:

- Pending open leaves → not completed.
- Done leaves without evidence (file pointer `@sha256:` / path, or a verify command that exited 0) → not completed. A test-command string with no recorded pass does not count.
- Empty leaf list, or every leaf cancelled → not completed.
- Last recorded verify exited non-zero → not completed.
- No open leaves, at least one done leaf cites file/verify evidence, and the last verify did not fail → completed.

That verdict is what post-turn compact uses when it decides an objective is finished and writes `.g023/logs/`.

`evidence_satisfies_done` accepts a working-set file path, the canonical `path@sha256:<digest>` form, `path <hash>` (what models actually emit), and a truncated/mismatched digest as long as the path is already in `files[]`. Unknown paths still fail. TodoWrite's error includes a `hint` with an example citation and the known files.

**For a mid-level engineer:**

- Remaining work lives here so compact **must not paraphrase it**. The summarizer is told to omit remaining-work guesses.
- `/goal <text>` runs at effort 75 with no step cap, seeds `working_set.goal`, and asks the model to finish the objective. If the judge still says not completed, at most two continuation turns name the open leaves. A continuation that does not change the working set stops.

**Observations:**

- A final assistant message is not success. That is the whole point of the judge. Checked offline: pending / done-without-evidence / empty / file-evidence / verify-evidence / truncated-hash citations.

**Benefits:**

- Compact cannot "lose the todo list" by summarising it away.
- Post-turn handoff fires on evidence, not on vibes.
- The prefix stays stable because the object is not rewritten in the middle of history.

---

### 4.24 Session log, `/resume`, `--continue`

**In simple terms:** two on-disk records. One is the full event log (you can grep it, even for things compaction dropped from the live view). The other is a one-page "start here" brief for the next job. `/resume` loads the log; `--continue` / `/resume log` starts from the brief.

**Architecture / how it works:**

**JSONL ancestry** — `.g023/sessions/{YYYYMMDD-HHMMSS}.jsonl` (UTC), append-only. `CURRENT` points at it.

Record kinds: `user`, `output`, `tool_result`, `compact_boundary` (the compacted *view* plus working set), `working_set`, `interrupt` (Ctrl-C), `prefix_fingerprint` (sha256 of instructions and of the tools list).

`sanitize_item` omits only re-derivable bodies: WriteFile/Edit `content` / large `old_string` / `new_string`, and large tool outputs (`LARGE_LOG_CHARS = 4096`), stored as hash + byte count. Message content (prompts, compaction summary, assistant text) is always kept in full, so `/resume` after compaction restores the real summary. Attaching to a log with a torn last line terminates it first, so a crash loses at most the torn record.

`replay_log` rebuilds the latest view: a `compact_boundary` **replaces** items rather than appending. `/resume` (bare) loads that view and repairs pairing. Prefix-fingerprint mismatch warns that the standing instructions or tool list no longer match (a prefix cache miss from token 0).

**SessionSearch** greps the JSONL without reconstructing omitted bodies. Hits include the hash when a body was omitted.

**Objective handoff** — `.g023/logs/{YYYYMMDD_HHMMSS}.log`, written by post-turn compact on a successful replace:

```
G023_OBJECTIVE_LOG 1
ts: ...
completed: true|false|unknown
goal: ...
key: <key id>              (absent when no key could be created)
sig: hmac-sha256:<hex>

--- working_set ---
{...}

--- summary ---
...
```

`--continue` / `-c` and `/resume log` seed a **new** orchestration: standing instructions + working-set item + summary. A file without the header is treated as the summary body so a hand-edited log still loads.

**Signed, evidence-bound handoffs** (`checkpoint.py`, docs/checkpoint.md). The log is signed with HMAC-SHA256 under a machine-local key (`<global cache dir>/handoff.key`, never in a project or release). On resume its provenance is `verified`, `modified`, `foreign` or `unsigned`, and the working set's file hashes are re-checked. Anything other than a verified, current state adds one `[rehydrate]` note naming the changed or missing files and a passing verify that no longer describes the disk; the CLI prints a `rehydrate ·` line. Bare `/resume` runs the same disk check.

**For a mid-level engineer:**

- Compact records a *view*; the JSONL keeps ancestry. That split is why SessionSearch can find text the live window no longer has.
- `/resume` vs `/resume log` is "continue this conversation" vs "start the next job from the brief". Different prefixes, different cache behaviour.

**Observations:**

- Sanitising write bodies in the JSONL is a safety and size choice. SessionSearch will not reconstruct a secret that was written; it will show a hash.

**Benefits:**

- Crash / Ctrl-C / restart does not throw away the session.
- Finished work becomes a clean next orchestration instead of an ever-growing transcript.
- Grep-able ancestry survives compaction.

---

### 4.25 Learn store

**In simple terms:** reusable procedures from earlier sessions live in markdown files. The model searches them when it hits an error or a workflow it has not used yet. They are **not** stuffed into the system prompt, so growing the library cannot bust the cache.

**Architecture / how it works:**

`g023_code/learn.py`. Disable with `G023_LEARN=0`.

| Root | Scope | Path |
|------|-------|------|
| Home | `env` and `user` | `~/.g023/learn/` |
| Project | `project` | `<project>/.g023/learn/` |

Each root: `INDEX.md` (routing lines only, cap 80), `guides/{id}.md` (YAML front matter + body, cap 40 files per root, body cap 80 lines / 4,000 chars), `ledger.jsonl` (append-only put / forget / outcome / reject).

Kinds: `procedure`, `pitfall`, `routing`, `fact`. Ids: `^[a-z0-9-]{2,64}$`. A project id shadows a home id of the same name. Search skips `stale` and `tombstone`. Optional `requires:` probe commands (subprocess, 5 s timeout) skip a guide when any probe fails.

| Surface | What it does |
|---------|----------------|
| `LearnSearch` | Routing hits only (id, title, when-tags). Cap 5. |
| `LearnGet` | One guide body. Cap 2 per turn. |
| `LearnPut` | Orchestrator-only write. Cap 3 per session unless `source` is `user` / `user:explicit`. |
| `/learn` | Status, search, get, save (user-explicit, bypasses the session cap), forget, outcome. |

There is no `LearnForget` tool. Forget is `/learn forget <id>` — tombstones the file, removes the index line, id cannot be reused. `/learn outcome <id> helped|harmed` increments counters; two+ `harmed` with `harmful > helpful` marks the guide `stale`.

`put` rejects (and ledgers) rather than writing: missing fields, invalid id/kind/scope, `orchestrator_only` when source is subagent, session/index/guide caps, `secret_reject` (API keys, Bearer, PEM), `auth_launder` (permission-change language), `untrusted_evidence` (`web:` / `fetch:` / `http(s):` unless source is the user), `duplicate_when`, `forgotten` tombstones. Evidence must be a local tool success or `user:explicit`.

On a tool error, `catalog_nudge` may return a one-line `[learn] LearnSearch query=…` hint when the error matches a when-tag.

**For a mid-level engineer:**

- Put does **not** change `instructions` or tools. That is the whole point of retrieve-don't-prepend.
- Env-scope guides are visible across projects on the same machine.
- `/cache clear` does not delete them (they are not SQLite).

**Observations:**

- Prepending a growing INDEX to the system prompt would be the obvious "memory" design and would bust the prefix on every new guide. Search-on-demand is the cache-compatible memory.

**Benefits:**

- Procedures travel across sessions and (for env/user) across projects.
- Secret/auth rejects stop the model from writing keys or "how to disable permissions" into a guide.
- Stale guides drop out of the index from harm votes instead of rotting in the prompt forever.

---

### 4.26 Memory notes

**In simple terms:** a project notebook at `.g023/memory/MEMORY.md`. Facts live there. They are never injected into the system prompt. The model is told it can SearchContent that directory.

**Architecture / how it works:**

`g023_code/memory_notes.py`. `MEMORY.md` is the index, cap 200 lines. Overflow spills to `topics/overflow.md`. WriteFile special-cases that path (`is_memory_index_path`). `/status` shows `none` / `MEMORY.md N line(s)` / `unreadable`.

This is not the learn store (cross-project procedures), not the working set (this-session remaining work), and not the bulletin board (forum at `.g023/bbs/`).

**For a mid-level engineer:**

- Never concatenating this into `_system` is a prefix-stability choice. A 200-line notebook changing mid-session would otherwise miss from token 0.

**Observations:**

- Four memory-like stores exist on purpose: working set (this session, structured, prefix-mirrored), learn (cross-session procedures, retrieved), memory notes (project facts, searched), bulletin board (forum / message passing, searched). Collapsing them into "just prepend a MEMORY.md" would undo the cache work.

**Benefits:**

- Durable project facts without a prompt-cache tax.
- Overflow instead of silent truncation of the index.

---

### 4.27 Prompt store

**In simple terms:** every prompt you type is saved as `.g023/prompts/1.txt`, `2.txt`, …. Ctrl+Up recalls the last one. Up-arrow still walks ordinary input history.

**Architecture / how it works:**

`g023_code/prompt_store.py`. Next index = max existing + 1. `Orchestrator.run_turn` calls `save_prompt` at the start of a turn. `/clear` does not delete the folder. With `prompt_toolkit`, Ctrl+Up replaces the input line with `last_prompt()`. `.g023/history` (FileHistory) is unchanged.

**For a mid-level engineer:**

- The REPL binds the key; it does not own numbering or I/O. That split is why prompt-store identity can be checked without a TTY.

**Benefits:**

- Re-running a long prompt is one chord, not a scroll through mixed slash commands.
- Prompts survive `/clear` and process restart.

---

### 4.28 Project instructions (`g023.md` / `AGENTS.md`)

**In simple terms:** drop a `g023.md` in the project root (this program's AGENTS.md). It is appended to the system prompt. `/g023-compact` writes a denser `g023_compact.md` and leaves the original. If the compact file exists, that is what is used. A project-root `AGENTS.md` is appended as well when readable. None of the files is required.

**Architecture / how it works:**

`g023_code/project_instructions.py`. Load order: `g023_compact.md` if present, else `g023.md`, plus `AGENTS.md` when readable. Each file is headed `Project instructions (<filename>)`. Unreadable or empty files are treated as absent.

The addendum is frozen for the life of an Orchestrator (`_frozen_addendum`) so a mid-session edit does not bust the prefix cache until the next process.

`/g023-compact` sends `g023.md` to `deepseek-flash` with `reasoning={"effort":"none"}` and no tools, writes `g023_compact.md`. Missing/empty original → no API call. Existing compact and `overwrite=False` → no API call. Empty model reply → original left. A wrapping ` ```markdown ` fence around the whole reply is stripped; fences *inside* the file are kept. Cost is recorded as a subagent call. CLI confirms before overwriting.

**For a mid-level engineer:**

- Compact winning whenever it is present is a footgun if you edit `g023.md` and forget to delete or regenerate the compact file. `/status` shows which file is loaded.
- `AGENTS.md` is extra, not a fallback. Other agentic CLIs' AGENTS.md still apply here, in addition.

**Observations:**

- Freezing the addendum for the process is the same prefix-stability idea as frozen tools. Live-reloading project instructions would feel nicer and would miss from token 0.

**Benefits:**

- Per-project standing rules without forking the harness.
- Compact form keeps the standing prefix smaller (output and miss both care).
- Compatibility with the AGENTS.md convention other tools already use.

---

### 4.29 Configuration scopes

**In simple terms:** settings live in two layers. Machine `config.json` next to `K.dat` is the default that follows you. Project `.g023/config.json` overrides it for this repo only. `/config` shows both and which is active.

**Architecture / how it works:**

`g023_code/config.py`. `load_config` applies machine first, then project (project wins). Missing/broken files are a no-op. `save_config(target, scope)` writes `global` (machine) or `project`. Only `PERSISTED_KEYS` survive between sessions.

`/config` (alias `/cfg`): `status` (bare is the same), `save`, `save project`, `global` (promote current settings to the machine default), `clear` (drop this project's override).

`_coerce` validates every persisted key: whitelist verbose/permission_default; `reasoning_effort` through `parse_effort_value`; in-range context window (1k–1M) and compact threshold (0.01–1.0); `LEGACY_KEYS` (model ids, `api_format`, Ollama/OpenRouter fields) ignored on load and dropped on save, with `vision_backend: "none"` migrated to `vision_enabled: false`; non-negative `max_iterations` / `max_tokens` (0 = unlimited); types checked against the current default. A hand-edited or older config can never put the app into a state its own commands cannot describe.

`Settings` holds every knob except the model (a constant): API, context, agent loop, subagents/tools, vision, surge, runtime (stream, verbose, auto_compact, gauges). Properties: `show_answer_detail` (verbose ≥ mid), `show_thinking` (verbose ≥ high), `orchestrator_model` / `subagent_model` (read-only, both `MODEL`).

**For a mid-level engineer:**

- `vision_enabled` can be set per project; the project file wins.
- `MODEL = "deepseek-flash"`, `MODEL_LABEL = "DeepSeek V4.1 Flash"`, `MESSAGES_PATH = "/anthropic/v1/messages"`. There is no model list to keep in sync.
- `/settings [save|reset]` is the all-knobs panel; `/config` is the scope tool. They are different jobs.

**Observations:**

- Unattended evals typically need a **project** `permission_default=allow` so the machine default can stay `ask`. That split is the feature working as designed.

**Benefits:**

- Tune once, promote to global, or pin a wild setting to one repo.
- Coercion means a stale config.json cannot launch a model every call fails on.
- Commands and persisted keys cannot drift apart.

---

### 4.30 CLI, slash commands, and the REPL

**In simple terms:** anything that does not start with `/` is a prompt. Everything else is a slash command. Commands with a fixed set of options open a picker when typed bare, so `/thinking` is as usable as `/thinking 50`.

**Architecture / how it works:**

`cli.py` owns the `CLI` class: banner, REPL loop, every `cmd_*` handler, `run_prompt`. `commands.py` is pure data: names, aliases, grouping, arguments, help, handler method name. `check_handlers(self)` at startup raises if a catalogue entry has no matching method, so help, tab completion, and "did you mean" cannot drift from implementation.

32 commands in 6 groups: Session, Model, Context, Tools, Vision, Work. See [§9](#9-slash-command-index).

`handle_slash` resolves via `commands.lookup` (tolerates a missing leading slash, case-insensitive). Unknown commands get `commands.suggest` (prefix matches first, then difflib; aliases collapse onto canonical). Handler errors print `Command failed: …` (traceback when `show_thinking`) and the REPL survives.

Startup: banner → `load_api_key` (dies early if missing/placeholder) → surge monitor → optional surge pause → read a line.

Toolbar: live effort integer (`think 75`), context fraction, session cost, vision, `SURGE 2×` / `or-sub` badges.

**For a mid-level engineer:**

- Extending: add a `Command(...)` to `COMMANDS` and a `cmd_*` method. Help, completion, and "did you mean" pick it up. `check_handlers` fails at startup if the method is missing.
- Interactive commands (`interactive=True`) run `choose` when no argument is given.
- `/help <command>` is the user-facing spec; `detail` strings in `commands.py` document semantics (75 ceiling, withheld pro, retrieve-don't-prepend, etc.).

**Observations:**

- Catalogue-as-data is what makes "a command cannot be completable but undocumented" true: every advertised choice is a handler branch.

**Benefits:**

- The REPL is navigable without memorising flags.
- One source of truth for help, completion, and dispatch.
- A failed command does not kill the session.

---

### 4.31 Terminal UI

**In simple terms:** one module decides every pixel. Tool traces say what *happened* (`708 lines, 9 classes, cached`), not what was sent. Gauges, panels, diffs, and the live spinner all go through here. Ugly terminals get ASCII.

**Architecture / how it works:**

`g023_code/ui.py`. Shared rich `console`. `_unicode_ok()` detects a non-Unicode terminal; `G023_ASCII=1` forces ASCII. `Glyph` is the symbol set with fallbacks.

- `TOOL_STYLES` colours every tool, including WebSearch and replayed `web_search` traces.
- `describe_call` / `describe_result` know each tool's argument shapes.
- `unified_change_diff` / `permission_diff` are the WriteFile/Edit preview at the permission prompt.
- `ActivityPrinter` is the live narration the orchestrator drives: `tool_start` (spinner + one line), `tool_end`, `tool_denied`, `step_notice` (persistent progress every N steps), `iteration` (per-iteration token/cost at high verbosity), `note`.
- `TurnTrace` / `ToolEvent` bookkeep events, tool counts, slowest, thinking/iteration timing.
- `gauge`, `sparkline`, `kv_table`, `panel`, `banner`, `status_bar`, `trace_summary`.
- `human_bytes`, `human_ms`, `truncate`, `shorten_path`, `short_url`.

**For a mid-level engineer:**

- New tools need a `TOOL_STYLES` entry and cases in `describe_call` / `describe_result` so the trace reads like the others.
- Presentation is deliberately separate from the agent loop. The orchestrator narrates through `ActivityPrinter`; it does not print.

**Benefits:**

- You never wonder what it is doing or what it just cost.
- ASCII fallback means CI logs and dumb terminals still work.
- Permission diffs show the change, not a wall of `content=`.

---

### 4.32 Input line

**In simple terms:** with `prompt_toolkit`, you get tab completion, persistent history, a live status bar pinned to the bottom, Esc-Enter for newline, and Ctrl+Up for the last stored prompt. Without it, a plain prompt. Everything else behaves identically.

**Architecture / how it works:**

`g023_code/prompt.py`. `Reader`: if `prompt_toolkit` is installed **and** stdin is a TTY, a `PromptSession` with toolbar (`ui.status_bar`), `.g023/history`, `SlashCompleter` (commands then first-arg choices via `commands.completions`), key bindings. Otherwise rich `Prompt.ask`. `enhanced` reports which mode is active. `install_hint()` suggests installing prompt_toolkit once.

Helpers: `confirm` (cancellation = no, never a crash), `choose` (numbered picker, 0 = cancel) used by interactive slash commands.

**For a mid-level engineer:**

- KeyboardInterrupt/EOF propagate so `cli.loop` can exit cleanly.
- Completions complete `/th` → `/thinking`, then `/thinking ` → `low/high/max/off`.

**Benefits:**

- Enhanced input is a dependency, not a requirement.
- Pickers make `/thinking` and `/vision` usable without memorising tokens.

---

### 4.33 Write backups and `/undo`

**In simple terms:** before the agent overwrites a file, the previous version is snapshotted. `/undo` copies the newest snapshot back. A bad write is one command away from undone.

**Architecture / how it works:**

`.g023/backups/` mirrors the file tree: one folder per file, timestamped `{YYYYMMDD-HHMMSS.nnnnnnnnn}-{hash8}.bak` copies inside (nanosecond stamps, strictly increasing per process; ordering is parsed, so legacy whole-second names still sort correctly, ties broken by mtime). Snapshots are written atomically. Newest `settings.write_backup_keep` kept (default 5, minimum 1). Skipped for `.g023/` paths **except** `.g023/bbs/`, new files, unchanged content, and when `G023_WRITE_BACKUP=0`.

`/undo [path]` — `restore_newest_backup`. Bare `/undo` uses the last working-set file pointer. Confined to the project root. A stack pop: the restored snapshot is consumed, so repeated `/undo` walks further back; the version it replaces is saved as `*.undone` (pruned to the same keep count). The file is replaced atomically with its mode kept.

**For a mid-level engineer:**

- Identical bytes are a no-op at WriteFile time, so a no-op write does not burn a backup slot.
- `G023_WRITE_BACKUP_KEEP` overrides keep-count.

**Observations:**

- This is the third rail of the "summary-based WriteFile truncates the file" incident. Edit + raw-before-write + backups are three independent safeguards. Backups are the one that still helps when the other two are ignored.

**Benefits:**

- Reversible overwrites without git.
- Layout is browsable (per-file folders) instead of a flat junk drawer.
- `/undo` is a first-class command, not a scavenger hunt in `.g023/`.

---

### 4.34 Parallel tool execution

**In simple terms:** if the model asks to read three files in one step, those reads can overlap. Writes to the same path, and anything that asks you for permission, stay one-at-a-time.

**Architecture / how it works:**

`READ_ONLY_PARALLEL` = ReadFile, SearchContent, ListDir, Glob, LearnSearch, LearnGet, SessionSearch. Those ALLOW calls in one model step may overlap. `WRITE_TOOLS` = WriteFile, Edit — same-path writes stay serial. ASK tools stay serial.

Working-set mirroring waits until **every** output in the batch is recorded, then reseats if a user/working-set item still sits inside an open batch.

**For a mid-level engineer:**

- Parallelism is a latency win, not a cache win. Isolated FileReader work still has its own input prefix.
- The 400 from splitting a parallel WriteFile batch with a working-set item is prevented: the first parallel WriteFile batch keeps its outputs contiguous.

**Benefits:**

- Multi-file reads in one model step do not pay serial round-trip time.
- Safety tools (ASK, same-path writes) cannot race.

---

### 4.35 Streaming

**In simple terms:** the answer appears as it is written. Long thinking shows a counter instead of looking hung. Web searches announce themselves when they start and again with the queries they ran.

**Architecture / how it works:**

`settings.stream` (default true). `_call_streaming` consumes `client.stream`. Reasoning deltas drive the thinking counter. Each assistant message renders into its own live panel. Authoritative result is the terminal `response` object in the SSE stream. `visible_reply_text` hides in-progress trailers. Blocking path (`_call_blocking`) is a spinner that counts seconds, used when stream is off or cannot be used.

**For a mid-level engineer:**

- Deltas are for the eye only. History is never assembled from deltas. That makes stream reassembly bugs impossible by construction.
- `rendered_ids` prevents double-painting items.

**Benefits:**

- A 20-second think or a slow web search is visibly progress, not a freeze.
- History fidelity does not depend on the stream parser.

---

### 4.36 Output-limit auto-continue

**In simple terms:** if the model hits a length ceiling mid-thought with no tool calls, the harness nudges it ("continue, smaller payloads") and keeps the same turn going, up to three times, instead of stranding you with a half-answer.

**Architecture / how it works:**

When `incomplete_reason == "max_output_tokens"` and the reply had no tool calls, the orchestrator explains *which* limit cut it off (the model's own, when no budget was sent, vs the configured `/max-tokens` budget), nudges, and continues, up to `MAX_OUTPUT_CONTINUES = 3`.

**For a mid-level engineer:**

- Default `max_tokens = 0` means no budget is sent, so the model's own maximum is the only ceiling. The notice names that honestly.
- This fired on a `/goal` that thought until the ceiling with nothing written (G023 Legends).

**Benefits:**

- Long thinking turns can still produce an answer.
- The user is told which ceiling applied, which is the difference between "raise `/max-tokens`" and "the model is done thinking".

---

### 4.37 Step cap, max-tokens, context window

**In simple terms:** you can cap how many tool steps one turn may take (default 255, `off` for none, progress printed every 10), how long one reply may be (default off), how big the context window is treated as (default 1M, DeepSeek's real limit), and when auto-compact fires (default 200k tokens, or 0.85 of the window if that is lower).

**Architecture / how it works:**

- `/steps [n|off|default]` → `settings.max_iterations`. 0 = `itertools.count(1)` (no cap). `ITERATION_NOTICE_EVERY = 10` prints a persistent progress line that stays in scrollback (the spinner is transient). Exhaustion → "Stopped after N tool iterations".
- `/max-tokens [n|off|default]` → `settings.max_tokens`. 0 = omit the field.
- `/max-ctx [n|default]` — 1,000 to 1,000,000. Default 1,000,000. This is the gauge denominator, not a server-enforced window.
- `/compact-threshold [fraction|default]` — 0.01 to 1.0. `0.9` or `90%` both work.

`describe_steps` / `describe_max_tokens` / `describe_context_window` / `describe_compact_threshold` are the one wording used by every command.

**For a mid-level engineer:**

- The client must not invent limits: the philosophy is visibility, not a hidden ceiling. 255 is a practical default, not a claimed model limit.
- Config coercion drops out-of-range values rather than clamping them into a surprise.

**Benefits:**

- Runaway tool loops are stoppable without killing the process.
- The context gauge is honest about what it is measuring (last API input tokens vs a user-set ceiling).

---

### 4.38 Verbosity

**In simple terms:** how much detail to print while working. Persisted.

| Level | Shows |
|-------|--------|
| `low` *(default)* | The answer, plus a one-line trace of every tool call |
| `mid` | ↑ plus each tool's outcome and duration, and the per-turn token/cost line |
| `high` | ↑ plus the orchestrator's reasoning excerpts and per-iteration token counts |

`/verbose [low|mid|high]`. Properties `show_answer_detail` / `show_thinking` derive from this. `show_tool_timing` and `show_context_bar` are separate knobs.

**Benefits:**

- Low is usable in a meeting. High is usable when you are debugging the agent itself.
- Cost and reasoning are opt-in, not a firehose.

---

### 4.39 Release media and capture tooling

**In simple terms:** screenshots and videos of the TUI are captured by driving a real PTY and re-rendering the byte stream, not by photographing a window.

**Architecture / how it works:**

The capture tooling (development-only): a recorder forks a PTY, types scripted keystrokes with human timing, answers cursor-position requests so prompt_toolkit's toolbar renders, supports `mark` events, optional `deny_on_prompt` so a scene stays read-only. Scripted scenes (`tour`, `settings`, `panels` use slash commands only; `session` and `edit` are live turns) drive it. A renderer replays the bytes through `pyte` and paints with Pillow (box-drawing drawn geometrically).

Screenshots in `media/screenshots/` were re-shot on 1.5.0 with `HOME` pointed at a throwaway shoot folder, so every path prints as `~/…` and no account or machine name appears (see `media/README.md`). The capture tooling is a development tool and is not part of the release archive.

**Observations:**

- Capture is not part of the release archive's runtime. It is how the docs stay honest.

**Benefits:**

- Crisp stills at any size, reproducible, no leaked home paths or keys.

---

### 4.40 Communication markers (`A_Improve.txt`)

**In simple terms:** the standing prompt asks the model to flag caveats, admit errors, draw distinctions, and frame key points using a small set of phrases ("One honest caveat:", "Fair pushback:", "Let me be precise:", "The key insight is:").

**Architecture / how it works:**

`A_Improve.txt` next to the package is appended to `ORCHESTRATOR_SYSTEM` via `_load_a_improve()`. If the file is missing, a fallback with the four named sections is used so instructions still build. This is the same text as the workspace `AGENTS.md` communication style.

**For a mid-level engineer:**

- This is standing-prompt content, therefore part of the frozen prefix. Editing `A_Improve.txt` busts the prefix on the next process (the Orchestrator freezes `_system` for vision-stable life).

**Benefits:**

- Reasoning is marked in a way a human can scan.
- Missing file does not break boot.

---

### 4.41 Data files and on-disk layout

**In simple terms:** keys and global defaults live next to the launcher. Everything about *this* project lives in that project's `.g023/` folder.

**Machine level (`G023_HOME`):**

| File | Role |
|------|------|
| `K.dat` | DeepSeek API key, first line |
| `config.json` | Global defaults |
| `ds_pricing.json` | Price sheet (hit/miss/output, regular/surge) |
| `ds_times.json` | Peak windows, UTC offset, weekdays |
| `A_Improve.txt` | Communication markers |
| `K.dat.example` | Template |

**Project level (`G023_PROJECT_ROOT`):**

| Path | Role |
|------|------|
| `.g023/config.json` | Per-project overrides |
| `.g023/cache.db` | SQLite cache |
| `.g023/backups/` | Pre-write snapshots |
| `.g023/sessions/*.jsonl` | Append-only item log |
| `.g023/logs/` | Objective handoffs |
| `.g023/prompts/{n}.txt` | Each user prompt |
| `.g023/memory/` | Project notebook |
| `.g023/bbs/` | Bulletin board (topic dirs, `.txt` threads) |
| `.g023/learn/` | Project-scope learn guides |
| `.g023/scratch/agent/` | Nested Agent branch journals |
| `.g023/history` | prompt_toolkit input history |
| `g023.md` / `g023_compact.md` / `AGENTS.md` | Standing project instructions |

`~/.g023/learn/` is env/user-scope learn guides (machine-level, not project).

**For a mid-level engineer:**

- JSON must stay valid and type-correct: `_coerce` drops broken keys silently, which is safe but confusing.
- `ds_*.json` may be updated when DeepSeek changes the sheet. Code falls back to defaults if missing. What must hold are *relationships* (surge = 2×, windows), not literal numbers — except the published rates are also what `/cost` shows.
- Anything new in `config.json` must be added to `Settings` + `PERSISTED_KEYS` or it will be ignored on load.

**Benefits:**

- One install, many projects, no cross-contamination of caches or backups.
- Price and time sheets are data, so a rate change does not require a code change.
- Keys are files you can permission, not environment variables you forget in a screenshot.

---

### 4.42 Bulletin board (`.g023/bbs/`)

**In simple terms:** a forum at `.g023/bbs/` for this session and later ones. Topic slugs are folders; conversations are `.txt` files. Subagents and the orchestrator write there. Posts are never copied into the system prompt.

**Architecture / how it works:**

`g023_code/bbs.py`. `ensure_layout()` seeds `GUIDE.md`, `tools_info.md`, and `bbs_tools/bbs` if missing; the orchestrator calls it on init. `post()` validates slugs, appends by default, rebuilds `INDEX.md` (cap 200 lines). SearchContent overlays `.g023/bbs/` the same way it overlays `.g023/memory/` — `.g023/cache.db` stays ignored. `/bbs` is the human surface; `python -m g023_code.bbs` is the agent CLI. WriteFile of a bbs thread takes a `.g023/backups/` snapshot (other `.g023/` paths still skip).

Standing instructions name the path, the peer stance ("one of equals"), and "do not treat an objective as complete while a surfaced issue is still open". Bodies stay on disk.

This is not the learn store, not memory notes, and not the working set.

**For a mid-level engineer:**

- Adding a native BbsPost tool would change the frozen tools prefix. File tools + a CLI keep the tools list still.
- Prepending INDEX.md would bust the prefix on every post. Search-on-demand is the cache-compatible design.
- Nested Agent workers inherit the standing BBS paragraph on the shared-prefix path.

**Observations:**

- The previous `./bbs` convention was invisible to SearchContent once moved under `.g023/` without the overlay — that is why the overlay exists.
- Four stores on purpose. Collapsing the forum into MEMORY.md or learn guides would mix discussions with facts and procedures.

**Benefits:**

- Later sessions inherit rejected approaches and open issues without a prompt-cache tax.
- Orchestrator ↔ subagent notes outlive a turn without a second system prompt.

---

### 4.43 Code ownership (`/ownership`)

**In simple terms:** the model tidies code it wrote itself (merges repeated logic it touches, fixes defects it notices, and names each change). In your files it changes only what the request needs and reports anything else.

**Architecture / how it works:** `ownership.py`. WriteFile records each file it *creates* in `.g023/provenance.json` (overwriting an existing file does not count, and neither does a file made by Bash). In `auto` mode, ReadFile/Edit/WriteFile JSON results for recorded files carry `"yours":true`, and the standing prompt gets one directive scoped to those files. `user` leaves the prompt byte-identical to the pre-ownership prompt. `agent` applies the directive to the whole project. The mode is part of the frozen-instructions key, so changing it rebuilds the cached prefix once.

| mode | standing prompt | tool results |
|---|---|---|
| `auto` (default) | directive for files marked `"yours":true` | marked |
| `user` | unchanged | unmarked |
| `agent` | directive for every file | unmarked |

**Observations:** this was measured live on deepseek-flash with one fixture: a duplicated loop, an untested docstring bug nearby, and the task "add a GBP total", 6 runs per arm. Every arm solved the task and kept existing behaviour. The guide-style identity sentence ("This codebase is yours ...") merged 0/12, the same as no sentence. The explicit directive merged 6/6 and fixed the nearby bug 3/6, for 2,581 vs 1,728 output tokens (about +28% cost). Identity framing written for Claude did not transfer to this model; the directive did. With the shipped `auto` mode, the same file merged 6/6 (nearby fix 3/6) when it was marked as created by g023, and 0/6 (0/6) when it was not.

**Benefits:** less duplicated code in files the agent owns, with no change in what it does to yours.

### 4.44 Near-miss names on a zero-hit search

**In simple terms:** when a search finds nothing because the model guessed a name's spelling (`count_words`), the result names the project's real spelling (`word_count`) and where it is defined.

**Architecture / how it works:** `near_miss.py`, called from `searcher._add_near_misses` only when `total` is 0. The query's identifiers are split into lowercased, lightly stemmed parts; names the project defines (one multi-language regex) and file stems are ranked by part-set overlap with per-part typo matching, or whole-name similarity. Up to 5 rows, one per name, within 1 s / 24 MB. `G023_NEAR_MISS=0` disables. See `docs/near_miss.md`.

**Observations:** live on deepseek-flash, a lookup question, 6 runs per arm: $0.000334 → $0.000247 per solved task, 3.67 → 2.83 calls, 6/6 solved both ways.

**Benefits:** one hop instead of a ListDir/ReadFile/re-grep chain, at no cost to searches that hit.

### 4.45 Predicated finish (on by default since 4.52) and `/settings set`

**In simple terms:** with `finish_on_success` on, the model may attach its final answer to the step that should finish the job (usually the test run). If that step succeeds, the turn ends there with the answer and a line quoting the real result; if it fails, the answer is thrown away and work continues.

**Architecture / how it works:** `finish.py`; see `docs/finish.md`. The success test is exit 0 with no test-failure summary in the output (a pipe can exit 0 after a failing run), or an applied edit with no syntax error and a passing re-run. No synthetic assistant message is added to history.

**Observations:** live, 4 task shapes × 3: 12/12 solved both ways, calls 5.58 → 4.33, wall time about −10%, cost per task unchanged when warm ($0.000830 vs $0.000845). The answer is still written, just earlier, so only one call's cached input is saved. It ships off; turn it on for speed. `/settings set KEY VALUE` changes any saved key with the same validation a loaded config gets.

**Benefits:** fewer round trips when on; every saved key is now visible and changeable in `/settings`.

### 4.46 Lean turns: closure receipts, lean codec, lean prompt, orientation card

**In simple terms:** four small changes that make a turn cheaper. An Edit result says whether the old name or value it removed still appears anywhere in the project. Command output is stored raw instead of JSON-escaped. Final answers no longer repeat what the trace already shows. The model is told up front where the functions and files the request names are.

**Architecture / how it works:** `closure.py` (Edit's `refs_left`), `tool_codec.encode_bash` and backup-path removal (`lean_codec`), `orchestrator.lean_system` (`lean_prompt`), and `orient.py` (`orient_card`). All four are on by default and can be turned off by setting or env. `scope_count` (off) adds `elsewhere_in_project` to scoped searches. See `docs/lean.md`.

**Observations:** live on deepseek-flash, 7 task shapes × 8 runs per arm: $0.000948 → $0.000703 per solved task (−26%), output tokens −32%, calls 5.05 → 4.41, 56/56 solved both ways. Edit tasks gained most; read-only lookups were flat. The model reads `refs_left` but often still greps to confirm, in the same batch as its import check, so that grep adds no call. `scope_count` measured neutral (0.000699 vs 0.000705), so it ships off.

**Benefits:** about a quarter off a typical edit task, with the same success rate.

### 4.47 View deltas: what a shell command changed, against what the model last saw

**In simple terms:** when a command such as a formatter, `sed -i` or a code generator changes a file the model has already read or edited, the command's result shows the file's new text (or a diff, for big files). The model does not have to read the file again before its next edit. If a file the model has seen changes between turns (the user's editor, a checkout, `/undo`), the next message says so and shows the change.

**Architecture / how it works:** `views.py` keeps the last-seen text per file (ReadFile, Edit, WriteFile; 64 files, 256 KB each). Bash is already bracketed by two stat fingerprints, so changed files are observed, not guessed. Small files (≤ 2,400 chars) are shown whole, larger ones as a unified diff, unseen ones by name, all within a 3,200-char budget. At turn start, any seen file whose text differs was changed by something other than the agent, because every agent write moves the view forward. So the drift notice cannot blame the agent's own changes on someone else. Setting `view_deltas`, env `G023_VIEW_DELTAS`. See `docs/views.md`.

Two opt-in search features ship with it: `search_tiers` ranks a def/class named by the query above module constants, local assignments and plain uses, and `def_peek` adds the body of an unambiguous definition to the search result (`docs/lean.md`).

**Observations:** live on deepseek-flash, two batches pooled, 9 runs per arm per task. Where a formatter rewrites a seen file between two edits (the deltas fire every run): mean $0.001901 → $0.001454 (−24%), spread down two thirds. Where they never fire (bug fix, pre-existing helper) the arms match within noise; a whole-bench mean is noise-dominated and is not claimed. A diff-only first version was worse, because the model re-read the file after seeing a hunk. Tiers + peek measured neutral (4 lookup tasks × 5: $0.000741 vs $0.000804), so they ship off.

**Benefits:** fewer re-reads after scripted rewrites, no stale-text Edit misses, and between-turn changes reported without guessing who made them.

---

### 4.48 Test oracle, request obligations, claim check and callee digests

**In simple terms:** five checks that watch what the model does with tests and with the request. When the model filters its test output (`pytest | tail -3`), the result still names the failing test and the line where it failed. When the request names a function ("add average_entry(ledger, account)") and the model wrote something else (a method), it is told once before it finishes. "Do not change the tests" makes the first edit of a test file stop and ask for a deliberate repeat. A final answer that says "all tests pass" when the last run failed gets a note under it. Reading a Python file also lists the signatures of the project functions it imports.

**Architecture / how it works:**
- `testoracle.py` + `probes/g023_pytest_probe.py`: a Bash command that runs pytest gets `PYTHONPATH`/`PYTEST_ADDOPTS` additions that load a dependency-free plugin, which writes a JSON report outside the project. The Bash result gains `[tests]` rows only for facts the visible output lacks: failing ids, `@ path:line`, the source line and error, a `(new this turn)` tag (AST diff against the turn's pre-images), and vacuous new tests. Vacuity: new passing tests are re-run in a shadow copy with the turn's source edits reverted. Commands that build their own environment (tox, docker, `env -i`, ...) are not touched; a plugin import failure re-runs the command without it. See `docs/testoracle.md`.
- `obligations.py`: identifier-only signatures after a creation verb are obligations; at the final answer the harness looks for them with `ast` and sends one note if missing or misshapen. Negative constraints become write guards. See `docs/obligations.md`.
- `claims.py`: the answer's unqualified pass claim against the test runs after the last write; display only. See `docs/claims.md`.
- `callees.py`: `uses:` rows on ReadFile of a `.py` file (≤ 10 rows). See `docs/callees.md`.

Settings `test_oracle`, `vacuity_check`, `obligations`, `write_guards`, `claim_check`, `callee_digest` (all on).

**Observations:** live on deepseek-flash. Every failure of the "add average_entry" task in runs without obligations (7 of 25) was the method-instead-of-function misread; with obligations 9/9 solved, the note fired once and that run recovered (one-sided Fisher p = 0.09: suggestive, not significant). The test oracle's first version, which also listed passing new tests, cost +11%; the delta-only version −14% (5 write tasks × 6). Callee digests: cheaper on all 4 write tasks pooled over two batches (−9% geo-mean, n = 10 per task), lookup tasks neutral. Final check of the shipped defaults against all of these off, 7 tasks × 5 runs: 35/35 solved both, mean $0.001916 vs $0.001905 (neutral) — the per-feature cost gains are within batch-to-batch noise and are not claimed. Two more options were measured and removed before release: re-running the last passing check after every write cost +15% (the model stopped batching edits), and an insert-after/before mode for Edit was used in 0 of 70 Edits. Write guards did not fire in 170 runs. The claim check (which never reaches the model) measured +7% in its own arm, which is the noise floor of a 4-run cell.

**Benefits:** a spec misread caught before the answer, test facts that survive the model's own filter, protected files protected, and a visible warning when an answer's test claim is not backed by the turn's runs — at no measured cost.

---

### 4.49 Salient excerpts, readable vault, and the optional local model

**In simple terms:** when a long output is cut, the lines the model was looking for are kept (it already said what it was looking for in its reasoning), and the full output stays in a file it can read. Optionally a small model on your own GPU (llama.cpp) reads big files for free (`LocalAsk`), reranks those lines, and answers screenshot questions first. It steps aside whenever the task is GPU work.

**Architecture / how it works:**
- `salience.py`: intent = last 600 chars of the step's reasoning + commentary (no extra call); BM25 + error-line picks; `build_excerpt`; vault files `.g023/vault/<sha16>.txt` (2 MB cap, newest 300) with a readback note. `tools/registry.keep_full_output` keeps the raw Bash streams when the cap dropped text. See `docs/salience.md`.
- `local_llm.py`: finds `llama-server` under `local_llama_dir`, the model under `local_models_dir`, runs one shared server per machine with `--sleep-idle-seconds 300`, JSON-schema calls with thinking off. GPU guard (other compute apps, free VRAM; browsers ignored) and GPU-work command detection release the server for the turn. See `docs/local_model.md`.
- `local_ask.py`: the `LocalAsk` tool (`question`, `paths`); 250-line chunks, cite-or-drop line ids, evidence copied from the files with heading-path/def anchors, short answer marked as a lead.
- `subagents/vision.py`: local VL model first when `local_vision_file` is set; the same image+question again goes to DeepSeek.

Settings `salience_excerpt` (on), `local_model` (`auto`), `local_llama_dir`, `local_models_dir`, `local_model_file`, `local_vision_file`, `local_gpu_layers`, `local_ctx`, `local_port`, `local_gpu_guard`; `/local`.

**Observations:** decisive line shown on 14 real long outputs: blind 3/14, salient 11/14, + local 1.7B rerank 12/14. Live A/B on three long-output tasks (12 runs per arm): 12/12 solved both, same cost (~$0.0005/task) — DeepSeek usually filters with `tail`/`grep` itself, so this is a safety net, not a routine saving. LFM3-VL-2B: 8/9 screenshot questions. Idle server: 120 MiB VRAM.

**Benefits:** no paid re-runs of slow or non-idempotent commands to see a cut middle; free bulk reading and vision on hardware you already have; nothing changes when the local model is off.

---

### 4.50 Scout card: the local model does the first digging hop

**In simple terms:** before DeepSeek sees your request, the small model on your GPU reads the parts of the project that best match it (~1 s) and the request carries the lines it picked, copied word for word from the files and marked as a lead. DeepSeek can start from them instead of listing and searching first. If DeepSeek keeps ignoring the cards, they switch themselves off. `LocalAsk` can now also search the whole project when no files are given.

**Architecture / how it works:**
- `scout.py`: project cut into def/class/heading units (cached by mtime/size), BM25 with path/symbol boost, tests and docs discounted unless asked for; the local model adds identifiers the code probably uses, then returns line ids from the top 8 units; ids never shown are dropped and the text comes from disk (cite-or-drop). Card = lines grouped by file and enclosing def, plus `also matching:` runner-up units. See `docs/scout.md`.
- `orchestrator._scout_card` appends the card after the orient card; no card for slash commands, short requests, GPU-work requests (`local_llm.request_wants_gpu`), a busy GPU, or a muted mode. `_scout_feedback` records whether the turn's tool arguments or answer named a scouted file or symbol (`scout_ledger.json`); `scout.trusted` mutes a mode below 30 % use over its last 10 cards.
- `local_ask._run_project`: `LocalAsk` without `paths` uses `scout.locate` over the whole project.
- `local_llm.chat_json` retries without a grammar when the model's vocabulary cannot build llama.cpp's sampler (HTTP 400), which had silently disabled every local feature with such models.

Setting `scout_card` (`local` default | `always` | `off`, `G023_SCOUT`); `/local scout`.

**Observations:** 20 natural-language questions over a 25k-line package: keyword top-3 13 -> 15–16 with the doc discount; LFM3-1.7B card hit 13/20 -> 17/20 with identifier expansion and the also-matching line, ~1 s; Nanbeige4.2-3B 14/20 at 5–10 s. Live A/B (DeepSeek Flash): 5 tasks × 3 reps per arm (2 package Q&A, 3 ledgerkit edit/trace), 15/15 solved in every arm; per task: no card $0.00125 and 5.4 calls, keyword card $0.00112 / 4.93, local card $0.00098 / 4.73 (−22 % cost). The card mostly replaces searching (SearchContent 1.27 → 0.40 per task; ReadFile unchanged at 2.67); all 30 cards were used by the paid model. Small sample, a direction rather than a measurement.

**Benefits:** free first hop on hardware you already have; a weak model cannot put invented code in front of the paid model; a card that does not help on your project turns itself off; nothing changes when the local model is off.

---

### 4.51 Local model auto-tune

**What it does:** `local_model` defaults to `auto`. A machine probe finds `llama-server`, a models folder and the GPU, and turns the local helper on in every project when the model fits; it picks the model the scout eval measured best.

- `autotune.py`: binary from `local_llama_dir`, common llama.cpp checkouts, then `PATH`; models from `local_models_dir`, `~/models`, LM Studio and llama.cpp caches; GPU from `nvidia-smi` total VRAM or Apple Silicon unified memory. No GPU = auto stays off (the scout's time box would cost up to ~12 s per turn on a CPU).
- Model rank: measured table (`KNOWN`: LFM3-VL-2B Q8 first, then LFM3-1.7B), then unmeasured chat models closest to a ~2 GB file; embeddings, rerankers, projectors, TTS and split shards are skipped; anything that does not fit 90 % of VRAM is dropped.
- One server serves text and vision, so local vision is on exactly when the chosen model has an `mmproj` next to it: no model swap ever. A projector that does not fit is dropped before the model is.
- Pins win: a set `local_model_file` / `local_vision_file` is served; `local_vision_file: off` keeps vision on DeepSeek. `/local tune` shows the probe; `/local auto|on|off`.
- A boolean `local_model` (`true` / `false`) reads as `on` / `off`; any other unknown value keeps the default `auto`.

**Benefits:** the measured −22 % $/task of the scout card without any setup, in every project on the machine; free local-first vision from the same server; nothing runs on machines without a GPU.

### 4.52 Round-trip economics: step budgets on, predicated finish on, two opt-ins

**In simple terms:** every model call ("hop") costs mostly the model's new thinking, not the re-read context. So g023 now asks the model to say how many calls each step should take, shows it a running `[hop N · step k of L-H · request $X]` line, and lets it put the final answer on the last step. Together: cheaper, fewer calls, faster, same quality.

**Architecture / how it works:** `hops.py`; see `docs/hops.md`. Measured over 543 earlier live runs: a hop's dollars are 55 % output, 34 % uncached input, 11 % cached prefix, and 60 % of write steps spent the next hop only on running the check. Settings: `step_budget` (on), `finish_on_success` (now on), and opt-in `ordered_batches` (program-order contract; a failed write skips the later checks and same-file writes of that response), `write_check` (Edit/WriteFile carry a `check` command run `&&` after the response's writes, counted as the turn's test run). Config files carry a `defaults_rev` stamp so the flipped defaults reach old installs.

**Observations:** live A/B, 7 task shapes, 3 rounds, 546 runs, every run solved. Task-stratified vs base with 90 % bootstrap CI: budget + finish $ −7.8 % [−14.4, −0.6], calls −16 %, time −10 %; finish alone (n=84) $ −5 %, calls −15 %; budget alone cost-neutral. Prompt prose describing ordered batches changed nothing (0 % of write steps co-batched their check); the same idea as a schema field (`check`) was used in about half of write runs, but did not lower cost and was worse together with finish. A third experiment, a `Rewind` tool (back to hop N with a lesson), was never called and its presence added 10 % calls; it was removed before release.

**Benefits:** cheaper and faster turns by default; the opt-ins are there for further experiments.

### 4.53 Pre-emptive reads: the harness does the predictable first hop

**In simple terms:** most coding requests start the same way: the model reads the files the request names. g023 now does those reads itself before the first model call and puts them into the conversation as if the model had asked. The model starts at the edit or the answer, one paid round trip earlier.

**Architecture / how it works:** `preread.py`; see `docs/preread.md`. From the request text alone: files it names (small enough to read whole), the project modules a named test imports, a named source file's test file when tests are mentioned, the unique definition of a named function (whole file or just that definition), a README next to another prediction, and for a rename one search for the name. Nothing is read on a guess: ambiguous or generic names, big-file outlines and plain English words never trigger a read. The reads go through the normal tool path and are recorded as the model's own tool calls, followed by a one-line note; that note is also what lets the API accept a tool exchange without the model's reasoning attached. Setting `preread` (`ghost` default, `prose`, `off`; env `G023_PREREAD`).

**Observations:** live, every run solved. Main A/B: cost per solved task −19 %, calls −20 %. On held-out prompts the first two sets showed the failure mode, a partial prediction (the model still needed one more read, so nothing was saved); after removing those cases a fresh held-out set gave −34 % cost, −35 % calls. Pooled over 130 runs per arm: cost −18 % [90 % CI −22, −12], calls −18 %. The model never re-read a pre-read file.

**Benefits:** cheaper, faster turns; questions about named code are answered in one call.

### 4.54 Closing calls: licensed finish, answer escrow, multi-file Edit, Rename

**In simple terms:** the last calls of a turn were often spent re-writing an answer the model had already given (because a test that was already broken before the turn failed again), or making one edit per call, or renaming a name with grep and sed one step at a time. g023 now delivers an answer whose only failures pre-date the turn (and says so), keeps a held answer so the model can release it with `[deliver]`, lets one Edit change several spots and files at once, and offers a `Rename` tool. A guard holds any answer that claims a file change that never happened.

**Architecture / how it works:** `finish.py` (`licence`, `escrow_note`/`release`, `unwritten_claims`), `multi_edit.py` + `ToolRegistry._exec_multi_file_edit`, `rename.py`; see `docs/finish.md`, `docs/multi_edit.md`, `docs/rename.md`. Settings `finish_licensed`, `answer_escrow`, `claimed_write_guard`, `multi_edit`, `rename_tool` (all on).

**Observations:** live A/B on held-out prompts, task-stratified: the five together $/solved −20.0 % [90 % CI −29.5, −8.8], calls −14 %, time −15 %, 24/24 solved; licensed finish alone −13.6 % [−19.5, −6.0]; rename tasks from ~5 calls to ~3; a fifth candidate (a no-match grep counts as a pass) saved nothing and was removed before release.

**Benefits:** cheaper, faster turns; answers never claim a clean run or an edit that did not happen.

## 5. Cross-cutting observations

These are the facts that show up in more than one feature. They are the ones that bite when you change the wrong thing.

1. **The 50× gap dominates local "save tokens" instincts.** Rewriting a cached prefix to shrink it is usually a cost increase. Policy lives in `cost_policy.py` so that instinct has to win an argument with arithmetic.

2. **Output is ~200× a cache hit.** Once the prefix is hot, the remaining bill is reasoning tokens and tool-loop completions. Effort 75 as ceiling, `[next_effort]` for follow-up, FileReader local index, and "prefer `[handoff]` over a second Flash call" are all the same observation.

3. **Prefix-cache keys include more than the prompt text.** Instructions, tools, effort alias, and temperature. Nested `max_output_tokens` busts it. Changing vision busts it (intentionally). Putting learn guides into instructions would bust it every write.

4. **History pairing is a session-killer.** Repair, rollback, reseat, and "wait until the parallel batch is done before mirroring the working set" exist because a 400 on the next turn is worse than a slightly delayed working-set item.

5. **Summary-first has a known hole.** Summaries omit bodies; nothing detects that the omitted detail was the one that mattered. The designed escape is the symbol map + range read + `raw=true` + fail-closed Edit + backups. There is no automatic "summary was too thin" detector.

6. **Delegation is smaller, not unconditionally cheaper.** Call counts are pinned. End-to-end session cost is not. `G023_READFILE_RAW=1` is how you measure it on your tree.

7. **Quiet failure is the chosen worst case.** Unvalidated responses + drift signals is a bet that additive API change should not crash you, and that a renamed field should be visible. It will not distinguish "schema change" from "model said nothing" from "your prompt drifted".

8. **Unattended runs are a different permission world.** ASK + EOF declines. Project `permission_default=allow` is required for piped `/goal` evals.

9. **Four memories, four jobs.** Working set (session, structured, mirrored, evidence-judged), learn store (cross-session procedures, retrieved, write-guarded), memory notes (project facts, searched, never injected), bulletin board (forum / message passing, searched, never injected). Do not merge them into a prepended blob.

10. **Two things tried and rejected with numbers.** Sharing the parent prefix for focused FileReader (miss cost went up). Slicing ReadFile/Agent JSON at 6,000 chars (symbol map vanished; pytest tails vanished). Depth-2 Agent (another miss plus another hop).

11. **Weekend surge was a real overcharge.** Peak is Monday–Friday Beijing. `ds_times.json` carries `peak_weekdays` so that cannot silently regress to "these clock hours, any day".

12. **One model is a feature.** A single constant model on a single wire removed three providers, two vision wires, a model picker and their config surface — and with them, every state where the config names something the API will not serve.

---

## 6. What is not claimed

Worth stating plainly, because the rest of this page is detailed enough to sound more certain than the measurements.

- **End-to-end session cost of summary-first vs raw-file inlining** has not been A/B'd on a controlled corpus. Smaller context is measured. Cheaper bill is arithmetic under assumptions.
- **Summary quality** is not checked. Offline checks cover structure, line ranges, call counts, and truncation flags — not whether the summary answered the question.
- **FetchUrl** sees what DeepSeek's fetcher sees: no JavaScript, no login. A refused page is remembered for 1 h.
- **Costs in `/cost`** are a local estimate from returned token counts and `ds_pricing.json`, not a reading of the invoice.
- **Bash confinement** is cwd + a dangerous-string filter, not a sandbox. The model can still run a command that leaves the project.

---

## 7. Module map

| Module | Feature area |
|--------|----------------|
| `g023_code/__main__.py`, `__init__.py` | Entry, version 1.5.0 |
| `cli.py` | REPL, slash handlers, one-shot, `--continue` |
| `commands.py` | Command catalogue as data |
| `orchestrator.py` | Agent loop, compact, pairing, streaming |
| `api.py` | `DeepSeekClient` (one model, one wire) + drift parsers |
| `effort.py` | 0–75 ↔ `none`/`low`/`high`; `[handoff]` / `[next_effort]` |
| `cost_policy.py` | Keep vs compact vs vault arithmetic |
| `economics.py` | Ski-rental compaction timing (`CompactionMeter`) |
| `anthropic_codec.py` | Items ↔ Anthropic messages, deterministic bytes |
| `tool_codec.py` | Tool results → densest lossless form before history |
| `edit_engine.py` | Edit match ladder, byte-faithful atomic writes |
| `attribution.py` | Shadow-copy re-run: new vs pre-existing failures |
| `testoracle.py` | pytest probe, `[tests]` rows, vacuity (+ `probes/g023_pytest_probe.py`) |
| `obligations.py` | Named-callable checks before the answer; write guards |
| `claims.py` | Final-answer pass claim vs the turn's test runs |
| `callees.py` | `uses:` rows for imported project names on ReadFile |
| `docindex.py` | Heading induction, outlines, BM25, fold-to-budget |
| `salience.py` | Step-intent excerpts, readable vault files |
| `local_llm.py` | Optional llama.cpp server, GPU guard, JSON calls, vision |
| `autotune.py` | Machine probe for the local model: binary, models, GPU; picks the measured-best model (`local_model` auto) |
| `local_ask.py` | LocalAsk: cite-or-drop bulk reading on the local model (no paths = whole project) |
| `scout.py` | Turn-zero scout card: unit index, BM25, local expansion + cite-or-drop picks, use-confirmed self-muting |
| `config.py` | Paths, Settings, scopes, keys, coercion |
| `paths.py` | Project-root confinement |
| `cache.py` | SQLite: files, vision, web, vault, prefix_stats, session_facts |
| `usage.py` | Token/cost accounting |
| `signals.py` | Quiet-failure log + hit-rate verdict |
| `surge.py` | Peak/off-peak windows, pause, notify |
| `working_set.py` | Remaining work, evidence, goal judge |
| `session_log.py` | JSONL ancestry + objective handoffs |
| `learn.py` | Cross-session procedures |
| `memory_notes.py` | `.g023/memory/` notebook |
| `ownership.py` | Code ownership: provenance of created files, `"yours"` marks, scoped directive |
| `near_miss.py` | Zero-hit SearchContent: the project's real spelling of the searched name |
| `finish.py` | Predicated finish: final answer on a step, delivered only if it succeeds (on by default); licensed finish, answer escrow, claimed-write guard |
| `multi_edit.py` | Edit's `more_edits`: several hunks, optionally in several files, one call, all or nothing |
| `rename.py` | `Rename` tool: whole-word identifier rename across the project, all files or none |
| `hops.py` | Round-trip economics: step budgets + hop tracker, ordered batches, write checks |
| `bbs.py` | `.g023/bbs/` bulletin board |
| `prompt_store.py` | `.g023/prompts/{n}.txt` |
| `project_instructions.py` | `g023.md` / compact / `AGENTS.md` |
| `prompt.py` | Input line, pickers, Ctrl+Up |
| `ui.py` | Every pixel |
| `web.py` | WebSearch/FetchUrl: DeepSeek Search/Fetch + machine-wide cache |
| `images.py` | Image load, media type, 800 px cap |
| `tools/schemas.py` | Tool definitions shown to the API |
| `tools/registry.py` | Permissions + light-tool executors |
| `subagents/router.py` | Heavy-tool dispatch + nested Agent |
| `subagents/file_reader.py` | ReadFile |
| `subagents/searcher.py` | SearchContent + Glob |
| `subagents/vision.py` | AnalyzeImage |

Per-module specification: [`docs/README.md`](docs/README.md).

---

## 8. Environment variables

Set by the launchers (override at your own risk):

| Variable | Meaning |
|----------|---------|
| `G023_HOME` | Installation folder — `K.dat`, machine `config.json`, `ds_*.json` |
| `G023_PROJECT_ROOT` | The project being worked on |

Read directly:

| Variable | Effect |
|----------|--------|
| `G023_READFILE_RAW` | ReadFile returns raw content, no subagent — the A/B baseline |
| `G023_WRITE_BACKUP` | `0`/`false`/`no`/`off` disables `.g023/backups/` |
| `G023_WRITE_BACKUP_KEEP` | How many backup versions to retain per file (minimum 1) |
| `G023_LEARN` | `0`/`false`/`off` disables the learn store |
| `G023_ASCII` | Force plain-ASCII glyphs |
| `G023_VERIFY_REPLAY` | `0`/`false`/`no`/`off` disables verify replay |
| `G023_ATTRIBUTION` | `0`/`false`/`no`/`off` disables failure attribution |
| `G023_FINISH_ON_SUCCESS` | `0` turns off predicated finish (default on) |
| `G023_STEP_BUDGET` | `0` turns off step budgets and the hop tracker (default on) |
| `G023_PREREAD` | `ghost` (default) / `prose` / `off`: pre-emptive reads before the first model call |
| `G023_FINISH_LICENSED` | `0` turns off licensed finish (default on) |
| `G023_ANSWER_ESCROW` | `0` turns off answer escrow / `[deliver]` (default on) |
| `G023_CLAIMED_WRITE_GUARD` | `0` turns off the claimed-write guard (default on) |
| `G023_MULTI_EDIT` | `0` removes Edit's `more_edits` (default on) |
| `G023_RENAME_TOOL` | `0` removes the `Rename` tool (default on) |
| `G023_ORDERED_BATCHES` | `1` program-order batch contract + skip after a failed write (default off) |
| `G023_WRITE_CHECK` | `1` lets Edit/WriteFile carry a `check` command (default off) |
| `G023_NEAR_MISS` | `0`/`false`/`no`/`off` disables zero-hit near-miss names |
| `G023_SALIENCE` | `0`/`false`/`no`/`off` restores the blind head/tail vault excerpt |
| `G023_LOCAL` | `1`/`true`/`yes`/`on` forces the local llama.cpp model on, `0`/`off` off; unset = `auto` (on when `autotune.py` finds llama-server, a model and a GPU it fits) |
| `G023_SCOUT` | scout card: `local` (default; needs the local model), `always` (keyword card without it), `off` |
| `G023_CLOSURE_RECEIPTS` | `0` turns off Edit `refs_left` (setting `closure_receipts`) |
| `G023_LEAN_CODEC` | `0` keeps Bash output JSON-escaped and backup paths in history (setting `lean_codec`) |
| `G023_LEAN_PROMPT` | `0` restores the pre-lean standing prompt (setting `lean_prompt`) |
| `G023_ORIENT_CARD` | `0` turns off the `[where: ...]` orientation line (setting `orient_card`) |
| `G023_SCOPE_COUNT` | `1` adds `elsewhere_in_project` to scoped SearchContent results (setting `scope_count`) |
| `G023_VIEW_DELTAS` | `0` turns off Bash view deltas and the between-turn drift notice (setting `view_deltas`) |
| `G023_SEARCH_TIERS` | `1` ranks search hits by what the line defines (setting `search_tiers`) |
| `G023_DEF_PEEK` | `1` adds an unambiguous definition's body to SearchContent results (setting `def_peek`) |
| `G023_TEST_ORACLE` | `0` turns off the pytest probe and `[tests]` rows (setting `test_oracle`) |
| `G023_VACUITY` | `0` turns off the shadow re-run of new passing tests (setting `vacuity_check`) |
| `G023_OBLIGATIONS` | `0` turns off the named-callable check before the final answer (setting `obligations`) |
| `G023_WRITE_GUARDS` | `0` turns off write guards from "do not change ..." (setting `write_guards`) |
| `G023_CLAIM_CHECK` | `0` turns off the note on unbacked "tests pass" claims (setting `claim_check`) |
| `G023_CALLEE_DIGEST` | `0` turns off `uses:` rows on ReadFile (setting `callee_digest`) |
| `G023_PYTEST_REPORT_DIR` | Internal: set by g023 in a probed pytest run's environment (report folder); not a user setting |
| `G023_GLOBAL_CACHE_DIR` | Folder of the machine-wide web cache (default `<install>/global_cache/`) |
| `DEEPSEEK_API_KEY` | Used by the installer if set, instead of prompting |

---

## 9. Slash-command index

Commands that take a fixed set of options open a picker when typed bare. `/help <command>` explains any one of them in full.

### Session

| Command | Action |
|---------|--------|
| `/help [command]` (`/?`, `/h`, `/commands`) | List commands, or explain one |
| `/status` (`/dash`) | Dashboard: model, context, cost, vision, cache, surge, which `g023.md` is loaded, memory-notes and bbs lines |
| `/clear` (`/reset`) | Reset conversation, usage counters, working set |
| `/resume` · `/resume log` (`/continue`) | Latest JSONL view, or seed from `.g023/logs/` |
| `/learn [search\|get\|save\|forget\|outcome]` | Cross-session procedures |
| `/bbs [list\|search\|read\|post\|index]` | Project bulletin board at `.g023/bbs/` |
| `/undo [path]` (`/restore`) | Restore newest `.g023/backups/` copy |
| `/exit` (`/quit`, `/q`) | Quit |
| `/config [status\|save\|global\|project\|clear]` (`/cfg`) | Global vs per-project config scopes |

### Model

| Command | Action |
|---------|--------|
| `/model` | Show the model (DeepSeek V4.1 Flash), limits, thinking levels, prices |
| `/thinking [1-100\|low\|high\|max\|off]` (`/effort`) | Reasoning effort (75 default and ceiling) |
| `/verbose [low\|mid\|high]` (`/v`) | Trace detail |
| `/steps [n\|off\|default]` (`/iterations`, `/maxsteps`) | Tool-step cap (default 255) |
| `/max-tokens [n\|off\|default]` (`/maxtokens`, `/output`) | Reply length budget (default off) |

### Context & cost

| Command | Action |
|---------|--------|
| `/compact [focus]` | Summarise history (`micro` = local; `auto on\|off`) |
| `/g023-compact` (`/g023compact`) | Compact `g023.md` → `g023_compact.md` |
| `/max-ctx [n\|default]` (`/max-context`, `/ctx`) | Context-window ceiling (default 1M) |
| `/compact-threshold [tokens\|fraction\|default\|economic\|fixed\|floor N]` (`/compact-at`) | Auto-compact trigger (200k / 0.85 ceiling); `economic` policy (default) also compacts once rent ≥ price, floor 64k |
| `/subagents [on\|off]` (`/subagent`) | Opt in to the `Agent` tool and model-assisted ReadFile focus (default off; saved) |
| `/ownership [auto\|user\|agent\|forget <path>]` | Whose code the model may tidy: files it created (`auto`, default), none (`user`), all (`agent`); saved |
| `/context` | Breakdown of what occupies the window |
| `/cost` (`/usage`) | Tokens and spend, hit/miss split, sparkline |
| `/signals` (`/drift`) | Drift log and hit-rate verdict |
| `/settings [save\|reset\|set KEY VALUE]` | Every setting; which persist; set one saved key |

### Tools

| Command | Action |
|---------|--------|
| `/tools` (`/permissions`, `/perm`) | Tools, permission level, run counts |
| `/tools <tool> allow\|ask\|block` | Change one tool's permission |
| `/fetch <url>` | Fetch a URL yourself (cache/fresh/deny) |
| `/search [fresh] <query>` | Web search through the machine-wide cache |
| `/cache [stats\|web\|clear]` | Inspect and purge SQLite caches |

### Vision / money

| Command | Action |
|---------|--------|
| `/vision` (`/config_vision`) | `on` / `off` / `status` |
| `/local` | `status` / `auto` / `on` / `off` / `tune` / `unload` / `models` / `model NAME\|auto` / `vision NAME\|off\|auto` / `scout local\|always\|off` |
| `/surge` | Peak/off-peak status, notify, pause, buffer, `now` |

### Work

| Command | Action |
|---------|--------|
| `/goal <text>` | High-level objective; effort 75, no step cap, up to two continuations while the judge says open |

---

## Related reading

- User-facing overview: [`README.md`](README.md)
- Per-module specification: [`docs/README.md`](docs/README.md)
- Techniques, evidence and prior art: [README → For researchers](README.md#for-researchers)
- Release history: [`CHANGELOG.md`](CHANGELOG.md)

## Interruption, retries and crash safety

**Ctrl-C stops the running work, not the session** (`interrupts.py`). A turn,
slash command or surge pause runs as an interruptible child task. Ctrl-C
cancels it; the turn rolls back as a transaction (completed tool calls kept,
the rest withdrawn with a note); Bash process groups are killed. At a
permission prompt Ctrl-C means "no". Three presses within 2 s force-quit.

**Replay equals memory.** The session log runs replay's own reducer as a
shadow and writes a `view` record whenever history was rewritten, so
`/resume` restores exactly what the model saw.

**Program-order tool batches.** Reads in a batch run in parallel; writes,
Bash and anything that asks are barriers.

**Bounded API retries** on transient statuses and transport errors, with
jitter and `Retry-After`; a cut-off stream falls back to one blocking call.

**Atomic, locked stores** (`fsutil.py`): no torn config or index files, no
two sessions claiming one name, no lost learn votes; identical parallel web
calls pay once; an identical re-sent BBS post is not duplicated.

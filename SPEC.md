# g023 Code — The Complete Exhaustive Technical Specification & Architecture Guide

## A Pure Python Powered by DeepSeek V4

**Version**: 3.2 (Responses API — g023 Edition)
**Compiled**: August 2026 · implementation 1.1
**Core Principle**: *Context is Currency. Subagents are the Treasury.*

---

## I. Executive Overview & Foundational Philosophy

**g023 Code** is a terminal-native, pure-Python AI programming assistant designed to replicate and surpass the capabilities of current agentic CLI apps while leveraging the cost-efficiency of DeepSeek V4 Flash. Vision is served locally by an Ollama daemon rather than by the API.

Unlike standard AI agents that naively dump raw search results, full file contents, and high-resolution images into a single massive context window (polluting the orchestrator's 1M-token space and destroying prefix-cache economics), g023 Code employs a strict **Subagent-First Delegation Architecture**. 

**The Core Philosophy**: 
- The **Orchestrator (Flash)** is reserved exclusively for high-level reasoning, tool orchestration, and maintaining conversational flow. 
- **Subagents (Flash, plus a local Ollama model for vision)** handle all data-heavy tasks (file reading, searching, vision) in isolated, minimal contexts, returning only compact, metadata-rich summaries.
- **Aggressive Caching (SQLite + Prefix)** ensures that repeated operations (reading the same file, analyzing the same image) cost effectively **$0.00** in API tokens.
- **The Responses API is the single transport.** Every call — orchestrator, subagent, compaction — goes to `POST /responses`, because that is the only DeepSeek endpoint exposing the server-side `web_search` tool (§XIV).

---

## II. Core Infrastructure: The Agentic Loop

### 2.1 The Deterministic Engine
The system operates on a synchronous `while True` loop. Approximately **98% of the codebase is deterministic infrastructure** (permissions, routing, context management), with only ~2% being AI decision logic. This makes g023 Code primarily an engineering project.

```python
async def agentic_loop(user_prompt: str):
    state.items.append({"type": "message", "role": "user",
                        "content": [{"type": "input_text", "text": user_prompt}]})

    while True:
        # 1. Build the request. instructions + tools are static -> cache hit.
        response = await client.create(
            model="deepseek-v4-flash",
            instructions=SYSTEM_PROMPT,
            input=state.items,                 # verbatim prior output items
            tools=registry.get_schemas(),      # thin proxies + {"type": "web_search"}
            reasoning=reasoning_param(),
        )

        # 2. Echo every output item back into history unchanged. Reasoning,
        #    messages and web_search_call items are carried as-is: that is both
        #    the highest-fidelity history and the most cache-friendly one.
        state.items.extend(response["output"])

        calls = [i for i in response["output"] if i["type"] == "function_call"]
        if not calls:
            return output_text(response)       # phase == "final_answer"

        for call in calls:
            # Permission check, then route: heavy work to a subagent, the rest
            # to a local executor. web_search never appears here — the server
            # runs it inside the turn and reports it as a web_search_call item.
            result = await registry.execute(call)
            # Pairing is mandatory: an unmatched call or output is a 400.
            state.items.append({"type": "function_call_output",
                                "call_id": call["call_id"], "output": result})
```

### 2.2 State Management — Items, Not Messages
`/responses` does not take a message list. It takes `input`: a flat array of
**items**, and the model's own `output` items are appended to it verbatim.

The orchestrator's `state.items` therefore holds:
- `message` items (user input, assistant `commentary` and `final_answer`)
- `reasoning` items — echoed back unmodified so the model keeps its own chain
- `web_search_call` items — the server's record of a search it ran (§XIV)
- `function_call` / `function_call_output` pairs
- **Explicitly excludes**: raw file contents, image base64, subagent internal logs.

Two invariants follow, and every history mutation must respect them:

1. **Pairing.** A `function_call` with no matching `function_call_output` is a
   400, and so is an output whose `call_id` was never called. Truncation,
   compaction and error rollback all repair pairing before the next request
   (`Orchestrator._rollback_to_last_complete`).
2. **Verbatim echo.** Items go back byte-identical. Rewriting or normalising
   them invalidates the prefix cache and can desync the model's reasoning.

### 2.3 Message Phases
Assistant `message` items carry a `phase`:

| Phase | Meaning | Rendering |
| :--- | :--- | :--- |
| `commentary` | Mid-flight narration ("let me check the docs page"). | Panel titled *thinking aloud*. |
| `final_answer` | The answer to the user's turn. | Normal answer panel. |

**Streaming caveat**: `response.output_item.added` always reports
`phase="final_answer"`. The real phase is only settled on
`response.output_item.done`, so that is where the live panel gets its title.

A turn can legitimately end with commentary and no final answer. Both the
orchestrator and `api.output_text()` fall back to the commentary rather than
reporting the model as silent.

### 2.4 Reasoning Control
`/responses` has no equivalent of the chat-completions `thinking:
{"type": "disabled"}` — that field is accepted and silently ignored, and the
model reasons anyway. `reasoning: {"effort": "none"}` is what actually disables
thinking, and that is what `/thinking off` maps to.

### 2.5 Tool Schema Shape
Responses tools are **flat**: `name`, `description` and `parameters` sit at the
top level of each entry. The chat-completions `{"type": "function", "function":
{...}}` nesting is rejected.

---

## III. Model Strategy & Economic Routing

### 3.1 One API Model
`deepseek-v4-flash` is the **only** model the program sends to the API, for
every role: orchestrator, subagents and compaction.

`config.AVAILABLE_MODELS` is `("deepseek-v4-flash",)`. `deepseek-v4-pro` is
listed in `UNAVAILABLE_MODELS` with the reason: DeepSeek has not enabled it on
`/responses` yet, and a request for it comes back with *"Codex integration with
deepseek-v4-pro will be available starting early August 2026. Please use
deepseek-v4-flash instead for now."* Offering the choice would hand the user a
model every call fails on, so `/model` refuses it and quotes that reason, and
`/cost` prices only what `/responses` will actually serve.

| Attribute | DeepSeek V4 Flash |
| :--- | :--- |
| **Total Parameters** | 284B (MoE) |
| **Active Parameters** | ~13B |
| **Context** | 1M Tokens |
| **Concurrency** | 2,500 req/s |
| **Vision Support** | ❌ Text-only — vision runs on Ollama (§VI) |
| **Role** | Orchestrator, all API subagents, compaction |

### 3.2 Pricing & Cache Economics
| Scenario | Flash Price |
| :--- | :--- |
| **Input (Cache Hit)** | $0.0028 / 1M |
| **Input (Cache Miss)** | $0.14 / 1M |
| **Output** | $0.28 / 1M |
| **Cache Hit vs Miss** | **50x cheaper — crucial for profitability** |

`usage.PRICING` still carries a pro row so historical figures and any future
re-enablement price correctly, but `/cost` filters its footer to
`AVAILABLE_MODELS`.

### 3.3 Routing Rules
- **Orchestrator**: `deepseek-v4-flash`.
- **Explore / Plan / FileReader**: `deepseek-v4-flash` (isolated, cheap).
- **Searcher**: no API call at all — pure local regex walk (§5.3).
- **Vision Subagent**: a local **Ollama** vision model (`/vision <model>`), never the DeepSeek API.
- **Web search**: executed **server-side by DeepSeek** inside the orchestrator's own turn (§XIV).

---

## IV. DeepSeek V4 Cache Optimization (The Economics Engine)

DeepSeek V4 operates on a **Hybrid Attention** mechanism (SWA + C4/C128). g023 Code exploits this with exacting precision.

### 4.1 Prefix Caching Mechanics
The API automatically caches the *beginning* of the prompt. If the first N tokens are identical between requests, the cache is hit.
- **Static Guarantees**: The System Prompt and Tool Definitions are **locked**. They form the first 5,000 tokens of every request. **Hit Rate: 100%**.
- **Block Size Optimization**: We configure the engine with `--block-size 32`. This granularity increases the probability of exact prefix matches in multi-turn conversations, pushing overall hit rates above 95%.

### 4.2 KV Cache Footprint
DeepSeek V4 Flash uses only **7%** of the KV cache memory required by V3.2 at 1M context. This allows the local harness to handle massive concurrency without manual tiered offloading (L1/L2/L3) unless running extreme 1M contexts. 

### 4.3 Cache Preservation Strategy
To avoid invalidating the prefix cache:
1. **Static Prefix**: System/Tools are immutable.
2. **Variable Middle**: Subagent summaries are injected *after* the static prefix. The cache engine ignores changes far down the prompt.
3. **Eviction**: When the sliding window evicts old summaries, it removes tokens *after* the static prefix. The cache remains intact for the next turn.

---

## V. Subagent Architecture (The Efficiency Engine)

Subagents are isolated workers. They are the **only** entities allowed to read raw files, run heavy greps, or process images. They always return compact summaries to the Orchestrator. Some are isolated model calls; others are pure local code — the distinction is invisible to the orchestrator, which sees one compact `function_call_output` either way.

### 5.1 Subagent Registry & Auto-Spawning
The Orchestrator intercepts specific intents via `subagents/router.py`.

| User/Orchestrator Intent | Subagent Spawned | Backend | Isolated Context Payload |
| :--- | :--- | :--- | :--- |
| "Read src/auth.py" | `FileReader` | Flash (`/responses`), single-shot | (System prompt) + (File slice) |
| "Find `validate_token`" | `Searcher` | **Local only — no API call** | (Regex) + (Glob) |
| "Analyze screenshot.png" | `Vision` | **Ollama daemon** | (Question) + (Downscaled image) |
| "Explore DB connections" | `Explore` / `Plan` | Flash (`/responses`), single-shot | (System prompt) + (Objective) |

Single-shot subagents have no tools to narrate towards, so `api.output_text()`
falls back to a `commentary` message when the run produced no `final_answer`
(§2.3) — that narration *is* their reply.

### 5.2 The FileReader Subagent (Read + One-Shot Compaction)
This is the most critical optimization for orchestrator health.

**System Prompt Rules**:
> 1. Extract only structural metadata (Classes, Functions, Imports, Top-level logic).
> 2. If the user provided a specific context (e.g., "auth logic"), filter the file to that section only.
> 3. DO NOT return raw text unless `raw=True` is explicitly set in the tool call.
> 4. Output strict JSON: `{ "metadata": {...}, "summary": "2-sentence description", "hash": "sha256" }`.

**The Swap Loop**:
1. Orchestrator needs `auth.py`.
2. Spawns `FileReader`. It reads the file, extracts metadata, and compacts it to ~200 tokens.
3. `FileReader` stores the raw content + summary in the local SQLite `file_cache.db`, keyed by the SHA256 hash.
4. Orchestrator discards the raw file from its memory and stores only the summary + hash.
5. *Later*: If the summary is evicted, the Orchestrator still holds the hash. It requests the summary again. `FileReader` checks SQLite, finds the hash, returns the cached summary in <10ms. **Cost: $0.00**.

### 5.3 The Searcher Subagent (Metadata-First Extraction — Local)
Standard `grep` returns hundreds of lines of raw code. g023 Code's Searcher
transforms this, and does so **without any API call**: `subagents/searcher.py`
is pure Python. It costs $0.00 and returns in milliseconds.

**Contract**:
> 1. Cap at `max_matches` (default 12; ≤3 per file).
> 2. For each match, return `{ "file", "line", "match", "context_before", "context_after" }`.
> 3. Aggregate into a `metadata_summary` (e.g. "Found 3 matches across 41 files scanned. Occurrences in: auth.py").
> 4. When the cap truncates results, **say so** in the summary — silence there reads as "that is all there is".

Directory pruning happens during the walk (`os.walk` with `dirnames[:]`
filtered), not after: `rglob('*')` would descend into `node_modules` and `.git`
before anything got filtered. Ignored directory names are only matched *below*
the search root, so a project that happens to live under a directory called
`build` or `venv` is still searchable.

**Orchestrator Impact**: The orchestrator receives a JSON blob of ~150 tokens instead of a 5,000-token text dump. The line numbers allow the orchestrator to request exact snippets later via FileReader if needed.

### 5.4 Subagent Context Isolation Metrics
| Task | Orchestrator Direct Cost | Subagent Isolated Cost | Savings |
| :--- | :--- | :--- | :--- |
| Reading a 500-line file | 100k tokens (context) + 2k raw | ~200 tokens (summary) | **99.8%** token reduction |
| Searching whole repo | 100k tokens + 10k results | ~150 tokens (JSON metadata) | **99.5%** reduction |
| Vision (Full HD) | 100k + 2.5k image | 0 API tokens (local Ollama) | **100%** — vision is off-API |

---

## VI. Vision System (Local, Ollama-Backed)

**Vision never touches the DeepSeek API.** `deepseek-v4-flash` is text-only, and
`deepseek-v4-pro` is not available on `/responses` (§3.1). Image analysis is
served entirely by an **Ollama daemon**, which may be local or on another
machine (§XII-A). The API cost of vision is therefore **$0.00**; the only cost
is GPU time on the daemon.

### 6.1 The Pipeline
1. **Gate**: `AnalyzeImage` is hidden from the tool schema entirely unless
   `settings.vision_enabled` — the model cannot call a tool that would only
   return "vision is disabled". Enable it with `/vision <model>`.
2. **Load & downscale**: `ollama_client.load_image()` reads the path or URL and
   downscales to `settings.vision_max_image_dim`, returning raw bytes + base64.
3. **Cache probe**: keyed on `(sha256(raw image), question, "ollama:<model>")`.
   A hit returns instantly with `"cached": true` and no GPU work.
4. **Inference**: `vision_chat_detailed()` posts the image and `VISION_SYSTEM`
   to the daemon, honouring `vision_timeout`, `vision_num_ctx` and
   `vision_keep_alive`.
5. **Return**: compact JSON — the orchestrator never sees image bytes.

### 6.2 The Vision System Prompt
The subagent is instructed to answer precisely and concisely, to transcribe code,
error messages, stack traces, file paths and UI labels **exactly** as they
appear, and to say the image is unclear rather than guess.

### 6.3 Failure Diagnosis
`_failure_hint()` re-probes the host on failure so the error distinguishes the
two genuinely different causes: *"no daemon answered at `host`"* versus *"the
daemon is up, so `model` is likely missing or not image-capable"*. See §XII-A
for the three-level diagnostic split behind `/ollama`.

### 6.4 Deferred: Tile-Based Selective Zoom
The original design called for a two-pass tile strategy — a downscaled first
pass, then `request_tile_detail(x, y)` crops at full resolution against a `4x4`
virtual grid, controlled by `/vision_tiles`. **This is not implemented.** Vision
currently sends one downscaled image. The economics that motivated tiling were
API-token economics; with vision on a local daemon the pressure is largely gone,
so this remains specified but unscheduled.

---

## VII. File & Directory Handling (One-Swoop Compact)

To avoid polluting the orchestrator's prefix cache, **all file reads are delegated to Subagents**. Raw file content is never stored in the orchestrator's message history.

### 7.1 The "Read-Compaction-Swap" Cycle
1. **Spawn**: Router spawns `FileReader` Subagent.
2. **Process**: Subagent reads the file (or directory structure) and immediately compiles a structured summary.
3. **Swap**: Subagent returns `{ summary, hash }`. Orchestrator stores this compact object.
4. **Local Cache**: Subagent stores the raw content + summary in `file_cache.db` (SQLite) keyed by `hash`.
5. **Eviction**: Orchestrator context fills up. Sliding window removes the summary.
6. **Re-fetch**: Orchestrator needs the file again. It passes the hash to the Subagent.
7. **Zero-Cost Return**: Subagent queries SQLite, finds the hash, returns the summary in <10ms. **No API call made**.

### 7.2 Directory Handling
If the user asks to "look at the src folder":
- `FileReader` performs a `glob` and reads essential files (`__init__.py`, `main.py`).
- It returns a tree:
  ```
  src/
  ├── auth.py (Imports: jwt, os | Functions: login, validate)
  ├── models.py (Classes: User, Session)
  └── utils.py (Functions: hash_password)
  ```

---

## VIII. Intelligent Search (Metadata-First + Snippet Extraction)

Standard `grep` is noisy. g023 Code's `Searcher` Subagent performs **structured extraction**.

### 8.1 Tool: `SearchContent`
| Parameter | Description |
| :--- | :--- |
| `query` | Text or regex pattern. An invalid regex is retried as a literal rather than erroring. |
| `path` | Target directory or file. Relative paths resolve against the project root. |
| `max_matches` | Default: 12 |
| `file_glob` | `*.py`, `py`, `.py`, `*.py,*.md`, `src/**/*.ts` — all normalised to the same thing. |

### 8.2 Output Transformation
Instead of raw lines, the Searcher returns a JSON object:
```json
{
  "query": "validate_token",
  "path": "/home/me/proj/src",
  "file_glob": "*.py",
  "max_matches": 12,
  "truncated": false,
  "files_scanned": 41,
  "metadata_summary": "Found 2 matches across 41 files scanned. Filtered to *.py (13 files skipped). Occurrences in: src/auth.py",
  "matches": [
    {
      "file": "src/auth.py",
      "line": 42,
      "match": "def validate_token(token):",
      "context_before": ["def authenticate(user):"],
      "context_after": ["    return decode_jwt(token)"]
    }
  ]
}
```
Paths in `matches` are relativised to the project root for readability.
**Orchestrator Benefit**: It receives ~100 tokens of structured data instead of 5,000 tokens of raw code. It knows exactly which file and line to target if deeper context is needed later.

---

## IX. Tool System (Orchestrator Proxy vs Subagent Implementation)

The Orchestrator exposes a thin set of tools. Heavy lifting is delegated.

### 9.1 Orchestrator Tool List
These are exactly the schemas in `tools/schemas.py`, in schema order.

| Tool Name | Function | Target | Permission Default |
| :--- | :--- | :--- | :--- |
| `ReadFile` | Read a file with automatic compaction; `start_line`/`end_line` return that range verbatim instead. | `FileReader` | `allow` |
| `SearchContent` | Grep/glob with metadata extraction. | `Searcher` (local) | `allow` |
| `AnalyzeImage` | Analyze an image (local/url). | `Vision` (Ollama) | `ask` |
| `Bash` | Execute a shell command. | Internal | `ask` |
| `WriteFile` | Create or overwrite a file. | Internal | `ask` |
| `ListDir` | List a directory. | Internal | `allow` |
| `Agent` | Spawn Explore/Plan. | `Explore` / `Plan` | `ask` |
| `FetchUrl` | Fetch a URL to Markdown. | Internal (free providers) | `ask` — always, see below |
| `web_search` | Search the web. | **DeepSeek, server-side** | n/a — not client-executed |

Two entries deserve emphasis:

- **`web_search` is not a tool g023 executes.** It is appended to the schema
  list as the bare `{"type": "web_search"}` marker and DeepSeek runs it inside
  the model's own turn. There is no executor, no permission entry and no point
  at which the harness could interpose a prompt. See §XIV.
- **`AnalyzeImage` is omitted from the schema entirely while vision is
  disabled**, so the model never proposes a call it cannot fulfil — and the
  static prefix stays stable for as long as the config does.

### 9.2 Permission Levels
- `allow`: auto-execute. `SAFE_TOOLS = (ReadFile, SearchContent, ListDir)` — read-only, local and cheap — stay allowed whatever the default is.
- `ask`: requires user confirmation. `DEFAULT_ASK_TOOLS = (AnalyzeImage, Bash, WriteFile, Agent)` follow `settings.permission_default`.
- `block`: hard disabled.

`FetchUrl` is the exception to the default: a network fetch leaves the machine
and touches a third party, so it is pinned to `ask` regardless of
`permission_default` — the only way to lower it is an explicit
`/tools FetchUrl allow`, and a `permission_default` of `block` still blocks it.

---

## X. Context Management & Compaction Strategy

### 10.1 Three-Layer Compaction
1. **MicroCompact (Layer 1)**: local, deterministic, free. Blanks the `output`
   text of old `function_call_output` items to `[Old tool result content
   cleared]`, keeping the most recent 6. **The items themselves stay** — removing
   one would orphan its `function_call` and make the next request a 400 (§2.2).
   So item *count* is unchanged; only characters are reclaimed.
2. **Session Memory (Layer 2)**: pre-extracted facts in `session_facts` reused
   as the summary. No API cost.
3. **API Summary (Layer 3)**: `/compact [focus]`. Renders the item list to a flat
   transcript (`_render_transcript`, dropping `reasoning` items — the model's
   scratch work, not the record of the session), summarises it with
   `deepseek-v4-flash` in an isolated context, and replaces history with that
   summary. Reported honestly: a short conversation can summarise to something
   *longer* than itself, and `/compact` says so rather than clamping the figure
   to a flattering "0 reclaimed".

### 10.2 Sliding Window
`max_context_tokens` is 900k (headroom under the 1M limit) and
`compact_threshold` is 0.85. Auto-compaction fires when the context fraction
crosses that threshold, if `settings.auto_compact` is on.

- The engine drops the oldest items, always preserving call/output pairing.
- Because `instructions` and the tool schemas are static and at the *start* of
  the request, dropping later items does not invalidate the prefix cache.

### 10.3 Prefix Cache Preservation
- The System Prompt and Tool Schemas are **immutable**.
- Subagent summaries are injected in the **middle** of the prompt.
- When the sliding window cleans the middle, the **start** of the prompt remains identical to the previous request. The cache engine hits immediately for the next turn, saving up to 50x on input costs.

---

## XI. The `.g023/` Operational Folder (Scratch & Memory)

The harness creates a hidden operational root at the project base to store all volatile and persistent state. **This folder is strictly excluded from all user-level searches** to prevent the AI from reading its own metadata.

### 11.1 Folder Structure
As implemented (`config.get_scratch_dir`, `config.get_cache_db`):

```
<project_root>/                     # cwd, or $G023_PROJECT_ROOT
└── .g023/                          # Auto-created on launch
    ├── cache.db                    # One SQLite file, four tables (§XIII)
    ├── cookies.json                # Per-host cookies (created on first fetch)
    └── history                     # prompt_toolkit REPL history
```

Machine-level state lives beside the program instead, in `get_home()` (the
package parent, or `$G023_HOME`):

```
<g023_home>/
├── K.dat                           # DeepSeek API key, first line
└── config.json                     # Persisted settings
```

The split is deliberate: `config.json` holds vision setup, which describes the
local machine's GPU and Ollama install, so it belongs to the machine rather than
to whichever project happens to be open.

**Not implemented**: the originally specified `scratch/`, `memory/`, `configs/`
and `logs/` subtrees, project-level `G023.md` memory, and `.g023ignore`.
Exclusions are the hardcoded list in §11.2 instead.

### 11.2 Strict Exclusion Strategy
1. **Hardcoded Global Ignore**: `SearchContent` and `ListDir` share the
   `IGNORE_DIRS` / `IGNORE_EXTS` lists in `subagents/searcher.py`, covering
   `.g023`, `.git`, `__pycache__`, `node_modules`, `.venv`, `dist`, `build`,
   caches and binary extensions.
2. **Prune, don't filter**: directories are pruned during the walk rather than
   after — `rglob('*')` would descend into `node_modules` before anything got
   filtered.
3. **Root-relative matching**: ignored names are only matched *below* the search
   root, so a project living under a directory called `build` is still
   searchable.
4. **Subagent Inheritance**: every search path goes through the same helpers, so
   there is one list to keep correct.

**Not implemented**: `pathspec`/`.g023ignore` support and a `--debug` override.

### 11.3 Startup Behavior
1. On launch, `get_scratch_dir()` creates `.g023/` if missing.
2. `Cache` opens `cache.db` and runs `CREATE TABLE IF NOT EXISTS` for all four tables.
3. `check_handlers()` asserts every catalogued command has a `cmd_*` method (§12.1).

---

## XII. Slash Commands

### 12.1 Single Source of Truth

The catalogue lives in `commands.py` as data — name, aliases, group, arguments,
summary, long help, examples. `/help`, tab completion, and the "did you mean"
suggester all read from that one list, so a command can never be completable but
undocumented, or documented but unroutable. `check_handlers()` runs at startup
and raises if any command's `cmd_*` method is missing on the CLI: the catalogue
and the behaviour cannot drift apart silently.

Commands declaring `interactive=True` open a numbered picker when invoked with
no argument. The rule is that a user should never need to know the option names
in advance — `/vision` and `/vision qwen3.5:2b` are both first-class.

### 12.2 Implemented Commands

| Command | Function | Impact on Context/Cache |
| :--- | :--- | :--- |
| `/help [cmd]`, `/?` | List commands, or explain one in full. | Local. |
| `/status` | Dashboard: model, session, vision, ollama, cache. | Local. |
| `/clear` | Start fresh. | Clears orchestrator state and counters. |
| `/exit` | Quit. | — |
| `/model [flash]` | Show or set the orchestrator model. Flash is the only one `/responses` serves; `pro` is refused with DeepSeek's own reason (§3.1). | Local config. |
| `/thinking [low\|high\|max\|off]` | Reasoning effort, or disable thinking. | Local config. |
| `/verbose [low\|mid\|high]` | Trace / +outcomes & timing / +reasoning. | Local config. |
| `/compact [focus]` | Compress conversation history. | API call to Flash (summary). |
| `/compact micro` | Layer-1 only: blank out stale tool results. | Local, free. |
| `/compact auto on\|off` | Toggle auto-compaction at `compact_threshold`. | Local config. |
| `/context` | Break down the window by role and size; recommends which compaction applies. | Local. |
| `/cost` | Token usage and cost, split by hit/miss, with a per-turn sparkline. | Local aggregator. |
| `/settings [save\|reset]` | All settings, marking which persist. | Local / `config.json`. |
| `/tools` | Tools, permission levels, and run counts this turn. | Local. |
| `/tools <tool> allow\|ask\|block` | Change one tool's permission. | Local. |
| `/fetch <url>` | Fetch a page yourself, via the cache/fresh prompt. | May write the URL cache. |
| `/fetch status` | Report browser-fidelity of outbound requests. | Local. |
| `/cache [stats\|web\|clear]` | Inspect and purge the SQLite caches. | Frees disk, forces re-fetch. |
| `/ollama [status]` | Host, version, latency, model count. | Network probe. |
| `/ollama host <addr\|default>` | Retarget the vision daemon; tested before saving. | `config.json`. |
| `/ollama models` | Installed models: size, params, quant, vision capability. | Network. |
| `/ollama test [model]` | Real inference round-trip against the daemon. | Network + GPU. |
| `/ollama ps` | What the daemon holds in VRAM. | Network. |
| `/vision` | Picker of installed vision models. | Persisted; hides/shows `AnalyzeImage`. |
| `/vision <model>\|off\|status` | Direct enable / disable / inspect. | Persisted. |
| `/goal <objective>` | One turn at forced max reasoning effort. | Restores prior effort after. |

Aliases: `/?` `/h` `/commands` → `/help`; `/reset` → `/clear`; `/quit` `/q` →
`/exit`; `/effort` → `/thinking`; `/v` → `/verbose`; `/usage` → `/cost`;
`/config` → `/settings`; `/permissions` `/perm` → `/tools`; `/dash` → `/status`;
`/config_vision` → `/vision` (retained from 1.0).

### 12.3 Deferred

`/btw` (isolated side question) and `/vision_tiles` (tile-grid control, §6.4)
remain specified but unimplemented. Vision currently sends one downscaled image
rather than a tile grid.

There is no `/websearch` command, and there should not be: search is not a
client-side capability to toggle (§14.1).

---

## XII-A. Vision Daemon Location

Vision is served by an Ollama daemon that need not be on the local machine, so
the GPU can live elsewhere. Host resolution has one precedence chain, resolved
in `ollama_client.resolve_host()`:

1. an explicit argument (used by the subagent, which passes `settings.vision_host`)
2. `settings.vision_host` — what `/ollama host` writes to `config.json`
3. the `OLLAMA_HOST` environment variable
4. `http://localhost:11434`

`normalize_host()` is deliberately forgiving about what a user types: a missing
scheme becomes `http://`, and a missing port becomes `:11434` — except on
`https://`, which is left alone because it is almost always a reverse proxy on
443. IPv6 literals are handled bracket-aware, so `[::1]:11434` parses correctly.

Diagnosis is split into three levels because the failures are genuinely
different and need different fixes:

| Level | Command | Answers |
| :--- | :--- | :--- |
| Reachability | `/ollama` | Is anything listening? `probe()` distinguishes connection-refused from timeout, and `unreachable_hint()` gives different advice for a local vs a remote host. |
| Inventory | `/ollama models` | Does that machine have the model? `find_model()` resolves exact → bare-name → unique-substring. |
| Capability | `/ollama test` | Does the whole path work? Sends a generated image with one unmistakable feature and checks the answer — proving encoding, transport, and genuine image support rather than an open port. |

`subagents/vision.py` applies the same split on failure: `_failure_hint()`
re-probes the host so the error says either "no daemon answered at *host*" or
"the daemon is up, so *model* is likely missing or not image-capable".

**Security.** Ollama has no authentication. A daemon bound to `0.0.0.0` is
usable by anyone who can reach the port, and sees everything sent to it. The
documented posture is: trusted network only, or behind an SSH tunnel or an
authenticating reverse proxy. `/help ollama` states this in-app.

---

## XIII. Local Caching Strategy (SQLite)

To eliminate redundant API calls, g023 Code uses an aggressive local SQLite cache.

Everything lives in **one** database — `.g023/cache.db` — with four tables.
Inspect and purge it with `/cache [stats|web|clear]`.

### 13.1 File Cache Schema
```sql
CREATE TABLE file_cache (
    file_hash   TEXT PRIMARY KEY,
    file_path   TEXT NOT NULL,
    summary     TEXT NOT NULL,
    metadata    TEXT,
    raw_content TEXT,
    created_at  REAL NOT NULL,
    accessed_at REAL NOT NULL
);
```

### 13.2 Vision Cache Schema
Keyed on `backend` — the string `"ollama:<model>"` — so switching vision models
re-analyses rather than serving another model's answer. (The originally
specified `grid_config` column is gone with the tile design, §6.4.)
```sql
CREATE TABLE vision_cache (
    image_hash    TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    backend       TEXT NOT NULL,
    response      TEXT NOT NULL,
    created_at    REAL NOT NULL,
    PRIMARY KEY (image_hash, question_hash, backend)
);
```

### 13.3 Web Cache Schema
Backs `FetchUrl` and `/fetch`. Records the fetch engine and browser-fidelity
profile alongside the body, so `/fetch status` can report how the request went
out.
```sql
CREATE TABLE web_cache (
    url_hash   TEXT PRIMARY KEY,
    url        TEXT NOT NULL,
    final_url  TEXT,
    status     INTEGER,
    headers    TEXT,
    body       TEXT NOT NULL,
    engine     TEXT,
    profile    TEXT,
    fetched_at  REAL NOT NULL,
    accessed_at REAL NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0
);
```

### 13.4 Session Facts Schema
Layer-2 compaction input (§10.1).
```sql
CREATE TABLE session_facts (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    created_at REAL NOT NULL
);
```

### 13.5 Benefits
- **Zero-cost re-reads**: file summaries are retrieved in <10ms.
- **Zero-cost vision**: repeated image analysis returns instantly, with no GPU work.
- **Zero-cost re-fetch**: a cached URL skips the network entirely.

---

## XIV. Web Search (Native) & URL Fetch (Free Providers)

### 14.1 Web Search — DeepSeek Server-Side

**Web search is native.** It is not a provider chain, not a client-side
executor, and not something the harness runs. It is DeepSeek's own
`web_search` tool, and it exists only on `/responses` — which is precisely why
the whole program moved to that endpoint.

**Wiring.** `tools/schemas.py` defines the entire integration:

```python
WEB_SEARCH_TOOL = {"type": "web_search"}
```

`registry.get_schemas()` appends it to the tool list. That is all. There is no
`name`, no `parameters`, and no executor — the marker tells the server the tool
is permitted, and the server does the rest.

**Execution model.** The search happens *inside* the model's turn, on
DeepSeek's infrastructure, before the response comes back. The loop never sees
a `function_call` for it and never returns a `function_call_output`. Instead
the response `output` contains `web_search_call` items recording what the
server did. Consequences worth being explicit about:

- **No permission hook.** There is no moment at which the harness could
  interpose a prompt, so `web_search` has no entry in the permission table
  (§9.2). Granting the model the tool is the decision; each search is not.
- **No latency signal by default.** The server can spend a long time out on the
  web without emitting anything, so a `web_search_call` at
  `response.output_item.added` draws a "searching the web…" progress line —
  otherwise it reads as a hang.
- **Multiple searches per turn** are normal (six observed in one turn).
- **Results persist as items.** `web_search_call` items are echoed back
  verbatim like any other item, so findings survive across loop iterations,
  across turns, and across compaction (`_render_transcript` renders them as
  `[web search] …`).

**Actions.** Each `web_search_call` carries an `action`, decoded by
`orchestrator.describe_search()`:

| `action.type` | Carries | Trace line |
| :--- | :--- | :--- |
| `search` | `queries[]` | the queries, comma-joined |
| `open_page` | `url` | `read <short url>` |
| `find_in_page` | `url`, `pattern` | `find '<pattern>' in <short url>` |

The server appends a `ws_call_id=…` pseudo-parameter to the queries and URLs it
reports; `_strip_call_id()` removes it so the trace shows what was actually
searched for. `open_page` can fail — the server meets timeouts and blocks like
any other client — so its status is surfaced rather than assumed.

### 14.2 URL Fetch Providers (Fallback Chain)

`FetchUrl` remains a client-side tool with its own provider chain, since the
server-side `open_page` is only reachable through the model's own reasoning:
1. **web4agent**: Self-hosted (Python library).
2. **webpeel**: 500/week (Free tier).
3. **crw (fastCRW)**: 500 credits (Free tier).
4. **Direct fallback**: `BeautifulSoup` parsing of raw HTML.

---

## XV. Development Roadmap

- **Phase 1**: Core Engine + Flash API + Basic CLI.
- **Phase 2**: Tool System (Permissions) + Bash/File ops.
- **Phase 3**: Subagent System (FileReader, Searcher, Explore, Plan) + SQLite cache.
- **Phase 4**: Vision Subagent (Ollama backend) + `/vision` + `/ollama` diagnostics.
- **Phase 5**: Goal System (`/goal`) + Compaction (`/compact`).
- **Phase 6**: Web Tools + Cache management.
- **Phase 7**: **Responses API migration** — items-based history, native
  `web_search`, `reasoning.effort`, message phases. Flash-only.
- **Phase 8** (open): `/btw`, `/vision_tiles` (§6.4), `deepseek-v4-pro` once
  DeepSeek enables it on `/responses`.

---

## XVI. Success Metrics & KPIs

| Metric | Target | Why it matters |
| :--- | :--- | :--- |
| **Orchestrator Context Pollution** | < 5% raw data. | Ensures 95% context is pure reasoning. |
| **Prefix Cache Hit Rate** | ≥ 95% (System/Tools). | Guarantees 50x input cost savings. |
| **File Summary Hit Rate** | ≥ 90% (SQLite). | Eliminates redundant file reading API costs. |
| **Vision API Cost** | $0.00. | Vision is served off-API by Ollama. |
| **Subagent Spawn Latency** | ≤ 500ms. | Ensures UX feels instant. |
| **Tool-Call Pairing Integrity** | 100%. | An unpaired item is a hard 400 (§2.2). |

---

## XVII. Summary of All Savings

| Optimization Layer | Mechanism | Token/Cost Reduction |
| :--- | :--- | :--- |
| **Subagent Isolation** | Orchestrator keeps 100k context; Subagent uses 200. | 99.8% context reduction. |
| **File One-Shot** | Read + Compact in one turn. Hash-based re-fetch. | 99% token reduction + $0.00 re-reads. |
| **Local Vision** | Ollama daemon instead of a vision-capable API model. | 100% — no API tokens at all. |
| **Local Search** | `Searcher` is pure Python; no model call. | 100% — no API tokens at all. |
| **Search Metadata** | JSON snippets vs raw text dumps. | 95% search overhead reduction. |
| **Prefix Caching** | Static instructions/tools + verbatim item echo. | 50x cheaper input tokens. |
| **Native Web Search** | Server-side; results never round-trip as tool output. | No client-side fetch, parse or token spend. |

g023 Code delivers a coding assistant that completely decouples data retrieval from high-level reasoning, preserving the LLM's precious context for what it does best: logic, planning, and conversation. The result is a system that is not only functionally equivalent to current agentic CLI, but is algorithmically superior in cost-efficiency and context hygiene.

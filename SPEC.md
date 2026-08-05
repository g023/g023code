# g023 Code — The Complete Exhaustive Technical Specification & Architecture Guide

## A Pure Python Powered by DeepSeek V4

**Version**: 3.2 (Responses API — g023 Edition)
**Compiled**: August 2026 · implementation 1.2
**Core Principle**: *Context is Currency. Subagents are the Treasury.*

> **On the numbers in this document.** Anything stated as a measurement is
> either asserted by `tests/` (and named as such), quoted from DeepSeek's
> published price sheet with attribution, or reported by the API itself.
> Anything else — design targets, illustrative arithmetic, vendor
> specifications — is labelled. Sections that describe unimplemented or
> unverified behaviour say so in place rather than in a footnote.

---

## I. Executive Overview & Foundational Philosophy

**g023 Code** is a terminal-native, pure-Python AI programming assistant built
around the cost profile of DeepSeek V4 Flash. Vision is served locally by an
Ollama daemon rather than by the API. No comparison against other agentic CLIs
has been benchmarked; the design differences below are stated as design
differences, not as measured advantages.

The central bet is that dumping raw search results and full file contents into
one large context window is expensive in two ways — tokens, and the model's
attention — and that summarising first is usually the better trade.

**The Core Philosophy**:
- The **Orchestrator (Flash)** is reserved for high-level reasoning, tool
  orchestration, and conversational flow.
- **Subagents (Flash, plus a local Ollama model for vision)** handle data-heavy
  tasks (file reading, searching, vision) in isolated contexts, returning
  compact, metadata-rich summaries — plus, for Python, an exact symbol →
  line-range map so the orchestrator can escalate to verbatim source cheaply.
- **Caching (SQLite + prefix)** means a repeated operation on unchanged bytes
  makes **no API call**. It is not free in wall-clock terms — it is a local
  SQLite read — and it does not make the *first* read free.
- **The Responses API is the single transport.** Every call — orchestrator,
  subagent, compaction — goes to `POST /responses`, because that is the only
  DeepSeek endpoint exposing the server-side `web_search` tool (§XIV).

**What is delegated and what is not.** The orchestrator does not receive whole
files by default, but it *can* receive verbatim source: a `ReadFile` with
`start_line`/`end_line` returns the exact text of that range (capped at 20k
chars, and it reports the line it actually reached when the cap bites). The
accurate statement of the design is *summary first, verbatim on request* —
see §5.2 and §XVIII.

---

## II. Core Infrastructure: The Agentic Loop

### 2.1 The Deterministic Engine
The system operates on an `async while True` loop. Almost all of the ~7,700
lines of Python are deterministic infrastructure — permissions, routing, context
management, rendering, caching; the model's contribution is the content of a
handful of prompts and the decisions it makes inside the loop. No line-level
split between "infrastructure" and "AI logic" has been computed, because the
boundary is not well defined enough for a percentage to mean anything.

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

Model attributes below are **DeepSeek's published figures**, not measurements
taken by this project. Only the last two rows describe g023's own behaviour.

| Attribute | DeepSeek V4 Flash | Source |
| :--- | :--- | :--- |
| **Total Parameters** | 284B (MoE) | vendor |
| **Active Parameters** | ~13B | vendor |
| **Context** | 1M tokens | vendor |
| **Vision Support** | ❌ Text-only — vision runs on Ollama (§VI) | vendor |
| **Role here** | Orchestrator, all API subagents, compaction | this project |
| **Context ceiling used** | 900k (`max_context_tokens`, headroom under 1M) | this project |

### 3.2 Pricing & Cache Economics

DeepSeek's published rates for V4 Flash, mirrored in `usage.PRICING`:

| Scenario | Flash Price |
| :--- | :--- |
| **Input (Cache Hit)** | $0.0028 / 1M |
| **Input (Cache Miss)** | $0.14 / 1M |
| **Output** | $0.28 / 1M |

The ratio between the two input rates is exactly **50x**. That is a property of
the price sheet, not an achievement of this harness — `tests/test_usage_
accounting.py::test_the_published_ratio_is_the_price_sheet` asserts only that
the two numbers in the table divide to 50. What the harness contributes is
keeping the prompt prefix stable so that more tokens land on the cheaper side;
how *much* more, in any given session, is not measured (§XVIII).

Costs shown by `/cost` are computed locally from the token counts the API
reports, priced against this table. They are an estimate of the bill, not a
reading of the account. A model with no row here is priced as Flash
(`usage.DEFAULT_PRICING`), so its reported spend is an approximation.

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

## IV. Prefix Cache Behaviour

Prefix caching happens **entirely on DeepSeek's side**. This project does not
run, configure, or tune an inference engine: there is no block size to set, no
KV cache to manage, no tiering to arrange. What it can do is avoid changing the
front of the prompt between requests, and then report what the server says
happened.

### 4.1 What the client actually controls
The API caches a matching prefix of the prompt. So:

- **The prefix is held still.** `instructions` and the tool schemas are built
  from static data and sent first on every request. They change only when the
  configuration does — notably when `/vision` toggles, which adds or removes the
  `AnalyzeImage` schema and therefore *does* invalidate the prefix once.
- **Output items are echoed back byte-identical** (§2.2). Reconstructing them
  from parsed fields would serialise differently and break the match;
  `tests/test_history_integrity.py` asserts the echo is the server's own dicts,
  not rebuilt ones.
- **History grows at the end.** New items are appended, so the unchanged head of
  the request stays a valid prefix.

No target hit rate is claimed here. The rate the server reports is shown live in
the status bar, broken down in `/cost`, and diffed against previous days in
`/signals` (§XIX). Those are observations, not guarantees — and a cache hit rate
is a property of the server's cache state, which no client can promise.

### 4.2 Cache preservation, and where it is knowingly given up
1. **Static prefix**: system prompt and tool schemas first, unchanged.
2. **Variable middle**: subagent summaries and tool results are appended after it.
3. **Eviction**: the sliding window drops the *oldest items after* the static
   prefix, which keeps the head of the request identical to last time.
4. **Compaction breaks it deliberately.** `/compact` replaces the history with a
   summary, so the next request's middle is entirely new. That is the intended
   trade — a smaller window at the cost of one cold turn — and it is a normal
   cause of a hit-rate dip in `/signals`.

---

## V. Subagent Architecture (The Efficiency Engine)

Subagents are isolated workers. They are the only entities that read whole files,
run repository-wide greps, or process images. They return compact summaries by
default — with one deliberate exception: an explicit line-range `ReadFile`
returns verbatim source, because a summary is the wrong answer when the caller
already knows exactly which lines it needs (§5.2). Some subagents are isolated
model calls; others are pure local code — the distinction is invisible to the
orchestrator, which sees one compact `function_call_output` either way.

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
This is the central optimisation for orchestrator context health, and the one
with the most honest caveats attached.

**Four paths, and what each costs.** These counts are asserted in
`tests/test_call_accounting.py`:

| Condition | Path | API calls |
| :--- | :--- | :--- |
| `G023_READFILE_RAW=1`, whole file | raw baseline (§5.2.3) | 0 |
| `start_line` / `end_line` given | verbatim slice | 0 |
| Content hash + focus already cached | cache hit | 0 |
| Python, < 12k chars, no focus | local `ast` summary | 0 |
| Anything else (non-Python, ≥ 12k chars, or focused) | Flash summary | 1 |

**Result shape.** Every summary result names its own provenance, so a caller can
tell a measured fact from a generated one:

```json
{
  "path": "src/auth.py", "hash": "sha256:…", "lines": 412,
  "summary_source": "local_ast" | "model" | "local_fallback",
  "summary_covers": {"chars_summarised": 18000, "chars_total": 54211, "complete": false},
  "metadata": {"language": "python", "classes": […], "functions": […],
               "symbols": {"TokenStore.validate": [88, 141], …}},
  "note": "metadata.symbols maps every top-level symbol …",
  "from_cache": false
}
```

- `summary_source` distinguishes a summary derived from the AST from one written
  by the model from one produced after an API failure.
- `summary_covers` appears when the file exceeded the 18k-char summariser input.
  A summary of the first 18k characters is not a summary of the file, and saying
  so is the difference between a partial answer and a wrong one. The model is
  told too — its prompt is headed `--- FILE CONTENT (TRUNCATED) ---`.
- `metadata.parse_error` appears when Python fails to parse. Previously a syntax
  error produced an empty structure, which read as *"this file has no classes and
  no functions"*.

**§5.2.1 The symbol map (escalation path).** For Python, `metadata.symbols` maps
every top-level symbol and `Class.method` to its exact `[start_line, end_line]`,
computed from `ast` node positions with `decorator_list` taken into account, so
the range starts at the first decorator rather than at `def`. These are measured
line numbers; where the model returns its own, the local values win
(`_merge_metadata`), because a line number the model did not compute is a guess.

This is what makes a thin summary recoverable at low cost: name → range → a
line-range `ReadFile` → verbatim text, zero API calls. `tests/test_file_reader.py`
asserts that round trip.

**§5.2.2 The known gap.** Nothing detects that a summary was *insufficient* for
the question asked. The orchestrator escalates only if it recognises that it is
missing something, which is exactly the judgement a model is unreliable at. The
symbol map lowers the cost of escalating; it does not trigger it. No detector is
specified here because none is implemented, and a specified-but-absent detector
in this document would be the same class of claim this pass exists to remove.

**§5.2.3 The raw baseline (`G023_READFILE_RAW=1`).** With this environment
variable set, a whole-file `ReadFile` returns the file's content verbatim and
makes no subagent call — the behaviour a plain agent loop would have. It exists
so the delegation trade can be measured rather than asserted: run the same task
script with and without it and compare `/cost`. No such comparison is published
here, because none has been run under controlled conditions.

**The swap loop.**
1. Orchestrator needs `auth.py`.
2. `FileReader` reads it and returns metadata + summary (typically a few hundred
   tokens; the exact size depends on the file).
3. Content and summary are stored in `file_cache`, keyed by SHA-256 of the bytes
   **and** the focus string — a summary written for one question is not served
   as the answer to another.
4. The orchestrator keeps the compact object, not the file.
5. A later read of unchanged bytes with the same focus is a local SQLite lookup:
   **no API call**. Changed bytes miss and re-summarise.

### 5.3 The Searcher Subagent (Metadata-First Extraction — Local)
Standard `grep` returns hundreds of lines of raw code. g023 Code's Searcher
transforms this, and does so **without any API call**: `subagents/searcher.py`
is pure Python, so it costs nothing in tokens. Wall-clock time is whatever
walking your tree costs. `tests/test_call_accounting.py` asserts the zero.

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

### 5.4 What Isolation Actually Buys

Earlier revisions of this document carried a table of percentage savings
(99.8%, 99.5%) computed from invented baselines. Those numbers were not
measured and have been removed rather than restated more carefully.

What can be said precisely:

| Task | Enters orchestrator context | Extra API calls |
| :--- | :--- | :--- |
| Reading a 500-line file | Structural summary + symbol map, in place of the file | 1, or 0 for small Python / cache hits / line ranges |
| Searching the repo | A capped JSON match list, in place of raw grep output | 0 — always local |
| Analysing an image | The vision model's text answer, never image bytes | 0 DeepSeek calls; the work moves to your GPU |

The ratio between "the file" and "the summary" depends entirely on the file, so
no single percentage describes it. The generalisation that survives scrutiny is
directional: the orchestrator's context grows by a bounded, small amount per
operation instead of by the size of the data. What that saves in money depends
on turn count and prefix survival, which §XVIII spells out.

---

## VI. Vision System (Local, Ollama-Backed)

**Vision never touches the DeepSeek API.** `deepseek-v4-flash` is text-only, and
`deepseek-v4-pro` is not available on `/responses` (§3.1). Image analysis is
served entirely by an **Ollama daemon**, which may be local or on another
machine (§XII-A). The DeepSeek API cost of vision is therefore **$0.00** — the
cost moves to GPU time, VRAM, and whatever the daemon's hardware costs to run.
"Off-API" is the accurate description; "free" is not.

### 6.1 The Pipeline
1. **Gate**: `AnalyzeImage` is hidden from the tool schema entirely unless
   `settings.vision_enabled` — the model cannot call a tool that would only
   return "vision is disabled". Enable it with `/vision <model>`.
2. **Load & downscale**: `ollama_client.load_image()` reads the path or URL and
   downscales to `settings.vision_max_image_dim`, returning raw bytes + base64.
3. **Cache probe**: keyed on `(sha256(raw image), question, "ollama:<model>")`.
   A hit is a local SQLite read — `"cached": true`, no GPU work. Because the
   question is part of the key, a *different* question about the same image is a
   fresh inference.
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

File reads are delegated to subagents by default. Whole file bodies do not enter
the orchestrator's history unless a line range was explicitly requested, in
which case that range's text does — capped at 20k characters, cut at a line
boundary, with the true final line reported.

### 7.1 The "Read-Compaction-Swap" Cycle
1. **Spawn**: Router spawns `FileReader` Subagent.
2. **Process**: Subagent reads the file (or directory structure) and compiles a structured summary.
3. **Swap**: Subagent returns `{ summary, metadata, hash }`. Orchestrator stores this compact object.
4. **Local Cache**: Subagent stores the content + summary in `file_cache` (SQLite), keyed by content hash **and** focus.
5. **Eviction**: Orchestrator context fills up. Sliding window removes the summary.
6. **Re-fetch**: Orchestrator asks for the file again.
7. **Return without an API call**: the bytes still hash the same, so the cached summary is served from SQLite. If the file changed on disk, the hash differs and it is re-summarised — a stale summary is never served for changed content.

Raw content is **not** never-seen: step 2 is bypassed entirely for a line-range
request, which returns the exact text (§5.2). The invariant is that the
orchestrator does not receive whole file bodies *unrequested*.

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
    ├── cache.db                    # One SQLite file, five tables (§XIII)
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
2. `Cache` opens `cache.db` and runs `CREATE TABLE IF NOT EXISTS` for all five tables.
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
| `/signals` | Hit rate against its own daily history, unknown item types, unexplained empty responses (§XIX). | Local; reads `prefix_stats`. |
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
`/drift` → `/signals`; `/config_vision` → `/vision` (retained from 1.0).

### 12.2.1 Environment Overrides

| Variable | Effect |
| :--- | :--- |
| `G023_HOME` | Installation folder — `K.dat`, `config.json` |
| `G023_PROJECT_ROOT` | The project being worked on; `.g023/` is created under it |
| `OLLAMA_HOST` | Vision daemon, when `settings.vision_host` is unset (§XII-A) |
| `G023_ASCII=1` | Force plain-ASCII rendering |
| `G023_READFILE_RAW=1` | `ReadFile` returns raw content instead of delegating — the measurement baseline of §5.2.3 |

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

Everything lives in **one** database — `.g023/cache.db` — with five tables: four
caches, plus `prefix_stats`, which is a record rather than a cache (§13.5).
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

### 13.5 Prefix Stats Schema
Not a cache — a record of what the server reported, kept per local day so
`/signals` can diff today against the trailing baseline (§XIX). `/cache clear`
deliberately leaves it alone: erasing the baseline to reclaim a few kilobytes
would destroy the only history that makes the hit rate interpretable.
```sql
CREATE TABLE prefix_stats (
    day         TEXT NOT NULL,
    model       TEXT NOT NULL,
    hit_tokens  INTEGER NOT NULL DEFAULT 0,
    miss_tokens INTEGER NOT NULL DEFAULT 0,
    calls       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, model)
);
```

### 13.6 What Caching Does and Does Not Do
- **Re-reads**: a repeated read of unchanged bytes with the same focus makes no
  API call. It is a local SQLite query, not an instant one — and the *first*
  read still costs whatever it costs.
- **Vision**: an identical (image, question, model) triple is served from SQLite
  with no GPU work. A different question about the same image is not.
- **Re-fetch**: a cached URL skips the network. The user is still asked, and can
  still choose `fresh`.
- **What it cannot do**: notice that a cached summary is inadequate for the
  question being asked (§5.2.2), or that a file's *meaning* changed while its
  bytes did not (an edited import elsewhere, say).

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

### 14.2 URL Fetch Engines

`FetchUrl` remains a client-side tool, since the server-side `open_page` is only
reachable through the model's own reasoning.

> Earlier revisions of this document listed a provider chain of third-party
> services (`web4agent`, `webpeel`, `crw`). **No such integration exists and none
> ever did in this codebase** — `web_fetch.py` makes the request itself. The list
> is removed rather than corrected in place.

What is actually implemented is an engine ladder, chosen by what is installed
(`web_fetch.engine_name()`), plus HTML-to-text extraction:

| Engine | Headers | HTTP/2 | TLS fingerprint |
| :--- | :--- | :--- | :--- |
| `curl_cffi` | Chrome order | yes | Chrome's own handshake, via impersonation |
| `httpx` + `h2` | Chrome order | yes | generic Python |
| `httpx` alone | Chrome order | no | generic Python |

Beyond the engine: per-host cookie persistence (`.g023/cookies.json`), per-host
request pacing, and header ordering matched to the profile being impersonated.
`/fetch status` reports which rung you are on.

**What is not claimed.** That any of this defeats a particular bot defence.
`fidelity_report()` describes what the installed engine *impersonates*; it is not
a measurement of your traffic. To measure it, fetch `https://tls.peet.ws/api/all`
and read your own fingerprint back. Anti-bot systems weigh IP reputation,
behaviour and history alongside TLS, and they change; a matching handshake
removes one tell and guarantees nothing.

There is also **no JavaScript engine**. A page that builds its body client-side
returns the shell.

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
- **Phase 9** (done, 1.2): symbol → line-range map (§5.2.1), raw-read baseline
  flag (§5.2.3), drift signals and `/signals` (§XIX), first test suite (§XX),
  and this honesty pass over the documentation.
- **Phase 10** (open): an actual A/B measurement using §5.2.3; a summary-
  sufficiency signal, if one can be designed that is not just another guess.

---

## XVI. Metrics: What Is Asserted, Targeted, and Unmeasured

Earlier revisions listed these as "KPIs" with targets and the word *guarantees*.
Nothing was measuring them. Split by what is actually known:

**Asserted by tests** (`python3 -m pytest tests/`):

| Property | Where |
| :--- | :--- |
| Tool-call pairing survives rollback, repair, and error recovery | `test_history_integrity.py` |
| Output items are echoed back byte-identical, unknown types included | `test_history_integrity.py` |
| The per-operation API-call counts in §5.2 | `test_call_accounting.py` |
| Symbol ranges are decorator-aware, in-bounds, and round-trip to real source | `test_file_reader.py` |
| Truncated summaries and truncated ranges declare themselves | `test_file_reader.py` |
| Cost arithmetic, both usage spellings, worst-case when the split is unreported | `test_usage_accounting.py` |
| Each drift signal fires on the shape that matters and is quiet otherwise | `test_drift_signals.py` |

**Observed live, not guaranteed**: prefix-cache hit rate (status bar, `/cost`,
`/signals`), file-summary cache hit rate (`/cache stats`), DeepSeek API spend
(`/cost`, priced locally per §3.2).

**Design intent, unmeasured**: that the orchestrator's context stays mostly
reasoning rather than data; that subagent spawn latency is imperceptible; that
delegation lowers end-to-end session cost (§XVIII).

**Structurally true**: vision costs $0.00 in DeepSeek tokens, because it never
calls DeepSeek. `SearchContent` costs $0.00 in tokens, because it never calls
the API at all.

---

## XVII. Where the Savings Actually Come From

| Layer | Mechanism | What is true |
| :--- | :--- | :--- |
| **Subagent isolation** | Summary enters context instead of the file | The context increment is bounded and small; the ratio depends on the file, so no single percentage applies |
| **File cache** | Keyed on content hash + focus | A repeat read of unchanged bytes makes no API call; the first read is unchanged |
| **Local vision** | Ollama instead of a vision API model | Zero DeepSeek tokens; the cost moves to your GPU |
| **Local search** | `Searcher` is pure Python | Zero tokens, always |
| **Search metadata** | Capped JSON matches instead of raw dumps | Bounded output regardless of match count, and it says when the cap truncated |
| **Prefix caching** | Static instructions/tools + verbatim echo | Tokens that hit are billed at 1/50th; the harness affects *how many* hit, not the ratio |
| **Native web search** | Server-side, inside the turn | No client fetch/parse; the search's own tokens are still billed by DeepSeek |

The honest summary: g023 Code decouples data retrieval from high-level reasoning
and keeps the orchestrator's window small and interpretable. Whether that also
makes it cheaper end-to-end than a naive loop is a claim this project has set up
the means to test (§5.2.3) and has not yet tested.

---

## XVIII. The Delegation Trade

Delegating a read is a second API call. It is worth stating when that pays and
when it does not, rather than asserting that it always does.

**The arithmetic** (illustrative — the token figures are approximations, and the
conclusion is a conditional, not a result):

A 40 KB source file is roughly 10k tokens.

- *Inlined*: 10k tokens enter the orchestrator's context and are re-sent every
  subsequent turn. If they stay inside a surviving cached prefix, each resend is
  ~$0.000028. If a compaction or an edit breaks the prefix, the same tokens cost
  ~$0.0014 to resend.
- *Delegated*: one Flash call (~10k in at miss rates, ~300 out) ≈ $0.0023, and
  ~300 tokens enter context instead of 10k.

So delegation costs more up front and less thereafter, and the crossover depends
on turn count and prefix survival — both of which are properties of a session,
not of the harness. **Delegation is not unconditionally cheaper. It is
unconditionally smaller**, and a smaller window is what keeps the model on task,
keeps `/context` readable, and delays compaction.

**How to settle it for your own repo**: set `G023_READFILE_RAW=1` (§5.2.3), run
the same task script both ways, compare `/cost`. Publishing a number here
without having done that would be exactly the kind of claim this revision
removed.

---

## XIX. Drift Signals

The client does not validate responses, deliberately (§I): an unknown field must
reach the model without a client release. The cost of that openness is that a
**renamed** field fails silently — `output_text()` returns `""`, the model looks
like it said nothing, and nothing raises. Silent degradation, not a crash, is
this program's realistic worst case.

`g023_code/signals.py` records the three cheapest observations that would move
first. `/signals` (alias `/drift`) displays them.

| Signal | Implementation | Fires on | Cannot tell you |
| :--- | :--- | :--- | :--- |
| Unknown item `type` | `api.unknown_item_types()` vs `KNOWN_ITEM_TYPES` | An item type this client does not read — the earliest visible sign of an additive API change | Whether it matters |
| Unexplained empty output | `api.silent_degradation()` | Assistant `message` items with no readable `output_text` and no `incomplete_reason` | Whether the model simply said nothing |
| Hit-rate drift | `cache.record_prefix_stats()` per local day + `signals.hit_rate_verdict()` | The newest day falling ≥ 15 points below the trailing baseline | Whether the cause is your prompts, the tool list, or the server |

Design decisions worth stating:

- **Persistent, because the interesting comparison is across days.** The hit rate
  was already logged per turn; a per-turn number has no baseline to be judged
  against. `prefix_stats` is keyed `(day, model)` and survives `/cache clear`.
- **It refuses to call one day a baseline.** With fewer than
  `MIN_BASELINE_DAYS = 2` prior days it says so instead of reporting a
  comparison that means nothing.
- **The threshold is 15 points**, not 1. Below that, ordinary session-to-session
  variation — one long session versus one short, a compaction versus none —
  dominates, and a signal that fires on normal turns gets ignored.
- **Recording can never cost a turn.** `Orchestrator._record_signals` is wrapped
  in a bare `except`. Observability that can break the thing it observes is worse
  than no observability.
- **None of this is a diagnosis.** A model behaviour change, a schema change, and
  drift in this project's own prompts all present identically from here: same
  call, worse output, no error. These signals make the change visible and date
  it. Separating the causes still requires a person.

---

## XX. Tests

```bash
pip install pytest && python3 -m pytest tests/ -q
```

No plugins: `tests/conftest.py` runs coroutine tests through a `pytest_pyfunc_
call` hook, because a suite that needs its own install story tends not to get
run. An autouse fixture points `G023_PROJECT_ROOT` and `G023_HOME` at a
temporary directory and resets the cache and signal singletons, so a run never
reads a developer's real `.g023/cache.db` and never reaches the network — API
behaviour is exercised through a stub client that counts calls.

| File | Holds to account |
| :--- | :--- |
| `test_call_accounting.py` | The per-operation API-call table (§5.2) |
| `test_file_reader.py` | Symbol ranges, range clamping and truncation, partial-summary disclosure, local AST facts outranking the model's |
| `test_drift_signals.py` | Each signal's true positives *and* its true negatives; hit-rate history surviving a restart and a `/cache clear` |
| `test_history_integrity.py` | Pairing repair, rollback, byte-identical echo |
| `test_usage_accounting.py` | Pricing arithmetic, both usage spellings, the command/handler contract |

**Not covered**: anything requiring the live API, end-to-end session cost, vision
against a real daemon, `FetchUrl` against a real server, and — the significant
one — whether a summary was good enough for the question that prompted it
(§5.2.2).

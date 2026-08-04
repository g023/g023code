# g023 Code — The Complete Exhaustive Technical Specification & Architecture Guide

## A Pure Python Powered by DeepSeek V4

**Version**: 3.1 (Final Consolidated Specification – g023 Edition)
**Compiled**: August 2026 · implementation 1.1
**Core Principle**: *Context is Currency. Subagents are the Treasury.*

---

## I. Executive Overview & Foundational Philosophy

**g023 Code** is a terminal-native, pure-Python AI programming assistant designed to replicate and surpass the capabilities of current agentic CLI apps while leveraging the cost-efficiency of DeepSeek V4 Flash and the native vision capabilities of DeepSeek V4 Pro. 

Unlike standard AI agents that naively dump raw search results, full file contents, and high-resolution images into a single massive context window (polluting the orchestrator's 1M-token space and destroying prefix-cache economics), g023 Code employs a strict **Subagent-First Delegation Architecture**. 

**The Core Philosophy**: 
- The **Orchestrator (Flash)** is reserved exclusively for high-level reasoning, tool orchestration, and maintaining conversational flow. 
- **Subagents (Flash/Pro)** handle all data-heavy tasks (file reading, searching, vision) in isolated, minimal contexts, returning only compact, metadata-rich summaries.
- **Aggressive Caching (SQLite + Prefix)** ensures that repeated operations (reading the same file, analyzing the same image) cost effectively **$0.00** in API tokens.

---

## II. Core Infrastructure: The Agentic Loop

### 2.1 The Deterministic Engine
The system operates on a synchronous `while True` loop. Approximately **98% of the codebase is deterministic infrastructure** (permissions, routing, context management), with only ~2% being AI decision logic. This makes g023 Code primarily an engineering project.

```python
async def agentic_loop(user_prompt: str):
    state = OrchestratorState()
    state.messages.append({"role": "user", "content": user_prompt})
    
    while True:
        # 1. Build Context (System/Tools are Static -> 100% Cache Hit)
        context = await context_manager.build_context(state)
        
        # 2. Call DeepSeek V4 Flash
        response = await deepseek_flash.chat.completions.create(
            model="deepseek-v4-flash",
            messages=context.messages,
            tools=router.get_tool_schemas(),  # Thin proxy tools
        )
        
        # 3. Process Response & Route
        msg = response.choices[0].message
        state.messages.append(msg)
        
        if msg.tool_calls:
            for tool_call in msg.tool_calls:
                # Permission Check
                if not await permission_manager.check(tool_call):
                    continue
                
                # THE CRITICAL ROUTE: Intercept heavy tasks
                if tool_call.function.name in ["ReadFile", "SearchContent", "AnalyzeImage"]:
                    # Delegate to Subagent -> Receive Summary
                    summary = await subagent_router.delegate(tool_call)
                    state.messages.append({"role": "tool", "content": summary})
                else:
                    # Execute lightweight tools directly (Bash, AskUser)
                    result = await tool_registry.execute(tool_call)
                    state.messages.append({"role": "tool", "content": result})
            continue  # Loop back with results
        
        # 4. Goal Check / Termination
        if goal_manager.is_satisfied(state):
            return msg.content
```

### 2.2 State Management
The `OrchestratorState` strictly holds:
- System Prompt (Static)
- Tool Schemas (Static)
- Compacted File Summaries (from Subagents)
- Search Metadata (File + Line Number)
- Conversation Turns
- **Explicitly excludes**: Raw file contents, Full image base64, Subagent internal logs.

---

## III. Model Strategy & Economic Routing

### 3.1 Model Specifications
| Attribute | DeepSeek V4 Flash | DeepSeek V4 Pro |
| :--- | :--- | :--- |
| **Total Parameters** | 284B (MoE) | 1.6T (MoE) |
| **Active Parameters** | ~13B | ~49B |
| **Context** | 1M Tokens | 1M Tokens |
| **Concurrency** | 2,500 req/s | 500 req/s |
| **Vision Support** | ❌ Text-Only | ✅ Native Vision |
| **Primary Role** | Orchestrator, Subagents (Read/Search) | Vision Subagent ONLY |

### 3.2 Pricing & Cache Economics
| Scenario | Flash Price | Pro Price | Savings Factor |
| :--- | :--- | :--- | :--- |
| **Input (Cache Hit)** | $0.0028 / 1M | $0.003625 / 1M | Flash is 1.3x cheaper |
| **Input (Cache Miss)** | $0.14 / 1M | $0.435 / 1M | Flash is 3.1x cheaper |
| **Output** | $0.28 / 1M | $0.87 / 1M | Flash is 3.1x cheaper |
| **Cache Hit vs Miss (Flash)** | 50x cheaper | — | **Crucial for profitability** |

### 3.3 Routing Rules
- **Orchestrator**: `deepseek-v4-flash` (Matches Pro on Agent tasks per 0731 release).
- **Explore/Plan/FileReader/Searcher**: `deepseek-v4-flash` (Cost-effective for isolated tasks).
- **Vision Subagent**: `deepseek-v4-pro` (Only model capable of parsing image URLs).

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

Subagents are isolated AI instances. They are the **only** entities allowed to read raw files, run heavy greps, or process images. They always return compact summaries to the Orchestrator.

### 5.1 Subagent Registry & Auto-Spawning
The Orchestrator intercepts specific intents via a Router.

| User/Orchestrator Intent | Subagent Spawned | Isolated Context Payload | Tool Access |
| :--- | :--- | :--- | :--- |
| "Read src/auth.py" | `FileReader` | (System prompt) + (File path) | `Read`, `Glob` |
| "Find `validate_token`" | `Searcher` | (System prompt) + (Regex) | `Grep`, `Glob` |
| "Analyze screenshot.png" | `Vision` | (System prompt) + (Low-res image) | `request_tile_detail` |
| "Explore DB connections" | `Explore` | (System prompt) + (Objective) | `Read`, `Grep`, `Glob` (Read-only) |

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

### 5.3 The Searcher Subagent (Metadata-First Extraction)
Standard `grep` returns hundreds of lines of raw code. g023 Code's Searcher transforms this.

**System Prompt Rules**:
> 1. Limit to `max_matches=10`.
> 2. For each match, return: `{ "file": "...", "line": 42, "match": "...", "context_before": "...", "context_after": "..." }`.
> 3. Aggregate: Provide a `metadata_summary` (e.g., "Found 3 occurrences in auth.py").

**Orchestrator Impact**: The orchestrator receives a JSON blob of ~150 tokens instead of a 5,000-token text dump. The line numbers allow the orchestrator to request exact snippets later via FileReader if needed.

### 5.4 Subagent Context Isolation Metrics
| Task | Orchestrator Direct Cost | Subagent Isolated Cost | Savings |
| :--- | :--- | :--- | :--- |
| Reading a 500-line file | 100k tokens (context) + 2k raw | ~200 tokens (summary) | **99.8%** token reduction |
| Searching whole repo | 100k tokens + 10k results | ~150 tokens (JSON metadata) | **99.5%** reduction |
| Vision (Full HD) | 100k + 2.5k image | ~650 tokens (tiles) | **99.3%** reduction |

---

## VI. Advanced Vision System (Tile-Based Selective Zoom)

Sending full high-resolution images to the Vision Subagent (DeepSeek Pro) costs tokens proportional to image size. To minimize cost while maximizing accuracy, g023 Code implements a **Two-Pass Tile strategy**.

### 6.1 The Tiling Mechanism
1. **Low-Res Pass**: The Vision Subagent receives a downscaled version (512x512) of the image. This costs ~50 tokens.
2. **Virtual Grid**: The subagent is told to imagine the original image divided into a dynamic grid. Default is `4x4` (16 tiles), adjustable via `/vision_tiles 8x8`.
3. **Selective Zoom Tool**: If the low-res version is insufficient, the Subagent calls `request_tile_detail(x, y)`.
4. **High-Res Crop**: The Router intercepts this, loads the *original* image from disk, crops the specific tile at **full original resolution**, and returns it to the Subagent via a `tool_result`.
5. **Iterative Analysis**: The Subagent merges the low-res context with the high-res tile details. It caps tile requests at 5 to prevent abuse.

### 6.2 Decision Heuristics (The System Prompt Logic)
The Vision Subagent contains explicit reasoning rules to decide when to zoom:
- **Skip Zoom**: If the low-res image clearly shows a large "Success" banner or simple UI layout.
- **Zoom for Text**: If the user asks "What error does it show?" and text is blurry -> Zoom the text block.
- **Zoom for Code**: If the image contains a code block -> Zoom the center and bottom of the block to transcribe accurately.
- **No Zoom for Color/Aesthetics**: Low-res is sufficient for colors or object recognition.

### 6.3 Economic Impact of Tiling
| Approach | Image Tokens | Context Pollution | Cost (Pro Input) |
| :--- | :--- | :--- | :--- |
| Direct Orchestrator (Full 4K) | 2,500 + 100k ctx | High | $0.435 (100k miss) |
| Vision Subagent (Full 4K) | 2,500 | Isolated | $0.010 (2.5k) |
| **Vision Subagent (Low + 2 Tiles)** | **~650** (50 + 300*2) | **Isolated** | **$0.0028** |
| **Savings vs Direct** | — | — | **~155x cheaper** |

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

### 8.1 Tool: `search_content`
| Parameter | Description |
| :--- | :--- |
| `query` | Text or regex pattern |
| `path` | Target directory/file |
| `max_matches` | Default: 10 |

### 8.2 Output Transformation
Instead of raw lines, the Searcher returns a JSON object:
```json
{
  "results": [
    {
      "file": "src/auth.py",
      "line": 42,
      "match": "def validate_token(token):",
      "context_before": ["def authenticate(user):"],
      "context_after": ["    return decode_jwt(token)"],
      "relevance_score": 0.95
    }
  ],
  "metadata_summary": "Found 'validate_token' in auth.py. Related function 'authenticate' found nearby."
}
```
**Orchestrator Benefit**: It receives ~100 tokens of structured data instead of 5,000 tokens of raw code. It knows exactly which file and line to target if deeper context is needed later.

---

## IX. Tool System (Orchestrator Proxy vs Subagent Implementation)

The Orchestrator exposes a thin set of tools. Heavy lifting is delegated.

### 9.1 Orchestrator Tool List
| Tool Name | Function | Target | Permission Default |
| :--- | :--- | :--- | :--- |
| `ReadFile` | Read a file with automatic compaction; `start_line`/`end_line` return that range verbatim instead. | `FileReader` | `allow` |
| `SearchContent` | Grep/Glob with metadata extraction. | `Searcher` | `allow` |
| `AnalyzeImage` | Analyze an image (local/url). | `Vision` (Tile) | `ask` |
| `Agent` | Spawn Explore/Plan. | `Explore` / `Plan` | `ask` |
| `Bash` | Execute shell command. | Internal | `ask` |
| `BashOutput` | Get background command output. | Internal | `ask` |
| `KillShell` | Terminate running command. | Internal | `ask` |
| `Git` | Git operations. | Internal | `ask` |
| `WebSearch` | Search the web. | Internal (Free APIs) | `allow` |
| `WebFetch` | Fetch URL to Markdown. | Internal (Free APIs) | `allow` |

### 9.2 Permission Levels
- `allow`: Auto-execute (Reads, Searches).
- `ask`: Requires user confirmation (Vision, Bash, Write).
- `block`: Hard disabled (Delete by default).

---

## X. Context Management & Compaction Strategy

### 10.1 Three-Layer Compaction
1. **MicroCompact (Layer 1)**: Local, deterministic. Clears old tool outputs (`Bash`, `Read`, `Grep`) to `[Old tool result content cleared]`. No API cost.
2. **Session Memory Compact (Layer 2)**: If `tengu_session_memory` is enabled, uses pre-extracted facts as summary. No API cost.
3. **API Summary (Layer 3)**: Manual `/compact` or fallback. Spawns a `FileReader`-like subagent to summarize conversation history using `deepseek-v4-flash`. Cost is minimal due to isolated context.

### 10.2 Sliding Window
When context reaches 85% of the 1M limit (850k tokens):
- The engine aggressively drops the oldest non-system messages.
- Because the system prompt and tool definitions are static and at the *start* of the prompt, dropping messages at the *end* does not invalidate the prefix cache.

### 10.3 Prefix Cache Preservation
- The System Prompt and Tool Schemas are **immutable**.
- Subagent summaries are injected in the **middle** of the prompt.
- When the sliding window cleans the middle, the **start** of the prompt remains identical to the previous request. The cache engine hits immediately for the next turn, saving up to 50x on input costs.

---

## XI. The `.g023/` Operational Folder (Scratch & Memory)

The harness creates a hidden operational root at the project base to store all volatile and persistent state. **This folder is strictly excluded from all user-level searches** to prevent the AI from reading its own metadata.

### 11.1 Folder Structure
```
<project_root>/
├── .g023/                         # Auto-created on launch
│   ├── scratch/                    # Volatile Temp (Purged on /clear)
│   │   ├── subagent_12345/         # Per-subagent workspace
│   │   └── extracted_texts/        # Temp text dumps
│   ├── memory/                     # Persistent Long-term Memory
│   │   ├── facts.json              # Structured knowledge (e.g., "Framework: Django")
│   │   └── session_history.db      # Embeddings for recall
│   ├── cache/                      # SQLite Performance Cache
│   │   ├── file_cache.db           # SHA256 -> Compact Summary
│   │   ├── vision_cache.db         # Image Hash -> Tile Responses
│   │   └── search_cache.db         # Regex -> Metadata
│   ├── configs/                    # Project Overrides
│   │   ├── permissions.yaml        # Local allow/ask/block rules
│   │   └── g023.toml              # Settings (model, grid size)
│   └── logs/                       # Debugging
│       ├── orchestrator.log
│       └── cost_metrics.json
├── G023.md                         # Auto-loaded Project Memory
└── .g023ignore                     # User-defined exclusions (optional)
```

### 11.2 Strict Exclusion Strategy
1. **Hardcoded Global Ignore**: All tools (`Grep`, `Glob`, `ListDir`) use a default ignore list that includes `.g023/`, `.git/`, `__pycache__/`, etc.
2. **Pathspec Filtering**: Uses `.gitignore`-style `pathspec` rules to skip matches.
3. **Locked Exclusion**: Even if a user adds `!.g023/` to `.g023ignore`, the Harness **hard-blocks** it to prevent the AI from consuming cache dumps.
4. **Subagent Inheritance**: All Subagents inherit the root exclusion list. They never search within `.g023/`.
5. **Explicit Override**: A user can view logs via `/read /full/path/.g023/logs/...` only if `--debug` mode is enabled. The orchestrator does not automatically search it.

### 11.3 Startup Behavior
1. On launch, check for `.g023/`.
2. If missing, create it and initialize SQLite databases.
3. Populate the global ignore list *before* tool initialization.
4. Load `G023.md` (project memory) into the static System Prompt.

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
in advance — `/model` and `/model pro` are both first-class.

### 12.2 Implemented Commands

| Command | Function | Impact on Context/Cache |
| :--- | :--- | :--- |
| `/help [cmd]`, `/?` | List commands, or explain one in full. | Local. |
| `/status` | Dashboard: model, session, vision, ollama, cache. | Local. |
| `/clear` | Start fresh. | Clears orchestrator state and counters. |
| `/exit` | Quit. | — |
| `/model [flash\|pro]` | Switch orchestrator model. Picker shows both prices. | Local config. |
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

`/btw` (isolated side question) and `/vision_tiles` (tile-grid control, §VI)
remain specified but unimplemented. Vision currently sends one downscaled image
rather than a tile grid.

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

### 13.1 File Cache Schema
```sql
CREATE TABLE file_cache (
    file_hash TEXT PRIMARY KEY,
    file_path TEXT,
    summary TEXT,
    raw_content TEXT, -- Optional, purged after 7 days
    created_at TIMESTAMP
);
```

### 13.2 Vision Cache Schema
```sql
CREATE TABLE vision_cache (
    image_hash TEXT,
    question_hash TEXT,
    grid_config TEXT, -- e.g., "4x4"
    response TEXT,
    created_at TIMESTAMP,
    PRIMARY KEY (image_hash, question_hash, grid_config)
);
```

### 13.3 Benefits
- **Zero-cost re-reads**: File summaries are retrieved in <10ms.
- **Zero-cost vision**: Repeated image analysis returns instantly.

---

## XIV. Web & URL Tools (Free Providers)

Since DeepSeek does not natively browse, g023 Code integrates external free services with automatic fallback.

### 14.1 Search Providers (Fallback Chain)
1. **Tavily**: Unlimited (No API Key required).
2. **You.com**: 100/day (No API Key).
3. **OpenSERP**: Self-hosted (Requires local setup).

### 14.2 URL Fetch Providers (Fallback Chain)
1. **web4agent**: Self-hosted (Python library).
2. **webpeel**: 500/week (Free tier).
3. **crw (fastCRW)**: 500 credits (Free tier).
4. **Direct fallback**: `BeautifulSoup` parsing of raw HTML.

---

## XV. Development Roadmap

- **Phase 1**: Core Engine + Flash API + Basic CLI.
- **Phase 2**: Tool System (Permissions) + Bash/File ops.
- **Phase 3**: Subagent System (FileReader, Searcher, Explore, Plan) + SQLite cache.
- **Phase 4**: Vision Tile Subagent + `/vision_tiles` + Pro integration.
- **Phase 5**: Goal System (`/goal`) + Compaction (`/compact`).
- **Phase 6**: BTW + Web Tools + Cache management.
- **Phase 7**: Testing, Benchmarking, Beta Release.

---

## XVI. Success Metrics & KPIs

| Metric | Target | Why it matters |
| :--- | :--- | :--- |
| **Orchestrator Context Pollution** | < 5% raw data. | Ensures 95% context is pure reasoning. |
| **Prefix Cache Hit Rate** | ≥ 95% (System/Tools). | Guarantees 50x input cost savings. |
| **File Summary Hit Rate** | ≥ 90% (SQLite). | Eliminates redundant file reading API costs. |
| **Vision Tile Efficiency** | 70% resolve with ≤ 2 tiles. | Maximizes image token savings. |
| **Subagent Spawn Latency** | ≤ 500ms. | Ensures UX feels instant. |

---

## XVII. Summary of All Savings

| Optimization Layer | Mechanism | Token/Cost Reduction |
| :--- | :--- | :--- |
| **Subagent Isolation** | Orchestrator keeps 100k context; Subagent uses 200. | 99.8% context reduction. |
| **File One-Shot** | Read + Compact in one turn. Hash-based re-fetch. | 99% token reduction + $0.00 re-reads. |
| **Vision Tiling** | Low-res + 2 high-res tiles instead of full 4K. | 74% image token reduction. |
| **Search Metadata** | JSON snippets vs raw text dumps. | 95% search overhead reduction. |
| **Prefix Caching** | Static System/Tools. | 50x cheaper input tokens. |

g023 Code delivers a coding assistant that completely decouples data retrieval from high-level reasoning, preserving the LLM's precious context for what it does best: logic, planning, and conversation. The result is a system that is not only functionally equivalent to current agentic CLI, but is algorithmically superior in cost-efficiency and context hygiene.

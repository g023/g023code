"""
Tool schemas for the DeepSeek Responses API.

The Responses API declares a function tool *flat* — ``name``, ``description``
and ``parameters`` sit directly on the tool object. The chat-completions shape,
with everything nested under a ``"function"`` key, is rejected outright
("tools[0]: missing field `name`"), so this file is not interchangeable with an
OpenAI-style schema list.

Web search is absent from this list on purpose: it is not a function we
implement. DeepSeek runs it server-side, and the orchestrator offers it as
:data:`WEB_SEARCH_TOOL` alongside these.
"""

from __future__ import annotations

# DeepSeek's own server-side search tool. It needs no schema and no executor —
# the model calls it, the server runs the whole search loop (issuing queries and
# opening pages), and the results arrive already folded into the same turn.
WEB_SEARCH_TOOL = {"type": "web_search"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "name": "ReadFile",
        "description": (
            "Read a file and return a compact structural summary + metadata. "
            "The summary is NOT the file: it omits code bodies, so never rewrite "
            "a file from it. Set raw=true to get the FULL file verbatim (capped) "
            "when you need exact bytes — e.g. immediately before a WriteFile rewrite. "
            "Prefer Edit (old_string/new_string) for a unique span. "
            "Pass start_line/end_line to get a specific range verbatim instead of "
            "a summary. Prefer summaries and ranges over full dumps: context is expensive."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute or relative path to the file",
                },
                "raw": {
                    "type": "boolean",
                    "description": (
                        "Return the FULL file verbatim, capped at 200,000 chars. "
                        "Use only when exact content is required — e.g. before a "
                        "WriteFile edit. False (default) returns a compact summary."
                    ),
                    "default": False,
                },
                "focus": {
                    "type": "string",
                    "description": (
                        "Optional focus. An exact symbol name (or unique method "
                        "suffix, e.g. 'User' or 'login') is resolved from the "
                        "local index with no API call and returns the line span. "
                        "A semantic sentence ('auth logic', 'what does login do') "
                        "still goes to the model."
                    ),
                },
                "start_line": {
                    "type": "integer",
                    "description": (
                        "First line of an exact range to return verbatim (1-based, inclusive). "
                        "When set, the file is returned as source, not as a summary."
                    ),
                },
                "end_line": {
                    "type": "integer",
                    "description": "Last line of the range (1-based, inclusive). Defaults to end of file.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "SearchContent",
        "description": (
            "Search the codebase with grep/glob semantics. "
            "Returns metadata-first JSON (file, line, match, short context) — never full file dumps."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text or regex pattern to search for",
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file to search in (default: project root)",
                },
                "max_matches": {
                    "type": "integer",
                    "description": "Maximum number of matches to return (default 12)",
                    "default": 12,
                },
                "file_glob": {
                    "type": "string",
                    "description": "Optional glob filter e.g. '*.py' or '**/*.{ts,js}'",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "AnalyzeImage",
        "description": (
            "Analyze an image (local path or URL) using an external vision model. "
            "Returns a concise textual description answering the question."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path_or_url": {
                    "type": "string",
                    "description": "Local file path or http(s) URL to the image",
                },
                "question": {
                    "type": "string",
                    "description": "What to look for / question about the image",
                    "default": "Describe the image in detail focusing on any code, UI, or technical content.",
                },
            },
            "required": ["path_or_url"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "Bash",
        "description": (
            "Execute a shell command in the project root. "
            "Prefer non-destructive commands. Requires confirmation for potentially dangerous ops."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 60)",
                    "default": 60,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "WriteFile",
        "description": (
            "OVERWRITES THE ENTIRE FILE with content. Never reconstruct a file "
            "from a ReadFile summary or from memory — summaries omit code, and a "
            "summary-based rewrite truncates the file. First ReadFile with "
            "raw=true (or read every range you will touch) so you hold the complete "
            "current content. The previous version is automatically backed up to "
            ".g023/backups/ before the write (newest few kept per file). "
            "Requires confirmation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "Full new content of the file"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "Edit",
        "description": (
            "Replace a unique span in an existing file. old_string must match "
            "exactly one region unless replace_all is true. Fails closed (no write) "
            "if the span is missing or not unique. Prefer this over WriteFile for "
            "small edits. The previous version is backed up to .g023/backups/. "
            "Requires confirmation. WriteFile remains the tool for create and "
            "full-file rewrite."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "old_string": {
                    "type": "string",
                    "description": "Exact text to find. Must be unique in the file unless replace_all is true.",
                },
                "new_string": {
                    "type": "string",
                    "description": "Replacement text",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace every occurrence. Default false (unique-match only).",
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "ListDir",
        "description": "List directory contents with basic metadata (name, type, size).",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path (default project root)",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "If true, show a shallow tree (max depth 2)",
                    "default": False,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "Glob",
        "description": (
            "Find files by glob pattern. Supports ** and brace expansion "
            "(e.g. '*.{ts,js}'). Returns paths relative to the project root. "
            "Does not use ListDir's depth-2, 80-entry listing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '**/*.py' or '*.{ts,js}'",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search from (default project root)",
                },
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "TodoWrite",
        "description": (
            "Update the session working set: open leaves (merge by id), optional "
            "live goal and constraints. Leaves have id, title, status "
            "(pending|in_progress|done|cancelled), evidence[]. At most 8 open "
            "leaves. Remaining work lives here; compact does not paraphrase it. "
            "status=done requires evidence[] citing a working-set file path+hash "
            "or a passing verify command."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "leaves": {
                    "type": "array",
                    "description": "Leaves to merge by id",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "title": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["pending", "in_progress", "done", "cancelled"],
                            },
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["id", "title"],
                    },
                },
                "goal": {
                    "type": "string",
                    "description": "Live objective string for the working set",
                },
                "constraints": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Explicit user constraints still in force",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "Agent",
        "description": (
            "Spawn a specialized subagent. research relays findings; planning relays a "
            "plan; todos writes a list, orchestrates task workers (each receives issues "
            "from the previous task), and relays a final summary. task implements one "
            "todo (used by todos). explore/plan are aliases for research/planning. "
            "Pass prior/issues so workers communicate both ways; the parent never "
            "ingests a nested transcript."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["research", "planning", "todos", "task", "explore", "plan"],
                    "description": (
                        "research: inspect and relay findings; "
                        "planning: produce a plan (optionally from prior research); "
                        "todos: make a todo list and orchestrate task workers; "
                        "task: implement one todo, receiving prior issues; "
                        "explore/plan alias research/planning."
                    ),
                },
                "objective": {
                    "type": "string",
                    "description": "Clear objective / question for the subagent",
                },
                "reasoning": {
                    "type": "string",
                    "enum": ["none", "low", "medium", "high"],
                    "description": (
                        "Optional reasoning effort for the subagent turn. "
                        "Clamped to what the backend model supports."
                    ),
                },
                "prior": {
                    "type": "string",
                    "description": (
                        "JSON payload from the previous worker in the pipeline "
                        "(research, plan, or issues)."
                    ),
                },
                "issues": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Issues surfaced by the previous task worker.",
                },
                "tasks": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Explicit todo titles for the todos worker. "
                        "If omitted, the worker writes its own list."
                    ),
                },
            },
            "required": ["kind", "objective"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "FetchUrl",
        "description": (
            "Fetch a web page and return its readable text (not raw HTML). "
            "Always asks the user for permission first, and when a cached copy "
            "exists the user chooses between the cache and a fresh fetch. "
            "Requests are made with a real browser's headers, protocol and cookies. "
            "No JavaScript is executed, so app-shell pages may return little text. "
            "To find a page in the first place, search the web instead — then fetch "
            "the specific URL you want in full."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The http(s) URL to fetch",
                },
                "cache_mode": {
                    "type": "string",
                    "enum": ["auto", "fresh", "cache"],
                    "description": (
                        "auto = use a cached copy younger than max_age, else fetch; "
                        "fresh = always hit the network; "
                        "cache = only use the cache, never the network. "
                        "The user can override this at the permission prompt."
                    ),
                    "default": "auto",
                },
                "max_age": {
                    "type": "integer",
                    "description": "In auto mode, the oldest acceptable cached copy, in seconds (default 3600)",
                    "default": 3600,
                },
                "extract": {
                    "type": "string",
                    "enum": ["text", "markdown", "links", "raw"],
                    "description": "How to render the page. Use 'raw' only when the exact markup matters.",
                    "default": "text",
                },
                "max_chars": {
                    "type": "integer",
                    "description": "Truncate the returned content to this many characters (default 20000)",
                    "default": 20000,
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "LearnSearch",
        "description": (
            "Search short routing lines for procedures learned in earlier sessions, "
            "including other projects. Returns titles and when-tags, not full guides. "
            "Use when a tool fails or the task involves a local browser, MCP, Ollama, "
            "or a workflow you have not used this session."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to match against guide ids, titles, and when-tags",
                },
                "scope": {
                    "type": "string",
                    "enum": ["all", "project", "env", "user"],
                    "description": "Which learn-store roots to search (default all)",
                    "default": "all",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "LearnGet",
        "description": (
            "Read one learn-store guide by id from LearnSearch. Max two per turn."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Guide id returned by LearnSearch",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "LearnPut",
        "description": (
            "Save a reusable procedure or pitfall for future sessions. Use after a "
            "failed-then-successful attempt, or when the user asks to remember a "
            "procedure. Not for project file contents, secrets, or permission changes. "
            "Requires evidence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Lowercase hyphenated id, 2-64 chars, equals the filename stem",
                },
                "kind": {
                    "type": "string",
                    "enum": ["procedure", "pitfall", "routing", "fact"],
                },
                "scope": {
                    "type": "string",
                    "enum": ["project", "env", "user"],
                    "description": "project = this repo; env = this machine; user = portable taste",
                },
                "title": {"type": "string"},
                "when": {
                    "type": "string",
                    "description": "Comma-separated tags, including tool names when relevant",
                },
                "not_when": {"type": "string"},
                "body": {
                    "type": "string",
                    "description": "When / Do / Do not / Why (observed). Max 80 lines and 4000 chars.",
                },
                "evidence": {
                    "type": "string",
                    "description": "Local tool success or user:explicit. Not a URL.",
                },
                "requires": {
                    "type": "array",
                    "description": "Optional local probe commands; search skips the guide if any fail",
                    "items": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "string"},
                            "expect_exit": {"type": "integer", "default": 0},
                        },
                    },
                },
            },
            "required": ["id", "kind", "scope", "title", "when", "body", "evidence"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "SessionSearch",
        "description": (
            "Grep this project's .g023/sessions/*.jsonl (the full event log, including "
            "text dropped from the current view by compact). Returns excerpts, kinds, "
            "and hashes for omitted write bodies — not reconstructed file contents."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to match against session log excerpts",
                },
                "max_hits": {
                    "type": "integer",
                    "description": "Maximum hits to return (default 8)",
                    "default": 8,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]

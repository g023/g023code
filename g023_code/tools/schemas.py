"""
Tool schemas, in the flat internal shape. ``anthropic_codec.tools_to_anthropic``
converts them for the wire (``parameters`` -> ``input_schema``). A tool is
declared *flat* — ``name``, ``description`` and ``parameters`` sit directly on the tool object.

Web access is two client tools on DeepSeek's Search and Fetch APIs
(``WebSearch``, ``FetchUrl``), both served through the machine-wide cache in
``g023_code/web.py``. The server-side ``web_search`` tool is no longer offered:
its results are opaque and cannot be cached or compressed, and it reads the prefix twice per call.
"""

from __future__ import annotations

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
            "a summary. Prefer summaries and ranges over full dumps: context is expensive. "
            "Result: a one-line JSON header (path, 12-hex hash, lines), then an outline "
            "`signature  Lstart-end  # doc` per class/method/function; verbatim source, "
            "when returned, follows a `-----` line exactly as on disk. Re-read an "
            "outline span with start_line/end_line for its source. "
            "Data and binary files get a digest from the unranged read: CSV/TSV "
            "columns with exact row counts, JSON/JSONL shape with array lengths, "
            "log line templates with exact counts, and images/archives/SQLite/"
            "executables decoded. Use it for counts instead of line arithmetic."
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
                        "Optional focus, resolved locally with no API call. An exact "
                        "symbol ('do_goto', 'User.login') returns its body. A quoted or "
                        "multi-word phrase returns every exact hit with its section path "
                        "plus the passage. A section path from the outline ('Hamlet act 3 "
                        "scene 1', 'Chapter 12') returns that section. Anything else is "
                        "ranked by word match; each match carries its section."
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
            "Search the codebase with grep/glob semantics. Result: a JSON header "
            "(total, shown; shown=\"all\" means every hit is listed), then grep-style blocks: `## file`, `N: hit`, `N- context`. "
            "Samples are ranked definitions-first and spread across files; "
            "`more files (hits)` lists matching files that got no sample. "
            "Also searches .g023/memory/ (project notebook) and .g023/bbs/ (bulletin board)."
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
            "Analyze an image (project-relative path or http(s) URL; jpeg, png, gif or webp) "
            "in an isolated vision call. Returns a concise text answer to the question."
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
            "Runs under bash with pipefail when bash is installed, so a failing "
            "stage in a pipeline fails the command. "
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
            ".g023/backups/ before the write (newest few kept per file). An existing "
            "file's CRLF line endings, BOM and mode are kept; the write is atomic. "
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
            "Replace a unique span in an existing file (prefer over WriteFile for small edits). "
            "Matching: exact, then tolerant of line endings, trailing spaces and a consistent "
            "indentation difference (the file's indentation is kept). Line endings, BOM and file "
            "mode are preserved. Fails closed (no write): if old_string occurs more than once "
            "the error lists each occurrence (line + enclosing symbol); retry with line=N. If "
            "not found, the error includes the closest region verbatim. Success returns the "
            "changed line span and a syntax_error field if a .py/.json file stopped parsing. "
            "The previous version is backed up to .g023/backups/. Requires confirmation."
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
                    "description": "Replace every occurrence (exact matches only). Default false.",
                    "default": False,
                },
                "line": {
                    "type": "integer",
                    "description": "1-based line inside the intended occurrence when old_string is not unique.",
                },
            },
            "required": ["path", "old_string", "new_string"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "ListDir",
        "description": (
            "List directory contents with basic metadata (name, type, size). A directory "
            "too big for an exact list returns a digest instead: numbered names collapsed "
            "into templates with counts (frame_{0000..3999}.png x4000), sibling dirs with "
            "a shared layout as one row, and per-directory file counts, extension mix and size."
        ),
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
                    "enum": ["none", "low", "high"],
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
        "name": "WebSearch",
        "description": (
            "Search the web (DeepSeek Search API). Use it whenever the answer depends on current "
            "facts, or on a library or API you are not certain about. Returns ranked hits: title, "
            "url, site, date and a ~700-char abstract. Operators: site:, -site:, \"exact phrase\", "
            "-term, lang:en, after:2025, before:2026-06-01, OR between site: clauses. "
            "Results are cached machine-wide (time-sensitive queries 6 h, others 7 days), so "
            "repeating a query is free; order, case and close rewordings reuse it (the header "
            "note names the cached query). A hit marked 'page cached' can be read with FetchUrl at no cost."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "1-128 characters"},
                "count": {"type": "integer", "description": "Hits to show, 1-20 (default 5)", "default": 5},
                "fresh": {
                    "type": "boolean",
                    "description": "Bypass the cache (only when a cached answer is stale for the task)",
                    "default": False,
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "type": "function",
        "name": "FetchUrl",
        "description": (
            "Read a web page as Markdown (DeepSeek Fetch API; no JavaScript). Pages are cached "
            "machine-wide, and pages of WebSearch hits are usually cached already. A page up to "
            "8k chars comes back whole. A longer one returns a char window plus an outline of "
            "'@offset heading (size)'. Pass focus='topic' to get the best-matching sections "
            "instead, or offset=N to continue from a position. Result: one JSON header line "
            "(url, title, chars, source, shown, next_offset), then '-----' and the text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The http(s) URL"},
                "focus": {
                    "type": "string",
                    "description": "Return the sections that best match this topic (ranked locally, no extra cost)",
                },
                "offset": {"type": "integer", "description": "Start at this char offset (from the outline or next_offset)"},
                "max_chars": {"type": "integer", "description": "Window size, 500-40000 (default 8000)", "default": 8000},
                "cache_mode": {
                    "type": "string",
                    "enum": ["auto", "fresh", "cache"],
                    "description": "auto = cached copy if fresh for this kind of page; fresh = network; cache = never network",
                    "default": "auto",
                },
                "max_age": {
                    "type": "integer",
                    "description": "Optional: oldest acceptable cached copy in seconds (overrides the page-kind default)",
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
            "Use when a tool fails or the task involves a local browser, MCP, a local model server, "
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
    {
        "type": "function",
        "name": "LocalAsk",
        "description": (
            "Free reading on this machine's small local model (no paid tokens). Give a focused "
            "question and up to 8 project files (including .g023/vault/ files of earlier long outputs), "
            "or no paths to search the whole project (where is X handled / how does Y work). "
            "Returns a short lead plus verbatim cited lines (path:line: text) copied from the files. "
            "Verify the lead by its lines. Not for GPU-work tasks."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "What to find or extract"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Project-relative files to read; omit to search the whole project",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
]

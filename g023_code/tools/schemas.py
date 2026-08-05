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
            "NEVER returns raw full content unless raw=true is explicitly requested. "
            "Pass start_line/end_line to get the exact source of a known range "
            "verbatim instead of a summary — use that rather than shelling out to sed. "
            "Prefer this over dumping entire files into context."
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
                    "description": "If true, also include a truncated raw excerpt (use sparingly)",
                    "default": False,
                },
                "focus": {
                    "type": "string",
                    "description": "Optional focus hint (e.g. 'auth logic', 'class User')",
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
        "description": "Write or overwrite a file with the given content. Requires confirmation.",
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
        "name": "Agent",
        "description": (
            "Spawn a specialized subagent (Explore or Plan) for complex multi-step work. "
            "Use Explore for codebase understanding; Plan for multi-step implementation plans."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": ["explore", "plan"],
                    "description": "Type of subagent to spawn",
                },
                "objective": {
                    "type": "string",
                    "description": "Clear objective / question for the subagent",
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
]

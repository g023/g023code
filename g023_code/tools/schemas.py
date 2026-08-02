"""
OpenAI-compatible tool schemas for DeepSeek V4.
Strict mode ready (additionalProperties: false).
"""

from __future__ import annotations

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "ReadFile",
            "description": (
                "Read a file and return a compact structural summary + metadata. "
                "NEVER returns raw full content unless raw=true is explicitly requested. "
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
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
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
    },
    {
        "type": "function",
        "function": {
            "name": "WebSearch",
            "description": "Search the web for up-to-date information (uses free public APIs).",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "num_results": {
                        "type": "integer",
                        "description": "Number of results (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        },
    },
]

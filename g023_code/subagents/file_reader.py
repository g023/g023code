"""
FileReader Subagent
Reads a file, extracts structural metadata, produces a compact summary,
caches by content hash, and returns only the summary to the Orchestrator.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

from ..api import (
    ResponsesClient,
    get_client,
    incomplete_reason,
    output_text,
    reasoning_param,
)

from ..config import get_project_root, settings
from ..cache import get_cache
from ..usage import get_usage


FILE_READER_SYSTEM = """You are a FileReader Subagent. Your ONLY job is to produce a compact, high-signal summary of a source file.

Rules:
1. Extract structural metadata: top-level imports, classes, functions, constants, main logic.
2. If a focus hint is given, prioritise that section.
3. Output STRICT JSON with this schema:
{
  "metadata": {
    "language": "...",
    "imports": ["..."],
    "classes": [{"name": "...", "methods": ["..."]}],
    "functions": [{"name": "...", "signature": "..."}],
    "loc": 123
  },
  "summary": "2-4 sentence high-level description of what the file does and its key responsibilities.",
  "key_snippets": ["short relevant code fragments if useful, max 3"]
}
4. NEVER dump the entire file content.
5. Be precise and dense. The orchestrator will use this summary instead of the raw file.
"""


def _detect_language(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".go": "go",
        ".rs": "rust",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".md": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".sh": "bash",
        ".html": "html",
        ".css": "css",
    }.get(ext, "text")


def _local_python_metadata(content: str) -> dict:
    """Fast local AST extraction for Python files (no API needed)."""
    meta: dict[str, Any] = {
        "language": "python",
        "imports": [],
        "classes": [],
        "functions": [],
        "loc": content.count("\n") + 1,
    }
    try:
        tree = ast.parse(content)
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for n in node.names:
                        meta["imports"].append(n.name)
                else:
                    mod = node.module or ""
                    for n in node.names:
                        meta["imports"].append(f"{mod}.{n.name}" if mod else n.name)
            elif isinstance(node, ast.ClassDef):
                methods = [m.name for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))]
                meta["classes"].append({"name": node.name, "methods": methods})
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = [a.arg for a in node.args.args]
                meta["functions"].append({"name": node.name, "signature": f"{node.name}({', '.join(args)})"})
    except SyntaxError:
        pass
    return meta


def _slice_lines(content: str, start_line: Optional[int], end_line: Optional[int]) -> tuple[str, int, int]:
    """Return the requested 1-based inclusive line range, clamped to the file."""
    lines = content.splitlines()
    total = max(len(lines), 1)
    start = min(max(1, start_line or 1), total)
    end = min(max(end_line or total, start), total)
    return "\n".join(lines[start - 1 : end]), start, end


async def run_file_reader(
    path: str,
    focus: Optional[str] = None,
    raw: bool = False,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    client: Optional[ResponsesClient] = None,
) -> str:
    """
    Main entry for FileReader subagent.
    Returns a compact JSON string for the orchestrator.
    """
    root = get_project_root()
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    p = p.resolve()

    if not p.exists() or not p.is_file():
        return json.dumps({"error": f"File not found: {p}"})

    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return json.dumps({"error": f"Cannot read file: {e}"})

    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    cache = get_cache()

    # An explicit line range is a request for exact text, not for a summary:
    # answer it verbatim and never from the summary cache.
    if start_line is not None or end_line is not None:
        excerpt, start, end = _slice_lines(content, start_line, end_line)
        total = content.count("\n") + 1
        if len(excerpt) > 20_000:
            excerpt = excerpt[:20_000] + "\n…[range truncated at 20,000 chars]"
        return json.dumps(
            {
                "path": str(p),
                "hash": content_hash,
                "language": _detect_language(p),
                "start_line": start,
                "end_line": end,
                "total_lines": total,
                "content": excerpt,
            },
            ensure_ascii=False,
        )

    # Cache hit. A focused read is a different question about the same bytes, so
    # a generic cached summary would silently answer the wrong one — the key has
    # to carry the focus.
    cache_key = content_hash if not focus else hashlib.sha256(
        f"{content_hash}\x00{focus}".encode("utf-8")
    ).hexdigest()
    cached = cache.get_file_summary(str(p), content_hash=cache_key)
    if cached and not raw:
        return json.dumps(
            {
                "path": str(p),
                "hash": content_hash,
                "from_cache": True,
                "metadata": cached.get("metadata", {}),
                "summary": cached["summary"],
            },
            ensure_ascii=False,
        )

    # Local fast path for Python
    language = _detect_language(p)
    metadata = {}
    if language == "python":
        metadata = _local_python_metadata(content)

    # For small files or when we have good local metadata, we can skip LLM
    if language == "python" and len(content) < 12_000 and not focus:
        summary = (
            f"Python module with {len(metadata.get('classes', []))} classes "
            f"and {len(metadata.get('functions', []))} top-level functions. "
            f"Imports: {', '.join(metadata.get('imports', [])[:8])}."
        )
        result = {
            "path": str(p),
            "hash": content_hash,
            "from_cache": False,
            "metadata": metadata,
            "summary": summary,
        }
        if raw:
            result["raw_excerpt"] = content[:3000] + ("…" if len(content) > 3000 else "")
        cache.put_file_summary(str(p), summary, metadata, content, content_hash)
        return json.dumps(result, ensure_ascii=False)

    # LLM compaction for complex / focused cases
    if client is None:
        client = get_client()

    user_msg = f"File path: {p}\nLanguage: {language}\n"
    if focus:
        user_msg += f"Focus: {focus}\n"
    user_msg += f"\n--- FILE CONTENT (truncated if huge) ---\n{content[:18000]}"

    try:
        resp = await client.create(
            model=settings.subagent_model,
            instructions=FILE_READER_SYSTEM,
            input=[{"role": "user", "content": user_msg}],
            max_output_tokens=800,
            temperature=0.1,
            # Structural extraction needs no deliberation, and "effort: none" is
            # what actually switches thinking off on this endpoint — the old
            # chat-completions "thinking" toggle is accepted and ignored here.
            reasoning=reasoning_param(enabled=False),
        )
        get_usage().record(settings.subagent_model, resp.get("usage"), scope="subagent")
        text = output_text(resp)
        cut = incomplete_reason(resp)
        # Try to extract JSON
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            data = json.loads(m.group(0))
        else:
            # A summary cut off at the token limit arrives as unparseable text
            # (or nothing at all); the locally-derived metadata is still sound,
            # so keep it and say what happened rather than reporting an empty
            # summary as the file's contents.
            data = {"summary": text[:600], "metadata": metadata}
            if cut:
                data["summary"] = (
                    f"(summary truncated: {cut}) {text[:600]}".strip()
                    if text
                    else f"(no summary: model output stopped early — {cut})"
                )
    except Exception as e:
        data = {
            "summary": f"(LLM summary failed: {e}) Local fallback: {language} file, {content.count(chr(10))+1} lines.",
            "metadata": metadata,
        }

    result = {
        "path": str(p),
        "hash": content_hash,
        "from_cache": False,
        "metadata": data.get("metadata", metadata),
        "summary": data.get("summary", ""),
        "key_snippets": data.get("key_snippets", []),
    }
    if raw:
        result["raw_excerpt"] = content[:2500] + ("…" if len(content) > 2500 else "")

    cache.put_file_summary(
        str(p),
        result["summary"],
        result["metadata"],
        content if len(content) < 200_000 else None,
        cache_key,
    )
    return json.dumps(result, ensure_ascii=False)

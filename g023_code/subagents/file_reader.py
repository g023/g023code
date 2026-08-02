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

from openai import AsyncOpenAI

from ..config import get_project_root, load_api_key, settings
from ..cache import get_cache


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


async def run_file_reader(
    path: str,
    focus: Optional[str] = None,
    raw: bool = False,
    client: Optional[AsyncOpenAI] = None,
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

    # Cache hit
    cached = cache.get_file_summary(str(p), content_hash=content_hash)
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
        client = AsyncOpenAI(api_key=load_api_key(), base_url=settings.base_url)

    user_msg = f"File path: {p}\nLanguage: {language}\n"
    if focus:
        user_msg += f"Focus: {focus}\n"
    user_msg += f"\n--- FILE CONTENT (truncated if huge) ---\n{content[:18000]}"

    messages = [
        {"role": "system", "content": FILE_READER_SYSTEM},
        {"role": "user", "content": user_msg},
    ]

    try:
        resp = await client.chat.completions.create(
            model=settings.subagent_model,
            messages=messages,
            max_tokens=800,
            temperature=0.1,
            extra_body={"thinking": {"type": "disabled"}},  # no need for thinking on extraction
        )
        text = resp.choices[0].message.content or ""
        # Try to extract JSON
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            data = json.loads(m.group(0))
        else:
            data = {"summary": text[:600], "metadata": metadata}
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
        content_hash,
    )
    return json.dumps(result, ensure_ascii=False)

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


# A Python file under this size is indexed locally by the AST walk — no API
# call, no model opinion. Above it, or with a focus hint, the model is asked.
LOCAL_SUMMARY_MAX_CHARS = 12_000
# How much of a file is sent to the summariser. Anything beyond this is not
# summarised, and the result says so rather than implying whole-file coverage.
LLM_INPUT_MAX_CHARS = 18_000
# Hard ceiling on a verbatim range read, so an unbounded range cannot flood the
# orchestrator's context.
RANGE_MAX_CHARS = 20_000

SYMBOL_MAP_NOTE = (
    "metadata.symbols maps every top-level symbol (and Class.method) to its exact "
    "[start_line, end_line]. To see a definition's real source, call ReadFile again "
    "with those lines — that returns verbatim text, not a summary."
)
RANGE_NOTE = (
    "This is a summary, not the source. Call ReadFile with start_line/end_line to "
    "get verbatim text for any range you need to be sure about."
)


def _parse_summary_json(text: str) -> Optional[dict]:
    """The model's JSON object, or None when it did not produce a usable one."""
    match = re.search(r"\{[\s\S]*\}", text or "")
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _merge_metadata(local: dict, model: Any) -> dict:
    """Local AST facts win; the model only fills in what the walk could not.

    The walk is exact and the model is not, so where both have an opinion about
    the same file the measured one is kept. For a non-Python file ``local`` is
    empty and this is just the model's own metadata.
    """
    if not isinstance(model, dict):
        return dict(local)
    if not local:
        return model
    merged = dict(model)
    merged.update({k: v for k, v in local.items() if v not in (None, [], {})})
    return merged


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


def _span(node: ast.AST, total_lines: int) -> list[int]:
    """The 1-based inclusive line range a definition occupies.

    ``end_lineno`` is present on every node from Python 3.8 on, but decorators
    sit *above* ``lineno``, so a decorated definition would otherwise report a
    range that starts below its own first line.
    """
    start = getattr(node, "lineno", 1)
    for decorator in getattr(node, "decorator_list", []) or []:
        start = min(start, getattr(decorator, "lineno", start))
    end = getattr(node, "end_lineno", None) or start
    return [max(1, start), min(max(end, start), total_lines)]


def _local_python_metadata(content: str) -> dict:
    """Fast local AST extraction for Python files (no API needed).

    Every definition carries its line range. That is what turns "I need the body
    of ``_repair_pairing``" into one targeted ``start_line``/``end_line`` read
    instead of a guess — the summary says what is in the file, ``symbols`` says
    where, and the range read returns the exact bytes. It costs one extra
    attribute per node to produce.
    """
    total_lines = content.count("\n") + 1
    meta: dict[str, Any] = {
        "language": "python",
        "imports": [],
        "classes": [],
        "functions": [],
        "symbols": {},
        "loc": total_lines,
        "parse_error": None,
    }
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        # Say so rather than silently returning an empty structure that reads
        # like "this file has no classes and no functions".
        meta["parse_error"] = f"{e.msg} (line {e.lineno})"
        return meta

    symbols: dict[str, list[int]] = meta["symbols"]
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
            span = _span(node, total_lines)
            methods = []
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    m_span = _span(m, total_lines)
                    methods.append({"name": m.name, "lines": m_span})
                    symbols[f"{node.name}.{m.name}"] = m_span
            meta["classes"].append({"name": node.name, "lines": span, "methods": methods})
            symbols[node.name] = span
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = _span(node, total_lines)
            args = [a.arg for a in node.args.args]
            meta["functions"].append(
                {
                    "name": node.name,
                    "signature": f"{node.name}({', '.join(args)})",
                    "lines": span,
                }
            )
            symbols[node.name] = span
    return meta


def _slice_lines(
    content: str, start_line: Optional[int], end_line: Optional[int]
) -> tuple[str, int, int, bool]:
    """The requested 1-based inclusive line range, clamped to the file.

    Returns ``(text, start, actual_end, truncated)``. When the range is larger
    than :data:`RANGE_MAX_CHARS` it is cut at a line boundary and ``actual_end``
    is the last line genuinely included — reporting the requested end for a
    body that stops short is how a caller comes to believe it has seen a
    definition it has not.
    """
    lines = content.splitlines()
    total = max(len(lines), 1)
    start = min(max(1, start_line or 1), total)
    end = min(max(end_line or total, start), total)

    kept: list[str] = []
    size = 0
    last = start - 1
    for offset, line in enumerate(lines[start - 1 : end], start=start):
        cost = len(line) + 1
        if kept and size + cost > RANGE_MAX_CHARS:
            return "\n".join(kept), start, last, True
        kept.append(line)
        size += cost
        last = offset
    return "\n".join(kept), start, max(last, start), False


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

    # Baseline mode for the A/B measurement: return the file as a plain agent
    # loop would, so the same task script can be run both ways and the two /cost
    # readings compared. Off by default; it exists to be measured against.
    if settings.file_reader_raw and start_line is None and end_line is None:
        body, _, end, cut_short = _slice_lines(content, 1, None)
        payload = {
            "path": str(p),
            "hash": content_hash,
            "language": _detect_language(p),
            "mode": "raw_baseline",
            "total_lines": content.count("\n") + 1,
            "verbatim": True,
            "content": body,
        }
        if cut_short:
            payload["truncated"] = f"stopped at line {end} ({RANGE_MAX_CHARS:,}-character limit)."
        return json.dumps(payload, ensure_ascii=False)

    # An explicit line range is a request for exact text, not for a summary:
    # answer it verbatim and never from the summary cache.
    if start_line is not None or end_line is not None:
        excerpt, start, end, cut_short = _slice_lines(content, start_line, end_line)
        total = content.count("\n") + 1
        payload = {
            "path": str(p),
            "hash": content_hash,
            "language": _detect_language(p),
            "start_line": start,
            "end_line": end,
            "total_lines": total,
            "verbatim": True,
            "content": excerpt,
        }
        if cut_short:
            payload["truncated"] = (
                f"stopped at line {end}: the requested range exceeds the "
                f"{RANGE_MAX_CHARS:,}-character limit. Request the rest as a further range."
            )
        return json.dumps(payload, ensure_ascii=False)

    # Cache hit. A focused read is a different question about the same bytes, so
    # a generic cached summary would silently answer the wrong one — the key has
    # to carry the focus.
    cache_key = content_hash if not focus else hashlib.sha256(
        f"{content_hash}\x00{focus}".encode("utf-8")
    ).hexdigest()
    cached = cache.get_file_summary(str(p), content_hash=cache_key)
    if cached and not raw:
        cached_meta = cached.get("metadata") or {}
        return json.dumps(
            {
                "path": str(p),
                "hash": content_hash,
                "from_cache": True,
                "metadata": cached_meta,
                "summary": cached["summary"],
                "note": SYMBOL_MAP_NOTE if cached_meta.get("symbols") else RANGE_NOTE,
            },
            ensure_ascii=False,
        )

    # Local fast path for Python
    language = _detect_language(p)
    metadata = {}
    if language == "python":
        metadata = _local_python_metadata(content)

    # For small files or when we have good local metadata, we can skip LLM
    if language == "python" and len(content) < LOCAL_SUMMARY_MAX_CHARS and not focus:
        summary = (
            f"Python module with {len(metadata.get('classes', []))} classes "
            f"and {len(metadata.get('functions', []))} top-level functions. "
            f"Imports: {', '.join(metadata.get('imports', [])[:8])}."
        )
        if metadata.get("parse_error"):
            summary = (
                f"Python file that does not parse ({metadata['parse_error']}) — "
                "no structure could be extracted. Read a line range to see the source."
            )
        result = {
            "path": str(p),
            "hash": content_hash,
            "from_cache": False,
            "summary_source": "local_ast",
            "metadata": metadata,
            "summary": summary,
            "note": SYMBOL_MAP_NOTE if metadata.get("symbols") else RANGE_NOTE,
        }
        if raw:
            result["raw_excerpt"] = content[:3000] + ("…" if len(content) > 3000 else "")
        cache.put_file_summary(str(p), summary, metadata, content, content_hash)
        return json.dumps(result, ensure_ascii=False)

    # LLM compaction for complex / focused cases
    if client is None:
        client = get_client()

    sent = content[:LLM_INPUT_MAX_CHARS]
    truncated_input = len(content) > len(sent)
    summary_source = "model"

    user_msg = f"File path: {p}\nLanguage: {language}\n"
    if focus:
        user_msg += f"Focus: {focus}\n"
    if truncated_input:
        user_msg += (
            f"NOTE: only the first {len(sent):,} of {len(content):,} characters are shown. "
            "Summarise what is here and do not speculate about the rest.\n"
        )
    user_msg += f"\n--- FILE CONTENT ({'TRUNCATED' if truncated_input else 'COMPLETE'}) ---\n{sent}"

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
        data = _parse_summary_json(text)
        if data is None:
            # A summary cut off at the token limit arrives as unparseable text
            # (or nothing at all); the locally-derived metadata is still sound,
            # so keep it and say what happened rather than reporting an empty
            # summary as the file's contents.
            data = {"summary": text[:600], "metadata": {}}
            if cut:
                data["summary"] = (
                    f"(summary truncated: {cut}) {text[:600]}".strip()
                    if text
                    else f"(no summary: model output stopped early — {cut})"
                )
            elif not text:
                data["summary"] = "(no summary: the model returned no text)"
    except Exception as e:
        data = {
            "summary": f"(LLM summary failed: {e}) Local fallback: {language} file, {content.count(chr(10))+1} lines.",
            "metadata": {},
        }
        summary_source = "local_fallback"

    result = {
        "path": str(p),
        "hash": content_hash,
        "from_cache": False,
        "summary_source": summary_source,
        # Local AST facts outrank the model's: line numbers it did not compute
        # are guesses, and a targeted range read is only useful if the numbers
        # behind it are ground truth.
        "metadata": _merge_metadata(metadata, data.get("metadata")),
        "summary": data.get("summary", ""),
        "key_snippets": data.get("key_snippets", []),
        "note": SYMBOL_MAP_NOTE if metadata.get("symbols") else RANGE_NOTE,
    }
    if truncated_input:
        # The summary describes a prefix of the file. Saying so is the difference
        # between a partial index and a wrong one.
        result["summary_covers"] = {
            "chars_summarised": len(sent),
            "chars_total": len(content),
            "complete": False,
        }
        result["note"] = (
            f"Only the first {len(sent):,} of {len(content):,} characters were summarised. "
            + result["note"]
        )
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

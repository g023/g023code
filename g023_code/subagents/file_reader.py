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
from ..paths import confine_or_error
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

# Hard ceiling on a raw=true full-file read. Above this the read is refused and
# the caller is pointed at start_line/end_line ranges instead, so an explicit
# request for the whole file cannot blow the orchestrator's context either.
FULL_READ_MAX_CHARS = 200_000

SYMBOL_MAP_NOTE = (
    "metadata.symbols maps name → [start_line, end_line]. "
    "Re-read those lines for verbatim source."
)
RANGE_NOTE = (
    "Summary, not source. Re-read with start_line/end_line for verbatim text."
)
# Unfocused reads are indexed locally (no API call). Cap the scan so a huge
# file cannot stall the process; focused reads still go to the model unless
# the focus is an exact (or unique-suffix) symbol already in the map.
LOCAL_INDEX_MAX_CHARS = 400_000
SYMBOL_MAP_MAX = 80
# Verbatim body included on an exact-symbol focus only when it stays small
# enough to sit in history as a cheap tail, not a 20k dump of a whole class.
LOCAL_SYMBOL_BODY_MAX_CHARS = 2_000
_FOCUS_KIND_PREFIX = re.compile(
    r"^(?:class|function|def|method|fn|struct|trait|type|enum|interface)\s+",
    re.I,
)

_RE_JS_TYPE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(class|function|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    re.M,
)
_RE_JS_ARROW = re.compile(
    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
    r"(?:async\s*)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>",
    re.M,
)
_RE_GO_FUNC = re.compile(r"^func\s+(?:\([^)]+\)\s+)?([A-Za-z_]\w*)", re.M)
_RE_GO_TYPE = re.compile(r"^type\s+([A-Za-z_]\w*)\s+", re.M)
_RE_RS_FN = re.compile(
    r"^(?:pub(?:\([^)]+\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)", re.M
)
_RE_RS_TYPE = re.compile(
    r"^(?:pub(?:\([^)]+\))?\s+)?(?:struct|enum|trait|type|impl)\s+([A-Za-z_]\w*)",
    re.M,
)
_RE_JAVA_TYPE = re.compile(
    r"^\s*(?:public|protected|private|static|final|abstract|\s)*"
    r"(class|interface|enum|record)\s+(\w+)",
    re.M,
)
_RE_C_TYPE = re.compile(r"^\s*(?:typedef\s+)?(?:struct|class|enum)\s+(\w+)", re.M)
_RE_SH_FN = re.compile(r"^(?:function\s+)?([A-Za-z_][\w]*)\s*\(\s*\)\s*\{", re.M)
_RE_MD_H = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
_RE_YAML_KEY = re.compile(r"^([A-Za-z_][\w.-]*)\s*:", re.M)
_RE_HTML_H = re.compile(r"<h[1-6][^>]*>([^<]+)</h[1-6]>", re.I)
_RE_HTML_TITLE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)


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


def _cap_symbols(meta: dict) -> dict:
    """Keep the map small enough to sit in orchestrator history as hits."""
    symbols = meta.get("symbols")
    if not isinstance(symbols, dict) or len(symbols) <= SYMBOL_MAP_MAX:
        return meta
    items = sorted(
        symbols.items(),
        key=lambda kv: (str(kv[0]).count("."), len(str(kv[0])), str(kv[0])),
    )
    out = dict(meta)
    out["symbols"] = dict(items[:SYMBOL_MAP_MAX])
    out["symbols_omitted"] = len(symbols) - SYMBOL_MAP_MAX
    return out


def _empty_meta(language: str, loc: int) -> dict[str, Any]:
    return {
        "language": language,
        "imports": [],
        "classes": [],
        "functions": [],
        "symbols": {},
        "loc": loc,
        "parse_error": None,
        "summary_source": "local_index",
    }


def _add_symbol(meta: dict, name: str, line: int, kind: str) -> None:
    if not name or name in meta["symbols"]:
        return
    span = [line, line]
    meta["symbols"][name] = span
    bucket = "classes" if kind == "class" else "functions"
    meta[bucket].append({"name": name, "lines": span})


def _line_at(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _local_js_metadata(content: str, loc: int, language: str) -> dict:
    meta = _empty_meta(language, loc)
    for match in _RE_JS_TYPE.finditer(content):
        kind = "class" if match.group(1) in {"class", "interface", "enum", "type"} else "function"
        _add_symbol(meta, match.group(2), _line_at(content, match.start()), kind)
    for match in _RE_JS_ARROW.finditer(content):
        _add_symbol(meta, match.group(1), _line_at(content, match.start()), "function")
    return meta


def _local_go_metadata(content: str, loc: int) -> dict:
    meta = _empty_meta("go", loc)
    for match in _RE_GO_TYPE.finditer(content):
        _add_symbol(meta, match.group(1), _line_at(content, match.start()), "class")
    for match in _RE_GO_FUNC.finditer(content):
        _add_symbol(meta, match.group(1), _line_at(content, match.start()), "function")
    return meta


def _local_rust_metadata(content: str, loc: int) -> dict:
    meta = _empty_meta("rust", loc)
    for match in _RE_RS_TYPE.finditer(content):
        _add_symbol(meta, match.group(1), _line_at(content, match.start()), "class")
    for match in _RE_RS_FN.finditer(content):
        _add_symbol(meta, match.group(1), _line_at(content, match.start()), "function")
    return meta


def _local_java_metadata(content: str, loc: int, language: str) -> dict:
    meta = _empty_meta(language, loc)
    for match in _RE_JAVA_TYPE.finditer(content):
        _add_symbol(meta, match.group(2), _line_at(content, match.start()), "class")
    return meta


def _local_c_metadata(content: str, loc: int, language: str) -> dict:
    meta = _empty_meta(language, loc)
    for match in _RE_C_TYPE.finditer(content):
        _add_symbol(meta, match.group(1), _line_at(content, match.start()), "class")
    return meta


def _local_shell_metadata(content: str, loc: int) -> dict:
    meta = _empty_meta("bash", loc)
    for match in _RE_SH_FN.finditer(content):
        _add_symbol(meta, match.group(1), _line_at(content, match.start()), "function")
    return meta


def _local_markdown_metadata(content: str, loc: int) -> dict:
    meta = _empty_meta("markdown", loc)
    headings: list[str] = []
    for match in _RE_MD_H.finditer(content):
        title = match.group(2).strip()
        if title and title not in headings:
            headings.append(title)
        if len(headings) >= 24:
            break
    meta["headings"] = headings
    return meta


def _local_json_metadata(content: str, loc: int) -> dict:
    meta = _empty_meta("json", loc)
    try:
        data = json.loads(content)
    except json.JSONDecodeError as e:
        meta["parse_error"] = str(e)
        return meta
    if isinstance(data, dict):
        meta["keys"] = list(data.keys())[:40]
    elif isinstance(data, list):
        meta["keys"] = [f"array[{len(data)}]"]
    return meta


def _local_yaml_metadata(content: str, loc: int, language: str) -> dict:
    meta = _empty_meta(language, loc)
    keys: list[str] = []
    for match in _RE_YAML_KEY.finditer(content):
        key = match.group(1)
        if key not in keys:
            keys.append(key)
        if len(keys) >= 40:
            break
    meta["keys"] = keys
    return meta


def _local_html_metadata(content: str, loc: int) -> dict:
    meta = _empty_meta("html", loc)
    title = _RE_HTML_TITLE.search(content)
    headings: list[str] = []
    if title:
        headings.append(title.group(1).strip())
    for match in _RE_HTML_H.finditer(content):
        text = re.sub(r"\s+", " ", match.group(1)).strip()
        if text and text not in headings:
            headings.append(text)
        if len(headings) >= 24:
            break
    meta["headings"] = headings
    return meta


def _local_metadata(path: Path, content: str) -> dict:
    """Language-aware local index. No API call."""
    language = _detect_language(path)
    loc = content.count("\n") + 1
    truncated = len(content) > LOCAL_INDEX_MAX_CHARS
    body = content if not truncated else content[:LOCAL_INDEX_MAX_CHARS]
    if language == "python":
        meta = _local_python_metadata(body)
        meta["summary_source"] = "local_ast"
    elif language in {"javascript", "typescript"}:
        meta = _local_js_metadata(body, loc, language)
    elif language == "go":
        meta = _local_go_metadata(body, loc)
    elif language == "rust":
        meta = _local_rust_metadata(body, loc)
    elif language == "java":
        meta = _local_java_metadata(body, loc, language)
    elif language in {"c", "cpp"}:
        meta = _local_c_metadata(body, loc, language)
    elif language == "bash":
        meta = _local_shell_metadata(body, loc)
    elif language == "markdown":
        meta = _local_markdown_metadata(body, loc)
    elif language == "json":
        meta = _local_json_metadata(body, loc)
    elif language in {"yaml", "toml"}:
        meta = _local_yaml_metadata(body, loc, language)
    elif language == "html":
        meta = _local_html_metadata(body, loc)
    else:
        meta = _empty_meta(language, loc)
    meta["loc"] = loc
    if truncated:
        meta["index_truncated"] = True
    return _cap_symbols(meta)


def _resolve_symbol_focus(focus: str, symbols: dict) -> Optional[str]:
    """Map a focus string onto one local symbol key, or None.

    Exact key, optional ``class Foo`` / ``def foo`` prefix, or a unique
    dotted suffix (``method_one`` → ``Thing.method_one``). A sentence
    ("what does method_one do") does not resolve — that hint still goes
    to the model.
    """
    token = (focus or "").strip()
    if not token or not isinstance(symbols, dict) or not symbols:
        return None
    if token in symbols:
        return token
    stripped = _FOCUS_KIND_PREFIX.sub("", token).strip()
    if stripped and stripped in symbols:
        return stripped
    needle = stripped or token
    matches = [k for k in symbols if str(k).rsplit(".", 1)[-1] == needle]
    if len(matches) == 1:
        return matches[0]
    return None


def _local_summary(content: str, meta: dict) -> str:
    lang = meta.get("language") or "text"
    loc = meta.get("loc") or (content.count("\n") + 1)
    if meta.get("parse_error"):
        return (
            f"{lang} file that does not parse ({meta['parse_error']}) — "
            "no structure could be extracted. Read a line range to see the source."
        )
    bits = [f"{lang} file, {loc} lines"]
    classes = meta.get("classes") or []
    funcs = meta.get("functions") or []
    if classes:
        names = [c.get("name") for c in classes[:8] if c.get("name")]
        bits.append(f"{len(classes)} type(s): {', '.join(names)}")
    if funcs:
        names = [fn.get("name") for fn in funcs[:10] if fn.get("name")]
        bits.append(f"{len(funcs)} function(s): {', '.join(names)}")
    imports = meta.get("imports") or []
    if imports:
        bits.append("imports: " + ", ".join(imports[:8]))
    headings = meta.get("headings") or []
    if headings:
        bits.append("headings: " + ", ".join(headings[:10]))
    keys = meta.get("keys") or []
    if keys:
        bits.append("keys: " + ", ".join(str(k) for k in keys[:12]))
    omitted = meta.get("symbols_omitted")
    if omitted:
        bits.append(f"{omitted} more symbols omitted from the map")
    if meta.get("index_truncated"):
        bits.append(f"indexed first {LOCAL_INDEX_MAX_CHARS:,} chars only")
    return ". ".join(bits) + "."


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
    p, err = confine_or_error(path, root=root)
    if err or p is None:
        return json.dumps({"error": err or "Path is outside project root"})

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

    # raw=true is an explicit request for the whole file: return it verbatim,
    # capped so a huge file cannot flood the orchestrator's context. This is
    # the correct way to prepare a WriteFile edit — a summary is never enough
    # to rewrite a file safely, and a summary-based rewrite is how files end
    # up truncated.
    if raw and start_line is None and end_line is None:
        language = _detect_language(p)
        if len(content) > FULL_READ_MAX_CHARS:
            return json.dumps({
                "path": str(p),
                "hash": content_hash,
                "language": language,
                "mode": "raw_full",
                "verbatim": False,
                "error": (
                    f"File is {len(content):,} chars — too large for one raw read "
                    f"(limit {FULL_READ_MAX_CHARS:,}). Read it in start_line/end_line "
                    "ranges instead."
                ),
            }, ensure_ascii=False)
        return json.dumps({
            "path": str(p),
            "hash": content_hash,
            "language": language,
            "mode": "raw_full",
            "verbatim": True,
            "total_lines": content.count("\n") + 1,
            "bytes": len(content),
            "content": content,
        }, ensure_ascii=False)

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

    # Unfocused reads are free: a local index (AST for Python, regex/structure
    # for other languages) plus a symbol map. A focus that already names a
    # symbol in that map is free too. Semantic hints still go to Flash.
    language = _detect_language(p)
    metadata = _local_metadata(p, content)

    if not focus:
        summary = _local_summary(content, metadata)
        result = {
            "path": str(p),
            "hash": content_hash,
            "from_cache": False,
            "summary_source": metadata.get("summary_source") or "local_index",
            "metadata": metadata,
            "summary": summary,
            "note": SYMBOL_MAP_NOTE if metadata.get("symbols") else RANGE_NOTE,
        }
        if metadata.get("index_truncated"):
            result["summary_covers"] = {
                "chars_summarised": LOCAL_INDEX_MAX_CHARS,
                "chars_total": len(content),
                "complete": False,
            }
        cache.put_file_summary(str(p), summary, metadata, content, content_hash)
        return json.dumps(result, ensure_ascii=False)

    symbol = _resolve_symbol_focus(focus, metadata.get("symbols") or {})
    if symbol:
        span = (metadata.get("symbols") or {}).get(symbol) or [1, 1]
        start, end = int(span[0]), int(span[1] if len(span) > 1 else span[0])
        excerpt, start, end, cut_short = _slice_lines(content, start, end)
        summary = (
            f"{_local_summary(content, metadata)} Focused on {symbol} "
            f"(lines {start}-{end})."
        )
        result = {
            "path": str(p),
            "hash": content_hash,
            "from_cache": False,
            "summary_source": "local_symbol",
            "focus": focus,
            "symbol": symbol,
            "metadata": metadata,
            "summary": summary,
            "start_line": start,
            "end_line": end,
            "note": SYMBOL_MAP_NOTE,
        }
        if (
            excerpt
            and not cut_short
            and len(excerpt) <= LOCAL_SYMBOL_BODY_MAX_CHARS
        ):
            result["verbatim"] = True
            result["content"] = excerpt
        else:
            result["verbatim"] = False
            result["note"] = (
                f"Focused on {symbol} lines {start}-{end} "
                f"({end - start + 1} lines). Re-read that range for verbatim "
                "source. " + SYMBOL_MAP_NOTE
            )
        cache.put_file_summary(
            str(p), result["summary"], result["metadata"], content, cache_key
        )
        return json.dumps(result, ensure_ascii=False)

    # LLM compaction for focused reads only
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
            max_output_tokens=max(1, int(settings.file_summary_max_tokens or 400)),
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
    cache.put_file_summary(
        str(p),
        result["summary"],
        result["metadata"],
        content if len(content) < 200_000 else None,
        cache_key,
    )
    return json.dumps(result, ensure_ascii=False)

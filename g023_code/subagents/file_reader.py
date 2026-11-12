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
from bisect import bisect_left
from pathlib import Path
from typing import Any, Optional

from ..api import (
    DeepSeekClient,
    get_client,
    incomplete_reason,
    output_text,
    reasoning_param,
)

from ..config import MODEL, get_project_root, settings
from ..cache import get_cache
from .. import docindex, edit_engine, shapes
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
# Unfocused reads are indexed locally (no API call). The scans are linear, so the
# cap only guards against pathological inputs; a 340 KB C file indexes in ~10 ms.
LOCAL_INDEX_MAX_CHARS = 2_000_000
# Characters of outline rows (symbols or text sections) one read may carry. Over it the
# outline folds: symbol names group by prefix, sections show child counts. It never
# drops rows silently ("not shown" must not read as "not there").
OUTLINE_BUDGET = 2_400
# Above this many symbols, a focused read leaves the symbol map out (the model has the
# outline from the unfocused read, and the focus result says where it is).
FOCUS_META_SYMBOLS_MAX = 8
# Verbatim body included on an exact-symbol focus only when it stays small
# enough to sit in history as a cheap tail, not a 20k dump of a whole class.
LOCAL_SYMBOL_BODY_MAX_CHARS = 2_000
_FOCUS_KIND_PREFIX = re.compile(
    r"^(?:class|function|def|method|fn|struct|trait|type|enum|interface)\s+",
    re.I,
)

_RE_JS_TYPE = re.compile(
    r"^[ \t]*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?(?:async\s+)?"
    r"(class|function|interface|type|enum)\s+([A-Za-z_$][\w$]*)",
    re.M,
)
_RE_JS_ARROW = re.compile(
    r"^[ \t]*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*"
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
    r"^[ \t]*(?:public|protected|private|static|final|abstract|[ \t])*"
    r"(class|interface|enum|record)\s+(\w+)",
    re.M,
)
_RE_C_TYPE = re.compile(
    r"^[ \t]*(?:typedef\s+)?(?:struct|class|enum)\s+(\w+)", re.M
)
# A C/C++ function definition: column-0 (optional return type and qualifiers on the
# same line), name (C++ ``A::b``), parameters that may span lines, then ``{``.
# A prototype ends in ``;`` and a macro call has no brace, so neither matches.
_RE_C_FUNC = re.compile(
    r"^(?![ \t#])(?:[A-Za-z_][\w:<>,*& \t]*?[ \t*&])?"
    r"(~?[A-Za-z_]\w*(?:::~?[A-Za-z_]\w*)*)[ \t]*"
    r"\((?:[^;{}()]|\([^;{}()]*\))*\)"
    r"[ \t]*(?:const[ \t]*)?(?:noexcept[ \t]*)?(?:override[ \t]*)?(?=\s*\{)",
    re.M,
)
_C_NOT_FUNC = frozenset(
    "if while for switch return sizeof else do case defined catch".split()
)
# A Java/C# method: indented, at least one modifier, return type, name, params, ``{``.
_RE_JAVA_METHOD = re.compile(
    r"^[ \t]+(?:@\w+(?:\([^)]*\))?\s+)*"
    r"(?:(?:public|protected|private|static|final|abstract|synchronized|native|default|override|virtual|async)\s+)+"
    r"(?:<[^>]+>\s+)?[\w<>\[\],.? ]+?\s+(\w+)\s*\([^;{}]*\)\s*(?:throws\s+[\w.,\s]+)?(?=\{)",
    re.M,
)
_RE_SH_FN = re.compile(r"^(?:function\s+)?([A-Za-z_][\w]*)\s*\(\s*\)\s*\{", re.M)
_RE_MD_H = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.M)
_RE_YAML_KEY = re.compile(r"^([A-Za-z_][\w.-]*)\s*:", re.M)
_RE_HTML_H = re.compile(r"<h[1-6][^>]*>([^<]+)</h[1-6]>", re.I)
_RE_HTML_TITLE = re.compile(r"<title[^>]*>([^<]+)</title>", re.I)
_RE_HTML_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.I | re.S)
_JS_SCRIPT_TYPES = {
    "module",
    "text/javascript",
    "application/javascript",
    "text/ecmascript",
    "application/ecmascript",
}


def _line_count(text: str) -> int:
    """Lines as an editor numbers them: a trailing newline does not open a new line.

    ``count("\\n") + 1`` reported 200,002 lines for a 200,001-line CSV, and the
    model subtracted the header from that and answered one row too many.
    """
    n = text.count("\n")
    return n + 1 if text and not text.endswith("\n") else n


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
        ".cc": "cpp",
        ".cxx": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".hh": "cpp",
        ".hxx": "cpp",
        ".cs": "java",
        ".md": "markdown",
        ".markdown": "markdown",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".sh": "bash",
        ".html": "html",
        ".css": "css",
        ".csv": "csv",
        ".tsv": "tsv",
        ".jsonl": "jsonl",
        ".ndjson": "jsonl",
        ".log": "log",
    }.get(ext, "text")


def _fn_signature(node: ast.AST) -> str:
    """Argument list only. Defaults would bloat a summary that sits in history."""
    args = getattr(getattr(node, "args", None), "args", None) or []
    names = [a.arg for a in args]
    vararg = getattr(getattr(node, "args", None), "vararg", None)
    if vararg is not None:
        names.append("*" + vararg.arg)
    for a in getattr(getattr(node, "args", None), "kwonlyargs", None) or []:
        names.append(a.arg)
    kwarg = getattr(getattr(node, "args", None), "kwarg", None)
    if kwarg is not None:
        names.append("**" + kwarg.arg)
    return f"{getattr(node, 'name', '')}({', '.join(names)})"


def _fn_doc_line(node: ast.AST) -> str:
    doc = ast.get_docstring(node) or ""
    line = doc.strip().splitlines()[0].strip() if doc.strip() else ""
    if len(line) > 80:
        line = line[:77] + "..."
    return line


def _attach_fn(payload: dict, node: ast.AST) -> dict:
    payload["signature"] = _fn_signature(node)
    doc = _fn_doc_line(node)
    if doc:
        payload["doc"] = doc
    return payload


def _skip_string(content: str, j: int) -> int:
    quote = content[j]
    n = len(content)
    j += 1
    while j < n:
        c = content[j]
        if c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        if quote != "`" and c == "\n":
            return j
        j += 1
    return n


def _skip_comment(content: str, j: int, *, hash_comment: bool) -> int:
    if content.startswith("//", j) or (hash_comment and content.startswith("#", j)):
        nl = content.find("\n", j)
        return len(content) if nl < 0 else nl
    if content.startswith("/*", j):
        end = content.find("*/", j + 2)
        return len(content) if end < 0 else end + 2
    return j + 1


# A column-0 line that starts another declaration. Used so `type Foo int`
# does not swallow the next function's brace.
_NEXT_DECL = re.compile(
    r"^(?:export\s+|pub(?:\([^)]+\))?\s+|public\s+|private\s+|protected\s+)?"
    r"(?:default\s+|abstract\s+|async\s+|static\s+|final\s+)*"
    r"(?:func|fn|function|class|interface|type|struct|enum|impl|def)\b"
)


def _closing_brace_line(
    content: str, start: int, open_line: int, *, hash_comment: bool = False
) -> int:
    """1-based line of the `}` that closes the block beginning at or after `start`.

    Returns ``open_line`` when there is no block (a type alias, a one-line
    arrow). A one-line span used to be reported as the whole function, and a
    symbol focus then handed the model a single line as if it were the body.
    """
    n = len(content)
    j = start
    open_at = -1
    while j < n:
        c = content[j]
        if c == "\n":
            k = j + 1
            # Only a column-0 declaration ends the search. Indented `{` still
            # belongs to this one (a signature broken across lines).
            if k < n and content[k] not in " \t\r\n" and _NEXT_DECL.match(content[k:]):
                return open_line
            j += 1
            continue
        if c in "\"'`":
            j = _skip_string(content, j)
            continue
        if c == "/" and j + 1 < n and content[j + 1] in "/*":
            j = _skip_comment(content, j, hash_comment=hash_comment)
            continue
        if hash_comment and c == "#":
            j = _skip_comment(content, j, hash_comment=True)
            continue
        if c == "{":
            open_at = j
            break
        j += 1
    if open_at < 0:
        return open_line

    depth = 0
    j = open_at
    while j < n:
        c = content[j]
        if c in "\"'`":
            j = _skip_string(content, j)
            continue
        if c == "/" and j + 1 < n and content[j + 1] in "/*":
            j = _skip_comment(content, j, hash_comment=hash_comment)
            continue
        if hash_comment and c == "#":
            j = _skip_comment(content, j, hash_comment=True)
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                # Line from the offset: skipped comments/strings may span lines.
                return max(_line_at(content, j), open_line)
        j += 1
    return open_line


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
    total_lines = _line_count(content)
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
                    methods.append(
                        _attach_fn({"name": m.name, "lines": m_span}, m)
                    )
                    symbols[f"{node.name}.{m.name}"] = m_span
            meta["classes"].append({"name": node.name, "lines": span, "methods": methods})
            symbols[node.name] = span
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            span = _span(node, total_lines)
            meta["functions"].append(
                _attach_fn(
                    {
                        "name": node.name,
                        "lines": span,
                    },
                    node,
                )
            )
            symbols[node.name] = span
    return meta


def _group_key(name: str) -> str:
    """Prefix a symbol shares with its siblings: ``do_goto`` → ``do_``, ``Foo.bar`` → ``Foo.``."""
    if "." in name:
        return name.rsplit(".", 1)[0] + "."
    if "::" in name:
        return name.rsplit("::", 1)[0] + "::"
    if "_" in name.strip("_"):
        lead = len(name) - len(name.lstrip("_"))
        return name[: name.index("_", lead) + 1]
    m = re.match(r"[a-z]+(?=[A-Z])", name)
    return m.group(0) if m else ""


def _fold_symbols(symbols: dict, budget: int = OUTLINE_BUDGET) -> list[str]:
    """Every symbol in under ``budget`` chars: prefix groups with names and start lines.

    Level 1 is ``do_* (95): goto 961, regoto 1434, ...``. If that is still over the
    budget, level 2 is ``do_* 95  L85-9020`` per group. Names are never dropped
    without a count.
    """
    items = []
    for name, span in symbols.items():
        try:
            items.append((int(span[0]), int(span[-1]), str(name)))
        except (TypeError, ValueError, IndexError):
            continue
    items.sort()
    groups: dict[str, list[tuple[int, int, str]]] = {}
    for it in items:
        groups.setdefault(_group_key(it[2]), []).append(it)
    singles = [it for k, g in groups.items() if not k or len(g) < 3 for it in g]
    multi = [(k, g) for k, g in groups.items() if k and len(g) >= 3]
    multi.sort(key=lambda kg: kg[1][0][0])
    singles.sort()

    def pack(prefix: str, words: list[str]) -> list[str]:
        out, cur = [], prefix
        for w in words:
            if len(cur) + len(w) + 2 > 110 and cur != prefix:
                out.append(cur.rstrip(", "))
                cur = "  "
            cur += w + ", "
        out.append(cur.rstrip(", "))
        return out

    rows: list[str] = []
    for k, g in multi:
        rows.extend(pack(f"{k}* ({len(g)}): ", [f"{n[len(k):]} {a}" for a, _b, n in g]))
    if singles:
        rows.extend(pack("", [f"{n} {a}" for a, _b, n in singles]))
    if sum(len(r) + 1 for r in rows) <= budget:
        return rows
    rows = [f"{k}* {len(g)}  L{g[0][0]}-{max(b for _a, b, _n in g)}" for k, g in multi]
    if singles:
        rows.append(f"other {len(singles)}: " + ", ".join(n for _a, _b, n in singles[:20])
                    + (f", … {len(singles) - 20} more" if len(singles) > 20 else ""))
    return rows


def _outline_cost(meta: dict) -> int:
    cost = 0
    for c in meta.get("classes") or []:
        cost += len(str(c.get("name", ""))) + 16
        for m in c.get("methods") or []:
            cost += len(str(m.get("signature") or m.get("name") or "")) + len(str(m.get("doc") or "")) + 16
    for f in meta.get("functions") or []:
        cost += len(str(f.get("signature") or f.get("name") or "")) + len(str(f.get("doc") or "")) + 16
    return cost


def _payload_meta(meta: dict, *, focused: bool = False) -> dict:
    """The metadata a result carries, within the outline budget.

    Unfocused: the full outline if it fits, else the folded symbol index (all names, by
    prefix). Focused: no outline at all once the file has many symbols. The model has
    already seen it, and the focus result carries its own address.
    """
    out = dict(meta)
    symbols = out.get("symbols") or {}
    if focused:
        out.pop("outline", None)
        if len(symbols) > FOCUS_META_SYMBOLS_MAX:
            for k in ("classes", "functions", "symbols"):
                out.pop(k, None)
            out["symbols_total"] = len(symbols)
        return out
    if symbols and _outline_cost(out) > OUTLINE_BUDGET:
        for k in ("classes", "functions", "symbols"):
            out.pop(k, None)
        out["symbols_total"] = len(symbols)
        out["outline"] = _fold_symbols(symbols)
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


def _add_symbol(
    meta: dict, name: str, line: int, kind: str, end_line: Optional[int] = None
) -> None:
    if not name or name in meta["symbols"]:
        return
    end = line if not end_line or end_line < line else end_line
    span = [line, end]
    meta["symbols"][name] = span
    bucket = "classes" if kind == "class" else "functions"
    meta[bucket].append({"name": name, "lines": span})


_NL_CACHE: list = [None, None]  # (text, newline offsets) of the last file scanned


def _line_at(text: str, pos: int) -> int:
    """1-based line of offset ``pos``, by bisect over cached newline offsets (O(log n))."""
    if _NL_CACHE[0] is not text:
        _NL_CACHE[0] = text
        _NL_CACHE[1] = [m.start() for m in re.finditer("\n", text)]
    return bisect_left(_NL_CACHE[1], pos) + 1


def _decl_end(content: str, match: re.Match, *, hash_comment: bool = False) -> int:
    line = _line_at(content, match.start())
    return _closing_brace_line(
        content, match.end(), line, hash_comment=hash_comment
    )


def _local_js_metadata(content: str, loc: int, language: str) -> dict:
    meta = _empty_meta(language, loc)
    for match in _RE_JS_TYPE.finditer(content):
        kind = "class" if match.group(1) in {"class", "interface", "enum", "type"} else "function"
        line = _line_at(content, match.start())
        _add_symbol(meta, match.group(2), line, kind, _decl_end(content, match))
    for match in _RE_JS_ARROW.finditer(content):
        line = _line_at(content, match.start())
        _add_symbol(meta, match.group(1), line, "function", _decl_end(content, match))
    return meta


def _local_go_metadata(content: str, loc: int) -> dict:
    meta = _empty_meta("go", loc)
    for match in _RE_GO_TYPE.finditer(content):
        line = _line_at(content, match.start())
        _add_symbol(meta, match.group(1), line, "class", _decl_end(content, match))
    for match in _RE_GO_FUNC.finditer(content):
        line = _line_at(content, match.start())
        _add_symbol(meta, match.group(1), line, "function", _decl_end(content, match))
    return meta


def _local_rust_metadata(content: str, loc: int) -> dict:
    meta = _empty_meta("rust", loc)
    for match in _RE_RS_TYPE.finditer(content):
        line = _line_at(content, match.start())
        _add_symbol(meta, match.group(1), line, "class", _decl_end(content, match))
    for match in _RE_RS_FN.finditer(content):
        line = _line_at(content, match.start())
        _add_symbol(meta, match.group(1), line, "function", _decl_end(content, match))
    return meta


def _local_java_metadata(content: str, loc: int, language: str) -> dict:
    meta = _empty_meta(language, loc)
    for match in _RE_JAVA_TYPE.finditer(content):
        line = _line_at(content, match.start())
        _add_symbol(meta, match.group(2), line, "class", _decl_end(content, match))
    for match in _RE_JAVA_METHOD.finditer(content):
        if match.group(1) in _C_NOT_FUNC:
            continue
        line = _line_at(content, match.start())
        _add_symbol(meta, match.group(1), line, "function", _decl_end(content, match))
    return meta


def _local_c_metadata(content: str, loc: int, language: str) -> dict:
    meta = _empty_meta(language, loc)
    for match in _RE_C_TYPE.finditer(content):
        if not re.match(r"\s*\{", content[match.end() : match.end() + 200]):
            continue  # a forward declaration or a variable of struct type: no body
        line = _line_at(content, match.start())
        _add_symbol(meta, match.group(1), line, "class", _decl_end(content, match))
    for match in _RE_C_FUNC.finditer(content):
        name = match.group(1)
        if name.rsplit("::", 1)[-1] in _C_NOT_FUNC:
            continue
        line = _line_at(content, match.start())
        _add_symbol(meta, name, line, "function", _decl_end(content, match))
    return meta


def _local_shell_metadata(content: str, loc: int) -> dict:
    meta = _empty_meta("bash", loc)
    for match in _RE_SH_FN.finditer(content):
        line = _line_at(content, match.start())
        _add_symbol(
            meta,
            match.group(1),
            line,
            "function",
            _decl_end(content, match, hash_comment=True),
        )
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


def _html_script_is_js(attrs: str) -> bool:
    """Classic ``<script>`` and JS types are indexed. JSON-LD is not."""
    match = re.search(r"\btype\s*=\s*['\"]?([^'\"\s>]+)", attrs or "", re.I)
    if not match:
        return True
    kind = match.group(1).lower()
    return kind in _JS_SCRIPT_TYPES or "javascript" in kind


def _merge_offset_symbols(meta: dict, js: dict, base_line: int) -> None:
    """Copy a script body's symbol spans onto the HTML file's line numbers."""
    for bucket, kind in (("functions", "function"), ("classes", "class")):
        for item in js.get(bucket) or []:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            span = item.get("lines") or [1, 1]
            start = int(span[0]) + base_line
            end = int(span[1] if len(span) > 1 else span[0]) + base_line
            _add_symbol(meta, str(item["name"]), start, kind, end)


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
    # A canvas game is usually one HTML file with an inline script. Indexing
    # only the title left focus=tick as a semantic Flash call, or a rewrite
    # from a summary that had no function bodies.
    for match in _RE_HTML_SCRIPT.finditer(content):
        if not _html_script_is_js(match.group(1)):
            continue
        body = match.group(2)
        if not body.strip():
            continue
        base_line = content.count("\n", 0, match.start(2))
        js = _local_js_metadata(body, _line_count(body), "javascript")
        _merge_offset_symbols(meta, js, base_line)
    return meta


def _local_metadata(path: Path, content: str) -> dict:
    """Language-aware local index. No API call."""
    language = _detect_language(path)
    loc = _line_count(content)
    # Text and Markdown are outlined by docindex over the whole file; the cap is for regex scans.
    truncated = len(content) > LOCAL_INDEX_MAX_CHARS and language not in TEXT_LANGUAGES
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
    if language in TEXT_LANGUAGES:
        # Plain text and Markdown: a heading tree over the whole file (no char cap),
        # folded to the outline budget. Markdown keeps its flat heading list when short.
        outline = docindex.doc_for(path, content, _doc_kind(language)).outline
        if outline:
            nodes = sum(n.count() for n in outline.nodes)
            if language != "markdown" or nodes > 24:
                meta.pop("headings", None)
                meta["outline"] = docindex.fold(outline.nodes, OUTLINE_BUDGET)
                meta["sections"] = nodes
    return meta


TEXT_LANGUAGES = frozenset({"text", "markdown"})


def _doc_kind(language: str) -> str:
    return language if language in TEXT_LANGUAGES else "code"


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


LOCAL_FOCUS_TOP = 3
LOCAL_FOCUS_WINDOW = 40
# A file this small is cheaper to hand back whole than to rank: a miss would
# cost another model round-trip that re-reads the full prefix.
LOCAL_FOCUS_WHOLE_FILE_MAX_CHARS = 4_000
# Unfocused ReadFile of a file this small returns it whole and verbatim.
SMALL_FILE_WHOLE_MAX_CHARS = LOCAL_FOCUS_WHOLE_FILE_MAX_CHARS
# Verbatim excerpt ceiling for a focus answer (a section, a speech, a function).
LOCAL_FOCUS_EXCERPT_MAX_CHARS = 4_000
# Window shown around a focus hit inside a very long (minified) line.
LONG_LINE_WINDOW = 1_200
# An unquoted multi-word focus counts as a phrase only while it is this rare.
# "the king" with 500 hits is a term query, not a quotation.
PHRASE_MAX_HITS = 12
PHRASE_BEFORE, PHRASE_AFTER = 6, 24  # excerpt lines around a phrase hit
FOCUS_MATCH_ROWS = 6

def _addresser(outline: Optional[docindex.Outline], symbols: dict):
    """line → where it is: section breadcrumb (text) or enclosing symbol (code)."""
    if outline:
        return lambda ln: outline.address(ln)
    def where(ln: int) -> str:
        sym = docindex.enclosing_symbol(symbols, ln)
        return sym if sym else "module level"
    return where


def _clip_excerpt(lines: list[str], a: int, b: int) -> tuple[str, int, int]:
    """Lines a..b (1-based) cut to the excerpt ceiling at a line boundary."""
    kept, size, end = [], 0, a - 1
    for n in range(a, b + 1):
        line, _ = shapes.clip_line(lines[n - 1], n, LOCAL_FOCUS_EXCERPT_MAX_CHARS // 2)
        cost = len(line) + 1
        if kept and size + cost > LOCAL_FOCUS_EXCERPT_MAX_CHARS:
            break
        kept.append(line)
        size += cost
        end = n
    return "\n".join(kept), a, max(end, a)


def _long_line_window(line: str, phrase: str, n: int, width: int = LONG_LINE_WINDOW) -> Optional[str]:
    """A window of a very long line around the first occurrence of ``phrase``."""
    low = line.lower()
    col = low.find(phrase.lower())
    if col < 0:
        words = [w for w in re.findall(r"\w+", phrase.lower()) if len(w) > 2]
        hits = [low.find(w) for w in words]
        hits = [h for h in hits if h >= 0]
        if not hits:
            return None
        col = min(hits)
    a = max(0, col - width // 2)
    b = min(len(line), a + width)
    return (f"[line {n}, chars {a + 1:,}-{b:,} of {len(line):,}] "
            + ("..." if a else "") + line[a:b] + ("..." if b < len(line) else ""))


def _local_focus(
    content: str,
    metadata: dict,
    focus: str,
    *,
    doc: Optional[docindex.DocCache] = None,
) -> Optional[dict]:
    """Answer a free-text focus locally, with no model call. Every match carries its address.

    Order, cheapest-certain first:
    1. exact phrase (quoted, or the whole focus when 2+ words and rare): hit lines + the
       passage around the first hit;
    2. outline node (text): a focus naming a section path ("hamlet act 3 scene 1")
       returns that section;
    3. BM25 over sections (symbols + module-level windows for code, 40-line windows
       inside outline leaves for text), word-boundary tf, name boost. An oversized best
       symbol is narrowed to its best window, so the excerpt stays verbatim.
    """
    lines = content.splitlines()
    total = len(lines)
    if content.strip() and len(content) <= LOCAL_FOCUS_WHOLE_FILE_MAX_CHARS:
        return {
            "matches": [{"section": "whole file", "start_line": 1, "end_line": total, "score": None}],
            "start_line": 1,
            "end_line": total,
            "verbatim": True,
            "content": content,
            "note": "small file: returned whole instead of ranked",
        }
    if doc is None:
        doc = docindex.DocCache(lines, "code")
    outline = doc.outline if doc.kind in TEXT_LANGUAGES else None
    symbols = metadata.get("symbols") or {}
    where = _addresser(outline, symbols)

    # 1. Phrase.
    quote = docindex.quoted(focus)
    phrase = quote or (focus if len(docindex.norm_words(focus)) >= 2 else None)
    phrase_missed = False
    if phrase:
        hits, found = docindex.find_phrase(doc.stream, phrase, limit=FOCUS_MATCH_ROWS)
        if found and (quote or hits <= PHRASE_MAX_HITS):
            first = found[0] + 1
            a, b = max(1, first - PHRASE_BEFORE), min(total, first + PHRASE_AFTER)
            if outline:
                path = outline.path(first)
                if path:  # stay inside the section the hit is in
                    leaf = path[-1]
                    a, b = max(a, leaf.start), min(b, leaf.end)
            else:
                sym = docindex.enclosing_symbol(symbols, first)
                span = symbols.get(sym) if sym else None
                if span and len("\n".join(lines[int(span[0]) - 1 : int(span[-1])])) <= LOCAL_FOCUS_EXCERPT_MAX_CHARS:
                    a, b = int(span[0]), int(span[-1])
            excerpt, a, b = _clip_excerpt(lines, a, b)
            if len(lines[first - 1]) > shapes.LINE_CLIP_CHARS:
                win = _long_line_window(lines[first - 1], phrase, first)
                if win:
                    excerpt, a, b = win, first, first
            return {
                "how": "phrase",
                "phrase_hits": hits,
                "matches": [
                    {"section": where(ln + 1), "line": ln + 1,
                     "text": (lines[ln].strip()[:120] if len(lines[ln]) <= shapes.LINE_CLIP_CHARS
                              else (_long_line_window(lines[ln], phrase, ln + 1, 120) or lines[ln][:120]))}
                    for ln in found
                ],
                "start_line": a,
                "end_line": b,
                "verbatim": True,
                "content": excerpt,
            }
        phrase_missed = bool(quote)

    # 2. Section by title/path.
    if outline:
        nodes = docindex.find_nodes(outline, focus, limit=FOCUS_MATCH_ROWS)
        if nodes:
            node, anc = nodes[0]
            span_text_len = sum(len(lines[k]) + 1 for k in range(node.start - 1, min(node.end, total)))
            result: dict = {
                "how": "section",
                "matches": [
                    {"section": " > ".join(x.title for x in an + [n]), "start_line": n.start, "end_line": n.end}
                    for n, an in nodes
                ],
            }
            if span_text_len <= LOCAL_FOCUS_EXCERPT_MAX_CHARS:
                excerpt, a, b = _clip_excerpt(lines, node.start, node.end)
                result.update(start_line=a, end_line=b, verbatim=True, content=excerpt)
            else:
                head_end = node.children[0].start - 1 if node.children else node.end
                excerpt, a, b = _clip_excerpt(lines, node.start, min(head_end, node.start + 29))
                result.update(start_line=a, end_line=b, verbatim=True, content=excerpt,
                              section_lines=[node.start, node.end])
                if node.children:
                    result["outline"] = docindex.fold(node.children, OUTLINE_BUDGET // 2)
            return result

    # 3. Ranked sections.
    terms = docindex.terms(focus)
    if not terms:
        return None
    sections: list[tuple[str, int, int]] = []
    if outline:
        for addr, a0, b0 in outline.leaves():
            sections.extend((addr, wa, wb) for _n, wa, wb in docindex.windows(a0, b0, LOCAL_FOCUS_WINDOW))
        if outline.nodes and outline.nodes[0].start > 1:
            sections.extend(
                ("front matter", wa, wb) for _n, wa, wb in docindex.windows(1, outline.nodes[0].start - 1, LOCAL_FOCUS_WINDOW)
            )
    else:
        covered = bytearray(total + 2)
        for name, span in symbols.items():
            try:
                a0, b0 = int(span[0]), int(span[-1])
            except (TypeError, ValueError, IndexError):
                continue
            sections.append((str(name), a0, b0))
            covered[max(0, a0):min(total, b0) + 1] = b"\x01" * (min(total, b0) + 1 - max(0, a0))
        if not sections:
            sections = docindex.windows(1, max(total, 1), LOCAL_FOCUS_WINDOW)
        else:
            # Module-level code (constants, config, top-level statements) sits outside
            # every symbol span; rank it too, in windows over each gap.
            ln = 1
            while ln <= total:
                if covered[ln]:
                    ln += 1
                    continue
                a0 = ln
                while ln <= total and not covered[ln]:
                    ln += 1
                b0 = ln - 1
                while a0 <= b0 and not lines[a0 - 1].strip():
                    a0 += 1
                while b0 >= a0 and not lines[b0 - 1].strip():
                    b0 -= 1
                if a0 <= b0:
                    sections.extend(docindex.windows(a0, b0, LOCAL_FOCUS_WINDOW, " (module level)"))
    ranked = docindex.bm25_sections(doc.index, sections, terms, top=LOCAL_FOCUS_TOP)
    if not ranked:
        return None
    best_score, best_name, a, b = ranked[0]
    narrowed = None
    if sum(len(lines[k]) + 1 for k in range(a - 1, min(b, total))) > LOCAL_FOCUS_EXCERPT_MAX_CHARS:
        sub = docindex.bm25_sections(
            doc.index, [(best_name, wa, wb) for _n, wa, wb in docindex.windows(a, b, LOCAL_FOCUS_WINDOW)],
            terms, top=1, name_boost=0.0,
        )
        if sub:
            narrowed = (a, b)
            a, b = sub[0][2], sub[0][3]
    excerpt, start, end = _clip_excerpt(lines, a, b)
    result = {
        "how": "ranked",
        "matches": [
            {"section": name, "start_line": sa, "end_line": sb, "score": sc}
            for sc, name, sa, sb in ranked
        ],
        "start_line": start,
        "end_line": end,
        "verbatim": True,
        "content": excerpt,
    }
    if narrowed:
        result["section_lines"] = list(narrowed)
    if phrase_missed:
        result["phrase_hits"] = 0
    return result


def _local_summary(content: str, meta: dict) -> str:
    lang = meta.get("language") or "text"
    loc = meta.get("loc") or (_line_count(content))
    if meta.get("parse_error"):
        return (
            f"{lang} file that does not parse ({meta['parse_error']}) — "
            "no structure could be extracted. Read a line range to see the source."
        )
    bits = [f"{lang} file, {loc} lines"]
    if meta.get("symbols_total"):
        bits.append(f"{meta['symbols_total']} symbols, grouped by name prefix with start lines")
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
    if meta.get("sections"):
        bits.append(f"{meta['sections']} sections in the outline (focus=<title or path> opens one)")
    table = meta.get("table")
    if table:
        bits.append(f"{table['columns']} columns x {table['rows_scanned']:,} rows"
                    + ("" if table.get("scanned_all") else " scanned (more follow)")
                    + "; column profiles below (type, range, distinct, top values)")
    jl = meta.get("jsonl")
    if jl:
        bits.append(f"{jl['records']:,} JSON records; record shape: {jl['record_shape']}")
    if meta.get("shape"):
        bits.append(f"shape: {meta['shape']}")
    ll = meta.get("long_lines")
    if ll:
        bits.append(f"{ll['count']} line(s) longer than {shapes.LINE_CLIP_CHARS:,} chars (longest {ll['longest_chars']:,}, "
                    "likely minified/generated): range reads clip them; focus='<phrase>' returns a window around a match")
    lt = meta.get("line_templates")
    if lt:
        bits.append(f"{lt['templates']} line templates over {lt['lines_scanned']:,} lines"
                    + (" (partial scan)" if lt.get("partial") else "")
                    + "; frequent templates, then rare lines with line numbers")
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
    if start_line and end_line and end_line < start_line:
        # A reversed range (5..2) means the span between them, not just line 5.
        start_line, end_line = end_line, start_line
    start = min(max(1, start_line or 1), total)
    end = min(max(end_line or total, start), total)

    kept: list[str] = []
    size = 0
    last = start - 1
    for offset, line in enumerate(lines[start - 1 : end], start=start):
        # A minified line can be megabytes: clip it (with a marker) so one line
        # cannot flood the context. Clipped text is not exact, so callers check
        # _has_clipped() and report verbatim=False.
        line, _ = shapes.clip_line(line, offset)
        cost = len(line) + 1
        if kept and size + cost > RANGE_MAX_CHARS:
            return "\n".join(kept), start, last, True
        kept.append(line)
        size += cost
        last = offset
    return "\n".join(kept), start, max(last, start), False


_CLIP_MARK = re.compile(r" \.\.\.\[line \d+ continues: [\d,]+ more chars")


def _has_clipped(text: str) -> bool:
    return bool(text) and "...[line " in text and bool(_CLIP_MARK.search(text))


# Text files above this size are not loaded whole: range reads stream, and an
# unfocused read describes the head plus a bounded template digest.
HUGE_TEXT_BYTES = 64 << 20
# Bumped when unfocused summaries gain content, so cached older ones are rebuilt.
INDEX_VERSION = 3
# Files above this size are hashed by stat, not content.
HASH_STREAM_MAX_BYTES = 256 << 20


def _stream_hash(p: Path, size: int) -> str:
    if size > HASH_STREAM_MAX_BYTES:
        st = p.stat()
        return f"stat:{st.st_size}:{st.st_mtime_ns}"
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _binary_result(p: Path, fmt: str, size: int, *, asked_text: bool) -> str:
    desc = shapes.describe_binary(p, fmt, size)
    payload: dict[str, Any] = {
        "path": str(p),
        "mode": "binary",
        "format": desc["label"],
        "bytes": size,
        "size": shapes.human_size(size),
        "summary_source": "local_binary",
        "verbatim": False,
    }
    if desc["details"]:
        payload["details"] = desc["details"]
    if desc["outline"]:
        payload["outline"] = desc["outline"]
    note = "Binary file: decoded structurally (no text body)."
    if asked_text:
        note += " raw/start_line/end_line return no bytes for binary files; this digest replaces them."
    if desc.get("hint"):
        note += " " + desc["hint"]
    payload["note"] = note
    return json.dumps(payload, ensure_ascii=False)


def _data_digest(language: str, content: str, metadata: dict) -> None:
    """Add a shape digest for data and log files to ``metadata`` (in place).

    CSV/TSV: column profile. JSON Lines: merged record schema. JSON: whole-document
    schema (parsed in full, not the 2 MB index prefix). Logs and other repetitive
    text: line templates with counts, and the rare lines with their line numbers.
    """
    if len(content) <= SMALL_FILE_WHOLE_MAX_CHARS:
        return
    longest = max(map(len, content.split("\n", 4096)[:4096]), default=0)
    if longest > shapes.LINE_CLIP_CHARS:
        n_long = sum(1 for ln in content.split("\n") if len(ln) > shapes.LINE_CLIP_CHARS)
        metadata["long_lines"] = {"count": n_long, "longest_chars": max(len(ln) for ln in content.split("\n"))}
    if language in ("csv", "tsv"):
        lines = content.splitlines()
        d = shapes.table_digest(iter(lines), "\t" if language == "tsv" else None, sample_text=content[:20_000])
        if d and "error" not in d:
            metadata["outline"] = d.pop("outline")
            metadata["table"] = d
            metadata.pop("index_truncated", None)
            return
    if language == "jsonl":
        d = shapes.jsonl_digest(content.splitlines())
        if d:
            metadata["jsonl"] = d
            metadata.pop("index_truncated", None)
            return
    if language == "json" and len(content) <= HUGE_TEXT_BYTES:
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, RecursionError) as e:
            metadata["parse_error"] = str(e)[:200]
            return
        metadata["parse_error"] = None
        metadata.pop("index_truncated", None)
        if isinstance(data, dict):
            metadata["keys"] = list(data.keys())[:40]
        metadata["shape"] = shapes.json_shape(data)
        return
    if language in ("log", "text") and content.count("\n") >= 50:
        d = shapes.template_digest(content.splitlines())
        if d and (language == "log" or not metadata.get("outline")):
            metadata["outline"] = d.pop("rows")
            metadata.pop("sections", None)
            metadata.pop("index_truncated", None)
            metadata["line_templates"] = d


def _huge_text_result(p: Path, size: int, head: bytes, start_line: Optional[int], end_line: Optional[int],
                      focus: Optional[str], raw: bool) -> str:
    """Text too large to load whole: stream a range, or describe head + templates."""
    import itertools

    language = _detect_language(p)
    base: dict[str, Any] = {"path": str(p), "hash": _stream_hash(p, size), "language": language,
                            "bytes": size, "size": shapes.human_size(size)}
    with open(p, "r", encoding="utf-8", errors="replace", newline=None) as f:
        if start_line is not None or end_line is not None:
            a = max(1, start_line or 1)
            b = max(a, end_line or a + 199)
            kept, total_chars, last, cut = [], 0, a - 1, False
            for n, line in enumerate(itertools.islice(f, a - 1, b), start=a):
                line, _ = shapes.clip_line(line.rstrip("\n"), n)
                if kept and total_chars + len(line) + 1 > RANGE_MAX_CHARS:
                    cut = True
                    break
                kept.append(line)
                total_chars += len(line) + 1
                last = n
            body = "\n".join(kept)
            payload = {**base, "start_line": a, "end_line": max(last, a), "verbatim": not _has_clipped(body),
                       "content": body}
            if cut:
                payload["truncated"] = (f"stopped at line {last}: range exceeds {RANGE_MAX_CHARS:,} chars. "
                                        "Request the rest as a further range.")
            if not kept:
                payload["note"] = f"No lines at {a}: the file has fewer lines."
            return json.dumps(payload, ensure_ascii=False)
        digest = shapes.template_digest(f)
    rows = digest.pop("rows") if digest else []
    head_text = head.decode("utf-8", "replace").splitlines()[:8]
    payload = {**base, "mode": "huge_text", "summary_source": "local_stream", "verbatim": False,
               "head": [shapes.clip_line(h, i + 1, 200)[0] for i, h in enumerate(head_text)]}
    if digest:
        payload["line_templates"] = digest
        payload["outline"] = rows
    note = (f"File is {shapes.human_size(size)}: too large to load whole. Use start_line/end_line "
            "(streamed) or SearchContent for exact lines.")
    if raw or focus:
        note = "raw/focus are not available above " + shapes.human_size(HUGE_TEXT_BYTES) + ". " + note
    payload["note"] = note
    return json.dumps(payload, ensure_ascii=False)


async def run_file_reader(
    path: str,
    focus: Optional[str] = None,
    raw: bool = False,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
    client: Optional[DeepSeekClient] = None,
) -> str:
    """
    Main entry for FileReader subagent.
    Returns a compact JSON string for the orchestrator.
    """
    root = get_project_root()
    p, err = confine_or_error(path, root=root)
    if err or p is None:
        return json.dumps({"error": err or "Path is outside project root"})

    if p.is_dir():
        return json.dumps({"error": f"Is a directory, not a file: {p} (ListDir lists it)"})
    if not p.exists() or not p.is_file():
        return json.dumps({"error": f"File not found: {p}"})

    # Sniff before loading: a binary file gets a structural digest (never
    # mojibake), and a huge text file is streamed rather than read whole.
    try:
        size = p.stat().st_size
        with open(p, "rb") as f:
            head = f.read(shapes.SNIFF_BYTES)
    except Exception as e:
        return json.dumps({"error": f"Cannot read file: {e}"})
    fmt = shapes.sniff(head, p)
    if fmt:
        return _binary_result(p, fmt, size, asked_text=bool(raw or start_line or end_line))
    if size > HUGE_TEXT_BYTES:
        return _huge_text_result(p, size, head, start_line, end_line, focus, raw)
    try:
        data = p.read_bytes()
    except Exception as e:
        return json.dumps({"error": f"Cannot read file: {e}"})
    # Same text read_text() gives (universal newlines), but the line-ending
    # style is reported: Edit keeps CRLF, and a model that knows the file is
    # CRLF does not reach for byte-level sed/python rewrites.
    encoding = shapes.text_encoding(head)
    if encoding not in ("utf-16", "utf-32"):
        encoding = "utf-8"  # a UTF-8 BOM stays in the text, as Edit sees it
    content = data.decode(encoding, errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    eol = edit_engine.detect_eol(data.decode("latin-1"))
    eol_tag = {"line_endings": "crlf" if eol == "\r\n" else "mixed"} if eol in ("\r\n", "mixed") else {}

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
            "total_lines": _line_count(content),
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
        total = _line_count(content)
        payload = {
            "path": str(p),
            "hash": content_hash,
            "language": _detect_language(p),
            "start_line": start,
            "end_line": end,
            "total_lines": total,
            **eol_tag,
            "verbatim": not _has_clipped(excerpt),
            "content": excerpt,
        }
        if encoding != "utf-8":
            payload["encoding"] = encoding
        if not payload["verbatim"]:
            payload["note"] = ("Very long line(s) clipped at "
                               f"{shapes.LINE_CLIP_CHARS:,} chars; focus='<phrase>' shows a window around a match.")
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
            "total_lines": _line_count(content),
            "bytes": len(content),
            **eol_tag,
            "content": content,
        }, ensure_ascii=False)

    # A small file read without a focus: the body is the answer. A summary
    # here (measured: 888 chars for a 630-char file) is followed by a raw
    # re-read in most runs, so the summary only adds a hop.
    if not focus and content.strip() and len(content) <= SMALL_FILE_WHOLE_MAX_CHARS:
        return json.dumps({
            "path": str(p),
            "hash": content_hash,
            "language": _detect_language(p),
            "mode": "small_whole",
            "verbatim": True,
            "total_lines": _line_count(content),
            **eol_tag,
            **({"encoding": encoding} if encoding != "utf-8" else {}),
            "content": content,
        }, ensure_ascii=False)

    # Cache hit. A focused read is a different question about the same bytes, so
    # a generic cached summary would silently answer the wrong one — the key has
    # to carry the focus.
    cache_key = content_hash if not focus else hashlib.sha256(
        f"{content_hash}\x00{focus}".encode("utf-8")
    ).hexdigest()
    # Local focus ranking (single-agent default) is free and deterministic,
    # and its excerpt is not stored in the summary cache — never serve a
    # cached focus summary in its place.
    local_focus_mode = bool(focus) and not getattr(settings, "subagents_enabled", False)
    cached = None if local_focus_mode else cache.get_file_summary(str(p), content_hash=cache_key)
    if cached and not focus and (cached.get("metadata") or {}).get("index_v") != INDEX_VERSION:
        cached = None  # written by an older indexer (no data digests): rebuild
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
        _data_digest(language, content, metadata)
        metadata["index_v"] = INDEX_VERSION
        shown = _payload_meta(metadata)
        summary = _local_summary(content, shown)
        result = {
            "path": str(p),
            "hash": content_hash,
            "from_cache": False,
            "summary_source": metadata.get("summary_source") or "local_index",
            "metadata": shown,
            "summary": summary,
            "note": SYMBOL_MAP_NOTE if metadata.get("symbols") else RANGE_NOTE,
            **eol_tag,
        }
        if metadata.get("index_truncated"):
            result["summary_covers"] = {
                "chars_summarised": LOCAL_INDEX_MAX_CHARS,
                "chars_total": len(content),
                "complete": False,
            }
        cache.put_file_summary(str(p), summary, shown, None, content_hash)
        return json.dumps(result, ensure_ascii=False)

    symbol = _resolve_symbol_focus(focus, metadata.get("symbols") or {})
    if symbol:
        span = (metadata.get("symbols") or {}).get(symbol) or [1, 1]
        start, end = int(span[0]), int(span[1] if len(span) > 1 else span[0])
        excerpt, start, end, cut_short = _slice_lines(content, start, end)
        shown = _payload_meta(metadata, focused=True)
        summary = (
            f"{_local_summary(content, shown)} Focused on {symbol} "
            f"(lines {start}-{end})."
        )
        result = {
            "path": str(p),
            "hash": content_hash,
            "from_cache": False,
            "summary_source": "local_symbol",
            "focus": focus,
            "symbol": symbol,
            "metadata": shown,
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
        elif excerpt:
            # Oversized body: hand back its verbatim opening (signature, locals,
            # first statements) instead of a bare pointer, so a "where/what is X"
            # question rarely needs a second read. section_lines keeps the full span.
            head, size = [], 0
            for row in excerpt.split("\n"):
                if head and size + len(row) + 1 > LOCAL_SYMBOL_BODY_MAX_CHARS:
                    break
                head.append(row)
                size += len(row) + 1
            result.update(verbatim=True, content="\n".join(head),
                          end_line=start + len(head) - 1, section_lines=[start, end])
            result["note"] = (
                f"{symbol} spans lines {start}-{end}; its first {len(head)} lines are included "
                "verbatim. Read section_lines for the rest. " + SYMBOL_MAP_NOTE
            )
        else:
            result["verbatim"] = False
            result["note"] = (
                f"Focused on {symbol} lines {start}-{end} "
                f"({end - start + 1} lines). Re-read that range for verbatim "
                "source. " + SYMBOL_MAP_NOTE
            )
        cache.put_file_summary(
            str(p), result["summary"], result["metadata"], None, cache_key
        )
        return json.dumps(result, ensure_ascii=False)

    # Single-agent default: answer the focus locally (ranked sections), with
    # no model call. Subagent mode keeps the Flash summary path below.
    if not getattr(settings, "subagents_enabled", False):
        doc = docindex.doc_for(p, content, _doc_kind(language))
        ranked = _local_focus(content, metadata, focus, doc=doc)
        shown = _payload_meta(metadata, focused=True)
        result = {
            "path": str(p),
            "hash": content_hash,
            "from_cache": False,
            "summary_source": "local_focus",
            "focus": focus,
            "metadata": shown,
            "summary": _local_summary(content, shown),
            "total_lines": len(content.splitlines()),
        }
        if ranked is None:
            result["note"] = (
                "No section matched the focus terms. " + SYMBOL_MAP_NOTE
            )
        elif ranked.pop("note", None):
            result.update(ranked)
            result["note"] = (
                "Small file: returned whole and verbatim instead of ranked. "
                + SYMBOL_MAP_NOTE
            )
        else:
            how = ranked.pop("how", "ranked")
            result.update(ranked)
            if _has_clipped(str(result.get("content") or "")):
                result["verbatim"] = False
            if how == "phrase":
                lead = "Exact phrase found; matches give each hit's line and section. The passage around the first hit is included verbatim."
            elif how == "section":
                lead = "Section matched by title/path. " + (
                    "Its opening is included verbatim; read section_lines for the rest, or focus a sub-section from the outline."
                    if result.get("section_lines") else "Included verbatim.")
            else:
                lead = ("Sections ranked by word match against the focus (best first). The best one is included verbatim"
                        + (" (narrowed to its best window inside section_lines)." if result.get("section_lines") else "."))
                if result.get("phrase_hits") == 0:
                    lead = "The quoted phrase does not occur verbatim. " + lead
            result["note"] = lead + " " + SYMBOL_MAP_NOTE
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
            model=MODEL,
            instructions=FILE_READER_SYSTEM,
            input=[{"role": "user", "content": user_msg}],
            max_output_tokens=max(1, int(settings.file_summary_max_tokens or 400)),
            temperature=0.1,
            # Structural extraction needs no deliberation, and "effort: none" is
            # what actually switches thinking off on this endpoint — the old
            # chat-completions "thinking" toggle is accepted and ignored here.
            reasoning=reasoning_param(enabled=False),
        )
        get_usage().record(MODEL, resp.get("usage"), scope="subagent")
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
        "metadata": _payload_meta(_merge_metadata(metadata, data.get("metadata")), focused=True),
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
    cache.put_file_summary(str(p), result["summary"], result["metadata"], None, cache_key)
    return json.dumps(result, ensure_ascii=False)

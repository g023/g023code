"""Tool-result codec: the densest faithful form of a tool result for history.

Every byte stored in history is paid once as a cache miss and then again as a
cache hit on every later step, so the encoding is applied exactly once, at the
orchestrator's history boundary. The UI and the working set still see the raw
payload. The codec is deterministic and idempotent. Re-encoding its own output
returns it unchanged, so history bytes never shift.

What it removes is either derivable or never read:
- the tool's echoed arguments (query, path, max_matches, ...);
- duplicate views (``matches`` == ``samples``, ``files`` == the sample files,
  ``metadata.symbols`` == the outline spans, an auto-generated ``summary``);
- internal provenance (``from_cache``, ``summary_source``, ``backend``);
- null and empty values, absolute root prefixes, and the tail of 64-hex digests
  (12 hex chars are enough for a working-set citation);
- the fixed per-call ReadFile notes, which now live once in the tool schema.

What it adds is layout, not content. Search samples become a grep-style block,
code outlines become indented text, and verbatim ``content`` follows a
one-line JSON header raw, so an Edit copies exact bytes instead of
un-escaping JSON.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

# Per-call notes the ReadFile schema now states once (see tools/schemas.py).
_STATIC_NOTES = (
    "metadata.symbols maps name → [start_line, end_line]. "
    "Re-read those lines for verbatim source.",
    "Summary, not source. Re-read with start_line/end_line for verbatim text.",
)
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
HASH_CHARS = 12
# Below this, raw content stays inside the JSON header (no layout gain).
RAW_BODY_MIN = 120
SEARCH_DROP = frozenset(
    {"query", "path", "file_glob", "max_matches", "backend", "files_scanned",
     "matches", "files", "metadata_summary", "samples"}
)
READ_DROP = frozenset({"from_cache", "summary_source", "index_v"})


def _root_prefix(root: Optional[Path]) -> str:
    if root is None:
        try:
            from .config import get_project_root

            root = get_project_root()
        except Exception:
            return ""
    try:
        return str(Path(root).resolve()).rstrip("/") + "/"
    except OSError:
        return ""


def _clean(value: Any, prefix: str, key: str = "") -> Any:
    """Drop empties, relativize root paths, shorten digests. Recursive."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            v = _clean(v, prefix, k)
            if v is None or v == "" or v == [] or v == {}:
                continue
            out[k] = v
        return out
    if isinstance(value, list):
        return [_clean(v, prefix) for v in value]
    if isinstance(value, str):
        if key in ("hash", "sha256") and _HEX64.match(value):
            return value[:HASH_CHARS]
        # Only whole-value paths: file text (content, match lines) is data
        # and must reach the model byte-exact.
        if prefix and key not in ("content", "match"):
            if value == prefix.rstrip("/"):
                return "."
            if value.startswith(prefix):
                return value[len(prefix):]
    return value


def _header(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _span(lines: Any) -> str:
    if isinstance(lines, list) and lines:
        a = lines[0]
        b = lines[1] if len(lines) > 1 else a
        return f"{a}-{b}" if a != b else f"{a}"
    return ""


def _outline(meta: dict) -> tuple[list[str], set[str]]:
    """Indented outline of classes/functions and the symbol names it covers."""
    rows: list[str] = []
    covered: set[str] = set()

    def fn_row(fn: dict, indent: str, owner: str = "") -> None:
        name = str(fn.get("name") or "")
        if not name:
            return
        covered.add(f"{owner}.{name}" if owner else name)
        sig = str(fn.get("signature") or name)
        doc = fn.get("doc")
        row = f"{indent}{sig}  L{_span(fn.get('lines'))}"
        rows.append(row + (f"  # {doc}" if doc else ""))

    for cls in meta.get("classes") or []:
        if not isinstance(cls, dict) or not cls.get("name"):
            continue
        covered.add(str(cls["name"]))
        rows.append(f"class {cls['name']}  L{_span(cls.get('lines'))}")
        for m in cls.get("methods") or []:
            if isinstance(m, dict):
                fn_row(m, "  ", str(cls["name"]))
            elif isinstance(m, str):
                rows.append(f"  {m}")
    for fn in meta.get("functions") or []:
        if isinstance(fn, dict):
            fn_row(fn, "")
    return rows, covered


def _strip_notes(note: Any) -> Any:
    if not isinstance(note, str):
        return note
    for fixed in _STATIC_NOTES:
        note = note.replace(fixed, "")
    return " ".join(note.split()) or None


def encode_read(data: dict) -> str:
    """ReadFile: JSON header, then an outline, then raw verbatim content."""
    data = dict(data)
    meta = data.pop("metadata", None)
    meta = dict(meta) if isinstance(meta, dict) else {}
    source = str(data.get("summary_source") or meta.get("summary_source") or "")
    for k in READ_DROP:
        data.pop(k, None)
        meta.pop(k, None)
    # The local summary is generated from the metadata kept below.
    if source.startswith("local") and not meta.get("parse_error"):
        data.pop("summary", None)
    note = _strip_notes(data.pop("note", None))
    if note:
        data["note"] = note
    rows, covered = _outline(meta)
    # Folded outlines (text sections, prefix-grouped symbols) arrive pre-rendered.
    for extra in (meta.pop("outline", None), data.pop("outline", None)):
        if isinstance(extra, list):
            rows.extend(str(r) for r in extra)
    meta.pop("classes", None)
    meta.pop("functions", None)
    symbols = meta.pop("symbols", None)
    if isinstance(symbols, dict):
        extra = {k: v for k, v in symbols.items() if k not in covered}
        if extra:
            meta["symbols"] = extra
    imports = meta.pop("imports", None)
    uses = data.pop("uses", None)
    if meta:
        # language/loc/headings/keys/... stay; flatten into the header.
        for k, v in meta.items():
            data.setdefault(k, v)
    content = data.pop("content", None)
    raw_body = isinstance(content, str) and len(content) >= RAW_BODY_MIN
    if isinstance(content, str) and not raw_body:
        data["content"] = content
    parts = [_header(data)]
    if imports:
        parts.append("imports: " + ", ".join(str(i) for i in imports))
    if isinstance(uses, list) and uses:
        # Callee digests (callees.py): what the project-local imports are.
        parts.append("uses:")
        parts.extend(f"  {u}" for u in uses)
    parts.extend(rows)
    if raw_body:
        parts.append("-----")
        parts.append(content)
    return "\n".join(parts)


def _render_samples(samples: list) -> list[str]:
    """grep -C style: a file heading, then ``N:`` for hits and ``N-`` for context.

    A hit's address (``in``: section path or enclosing function) heads its block as
    ``@ address``, once per change, so "where" needs no second read.
    """
    by_file: dict[str, dict[int, tuple[str, bool, str]]] = {}
    order: list[str] = []
    for s in samples:
        if not isinstance(s, dict):
            continue
        f = str(s.get("file") or "?")
        if f not in by_file:
            by_file[f] = {}
            order.append(f)
        rows = by_file[f]
        try:
            n = int(s.get("line") or 0)
        except (TypeError, ValueError):
            n = 0
        before = s.get("context_before") or []
        after = s.get("context_after") or []
        for i, text in enumerate(before):
            rows.setdefault(n - len(before) + i, (str(text), False, ""))
        rows[n] = (str(s.get("match") or ""), True, str(s.get("in") or ""))
        for i, text in enumerate(after):
            rows.setdefault(n + 1 + i, (str(text), False, ""))
    out: list[str] = []
    for f in order:
        out.append(f"## {f}")
        nums = sorted(by_file[f])
        blocks: list[list[int]] = []
        for n in nums:
            if blocks and n == blocks[-1][-1] + 1:
                blocks[-1].append(n)
            else:
                blocks.append([n])
        last_where = ""
        for k, block in enumerate(blocks):
            if k:
                out.append("--")
            wheres: list[str] = []
            for n in block:
                w = by_file[f][n][2]
                if w and w not in wheres:
                    wheres.append(w)
            if wheres and " | ".join(wheres) != last_where:
                last_where = " | ".join(wheres)
                out.append(f"@ {last_where}")
            for n in block:
                text, hit, _w = by_file[f][n]
                out.append(f"{n}{':' if hit else '-'} {text}".rstrip())
    return out


def _render_peek(peek: dict) -> list[str]:
    """The peeked definition body, indentation kept, ``N|`` line numbers."""
    try:
        start = int(peek.get("start") or 1)
    except (TypeError, ValueError):
        start = 1
    lines = [str(x) for x in peek.get("lines") or []]
    end = start + len(lines) - 1
    out = [f"## definition {peek.get('file')}:{start}-{end}"]
    out.extend(f"{start + i}| {text}".rstrip() for i, text in enumerate(lines))
    more = peek.get("more_lines")
    if more:
        out.append(f"(+{more} more lines; ReadFile from line {end + 1})")
    return out


def encode_search(data: dict) -> str:
    samples = data.get("samples")
    if samples is None:
        samples = data.get("matches")
    head = {k: v for k, v in data.items() if k not in SEARCH_DROP}
    if isinstance(samples, list):
        # "all" says the list is exhaustive, so no confirming re-search
        # (live bench: the model re-grepped a complete 10/10 result).
        complete = not head.get("truncated") and head.get("total") == len(samples)
        head["shown"] = "all" if complete else len(samples)
    if not head.get("truncated"):
        head.pop("truncated", None)
    unshown = head.pop("unshown_files", None)
    near = head.pop("did_you_mean", None)
    peek = head.pop("definition", None)
    parts = [_header(head)]
    if isinstance(samples, list):
        parts.extend(_render_samples(samples))
    if isinstance(peek, dict) and isinstance(peek.get("lines"), list):
        parts.extend(_render_peek(peek))
    if isinstance(unshown, dict) and unshown:
        parts.append("more files (hits): " + ", ".join(f"{f} ({n})" for f, n in unshown.items()))
    if isinstance(near, list) and near:
        rows = []
        for n in near:
            if isinstance(n, dict) and n.get("name"):
                where = str(n.get("path") or "?") + (f":{n['line']}" if n.get("line") else "")
                rows.append(f"{n['name']} ({where})")
        if rows:
            parts.append("did you mean: " + ", ".join(rows))
    return "\n".join(parts)


# Pytest/unittest banners: "===== 3 passed in 0.1s =====" -> "== 3 passed in 0.1s ==".
_RULER = re.compile(r"^([=_-])\1{4,} ?(\S.*?) ?\1{5,}$", re.MULTILINE)
# Rulers are shrunk only in test-runner output. Elsewhere they are data: a
# `cat README.rst` must show the setext underline byte-exact, or an Edit built
# from it cannot match the file.
_TEST_OUTPUT = re.compile(
    r"test session starts|^Ran \d+ tests? in |\b\d+ (?:passed|failed|errors?)\b", re.MULTILINE)
_BARE_RULER = re.compile(r"^([=_-])\1{9,}$", re.MULTILINE)
WRITE_DROP = frozenset({"backup"})


def _lean() -> bool:
    try:
        from .config import settings

        return bool(getattr(settings, "lean_codec", False))
    except Exception:
        return False


def collapse_rulers(text: str) -> str:
    """Shorten banner rulers to two characters a side; idempotent."""
    text = _RULER.sub(lambda m: f"{m.group(1) * 2} {m.group(2)} {m.group(1) * 2}".replace("  ", " "), text)
    return _BARE_RULER.sub(lambda m: m.group(1) * 3, text)


def encode_bash(data: dict) -> str:
    """Bash: JSON header (exit code, notes), then stdout/stderr raw, not JSON-escaped."""
    head = {k: v for k, v in data.items() if k not in ("stdout", "stderr", "changed", "tests")}
    parts = []
    for key in ("stdout", "stderr"):
        body = data.get(key)
        if not isinstance(body, str) or not body:
            continue
        if _TEST_OUTPUT.search(body):
            body = collapse_rulers(body)
        if "\n" in body.strip() or len(body) >= RAW_BODY_MIN:
            parts += [f"[{key}]", body.rstrip("\n")]
        else:
            head[key] = body
    changed = data.get("changed")
    if isinstance(changed, str) and changed:
        # Files the command changed, diffed against what the model last saw (views.py).
        parts += ["[changed since you read]", changed]
    tests = data.get("tests")
    if isinstance(tests, list) and tests:
        # Test oracle (testoracle.py): facts the command's own view hid.
        parts += ["[tests]"] + [str(t) for t in tests]
    return "\n".join([_header(head)] + parts)


def encode_tool_result(name: str, text: str, root: Optional[Path] = None) -> str:
    """Encode one tool result for history. Non-JSON text passes through."""
    if not text or not text.lstrip().startswith("{"):
        return text
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Already encoded (JSON header + body) or plain text: leave as is.
        return text
    if not isinstance(data, dict):
        return text
    if data.get("error"):
        # Errors are short and their wording drives the nudge text; keep the
        # message, drop only the root prefix.
        return _header(_clean(data, _root_prefix(root)))
    data = _clean(data, _root_prefix(root))
    if name in ("Edit", "WriteFile") and isinstance(data.get("check"), dict):
        # A write-carried check (hops.py): the write's header, then the check
        # rendered as a Bash result (raw output, not JSON-escaped).
        check = data.pop("check")
        if _lean():
            for k in WRITE_DROP:
                data.pop(k, None)
        return _header(data) + "\n[check]\n" + encode_bash(check)
    if _lean():
        if name in ("Edit", "WriteFile"):
            for k in WRITE_DROP:
                data.pop(k, None)
        elif name == "Bash" and ("stdout" in data or "stderr" in data or "changed" in data or "tests" in data):
            return encode_bash(data)
    if name == "SearchContent" and ("samples" in data or "matches" in data or "total" in data):
        return encode_search(data)
    if name == "ReadFile":
        return encode_read(data)
    if name == "ListDir" and isinstance(data.get("lines"), list):
        lines = data.pop("lines")
        return "\n".join([_header(data)] + [str(x) for x in lines])
    if name == "Glob" and isinstance(data.get("files"), list):
        files = data.pop("files")
        digest = data.pop("digest", None)
        parts = [_header(data)]
        if isinstance(digest, list) and digest:
            parts += [str(x) for x in digest]
            parts.append(f"first {len(files)}:")
        parts += [str(f) for f in files]
        return "\n".join(parts)
    if name == "ListDir" and isinstance(data.get("entries"), list):
        # One row per entry ("name/" for dirs, "name  size" for files): the
        # same facts as the entry objects at a third of the characters.
        rows = []
        for e in data.pop("entries"):
            if not isinstance(e, dict):
                continue
            row = str(e.get("name", "?"))
            if e.get("error"):
                row += f"  (error: {e['error']})"
            elif e.get("size") is not None:
                row += f"  {e['size']}"
            rows.append(row)
        return "\n".join([_header(data)] + rows)
    return _header(data)

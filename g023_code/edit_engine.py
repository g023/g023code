"""Edit engine: byte-faithful string edits that tell the model where it missed.

Pure functions behind the Edit and WriteFile tools (tools/registry.py); no I/O except
``load``/``save``. A failed edit is the most expensive kind of tool call: it costs the
miss, a re-read and a retry, and each of those hops re-reads the whole prefix. So the
engine resolves what it safely can, and when it cannot, the error carries what the
retry needs.

  1. Fidelity. Files are read as bytes. The line-ending style (LF / CRLF / mixed) and
     a UTF-8 BOM are preserved, and the write is atomic (temp file + os.replace,
     mode kept). Path.read_text/write_text would silently turn a CRLF file into LF,
     and on Windows would turn every LF file into CRLF.
  2. A match ladder, applied only when the result is unique:
        exact -> line-ending-normalised -> trailing-whitespace-insensitive lines
              -> indentation drift: an affine indent map (shift, or 2<->4 spaces) proven
                 on every line; new_string is re-indented through the same map
     ``line=N`` picks one occurrence when old_string is not unique.
  3. Misses carry the fix. "Not unique" lists each occurrence's line and enclosing
     symbol. "Not found" returns the closest region verbatim, with its line span and
     similarity, so the retry needs no ReadFile.
  4. Receipt. A success reports the changed line span and how it matched, plus a
     syntax error only if the edit introduced one (Python / JSON).
"""
from __future__ import annotations

import ast
import difflib
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

BOM = "﻿"
NEAR_MISS_MAX_CHARS = 1_200
NEAR_MISS_MIN_RATIO = 0.55
NEAR_MISS_MAX_FILE = 2_000_000
OCCURRENCES_SHOWN = 8


@dataclass
class FileText:
    """A text file as the model sees it, plus what is needed to write it back byte-faithfully."""

    text: str  # LF-normalised when the file is pure CRLF; raw otherwise
    eol: str  # "\n", "\r\n", or "mixed"
    bom: bool

    def encode(self, text: str) -> bytes:
        if self.eol == "\r\n":
            text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        return ((BOM if self.bom else "") + text).encode("utf-8")


def detect_eol(raw: str) -> str:
    crlf = raw.count("\r\n")
    lf = raw.count("\n") - crlf
    if crlf and lf:
        return "mixed"
    return "\r\n" if crlf else "\n"


def decode(data: bytes) -> FileText:
    """Decode UTF-8 bytes (raises UnicodeDecodeError) into a :class:`FileText`."""
    raw = data.decode("utf-8")
    bom = raw.startswith(BOM)
    if bom:
        raw = raw[1:]
    eol = detect_eol(raw)
    text = raw.replace("\r\n", "\n") if eol == "\r\n" else raw
    return FileText(text=text, eol=eol, bom=bom)


def load(path: Path) -> FileText:
    return decode(Path(path).read_bytes())


def save(path: Path, data: bytes) -> None:
    """Atomic write: temp file in the same directory, then os.replace. Keeps the file mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".g023tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if path.exists():
            try:
                shutil.copymode(path, tmp)
            except OSError:
                pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def conform_new_content(content: str, existing: Optional[FileText]) -> bytes:
    """WriteFile bytes: keep an existing file's CRLF style and BOM when the model sent plain LF."""
    if existing is None:
        return content.encode("utf-8")
    if content.startswith(BOM):
        content = content[1:]
    if existing.eol == "\r\n" and "\r" not in content:
        return existing.encode(content)
    return ((BOM if existing.bom else "") + content).encode("utf-8")


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------


@dataclass
class EditResult:
    text: Optional[str] = None  # the new (normalised) text on success
    error: Optional[str] = None
    replacements: int = 0
    matched: str = "exact"  # exact | trailing_ws | indent
    start_line: int = 0  # 1-based span of the replacement in the NEW text
    end_line: int = 0
    hint: Optional[dict] = None  # extra fields for the error payload


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _positions(text: str, needle: str) -> list[int]:
    out, i = [], text.find(needle)
    while i != -1:
        out.append(i)
        i = text.find(needle, i + max(1, len(needle)))
    return out


def _span_after(new_text: str, start: int, inserted: str) -> tuple[int, int]:
    a = _line_of(new_text, start)
    b = a + max(0, inserted.count("\n") - (1 if inserted.endswith("\n") else 0))
    return a, max(a, b)


def _common_indent(lines: list[str]) -> str:
    ind = [re.match(r"[ \t]*", ln).group(0) for ln in lines if ln.strip()]
    if not ind:
        return ""
    return os.path.commonprefix(ind)


def _indent_width(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _indent_map(old_lines: list[str], window: list[str]) -> Optional[Callable[[str], str]]:
    """An affine map old-indent -> file-indent (file_w = a + f*old_w) proven on every line, or None.

    Covers a uniform shift (f=1) and a 2<->4 space drift (f=2 or 0.5). Tabs only map when
    both sides use the same leading characters up to a uniform prefix.
    """
    pairs = [(o, w) for o, w in zip(old_lines, window) if o.strip()]
    if not pairs:
        return None
    if any("\t" in o[: _indent_width(o)] or "\t" in w[: _indent_width(w)] for o, w in pairs):
        old_ind = _common_indent([o for o, _ in pairs])
        file_ind = _common_indent([w for _, w in pairs])
        if all(o[len(old_ind):_indent_width(o)] == w[len(file_ind):_indent_width(w)] for o, w in pairs):
            def tab_map(line: str) -> str:
                body = line[len(old_ind):] if line.startswith(old_ind) else line.lstrip(" \t")
                return file_ind + body
            return tab_map
        return None
    pts = sorted({(_indent_width(o), _indent_width(w)) for o, w in pairs})
    olds = sorted({p[0] for p in pts})
    if len(olds) != len(pts):
        return None  # one old width maps to two file widths
    if len(pts) == 1:
        f = 1.0
    else:
        (o1, w1), (o2, w2) = pts[0], pts[-1]
        f = (w2 - w1) / (o2 - o1)
    o0, w0 = pts[0]
    if f <= 0 or any(abs(w0 + f * (o - o0) - w) > 1e-9 for o, w in pts):
        return None

    def mapper(line: str) -> str:
        w = _indent_width(line)
        nw = w0 + f * (w - o0)
        if nw < 0 or abs(nw - round(nw)) > 1e-9:
            nw = max(0.0, float(w0 + (w - o0)))
        return " " * int(round(nw)) + line.lstrip(" \t")
    return mapper


def _line_windows(text: str, old: str, mode: str) -> list[tuple[int, int, Optional[Callable[[str], str]]]]:
    """Whole-line windows of ``text`` equal to ``old`` up to ``mode``.

    Returns ``(first_line_idx, n_lines, reindent)`` per match. ``mode`` is ``trailing_ws``
    (rstrip each line; reindent None) or ``indent`` (equal after strip, with a proven
    affine indentation map that new_string is passed through).
    """
    old_lines = old.rstrip("\n").split("\n")
    if not any(ln.strip() for ln in old_lines):
        return []
    lines = text.split("\n")
    n = len(old_lines)
    if mode == "trailing_ws":
        want = [ln.rstrip() for ln in old_lines]
        first = want[0]
        return [(i, n, None) for i in range(len(lines) - n + 1)
                if lines[i].rstrip() == first and [ln.rstrip() for ln in lines[i : i + n]] == want]
    want = [ln.strip() for ln in old_lines]
    k0 = next(j for j, ln in enumerate(want) if ln)
    out = []
    for i in range(len(lines) - n + 1):
        if lines[i + k0].strip() != want[k0]:
            continue
        window = lines[i : i + n]
        if [ln.strip() for ln in window] != want:
            continue
        mapper = _indent_map(old_lines, [ln.rstrip("\r") for ln in window])
        if mapper is not None:
            out.append((i, n, mapper))
    return out


def _apply_window(text: str, win: tuple, new: str) -> tuple[str, int, int]:
    i, n, mapper = win
    lines = text.split("\n")
    # Mixed-EOL files keep their raw "\r": a window whose lines end in CR gets CR back.
    cr = all(ln.endswith("\r") for ln in lines[i : i + n])
    new = new.replace("\r\n", "\n")
    new_lines = [] if new == "" else new.rstrip("\n").split("\n")
    if mapper is not None:
        new_lines = [mapper(ln) if ln.strip() else "" for ln in new_lines]
    if cr:
        new_lines = [ln + "\r" for ln in new_lines]
    out = lines[:i] + new_lines + lines[i + n :]
    a = i + 1
    return "\n".join(out), a, max(a, i + len(new_lines))


def apply_edit(
    text: str,
    old: str,
    new: str,
    *,
    replace_all: bool = False,
    line: Optional[int] = None,
    symbols_for: Optional[Callable[[], dict]] = None,
) -> EditResult:
    """Apply one edit to LF-normalised ``text``. Fail closed on anything ambiguous."""
    if old == "":
        return EditResult(error="old_string is empty")
    if "\r\n" in old and "\r\n" not in text:
        old = old.replace("\r\n", "\n")
        new = new.replace("\r\n", "\n")
    pos = _positions(text, old)
    if pos:
        if replace_all:
            out = text.replace(old, new)
            a = _line_of(out, text.find(old))
            return EditResult(text=out, replacements=len(pos), start_line=a,
                              end_line=a + new.count("\n"))
        if len(pos) > 1 and line:
            pick = [p for p in pos if _line_of(text, p) <= int(line) <= _line_of(text, p + len(old) - 1)
                    or _line_of(text, p) == int(line)]
            if len(pick) == 1:
                pos = pick
        if len(pos) == 1:
            p = pos[0]
            out = text[:p] + new + text[p + len(old):]
            a, b = _span_after(out, p, new)
            return EditResult(text=out, replacements=1, start_line=a, end_line=b)
        return EditResult(
            error=f"old_string is not unique ({len(pos)} occurrences); nothing written",
            hint=_occurrence_hint(text, pos, symbols_for),
        )
    # Fuzzy rungs. Never with replace_all: a loose match applied N times is too blind.
    if not replace_all:
        for mode in ("trailing_ws", "indent"):
            wins = _line_windows(text, old, mode)
            if len(wins) > 1 and line:
                wins = [w for w in wins if w[0] + 1 <= int(line) <= w[0] + w[1]]
            if len(wins) == 1:
                out, a, b = _apply_window(text, wins[0], new)
                return EditResult(text=out, replacements=1, matched=mode, start_line=a, end_line=b)
            if len(wins) > 1:
                starts = [sum(len(x) + 1 for x in text.split("\n")[: w[0]]) for w in wins]
                return EditResult(
                    error=(f"old_string is not unique ({len(wins)} occurrences, ignoring "
                           f"{'indentation' if mode == 'indent' else 'trailing spaces'}); nothing written"),
                    hint=_occurrence_hint(text, starts, symbols_for),
                )
    return EditResult(error="old_string not found in file; nothing written", hint=near_miss(text, old))


def apply_edits(
    text: str,
    hunks: list,
    *,
    symbols_for: Optional[Callable[[], dict]] = None,
) -> tuple[EditResult, list[str]]:
    """Apply several edits to one file in order, all or nothing.

    ``hunks`` rows are dicts with ``old_string``/``new_string`` and optional
    ``replace_all``/``line``. Each hunk matches against the text the earlier
    hunks produced. The first failing hunk fails the whole call (its error
    names the hunk). Returns the combined result plus each hunk's line span in
    the FINAL text (spans of earlier hunks shift when a later hunk above them
    changes the line count).
    """
    spans: list[list[int]] = []
    out = text
    total = 0
    matched = "exact"
    for i, h in enumerate(hunks):
        if not isinstance(h, dict):
            return EditResult(error=f"edit {i + 1}: expected an object with old_string/new_string; nothing written"), []
        old, new = h.get("old_string"), h.get("new_string")
        if not isinstance(old, str) or not isinstance(new, str):
            return EditResult(error=f"edit {i + 1}: old_string and new_string are required; nothing written"), []
        try:
            line = int(h["line"]) if h.get("line") not in (None, "") else None
        except (TypeError, ValueError):
            line = None
        res = apply_edit(out, old, new, replace_all=bool(h.get("replace_all")), line=line,
                         symbols_for=symbols_for if i == 0 else None)
        if res.error is not None or res.text is None:
            res.error = f"edit {i + 1} of {len(hunks)}: {res.error}" + ("" if i == 0 else
                         " (matched against the text after the earlier edits)")
            return res, []
        delta = res.text.count("\n") - out.count("\n")
        if delta and not h.get("replace_all"):
            for sp in spans:
                if sp[0] > res.start_line or (sp[0] == res.start_line and sp[1] > res.end_line):
                    sp[0] += delta
                    sp[1] += delta
        spans.append([res.start_line, res.end_line])
        total += res.replacements
        if res.matched != "exact":
            matched = res.matched
        out = res.text
    first = spans[0] if spans else [0, 0]
    return (EditResult(text=out, replacements=total, matched=matched, start_line=first[0], end_line=first[1]),
            [f"{a}-{b}" for a, b in spans])


def _occurrence_hint(text: str, positions: list[int], symbols_for: Optional[Callable[[], dict]]) -> dict:
    symbols: dict = {}
    if symbols_for is not None:
        try:
            symbols = symbols_for() or {}
        except Exception:
            symbols = {}
    from .docindex import enclosing_symbol

    occ = []
    for p in positions[:OCCURRENCES_SHOWN]:
        ln = _line_of(text, p)
        sym = enclosing_symbol(symbols, ln) if symbols else None
        occ.append(f"L{ln}" + (f" in {sym}" if sym else ""))
    more = len(positions) - len(occ)
    return {
        "occurrences": occ + ([f"+{more} more"] if more > 0 else []),
        "hint": "pass line=<one of these line numbers> to pick that occurrence, "
                "or extend old_string with surrounding lines",
    }


def near_miss(text: str, old: str) -> Optional[dict]:
    """The region of ``text`` most like ``old``: line span, similarity and verbatim text."""
    if len(text) > NEAR_MISS_MAX_FILE:
        return None
    lines = text.split("\n")
    old_lines = old.strip("\n").split("\n")
    n = max(1, len(old_lines))
    keyed = [ln.strip() for ln in old_lines if ln.strip()]
    if not keyed:
        return None
    stripped = [ln.strip() for ln in lines]
    # Candidate starts: anchored on any old line that occurs (stripped) in the file,
    # else on the lines most similar to the old block's longest line.
    starts: set[int] = set()
    index: dict[str, list[int]] = {}
    for i, s in enumerate(stripped):
        if s:
            index.setdefault(s, []).append(i)
    for j, ln in enumerate(old_lines):
        for i in index.get(ln.strip(), [])[:50]:
            starts.add(max(0, i - j))
    if not starts:
        probe = max(keyed, key=len)
        uniq = list(index.keys())
        for m in difflib.get_close_matches(probe, uniq, n=5, cutoff=0.5):
            j = next((k for k, ln in enumerate(old_lines) if ln.strip() == probe), 0)
            for i in index[m][:10]:
                starts.add(max(0, i - j))
    if not starts:
        return None
    target = "\n".join(ln.strip() for ln in old_lines)
    best = (0.0, 0, 0)
    for s in sorted(starts)[:200]:
        for size in {n - 1, n, n + 1}:
            if size < 1:
                continue
            cand = "\n".join(stripped[s : s + size])
            r = difflib.SequenceMatcher(None, target, cand, autojunk=False).ratio()
            if r > best[0]:
                best = (r, s, size)
    ratio, s, size = best
    if ratio < NEAR_MISS_MIN_RATIO:
        return None
    body = "\n".join(lines[s : s + size])
    if len(body) > NEAR_MISS_MAX_CHARS:
        body = body[:NEAR_MISS_MAX_CHARS] + "\n…"
    return {
        "closest": {"start_line": s + 1, "end_line": s + size, "similarity": round(ratio, 2), "text": body},
        "hint": "closest region shown verbatim; copy old_string from it",
    }


# ---------------------------------------------------------------------------
# Receipt checks
# ---------------------------------------------------------------------------


def syntax_problem(path: Path, text: str) -> Optional[str]:
    """``"line N: msg"`` when ``text`` does not parse as the file's language (Python, JSON), else None."""
    suffix = Path(path).suffix.lower()
    try:
        if suffix in (".py", ".pyw"):
            ast.parse(text)
        elif suffix == ".json":
            json.loads(text)
        else:
            return None
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except json.JSONDecodeError as e:
        return f"line {e.lineno}: {e.msg}"
    except (ValueError, RecursionError, MemoryError):
        return None
    return None


def introduced_syntax_error(path: Path, before: Optional[str], after: str) -> Optional[dict]:
    """A syntax error the write introduced (None when the file was already broken, or still parses)."""
    problem = syntax_problem(path, after)
    if problem is None:
        return None
    if before is not None and syntax_problem(path, before) is not None:
        return None
    out: dict = {"syntax_error": problem}
    m = re.match(r"line (\d+)", problem)
    if m:
        ln = int(m.group(1))
        lines = after.split("\n")
        lo, hi = max(1, ln - 2), min(len(lines), ln + 2)
        out["syntax_context"] = "\n".join(f"{i}: {lines[i - 1]}" for i in range(lo, hi + 1))
    return out

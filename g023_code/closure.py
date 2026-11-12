"""Change-closure receipts: what an edit removed, and where it still appears.

A rename or value change is only half done after the first Edit; the model
then greps to find the rest and, once done, greps again to confirm nothing is
left. Plain substring greps also alias: after ``load_cfg`` became
``safe_load_cfg`` a grep for the old name still matches the new one, which
costs extra "verify" hops on a task that is already complete.

After a successful Edit this module takes the tokens the edit removed (in
``old_string``, absent from ``new_string``: identifiers of 3+ chars that are
not keywords/builtins, numbers of 2+ digits), counts their whole-token
occurrences across the project (word boundaries, so the new name never
matches the old one), and returns ``{term: 0}`` or ``{term: {"n": k, "at":
[path:line, ...]}}``. A zero is the confirmation; sites are the to-do list.
The scan is bounded in files, bytes and time; a partial scan says so.
"""

from __future__ import annotations

import builtins
import keyword
import re
import time
from pathlib import Path
from typing import Iterable, Optional

MAX_TERMS = 4
MAX_SITES = 6
# A term this common is vocabulary, not a residue of this edit.
MAX_COUNT = 40
FILE_MAX_BYTES = 512_000
TOTAL_MAX_BYTES = 16_000_000
TIME_BUDGET_S = 0.5

_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+(?:\.\d+)?")
_COMMON = frozenset(keyword.kwlist) | frozenset(dir(builtins)) | frozenset(
    "self cls args kwargs none true false null undefined this var let const def return".split()
)


def enabled() -> bool:
    """Setting ``closure_receipts`` (env G023_CLOSURE_RECEIPTS)."""
    from .config import settings

    return bool(getattr(settings, "closure_receipts", False))


def _tokens(text: str) -> list:
    return _TOKEN.findall(text or "")


def removed_terms(old: str, new: str, limit: int = MAX_TERMS) -> list:
    """Distinctive tokens of ``old`` that ``new`` no longer contains, longest first."""
    keep = set(_tokens(new))
    out = []
    seen = set()
    for tok in _tokens(old):
        if tok in keep or tok in seen:
            continue
        seen.add(tok)
        if tok[0].isdigit():
            if len(tok.replace(".", "")) < 2:
                continue
        elif len(tok) < 3 or tok in _COMMON or tok.lower() in _COMMON:
            continue
        out.append(tok)
    out.sort(key=lambda t: (-len(t), t))
    return out[:limit]


def _pattern(term: str) -> re.Pattern:
    # Numbers: "42" must not match inside "1.42" or "42.5" (a different value).
    return re.compile(r"(?<![A-Za-z0-9_.])" + re.escape(term) + r"(?![A-Za-z0-9_]|\.\d)"
                      if term[0].isdigit() else
                      r"(?<![A-Za-z0-9_])" + re.escape(term) + r"(?![A-Za-z0-9_])")


def _project_files(root: Path) -> Iterable[Path]:
    from .subagents.searcher import _should_skip, _walk_files, build_ignorer

    ignored = build_ignorer(root)
    for f in _walk_files(root):
        try:
            rel = f.relative_to(root)
        except ValueError:
            continue
        if rel.parts[:1] == (".g023",):
            continue
        if f.is_file() and not _should_skip(f, root) and not ignored(f):
            yield f


# Data and generated files: matches there are records, not references.
_DATA_SUFFIXES = (".jsonl", ".log", ".lock", ".map", ".min.js", ".csv", ".tsv", ".svg")


def distinctive(term: str) -> bool:
    """A name no prose word collides with: snake_case, camelCase, digits, or long."""
    return "_" in term.strip("_") or any(c.isdigit() for c in term) or any(
        c.isupper() for c in term[1:]) or len(term) >= 12


def _read(path: Path) -> Optional[str]:
    if path.name.lower().endswith(_DATA_SUFFIXES):
        return None
    try:
        if path.stat().st_size > FILE_MAX_BYTES:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:2048]:
        return None
    return raw.decode("utf-8", errors="replace")


def scan(terms: list, root: Path, files: Optional[Iterable[Path]] = None,
         budget_s: float = TIME_BUDGET_S) -> dict:
    """``{"left": {term: 0 | {"n", "at"}}, "partial": bool}`` for whole-token hits."""
    if not terms:
        return {"left": {}, "partial": False}
    pats = {t: _pattern(t) for t in terms}
    counts = {t: 0 for t in terms}
    sites: dict = {t: [] for t in terms}
    deadline = time.monotonic() + budget_s
    spent = 0
    partial = False
    root_res = root.resolve()
    for path in files if files is not None else _project_files(root):
        if time.monotonic() > deadline or spent > TOTAL_MAX_BYTES:
            partial = True
            break
        text = _read(path)
        if text is None:
            continue
        spent += len(text)
        rel = None
        for t, pat in pats.items():
            if t not in text:  # C-speed prefilter
                continue
            for m in pat.finditer(text):
                counts[t] += 1
                if len(sites[t]) < MAX_SITES:
                    if rel is None:
                        try:
                            rel = path.resolve().relative_to(root_res).as_posix()
                        except (ValueError, OSError):
                            rel = str(path)
                    sites[t].append(f"{rel}:{text.count(chr(10), 0, m.start()) + 1}")
    left: dict = {}
    for t in terms:
        n = counts[t]
        if n > MAX_COUNT and not distinctive(t):
            continue  # vocabulary, not a residue
        if n == 0:
            left[t] = 0
        else:
            row = {"n": n, "at": sites[t]}
            left[t] = row
    return {"left": left, "partial": partial}


def receipt(old: str, new: str, root: Path) -> Optional[dict]:
    """The fields an Edit result gains, or None when nothing distinctive was removed."""
    terms = removed_terms(old, new)
    if not terms:
        return None
    found = scan(terms, root)
    if not found["left"]:
        return None
    out = {"refs_left": found["left"]}
    if found["partial"]:
        out["refs_scan"] = "partial"
    return out

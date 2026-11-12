"""
Scout: a free pre-read of the project before the paid model's first call.

Most turns open with digging: ListDir, SearchContent, ReadFile, then the edit
(about 3.6 of 7.8 steps on the recorded benches). Each hop is a paid model call:
the context is replayed and the reasoning is billed as output. The scout does
that first hop on this machine:

1. Retrieve (deterministic): the project is cut into code units (one per
   def/class, long ones windowed; markdown by heading) and ranked by BM25 over
   the request, with path and symbol names counted as strong evidence.
2. Read (local model, optional): the best units are shown as numbered lines
   and the local model returns the ids of the lines that locate the request.
   Ids outside what was shown are dropped and the text is copied from the
   file, so every line in the card is verbatim (cite-or-drop).
3. Card: the picked lines grouped by file and enclosing def/heading
   (``path (in: def ...):`` then ``  N: text``), plus an ``also matching:``
   line naming the next best units, appended to the user message and
   labelled as a lead. Without the local model the card names the top units
   only (``path:a-b name``).

The scout never edits, never decides, and fails open: any error, an empty
project, a weak match or a busy GPU means no card. After the turn
``record_use`` records whether the paid model's tool arguments or answer named
a scouted file or symbol: the scout's precision, measured for free, and
``trusted`` mutes a mode whose cards keep being ignored.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from . import docindex

UNIT_MAX = 80          # lines; longer units are windowed
WINDOW = 60
FILE_MAX_BYTES = 400_000
TOTAL_MAX_BYTES = 30_000_000
INDEX_BUDGET_S = 2.5
TOP_UNITS = 8          # units the local model reads
CARD_LINES = 6
LINE_CLIP = 160
MIN_TERMS = 2          # requests with fewer content words get no card
LOCAL_TIMEOUT_S = 8.0

_ANCHOR = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+def|def|class|async\s+function|function|func|fn|pub\s+fn|impl|struct|interface)\s+\w"
)
_DEF_NAME = re.compile(r"(?:def|class|function|func|fn|struct|interface|impl)\s+([A-Za-z_]\w*)")
_MD_HEAD = re.compile(r"^#{1,6}\s+\S")
_CODE_EXT = {".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".c", ".h", ".cc", ".cpp", ".hpp",
             ".cs", ".rb", ".php", ".kt", ".swift", ".scala", ".lua", ".sh"}
_DOC_EXT = {".md", ".markdown", ".rst", ".txt"}
_DOC_WORDS = {"doc", "docs", "readme", "documentation", "documented", "changelog", "guide", "manual", "md"}
DOC_DISCOUNT = 0.6     # a request names behaviour; prose restates it (unless the request asks for docs)
_SKIP_SUFFIX = (".jsonl", ".log", ".lock", ".map", ".min.js", ".csv", ".tsv", ".svg", ".json")


class Unit:
    __slots__ = ("rel", "a", "b", "name", "bag", "nbag", "n")

    def __init__(self, rel: str, a: int, b: int, name: str, text: str):
        self.rel, self.a, self.b, self.name = rel, a, b, name  # a, b: 0-based, end exclusive
        self.bag = Counter(docindex.tokens(text))
        self.nbag = set(docindex.tokens(rel.replace("/", " ") + " " + name))
        self.n = max(1, sum(self.bag.values()))


def _cut(lines: list, rel: str) -> list[tuple[int, int, str]]:
    """(start, end, name) spans: def/class units for code, heading sections for docs, windows otherwise."""
    ext = os.path.splitext(rel)[1].lower()
    starts: list[tuple[int, str]] = []
    if ext in _CODE_EXT:
        for i, ln in enumerate(lines):
            if _ANCHOR.match(ln):
                m = _DEF_NAME.search(ln)
                starts.append((i, m.group(1) if m else ln.strip()[:40]))
    elif ext in _DOC_EXT:
        for i, ln in enumerate(lines):
            if _MD_HEAD.match(ln):
                starts.append((i, ln.lstrip("#").strip()[:60]))
    if not starts or starts[0][0] > 0:
        starts.insert(0, (0, ""))
    spans = []
    for k, (a, name) in enumerate(starts):
        b = starts[k + 1][0] if k + 1 < len(starts) else len(lines)
        if b <= a:
            continue
        if b - a <= UNIT_MAX:
            spans.append((a, b, name))
        else:
            for w in range(a, b, WINDOW):
                spans.append((w, min(w + WINDOW, b), name))
    return spans


class Index:
    """Units of every readable project file, refreshed per file by (mtime, size)."""

    def __init__(self, root: Path):
        self.root = root
        self.files: dict[str, tuple[tuple, list, list]] = {}  # rel -> (stamp, lines, units)

    def refresh(self, budget_s: float = INDEX_BUDGET_S) -> bool:
        """Re-read changed files. False when the budget or size cap cut the walk short."""
        from .closure import _project_files

        t0 = time.monotonic()
        seen, total, complete = set(), 0, True
        for f in _project_files(self.root):
            if time.monotonic() - t0 > budget_s:
                complete = False
                break
            name = f.name.lower()
            if name.endswith(_SKIP_SUFFIX):
                continue
            try:
                st = f.stat()
                rel = f.relative_to(self.root).as_posix()
            except (OSError, ValueError):
                continue
            if st.st_size > FILE_MAX_BYTES or st.st_size == 0:
                continue
            total += st.st_size
            if total > TOTAL_MAX_BYTES:
                complete = False
                break
            seen.add(rel)
            stamp = (st.st_mtime_ns, st.st_size)
            old = self.files.get(rel)
            if old and old[0] == stamp:
                continue
            try:
                raw = f.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:2048]:
                continue
            lines = raw.decode("utf-8", errors="replace").splitlines()
            units = [Unit(rel, a, b, nm, "\n".join(lines[a:b])) for a, b, nm in _cut(lines, rel)]
            self.files[rel] = (stamp, lines, units)
        if complete:
            for rel in list(self.files):
                if rel not in seen:
                    del self.files[rel]
        return complete

    def units(self) -> list:
        return [u for _s, _l, us in self.files.values() for u in us]

    def lines(self, rel: str) -> list:
        return self.files[rel][1] if rel in self.files else []


_INDEXES: dict[str, Index] = {}


def index_for(root: Path) -> Index:
    key = str(root.resolve())
    if key not in _INDEXES:
        _INDEXES[key] = Index(root.resolve())
    idx = _INDEXES[key]
    idx.refresh()
    return idx


def rank(units: list, query: str, top: int = TOP_UNITS, extra: Optional[list] = None,
         extra_weight: float = 0.5) -> list[tuple[float, Unit]]:
    """BM25 over units; a query term in the unit's path or symbol name adds a name boost.

    ``extra`` are expansion terms (the local model's guess at the code's own
    vocabulary for the request), counted at ``extra_weight``. Test files are
    discounted unless the request talks about tests: a request names
    behaviour, and tests restate it.
    """
    q = docindex.terms(query)
    if not q or not units:
        return []
    weight = {t: 1.0 for t in q}
    for t in docindex.terms(" ".join(extra or [])):
        weight.setdefault(t, extra_weight)
    vocab: set = set()
    for u in units:
        vocab.update(u.bag)
        vocab.update(u.nbag)
    n = len(units)
    avg = sum(u.n for u in units) / n
    scores = [0.0] * n
    for t, wt in weight.items():
        words = docindex.term_matches(t, vocab)
        if not words:
            continue
        tf = [sum(u.bag[w] for w in words) for u in units]
        named = [bool(words & u.nbag) for u in units]
        df = sum(1 for k in range(n) if tf[k] or named[k])
        if not df:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for k in range(n):
            f = tf[k]
            s = f * 2.2 / (f + 1.2 * (0.25 + 0.75 * units[k].n / avg)) if f else 0.0
            if s or named[k]:
                scores[k] += wt * idf * (s + (1.5 if named[k] else 0.0))
    wants_tests = any(t.startswith("test") for t in q)
    wants_docs = bool(set(q) & _DOC_WORDS)
    out = []
    for k, u in enumerate(units):
        s = scores[k]
        if not s:
            continue
        if not wants_tests and (u.rel.startswith("tests/") or "/tests/" in u.rel or "test_" in u.rel):
            s *= 0.6
        if not wants_docs and os.path.splitext(u.rel)[1].lower() in _DOC_EXT:
            s *= DOC_DISCOUNT
        out.append((round(s, 3), u))
    out.sort(key=lambda x: (-x[0], x[1].rel, x[1].a))
    return out[:top]


_SYS_EXPAND = (
    "Guess how a codebase would name what this request is about: up to 10 likely function, class or "
    "variable names (snake_case or camelCase, as code writes them) and technical keywords a docstring "
    "would use. No explanations."
)
_SCHEMA_EXPAND = {
    "type": "object",
    "properties": {"terms": {"type": "array", "items": {"type": "string", "maxLength": 40}, "maxItems": 10}},
    "required": ["terms"],
}


async def expand(query: str, chat) -> list:
    """The local model's guess at the code's vocabulary for the request ([] on any failure)."""
    try:
        obj = await chat(_SYS_EXPAND, f"Request: {query[:1500]}", _SCHEMA_EXPAND, max_tokens=120, wait=False)
    except Exception:  # noqa: BLE001
        return []
    terms = obj.get("terms") if isinstance(obj, dict) else None
    return [str(t)[:40] for t in terms[:10] if isinstance(t, str)] if isinstance(terms, list) else []


_SYS = (
    "An engineer is about to work on a request in a codebase. You are shown numbered lines from the "
    "parts of the code a search found. Return the ids of at most 6 lines that locate what the request "
    "is about: the definition that handles it and the lines that decide the behaviour asked about. "
    "Most useful first. If none of the lines are about the request, return an empty list. Only ids that appear."
)
_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "integer"}, "maxItems": 6}},
    "required": ["lines"],
}


def show(ranked: list, idx: Index) -> tuple[str, list[tuple[str, int]]]:
    """Numbered rows for the local model and the id -> (rel, 1-based line) map."""
    rows, ids = [], []
    for _s, u in ranked:
        lines = idx.lines(u.rel)
        rows.append(f"--- {u.rel}" + (f" ({u.name})" if u.name else ""))
        for i in range(u.a, u.b):
            if not lines[i].strip():
                continue
            ids.append((u.rel, i + 1))
            rows.append(f"{len(ids)}: {lines[i][:LINE_CLIP]}")
    return "\n".join(rows), ids


def verify(picks: Any, ids: list) -> list[tuple[str, int]]:
    """Cite-or-drop: ids the model returned that were really shown, deduplicated, in order."""
    out = []
    for x in picks if isinstance(picks, list) else []:
        if isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= len(ids) and ids[x - 1] not in out:
            out.append(ids[x - 1])
    return out


def _anchor(idx: Index, rel: str, n: int) -> str:
    from .local_ask import section_of

    return section_of(idx.lines(rel), n, rel)


def render(rows: list[tuple[str, int]], idx: Index) -> str:
    """Card rows grouped by file and enclosing section, lines in file order.

    ``path (in: def name):`` once per group, then ``  N: text`` rows, so a
    six-line pick costs one anchor instead of six.
    """
    groups: dict[tuple[str, str], list[int]] = {}
    for rel, n in rows[:CARD_LINES]:
        groups.setdefault((rel, _anchor(idx, rel, n)), []).append(n)
    out = []
    for (rel, sec), ns in groups.items():
        out.append(f"{rel}" + (f" (in: {sec})" if sec else "") + ":")
        for n in sorted(ns):
            out.append(f"  {n}: {idx.lines(rel)[n - 1].strip()[:LINE_CLIP]}")
    return "\n".join(out)


def render_units(ranked: list, k: int = 3) -> str:
    return "; ".join(f"{u.rel}:{u.a + 1}-{u.b}" + (f" {u.name}" if u.name else "") for _s, u in ranked[:k])


async def locate(query: str, root: Path, chat, *, system: str = "", top: int = TOP_UNITS) -> Optional[dict]:
    """Retrieve, then (with ``chat``) pick lines: ``{"idx", "ranked", "rows", "terms", "answered"}``.

    ``rows`` are verified ``(rel, line)`` picks (empty without ``chat``);
    ``answered`` is False when the local model did not answer (fail open).
    """
    import asyncio

    idx_task = asyncio.ensure_future(asyncio.to_thread(index_for, root))
    extra: list = []
    if chat is not None:
        try:
            extra = await asyncio.wait_for(expand(query, chat), LOCAL_TIMEOUT_S / 2)
        except Exception:  # noqa: BLE001 - expansion is optional
            extra = []
    idx = await idx_task
    ranked = await asyncio.to_thread(rank, idx.units(), query, top, extra)
    out = {"idx": idx, "ranked": ranked, "rows": [], "terms": extra, "answered": False}
    if not ranked or chat is None:
        return out
    text, ids = show(ranked, idx)
    obj = None
    try:
        obj = await asyncio.wait_for(
            chat(system or _SYS, f"Request: {query[:1500]}\n\nCode:\n{text}", _SCHEMA, max_tokens=80, wait=False),
            LOCAL_TIMEOUT_S,
        )
    except Exception:  # noqa: BLE001 - a hint; fail open
        obj = None
    if obj is not None:
        out["rows"] = verify(obj.get("lines"), ids)
        out["answered"] = True
    return out


async def scout(query: str, root: Path, *, chat=None, use_local: bool = True) -> Optional[dict]:
    """``{"card", "files", "lines", "mode", "terms", "secs"}`` for the request, or None (no card).

    With the local model: expansion terms (asked while the index refreshes)
    widen the BM25 query, then the model picks lines from the best units.
    Without it: the best three units by plain BM25.
    """
    t0 = time.monotonic()
    if len(docindex.terms(query)) < MIN_TERMS:
        return None
    if use_local and chat is None:
        from . import local_llm

        chat = local_llm.chat_json
    found = await locate(query, root, chat if use_local else None)
    if not found or not found["ranked"]:
        return None
    idx, ranked, rows = found["idx"], found["ranked"], found["rows"]
    mode = "local" if found["answered"] else "units"
    if found["answered"] and not rows:
        return None  # the model read the best candidates and found nothing: say nothing
    if rows:
        card = render(rows, idx)
        files = list(dict.fromkeys(r for r, _n in rows))
        # Keyword runners-up the model did not pick: a small model's pick misses
        # more often than the top-3 retrieval does (scout eval), and a header row is cheap.
        shown = set(rows)
        others = [(sc, u) for sc, u in ranked[:3]
                  if not any(r == u.rel and u.a < n <= u.b for r, n in shown)]
        if others:
            card += "\nalso matching: " + render_units(others)
            files += [u.rel for _s, u in others if u.rel not in files]
    else:
        card = render_units(ranked)
        files = list(dict.fromkeys(u.rel for _s, u in ranked[:3]))
    return {"card": card, "files": files, "lines": rows[:CARD_LINES], "mode": mode, "terms": found["terms"],
            "secs": round(time.monotonic() - t0, 2)}


def card_text(res: dict) -> str:
    head = ("[scout: a free local pre-read of this request; lines are verbatim from the files, "
            "the choice is a lead to check, not a fact]" if res["mode"] == "local"
            else "[scout: best-matching code units for this request (keyword match, a lead)]")
    return head + "\n" + res["card"]


# ---------------------------------------------------------------------------
# Use-confirmed precision: did the paid model touch the scouted files?
# ---------------------------------------------------------------------------


def _ledger_path() -> Path:
    from .config import get_global_cache_dir

    return get_global_cache_dir() / "local_llm" / "scout_ledger.json"


_CARD_HEAD = re.compile(
    r"^(\S+?)(?: \(in: (?:async\s+)?(?:def|class|function|func|fn)\s+([A-Za-z_]\w*)[^\n]*\))?:$", re.M)


def used_files(res: dict, seen: str) -> list:
    """Scouted files the turn used: the path or file name appears in a tool argument or the
    answer, or the symbol a card group sits in (``(in: def name)``) is named there. A Q&A turn
    that answers straight from the card reads nothing, but names what the card showed."""
    syms: dict[str, set] = {}
    for rel, name in _CARD_HEAD.findall(res.get("card", "")):
        if name:
            syms.setdefault(rel, set()).add(name)
    out = []
    for rel in res.get("files", []):
        base = rel.rsplit("/", 1)[-1]
        if rel in seen or re.search(rf"(?<![\w.-]){re.escape(base)}\b", seen) or any(
            re.search(rf"\b{re.escape(n)}\b", seen) for n in syms.get(rel, ())
        ):
            out.append(rel)
    return out


def record_use(scouted: list, touched: list, mode: str) -> Optional[bool]:
    """After the turn: True when a file the paid model read/edited was scouted. Persisted per mode."""
    if not scouted:
        return None
    hit = bool(set(scouted) & set(touched))
    p = _ledger_path()
    try:
        data = json.loads(p.read_text()) if p.is_file() else {}
    except Exception:  # noqa: BLE001
        data = {}
    row = data.setdefault(mode, {"cards": 0, "used": 0, "recent": []})
    row["cards"] += 1
    row["used"] += int(hit)
    row["recent"] = (row.get("recent", []) + [int(hit)])[-20:]
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(p)
    except OSError:
        pass
    return hit


def ledger() -> dict:
    try:
        return json.loads(_ledger_path().read_text())
    except Exception:  # noqa: BLE001
        return {}


def trusted(mode: str, floor: float = 0.3, window: int = 10) -> bool:
    """Keep carding unless the last ``window`` cards of this mode were mostly ignored."""
    recent = ledger().get(mode, {}).get("recent", [])
    if len(recent) < window:
        return True
    return sum(recent[-window:]) / window >= floor

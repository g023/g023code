"""Near-miss names for a search that found nothing.

The model searches for the name it expects (``count_words``) and the project
spells it differently (``word_count``, ``countWords``, ``WordCounter``). A
zero-hit grep says only "0 matches", and the next three calls go to ListDir
and guessing. This module turns that dead end into one hop: split the
query's identifiers into name parts, reduce simple word endings, and rank the
names the project *defines* (plus file stems) by

* part-set overlap (Jaccard over stemmed parts — order and case style free:
  ``count_words`` = ``WordCounter`` = ``words_count``), and
* character similarity of the joined parts, for typos (``slugfy``).

It only runs on the zero-hit path, with a time and byte budget, so a normal
search costs nothing extra.
"""

from __future__ import annotations

import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

SUGGEST_MAX = 5
MIN_SCORE = 0.5
TYPO_RATIO = 0.8
FILE_MAX_BYTES = 512_000
TOTAL_MAX_BYTES = 24_000_000
TIME_BUDGET_S = 1.0

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
# One MULTILINE alternation, scanned in C over the whole file (seven per-line
# Python matches cost ~10x more). Each branch captures the defined name;
# ``[ \t]`` (not ``\s``) and ``[^...\n]`` keep every branch on one line.
_DEFS = re.compile(
    "|".join(
        (
            # def / class / function / fn / struct / const / let ... NAME
            r"^[ \t]*(?:export[ \t]+(?:default[ \t]+)?)?(?:pub(?:\([^)\n]*\))?[ \t]+)?(?:async[ \t]+)?"
            r"(?:static[ \t]+)?(?:def|class|function\*?|func|fn|interface|struct|enum|trait|type|const|let"
            r"|var|module|macro)[ \t]+\*?([A-Za-z_$][\w$]*)",
            r"^func[ \t]+\([^)\n]*\)[ \t]*([A-Za-z_]\w*)",  # Go method
            r"^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)",  # C macro
            r"^([A-Za-z_]\w*)[ \t]*(?::[^=\n]+)?=[^=]",  # top-level assignment
            r"^[A-Za-z_][\w*&:<>, \t]*?[ \t*&]([A-Za-z_]\w*)[ \t]*\([^;\n]*$",  # C/C++/Java function
            r"^[ \t]+(?:(?:public|private|protected|static|async|override|final)[ \t]+)*"
            r"(?:[\w<>\[\],.?]+[ \t]+)?([A-Za-z_]\w*)[ \t]*\([^;\n]*\)[ \t]*(?::[^{;\n]*)?"
            r"(?:throws[ \t][^{;\n]*)?\{[ \t]*$",  # method (optional return type)
        )
    ),
    re.MULTILINE,
)
_NOT_NAMES = frozenset(
    "if while for switch return sizeof else do case catch with elif try new delete".split()
)
# Query words that describe a search, not a name.
_STOP = frozenset(
    """def class function func fn method const let var async await return import from self this
    the and for with struct enum type interface impl pub export default static void int str bool
    where what which find defined definition called named name names file files code""".split()
)
_SKIP_SUFFIXES = frozenset(
    ".md .txt .rst .adoc .log .csv .tsv .json .jsonl .lock .svg .html .htm .xml .min.js .map".split()
)


def enabled() -> bool:
    """On unless G023_NEAR_MISS is 0/false/no/off (read per call)."""
    return os.environ.get("G023_NEAR_MISS", "").strip().lower() not in ("0", "false", "no", "off")


def _stem(word: str) -> str:
    for suffix in ("ings", "ing", "ers", "er", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def name_parts(name: str) -> List[str]:
    """``getHTTPResponse_code2`` -> ['get', 'http', 'response', 'code', '2'] (stemmed)."""
    out: List[str] = []
    for chunk in re.split(r"[_\W]+", name):
        for piece in _CAMEL.findall(chunk):
            piece = piece.lower()
            if piece:
                out.append(_stem(piece))
    return out


def query_bags(query: str) -> List[tuple]:
    """Name-part bags to look for: each identifier, plus all words together."""
    text = re.sub(r"\\[A-Za-z]", " ", query or "")  # regex escapes: \bname, \w+, \s
    idents = [t for t in _IDENT.findall(text) if t.lower() not in _STOP and len(t) >= 3]
    bags: List[tuple] = []
    seen = set()
    for ident in idents:
        parts = tuple(name_parts(ident))
        if parts and parts not in seen:
            seen.add(parts)
            bags.append(parts)
    if len(idents) > 1:
        merged = tuple(p for ident in idents for p in name_parts(ident))
        if merged and merged not in seen:
            bags.append(merged)
    return bags


def _part_match(a: str, b: str) -> bool:
    if a == b:
        return True
    if min(len(a), len(b)) < 4 or abs(len(a) - len(b)) > 2:
        return False
    return SequenceMatcher(None, a, b).ratio() >= TYPO_RATIO


def _score(qparts: Sequence[str], cparts: Sequence[str]) -> float:
    qs, cs = set(qparts), set(cparts)
    if not qs or not cs:
        return 0.0
    # Jaccard where a part also matches its typo (serch ~ search).
    matched = sum(1 for q in qs if any(_part_match(q, c) for c in cs))
    jac = matched / (len(qs) + len(cs) - matched)
    qj, cj = "".join(qparts), "".join(cparts)
    if qj == cj:
        return 1.0
    ratio = SequenceMatcher(None, qj, cj).ratio() if abs(len(qj) - len(cj)) <= 4 else 0.0
    return max(jac, ratio if ratio >= TYPO_RATIO else 0.0)


def defined_names(text: str) -> Iterable[tuple]:
    """(name, line) for every name a line of source defines."""
    line, pos = 1, 0
    for m in _DEFS.finditer(text):
        name = next((g for g in m.groups() if g), None)
        if not name or name in _NOT_NAMES:
            continue
        line += text.count("\n", pos, m.start())
        pos = m.start()
        yield name, line


def _readable(path: Path) -> Optional[str]:
    name = path.name.lower()
    if any(name.endswith(s) for s in _SKIP_SUFFIXES):
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


def suggest(
    query: str,
    files: Iterable[Path],
    root: Path,
    limit: int = SUGGEST_MAX,
    budget_s: float = TIME_BUDGET_S,
) -> dict:
    """Rank defined names (and file stems) close to the query's names.

    Returns ``{"did_you_mean": [...], "scanned": n, "complete": bool}``; the
    list is empty when nothing scores at least ``MIN_SCORE``.
    """
    bags = query_bags(query)
    if not bags:
        return {"did_you_mean": [], "scanned": 0, "complete": True}
    best: dict = {}
    deadline = time.monotonic() + budget_s
    spent = 0
    scanned = 0
    complete = True

    scores: dict = {}  # names repeat across files: score each spelling once

    def consider(name: str, rel: str, line: Optional[int], kind: str) -> None:
        score = scores.get(name)
        if score is None:
            parts = name_parts(name)
            score = scores[name] = max(_score(bag, parts) for bag in bags)
        if score < MIN_SCORE:
            return
        # One row per name: the same name in a vendored or release copy
        # adds nothing; keep the best score, then the shortest path.
        key = name
        old = best.get(key)
        if old and (old["score"], -len(old["path"])) >= (score, -len(rel)):
            return
        best[key] = {"name": name, "path": rel, "line": line, "kind": kind, "score": round(score, 2)}

    for path in files:
        if time.monotonic() > deadline or spent > TOTAL_MAX_BYTES:
            complete = False
            break
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except (ValueError, OSError):
            rel = str(path)
        consider(path.stem, rel, None, "file")
        text = _readable(path)
        if text is None:
            continue
        scanned += 1
        spent += len(text)
        for name, line in defined_names(text):
            consider(name, rel, line, "def")

    ranked = sorted(
        best.values(),
        key=lambda h: (-h["score"], h["kind"] != "def", len(h["path"]), h["path"], h["line"] or 0),
    )
    out = []
    for hit in ranked[:limit]:
        row = {"name": hit["name"], "path": hit["path"]}
        if hit["line"]:
            row["line"] = hit["line"]
        out.append(row)
    return {"did_you_mean": out, "scanned": scanned, "complete": complete}

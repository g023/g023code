"""Document index: structure, ranking and addresses for long files. Local, no API calls.

One shared module behind ReadFile (outline, focus), SearchContent (hit addresses)
and FetchUrl (page focus), so they cannot drift apart:

- ``terms`` / ``tokens``: word-boundary tokenisation that splits snake_case and
  camelCase. Stopwords are dropped unless the query is made of nothing else
  ("To be, or not to be").
- ``find_phrase``: exact phrase over normalised words, across line breaks.
- ``Bm25``: a ranker over arbitrary sections. Term frequency counts whole words, plus short
  suffixes (save → saves, saved), never substrings (be ↛ beauty).
- ``induce_outline``: a heading tree for plain text. It is built from line shapes
  (keyword headings with compound parents, ALL-CAPS titles, bare numbers,
  Markdown/setext/numbered headings), with boilerplate suppression and end markers.
- ``fold``: progressive disclosure under a character budget. Folded nodes show
  their child count.
- ``Outline.address(line)`` / ``enclosing_symbol``: the breadcrumb of a line, so a hit
  says where it is without a second read.

Everything is deterministic. The same bytes give the same output, so results sit in
the prefix cache without shifting it.
"""

from __future__ import annotations

import math
import re
from array import array
from bisect import bisect_left, bisect_right
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

STOPWORDS = frozenset(
    "a an and are as at be by can do does for from how in is it of on or that the this "
    "to was what when where which who why with file code show me find about".split()
)
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9]*|[0-9]+")
_CAMEL = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")


def _expand(raw: str) -> list[str]:
    """``getUserName`` → getusername, get, user, name (lower-case, deduplicated)."""
    parts = _CAMEL.findall(raw)
    out = [raw.lower()]
    if len(parts) > 1:
        out.extend(p.lower() for p in parts)
    return out


def terms(text: str) -> list[str]:
    """Query terms: lower-case, snake/camel split, stopwords dropped.

    A query made only of stopwords keeps them. Otherwise "to be or not to be" would
    rank on nothing.
    """
    out: list[str] = []
    allstop: list[str] = []
    for raw in _WORD.findall(text or ""):
        for t in _expand(raw):
            if len(t) < 2:
                continue
            if t not in allstop:
                allstop.append(t)
            if t not in STOPWORDS and t not in out:
                out.append(t)
    return out or allstop


def tokens(text: str) -> list[str]:
    """Document tokens, with the same splitting as :func:`terms` but keeping stopwords."""
    out: list[str] = []
    for raw in _WORD.findall(text):
        out.extend(_expand(raw))
    return out


def term_matches(term: str, vocab: Iterable[str]) -> set[str]:
    """Vocabulary words that count as an occurrence of ``term``.

    The exact word always counts. A term of 4+ chars also matches the same word with up to
    3 more trailing chars (save → saves/saved/saving). Short terms never do, so "be" stays
    "be".
    """
    if len(term) < 4:
        return {term} if term in vocab else set()
    return {w for w in vocab if w == term or (w.startswith(term) and len(w) - len(term) <= 3)}


# ---------------------------------------------------------------------------
# Phrase finding (across line breaks, punctuation- and case-insensitive)
# ---------------------------------------------------------------------------

_PHRASE_WORD = re.compile(r"[A-Za-z0-9]+")


def norm_words(text: str) -> list[str]:
    return [w.lower() for w in _PHRASE_WORD.findall(text or "")]


def quoted(focus: str) -> Optional[str]:
    """The quoted part of a focus (``"..."``, ``'...'``, “...”), if any."""
    m = re.search(r"[\"“”]([^\"“”]{3,})[\"“”]", focus or "")
    if m:
        return m.group(1)
    m = re.search(r"(?:^|\s)'([^']{3,})'(?:\s|$|[.,;:?!])", focus or "")
    return m.group(1) if m else None


class LineStream:
    """Normalised words of a text joined by single spaces, with a line map."""

    def __init__(self, lines: list[str]):
        parts: list[str] = []
        starts = array("l")
        pos = 1  # leading space
        for line in lines:
            starts.append(pos)
            w = " ".join(norm_words(line))
            parts.append(w)
            pos += len(w) + (1 if w else 0)
        self.text = " " + " ".join(p for p in parts if p) + " "
        # starts[i] is the stream offset where line i's words begin. For an empty line,
        # it is where the next words begin, so bisect maps to the right line.
        self.starts = starts

    def line_of(self, pos: int) -> int:
        """0-based line index containing stream offset ``pos``."""
        i = bisect_right(self.starts, pos) - 1
        return max(0, i)


def find_phrase(stream: LineStream, phrase: str, limit: int = 20) -> tuple[int, list[int]]:
    """``(total, [0-based start lines])`` of whole-word occurrences of ``phrase``.

    The phrase needs at least 2 words: a single word is a term query, not a phrase.
    """
    words = norm_words(phrase)
    if len(words) < 2:
        return 0, []
    needle = " " + " ".join(words) + " "
    text = stream.text
    total = 0
    found: list[int] = []
    i = text.find(needle)
    while i >= 0:
        total += 1
        if len(found) < limit:
            ln = stream.line_of(i + 1)
            if not found or found[-1] != ln:
                found.append(ln)
        i = text.find(needle, i + 1)
    return total, found


# ---------------------------------------------------------------------------
# BM25 over sections, backed by a per-file line-level inverted index
# ---------------------------------------------------------------------------


class LineIndex:
    """Word → array of 0-based line numbers (one entry per occurrence), plus line lengths."""

    def __init__(self, lines: list[str]):
        post: dict[str, array] = {}
        lens = array("l")
        for i, line in enumerate(lines):
            toks = tokens(line)
            lens.append(len(toks))
            for t in toks:
                a = post.get(t)
                if a is None:
                    a = post[t] = array("l")
                a.append(i)
        self.post = post
        # prefix[i] = tokens in lines [0, i)
        prefix = array("l", [0])
        run = 0
        for n in lens:
            run += n
            prefix.append(run)
        self.prefix = prefix
        self.n_lines = len(lines)

    def line_hits(self, term: str) -> Counter:
        """0-based line → occurrences of ``term`` (with its suffix matches)."""
        c: Counter = Counter()
        for w in term_matches(term, self.post):
            c.update(self.post[w])
        return c

    def length(self, a: int, b: int) -> int:
        """Tokens in 0-based lines [a, b)."""
        a = max(0, min(a, self.n_lines))
        b = max(a, min(b, self.n_lines))
        return self.prefix[b] - self.prefix[a]


def bm25_sections(
    index: LineIndex,
    sections: list[tuple[str, int, int]],
    query_terms: list[str],
    top: int = 3,
    name_boost: float = 3.0,
) -> list[tuple[float, str, int, int]]:
    """Rank 1-based inclusive ``(name, start, end)`` sections; best ``top`` as ``(score, name, a, b)``.

    Term frequency comes from the inverted index, so the cost is O(hits + sections), not
    O(text). A term in the section's name adds ``name_boost`` (the heading or symbol names
    the topic).
    """
    if not query_terms or not sections:
        return []
    n = len(sections)
    lens = [max(1, index.length(a - 1, b)) for _name, a, b in sections]
    avg = sum(lens) / n or 1.0
    name_terms = [set(tokens(s[0])) for s in sections]
    scores = [0.0] * n
    for t in query_terms:
        hits = index.line_hits(t)
        tf = [0] * n
        if hits:
            # Sorted hit lines + prefix counts: tf of any span is two bisects (spans may overlap).
            lines_sorted = sorted(hits)
            pref = [0]
            for ln0 in lines_sorted:
                pref.append(pref[-1] + hits[ln0])
            for k, (_name, a, b) in enumerate(sections):
                lo = bisect_left(lines_sorted, a - 1)
                hi = bisect_right(lines_sorted, b - 1)
                tf[k] = pref[hi] - pref[lo]
        named = [bool(term_matches(t, name_terms[k])) for k in range(n)]
        df = sum(1 for k in range(n) if tf[k] or named[k])
        if not df:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for k in range(n):
            f = tf[k]
            norm = f * 2.2 / (f + 1.2 * (0.25 + 0.75 * lens[k] / avg)) if f else 0.0
            if norm or named[k]:
                scores[k] += idf * (norm + (name_boost if named[k] else 0.0))
    ranked = [
        (round(scores[k], 3), sections[k][0], sections[k][1], sections[k][2])
        for k in range(n)
        if scores[k] > 0
    ]
    ranked.sort(key=lambda x: (-x[0], x[2]))
    return ranked[:top]


def rank_texts(
    sections: list[tuple[str, str]],
    query_terms: list[str],
    top: int = 3,
    name_boost: float = 3.0,
) -> list[tuple[float, int]]:
    """BM25 over in-memory ``(name, text)`` sections (a fetched page); best ``top`` as ``(score, index)``.

    It has the same word-boundary tf, suffix matching and name boost as :func:`bm25_sections`.
    """
    if not query_terms or not sections:
        return []
    bags = [Counter(tokens(text)) for _name, text in sections]
    names = [set(tokens(name)) for name, _text in sections]
    lens = [max(1, sum(b.values())) for b in bags]
    n = len(sections)
    avg = sum(lens) / n
    scores = [0.0] * n
    for t in query_terms:
        tf = [sum(b[w] for w in term_matches(t, b)) for b in bags]
        named = [bool(term_matches(t, nm)) for nm in names]
        df = sum(1 for k in range(n) if tf[k] or named[k])
        if not df:
            continue
        idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
        for k in range(n):
            f = tf[k]
            norm = f * 2.2 / (f + 1.2 * (0.25 + 0.75 * lens[k] / avg)) if f else 0.0
            if norm or named[k]:
                scores[k] += idf * (norm + (name_boost if named[k] else 0.0))
    ranked = [(round(scores[k], 3), k) for k in range(n) if scores[k] > 0]
    ranked.sort(key=lambda x: (-x[0], x[1]))
    return ranked[:top]


def windows(a0: int, b0: int, size: int = 40, label: str = "") -> list[tuple[str, int, int]]:
    """Half-overlapping line windows over the 1-based inclusive range [a0, b0]."""
    out: list[tuple[str, int, int]] = []
    step = max(1, size // 2)
    a = a0
    while a <= b0:
        b = min(a + size - 1, b0)
        out.append((f"lines {a}-{b}{label}", a, b))
        if b == b0:
            break
        a += step
    return out


# ---------------------------------------------------------------------------
# Outline tree
# ---------------------------------------------------------------------------


@dataclass
class Node:
    title: str
    level: int
    start: int  # 1-based line of the heading
    end: int = 0  # 1-based inclusive last line
    children: list["Node"] = field(default_factory=list)
    synthetic: bool = False  # implied parent of a compound heading ("ACT I" of "ACT I. SCENE 2.")

    def count(self) -> int:
        return 1 + sum(c.count() for c in self.children)


@dataclass
class Outline:
    nodes: list[Node]
    total_lines: int
    boilerplate: list[tuple[int, int]] = field(default_factory=list)  # (lines per copy, copies)
    _flat: Optional[list[tuple[int, int, list[Node]]]] = None

    def __bool__(self) -> bool:
        return bool(self.nodes)

    def walk(self) -> Iterable[tuple[Node, list[Node]]]:
        """(node, ancestors) in document order."""
        stack: list[tuple[Node, list[Node]]] = [(n, []) for n in reversed(self.nodes)]
        while stack:
            node, anc = stack.pop()
            yield node, anc
            for c in reversed(node.children):
                stack.append((c, anc + [node]))

    def path(self, line: int) -> list[Node]:
        """Nodes from the root down to the deepest one containing 1-based ``line``."""
        out: list[Node] = []
        level = self.nodes
        while True:
            hit = None
            lo = bisect_right([n.start for n in level], line) - 1
            if lo >= 0 and level[lo].start <= line <= level[lo].end:
                hit = level[lo]
            if hit is None:
                return out
            out.append(hit)
            level = hit.children

    def address(self, line: int, max_chars: int = 140) -> str:
        """Breadcrumb ``"TITLE > ACT III > SCENE 1"`` for a 1-based line ('' outside any node)."""
        crumbs = [_short(n.title, 60) for n in self.path(line)]
        text = " > ".join(crumbs)
        while len(text) > max_chars and len(crumbs) > 2:
            crumbs.pop(1)
            text = " > ".join([crumbs[0], "…"] + crumbs[1:])
        return text

    def leaves(self) -> list[tuple[str, int, int]]:
        """Each node's own body (heading to first child), as ``(address, start, end)`` sections."""
        out: list[tuple[str, int, int]] = []
        for node, anc in self.walk():
            own_end = node.children[0].start - 1 if node.children else node.end
            if own_end >= node.start:
                name = " > ".join(_short(n.title, 60) for n in anc + [node])
                out.append((name, node.start, own_end))
        return out


def _short(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


# -- heading induction -------------------------------------------------------

# Rank of a structural keyword; lower is higher in the tree.
KEYWORD_RANK = {
    "volume": 1, "vol": 1, "book": 2, "part": 3, "act": 4,
    "chapter": 5, "canto": 5, "stave": 5, "letter": 5, "lecture": 5, "lesson": 5,
    "psalm": 5, "article": 5, "appendix": 5,
    "section": 6, "scene": 6, "prologue": 6, "epilogue": 6, "induction": 6, "chorus": 6,
}
_KW_ALT = "|".join(sorted(KEYWORD_RANK, key=len, reverse=True))
_KW_PAIR = re.compile(
    rf"\b({_KW_ALT})\b\.?(?:\s+((?:[0-9]+|[IVXLCDM]+|[a-z]+)\b)\.?)?", re.I
)
_KW_HEAD = re.compile(rf"^\s*(?:the\s+)?(?:{_KW_ALT})\b", re.I)
_ROMAN = re.compile(r"^[IVXLCDM]+$", re.I)
_BARE_NUM = re.compile(r"^\s*(?:[0-9]{1,4}|[IVXLCDM]{1,7})\.?\s*$")
_NUMBERED = re.compile(r"^\s*((?:[0-9]+\.)*[0-9]+)\.?\s+[A-Z][^.!?]{1,60}$")
_MD_HEAD = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_UNDERLINE = re.compile(r"^\s*(=+|-+|~+|\*+)\s*$")
_END_MARKERS = {"the end", "finis", "end", "the end of the project gutenberg ebook"}
_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
}
INDUCE_MAX_HEADING = 80
# A shape seen fewer times than this is noise, unless it is explicit markup or a keyword.
SHAPE_MIN_COUNT = 2


def roman_to_int(s: str) -> Optional[int]:
    vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    s = s.lower()
    if not s or any(ch not in vals for ch in s):
        return None
    total = 0
    for i, ch in enumerate(s):
        v = vals[ch]
        total += -v if i + 1 < len(s) and vals[s[i + 1]] > v else v
    return total


def num_value(tok: str) -> Optional[int]:
    """'3' / 'III' / 'three' / 'third' → 3."""
    t = (tok or "").strip(".").lower()
    if t.isdigit():
        return int(t)
    if t in _WORD_NUMS:
        return _WORD_NUMS[t]
    if _ROMAN.match(t):
        return roman_to_int(t)
    return None


def _is_caps_title(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 3 or len(s.split()) > 12 or "   " in s:
        return False  # three inner spaces = a table row ("BOY      A HERALD"), not a title
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.9


def _boilerplate_lines(lines: list[str]) -> tuple[set[int], list[tuple[int, int]]]:
    """0-based lines inside paragraphs (2+ lines) repeated 3+ times verbatim.

    Licence banners, page furniture and running headers repeat. Headings never repeat
    as whole paragraphs.
    """
    paras: dict[str, list[tuple[int, int]]] = {}
    i, n = 0, len(lines)
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        j = i
        while j < n and lines[j].strip():
            j += 1
        if j - i >= 2:
            key = "\n".join(l.strip() for l in lines[i:j])
            paras.setdefault(key, []).append((i, j))
        i = j
    out: set[int] = set()
    stats: list[tuple[int, int]] = []
    for spans in paras.values():
        if len(spans) >= 3:
            for a, b in spans:
                out.update(range(a, b))
            stats.append((spans[0][1] - spans[0][0], len(spans)))
    stats.sort(key=lambda s: -s[0] * s[1])
    return out, stats


@dataclass
class _Cand:
    line: int  # 0-based
    text: str
    shape: str
    rank: Optional[float]  # explicit rank (markup / keyword); None = by frequency
    pairs: list[tuple[str, str]] = field(default_factory=list)  # compound keyword pairs


def _candidates(lines: list[str], skip: set[int]) -> list[_Cand]:
    out: list[_Cand] = []
    n = len(lines)
    for i, line in enumerate(lines):
        if i in skip:
            continue
        s = line.strip()
        if not s or len(s) > INDUCE_MAX_HEADING:
            continue
        prev_blank = i == 0 or not lines[i - 1].strip() or (i - 1) in skip
        next_blank = i + 1 >= n or not lines[i + 1].strip()
        md = _MD_HEAD.match(line)
        if md:
            out.append(_Cand(i, md.group(2).strip(), f"md{len(md.group(1))}", float(len(md.group(1)))))
            continue
        if i + 1 < n and prev_blank and _UNDERLINE.match(lines[i + 1]) and len(lines[i + 1].strip()) >= max(3, len(s) // 2):
            ch = lines[i + 1].strip()[0]
            out.append(_Cand(i, s, f"ul{ch}", 1.0 if ch == "=" else 2.0))
            continue
        # A keyword heading may sit under a one-line running title ("THE TEMPEST" / "ACT I. SCENE 1").
        under_title = i >= 2 and not lines[i - 2].strip() and len(lines[i - 1].strip()) <= 60
        if not prev_blank and not (under_title and _KW_HEAD.match(s)):
            continue
        if s.lower().rstrip(".") in _END_MARKERS:
            out.append(_Cand(i, s, "end", None))
            continue
        if _KW_HEAD.match(s):
            pairs = []
            for m in _KW_PAIR.finditer(s):
                kw = m.group(1).lower()
                num = m.group(2) or ""
                # A bare part name is a heading only in capitals ("PROLOGUE"); "Chorus." is a cast entry.
                bare = kw in ("prologue", "epilogue", "induction", "chorus") and _is_caps_title(s)
                if bare or num_value(num) is not None:
                    pairs.append((kw, num))
            # "Chapter 3: The Storm" or "ACT I. SCENE 2." — a keyword followed by a number,
            # or a standalone part name. A sentence that starts with "Part of the" is not one.
            if pairs and (next_blank or len(s) <= 40):
                out.append(_Cand(i, s, "kw:" + pairs[-1][0], float(10 + KEYWORD_RANK[pairs[-1][0]]), pairs))
                continue
        if _BARE_NUM.match(s):
            # A number alone on its line: a numbered piece (sonnet, verse) when set apart by
            # a blank after it or by centring. Years (1000-2100) are dates, not headings.
            if s.isdigit() and len(s) == 4 and 1000 <= int(s) <= 2100:
                continue
            if next_blank or len(line) - len(line.lstrip()) >= 4:
                out.append(_Cand(i, s, "num", None))
            continue
        if not next_blank:
            continue
        if _NUMBERED.match(line):
            depth = _NUMBERED.match(line).group(1).count(".") + 1
            out.append(_Cand(i, s, f"n{depth}", None))
        elif _is_caps_title(s):
            out.append(_Cand(i, s, "caps", None))
    return out


TITLE_LOOKAHEAD = 400  # lines between a title and the first numbered heading it owns


def _opens_sequence(c: _Cand) -> bool:
    """True for a heading that restarts numbering: "ACT I. SCENE 1.", "PROLOGUE", "1"."""
    if c.shape == "num":
        return num_value(c.text) == 1
    if c.pairs:
        kw, num = c.pairs[0]
        return kw in ("prologue", "induction", "chorus") or num_value(num) == 1
    return False


def _has_byline(lines: list[str], i: int) -> bool:
    """A "by <Author>" line within the next 4 lines (the book/title convention)."""
    for k in range(i + 1, min(i + 5, len(lines))):
        t = lines[k].strip()
        if t:
            return t.lower().startswith("by ") and len(t) <= 60
    return False


def _promote_titles(cands: list[_Cand], lines: list[str]) -> dict[int, float]:
    """Instance ranks for titles: a heading that precedes a numbering reset owns that sequence.

    For each heading that restarts numbering ("ACT I", "PROLOGUE", "1"), look at the ALL-CAPS
    lines since the previous numbered heading, within ``TITLE_LOOKAHEAD`` lines. The one
    with a byline ("by ...") wins, else the first one. It is ranked above every keyword
    level. Labels that repeat in the document ("DRAMATIS PERSONAE", "SONG") never qualify.
    Other caps lines keep the low frequency rank, so they nest instead of swallowing what follows.
    """
    kw_ranks = [10.0 + KEYWORD_RANK[kw] for c in cands for kw, _ in c.pairs]
    title_rank = min(kw_ranks + [20.0]) - 1.0
    seen = Counter(" ".join(norm_words(c.text)) for c in cands if c.shape == "caps")
    out: dict[int, float] = {}
    pool: list[_Cand] = []
    for c in cands:
        if c.shape == "end":
            pool = []
            continue
        if c.shape == "num" or c.pairs:
            if _opens_sequence(c):
                near = [t for t in pool if c.line - t.line <= TITLE_LOOKAHEAD]
                if near:
                    pick = next((t for t in near if _has_byline(lines, t.line)), near[0])
                    out[pick.line] = title_rank
            pool = []
            continue
        if c.shape == "caps" and seen[" ".join(norm_words(c.text))] < 3:
            pool.append(c)
    return out

def induce_outline(text: str | list[str], *, min_nodes: int = 2) -> Outline:
    """Heading tree of a plain-text (or Markdown) document. Empty outline when none found.

    Levels: Markdown/setext markup is absolute (1-6). Keyword headings rank by
    ``KEYWORD_RANK`` and sit below markup. Other shapes (ALL-CAPS titles, bare numbers,
    numbered headings) are ordered by frequency (rarer = higher), and placed above
    every keyword shape they enclose. A compound heading ("ACT I. SCENE 2.")
    reuses or synthesises its parent ("ACT I"). An end marker ("THE END") closes all open
    nodes. Nodes with no body and no children (a year line before a title) are dropped.
    """
    lines = text if isinstance(text, list) else text.splitlines()
    total = len(lines)
    skip, stats = _boilerplate_lines(lines)
    cands = _candidates(lines, skip)
    counts = Counter(c.shape for c in cands)
    cands = [
        c for c in cands
        if c.shape == "end" or c.rank is not None or counts[c.shape] >= SHAPE_MIN_COUNT
    ]
    # Frequency shapes: rarer is higher, and all sit below keyword headings by default.
    freq_shapes = sorted(
        {c.shape for c in cands if c.rank is None and c.shape != "end"},
        key=lambda s: (counts[s], s),
    )
    shape_rank = {shape: 20.0 + idx * 0.01 for idx, shape in enumerate(freq_shapes)}
    inst_rank = _promote_titles(cands, lines)
    roots: list[Node] = []
    stack: list[Node] = []

    def close_to(rank: float, line0: int) -> None:
        while stack and stack[-1].level >= rank:
            node = stack.pop()
            node.end = line0  # line0 is 0-based of the new heading = 1-based previous line

    def attach(node: Node) -> None:
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)

    for c in cands:
        if c.shape == "end":
            # Close everything; the marker line belongs to the node it ends.
            while stack:
                stack.pop().end = c.line + 1
            continue
        rank = c.rank if c.rank is not None else inst_rank.get(c.line, shape_rank[c.shape])
        if c.pairs and len(c.pairs) > 1:
            # Compound: ensure ancestors for every pair but the last.
            for kw, num in c.pairs[:-1]:
                prank = 10.0 + KEYWORD_RANK[kw]
                key = f"{kw} {num_value(num) if num_value(num) is not None else num}".strip()
                open_same = next(
                    (n for n in stack if n.level == prank and getattr(n, "_key", None) == key), None
                )
                if open_same is not None:
                    while stack and stack[-1] is not open_same:
                        node = stack.pop()
                        node.end = c.line
                    continue
                close_to(prank, c.line)
                parent = Node(f"{kw.upper()} {num.upper()}".strip(), prank, c.line + 1, synthetic=True)
                parent._key = key  # type: ignore[attr-defined]
                attach(parent)
        elif c.pairs:
            kw, num = c.pairs[0]
            key = f"{kw} {num_value(num) if num_value(num) is not None else num}".strip()
        close_to(rank, c.line)
        title = c.text
        if c.pairs and len(c.pairs) > 1:
            # "ACT III. Scene I." under the synthetic/reused "ACT III" reads as "Scene I.".
            last = list(_KW_PAIR.finditer(c.text))
            if last:
                title = c.text[last[-1].start():].strip() or c.text
        node = Node(title, rank, c.line + 1)
        if c.pairs:
            kw, num = c.pairs[-1]
            node._key = f"{kw} {num_value(num) if num_value(num) is not None else num}".strip()  # type: ignore[attr-defined]
        attach(node)
    while stack:
        stack.pop().end = total
    roots = _prune(roots, lines, skip)
    _renumber(roots)
    if sum(n.count() for n in roots) < min_nodes:
        return Outline([], total, stats)
    return Outline(roots, total, stats)


def _prune(nodes: list[Node], lines: list[str], skip: set[int]) -> list[Node]:
    """Drop empty leaves (no body, no children) and fix ends to stay within parents."""
    out: list[Node] = []
    for node in nodes:
        node.children = _prune(node.children, lines, skip)
        if node.end < node.start:
            node.end = node.start
        if not node.children:
            body = any(
                lines[k].strip() and k not in skip
                for k in range(node.start, min(node.end, len(lines)))
            )
            if not body:
                continue
        out.append(node)
    return out


def _renumber(nodes: list[Node], depth: int = 1) -> None:
    for n in nodes:
        n.level = depth
        _renumber(n.children, depth + 1)


def markdown_outline(lines: list[str]) -> Outline:
    """Markdown ATX headings only (fenced code skipped), as a tree."""
    roots: list[Node] = []
    stack: list[Node] = []
    fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith(("```", "~~~")):
            fence = not fence
            continue
        if fence:
            continue
        m = _MD_HEAD.match(line)
        if not m:
            continue
        lvl = len(m.group(1))
        while stack and stack[-1].level >= lvl:
            stack.pop().end = i
        node = Node(m.group(2).strip(), lvl, i + 1)
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)
    while stack:
        stack.pop().end = len(lines)
    _renumber(roots)
    return Outline(roots, len(lines))


# -- folding ------------------------------------------------------------------


def _row(node: Node, depth: int, folded: bool) -> str:
    span = f"L{node.start}-{node.end}" if node.end != node.start else f"L{node.start}"
    more = f"  +{len(node.children)}" if folded and node.children else ""
    return f"{'  ' * depth}{_short(node.title, 70)}  {span}{more}"


def fold(nodes: list[Node], budget: int = 3000, depth: int = 0) -> list[str]:
    """Outline rows under ``budget`` chars, breadth-first.

    Expand all of the top level, then each next level, but only for nodes whose whole
    child list still fits. A node left folded shows ``+N`` (its child count). The model
    asks for a folded node by title (focus) or by its line span. If even the top level
    does not fit, it is cut with a count of the rest.
    """
    if not nodes:
        return []
    expanded: set[int] = set()
    rows_cost = sum(len(_row(n, depth, True)) + 1 for n in nodes)
    if rows_cost > budget:
        rows: list[str] = []
        used = 0
        for k, n in enumerate(nodes):
            r = _row(n, depth, True)
            if used + len(r) + 1 > budget - 40:
                rows.append(f"{'  ' * depth}… {len(nodes) - k} more (L{n.start}-{nodes[-1].end})")
                break
            rows.append(r)
            used += len(r) + 1
        return rows
    used = rows_cost
    frontier = [(n, depth) for n in nodes]
    while frontier:
        nxt = []
        for n, d in frontier:
            if not n.children:
                continue
            cost = sum(len(_row(c, d + 1, True)) + 1 for c in n.children)
            if used + cost <= budget:
                expanded.add(id(n))
                used += cost
                nxt.extend((c, d + 1) for c in n.children)
        frontier = nxt
    rows = []

    def emit(ns: list[Node], d: int) -> None:
        for n in ns:
            open_ = id(n) in expanded
            rows.append(_row(n, d, not open_))
            if open_:
                emit(n.children, d + 1)

    emit(nodes, depth)
    return rows


# -- node lookup by title/path --------------------------------------------------


def _title_terms(text: str) -> set[str]:
    """Title words, with numbers in every spelling ('III' and 'third' also give '3')."""
    out = set()
    for w in norm_words(text):
        out.add(w)
        if w.isdigit() or w in _WORD_NUMS or (len(w) <= 7 and _ROMAN.match(w)):
            v = num_value(w)
            if v is not None:
                out.add(str(v))
    return out - STOPWORDS


def find_nodes(outline: Outline, focus: str, limit: int = 5) -> list[tuple[Node, list[Node]]]:
    """Nodes whose path (ancestors + own title) covers every focus term.

    The node's own title must match at least one term. Numbers match across forms
    ("act 3 scene 1" finds "ACT III. Scene I."). Keyword-number pairs must pair up
    on the path: "act 3 scene 1" rejects ACT I > Scene III. Deepest first, then document
    order. ``>`` in the focus is allowed as a separator.
    """
    if not outline:
        return []
    parts = [p for p in re.split(r"\s*>\s*", focus or "") if p.strip()]
    want = set()
    for p in parts:
        want |= _title_terms(p)
    if not want:
        return []
    want_pairs = _kw_pairs(focus)
    hits: list[tuple[Node, list[Node]]] = []
    for node, anc in outline.walk():
        own = _title_terms(node.title)
        if not own & want:
            continue
        path_terms = set(own)
        for a in anc:
            path_terms |= _title_terms(a.title)
        if want <= path_terms:
            if want_pairs:
                have = set()
                for a in anc + [node]:
                    have |= _kw_pairs(a.title)
                if not want_pairs <= have:
                    continue
            hits.append((node, anc))
    # Prefer the deepest match on each branch: drop a node when a descendant also matched.
    matched = {id(n) for n, _ in hits}
    hits = [(n, a) for n, a in hits if not any(id(c) in matched for c, _ in _descendants(n))]
    hits.sort(key=lambda h: (-len(h[1]), h[0].start))
    return hits[:limit]


def _kw_pairs(text: str) -> set[tuple[str, int]]:
    """{("act", 3), ("scene", 1)} from "Act III. Scene I." / "act 3 scene 1"."""
    out = set()
    for m in _KW_PAIR.finditer(text or ""):
        v = num_value(m.group(2) or "")
        if v is not None:
            out.add((m.group(1).lower(), v))
    return out


def _descendants(node: Node) -> Iterable[tuple[Node, list[Node]]]:
    for c in node.children:
        yield c, []
        yield from _descendants(c)


# ---------------------------------------------------------------------------
# Code: enclosing symbol of a line
# ---------------------------------------------------------------------------


def enclosing_symbol(symbols: dict, line: int) -> Optional[str]:
    """Name of the smallest symbol span containing 1-based ``line`` (None outside all)."""
    best = None
    best_len = None
    for name, span in (symbols or {}).items():
        try:
            a, b = int(span[0]), int(span[1] if len(span) > 1 else span[0])
        except (TypeError, ValueError, IndexError):
            continue
        if a <= line <= b and (best_len is None or b - a < best_len):
            best, best_len = str(name), b - a
    return best


# ---------------------------------------------------------------------------
# Per-file cache (in-process LRU keyed by path + size + mtime)
# ---------------------------------------------------------------------------


class DocCache:
    """Lazily built per-file artefacts: lines, outline, line index, phrase stream."""

    def __init__(self, lines: list[str], kind: str):
        self.lines = lines
        self.kind = kind
        self._outline: Optional[Outline] = None
        self._index: Optional[LineIndex] = None
        self._stream: Optional[LineStream] = None

    @property
    def outline(self) -> Outline:
        if self._outline is None:
            if self.kind == "markdown":
                self._outline = markdown_outline(self.lines)
            elif self.kind == "text":
                self._outline = induce_outline(self.lines)
            else:
                self._outline = Outline([], len(self.lines))
        return self._outline

    @property
    def index(self) -> LineIndex:
        if self._index is None:
            self._index = LineIndex(self.lines)
        return self._index

    @property
    def stream(self) -> LineStream:
        if self._stream is None:
            self._stream = LineStream(self.lines)
        return self._stream


_LRU: "OrderedDict[tuple, DocCache]" = OrderedDict()
LRU_MAX = 6


def doc_for(path: Path, content: Optional[str] = None, kind: str = "text") -> DocCache:
    """The cached :class:`DocCache` for ``path``. It is rebuilt when size or mtime change."""
    try:
        st = path.stat()
        key = (str(path.resolve()), st.st_size, st.st_mtime_ns, kind)
    except OSError:
        key = (str(path), len(content or ""), 0, kind)
    hit = _LRU.get(key)
    if hit is not None:
        _LRU.move_to_end(key)
        return hit
    if content is None:
        content = path.read_text(encoding="utf-8", errors="replace")
    doc = DocCache(content.splitlines(), kind)
    _LRU[key] = doc
    while len(_LRU) > LRU_MAX:
        _LRU.popitem(last=False)
    return doc

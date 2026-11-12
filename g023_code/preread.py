"""Pre-emptive reads: the ReadFile calls a request makes predictable, run before call 1.

Live runs show the first model call of a coding turn is almost always the same
hop: ReadFile of the files the request names, of the module a named test file
imports, or of the file that defines a named function. That hop is paid in
full (the model writes the calls, then the whole context is replayed), yet the
harness can predict it from the request text alone.

``plan`` returns tool argument dicts (ReadFile; one SearchContent), in tiers of
falling precision:

* T1 paths the request names that exist, are text and fit in one whole read;
* T2 project modules a named Python *test* file imports (test -> subject);
  T2b the reverse: a named source file's unique ``test_<name>.py`` when the
  request mentions tests (a partial read set saves no hop: live, the model
  read the test file itself when only the source was pre-read);
* T3 the unique definition of a code-like identifier the request names: the
  whole file when small, else ``focus=<name>`` (just the definition body);
* T4 (only when the request names no path and nothing above fired) a word
  that is the stem of exactly one small source file whose folder the request
  also names (``counts`` ... ``in textkit`` -> ``textkit/count.py``) or that
  the request uses as a code noun (``the slug helper``), read whole. Generic stems (main, utils, config, ...)
  never match. A big file is never pre-read as an outline: live, the model
  asked for a line range anyway, so the outline saved no hop;
* T1b ``README``/``Makefile``-style words name a top-level file the same way,
  but only as a complement to another prediction (README is where a change
  lands, not what the request is about: alone it saved no hop);
* T5 a rename / every-reference request about a uniquely defined identifier:
  ``SearchContent`` for it (the model's first hop on every such live run).

Bounded: at most ``MAX_READS`` reads and ``TOTAL_MAX_CHARS`` of files, plus
one search. Entries are tool argument dicts; ``tool_of`` names the tool. The
orchestrator runs them through the normal tool path (permission, codec, view
tracking, repeat guard) and records them either as the model's own tool
exchange (``ghost``) or as text on the request (``prose``); setting
``preread`` (env ``G023_PREREAD``). Docs: docs/preread.md.

Why caps this small: a whole read the model then ignores is paid once as a
cache miss ($0.15/M) — ~1k tokens of unused file costs more than the ~$0.0001
hop a correct guess saves, so only high-precision tiers are used.
"""

from __future__ import annotations

import ast
import re
import time
from pathlib import Path
from typing import Iterable, Optional

from . import callees, closure, near_miss, orient

MAX_READS = 4
WHOLE_MAX_CHARS = 4_000  # == file_reader.LOCAL_FOCUS_WHOLE_FILE_MAX_CHARS: {path} returns the file whole
TOTAL_MAX_CHARS = 12_000
DEF_SCAN_BUDGET_S = 0.4
DEF_SCAN_MAX_BYTES = 12_000_000
SEARCH_MAX_MATCHES = 40
MAX_FILES_SCANNED = 20_000
GHOST_PREFIX = "ghost_"
_WORD = re.compile(r"(?<![\w./-])([A-Za-z][A-Za-z0-9]{3,})(?![\w/])")
SOURCE_SUFFIXES = frozenset(
    ".py .pyi .js .jsx .ts .tsx .go .rs .rb .java .kt .c .h .cc .cpp .hpp .cs .php .swift .scala .lua .sh".split())
# A request to change every use of a name: its first hop is a search for the name.
_ALL_REFS = re.compile(
    r"\brenam(?:e|ing)\b|\b(?:every|all)\s+(?:the\s+)?(?:references?|uses|usages|callers|call sites|occurrences)\b"
    r"|\beverywhere\b",
    re.IGNORECASE,
)
# The request asks for a test to be written or run (T2b: pull in the named source's test file).
_WANTS_TEST = re.compile(r"\btests?\b|\bpytest\b", re.IGNORECASE)
# "the slug helper", "the pricing module": a word used as the name of a piece of code.
_CODE_NOUN = r"(?:helpers?|modules?|functions?|files?|class(?:es)?|code|logic|utils?|parser|routines?|methods?)\b"
# Stems so common across projects that matching one is a guess, not a prediction.
GENERIC_STEMS = frozenset(
    "main utils util helpers helper common base core index init setup types type models model views view "
    "tests test data file files code function functions class module modules app config settings constants "
    "errors error api client server lib misc tools tool run build version".split())

# Follows the ghost results as a user text block. It is required on the wire
# (DeepSeek thinking mode 400s on an assistant tool_use turn with no thinking
# block unless user text follows its results) and says where the reads came
# from. One clause, no instruction: the exchange itself is the signal.
GHOST_NOTE = "[the harness ran the reads above for your request; their results are current]"
PROSE_HEAD = "[pre-read by the harness for this request; current file contents]"


def mode() -> str:
    try:
        from .config import settings

        m = str(getattr(settings, "preread", "off") or "off").lower()
    except Exception:
        return "off"
    return m if m in ("ghost", "prose") else "off"


def is_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _text_size(path: Path) -> Optional[int]:
    """Character count of a UTF-8 text file, or None for binary/unreadable."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    try:
        return len(data.decode("utf-8"))
    except UnicodeDecodeError:
        return None


def _inside(path: Path, root: Path) -> Optional[str]:
    try:
        return path.resolve().relative_to(root).as_posix()
    except (ValueError, OSError):
        return None


def subjects(path: Path, root: Path) -> list[Path]:
    """Project modules a Python file imports (``import a.b``, ``from a import b``), in source order.

    ``from pkg import mod`` resolves to ``pkg/mod.py`` when that is a module,
    else to ``pkg/__init__.py``. Standard library and site packages never
    resolve (they are outside ``root``).
    """
    tree = callees._parse(path)
    if tree is None:
        return []
    out: list[Path] = []

    def add(p: Optional[Path]) -> None:
        if p is not None and p not in out and p != path.resolve():
            out.append(p)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                add(callees._resolve_in(root, alias.name.split("."), root))
        elif isinstance(node, ast.ImportFrom):
            mod = callees._resolve_module(path, root, node.module, node.level or 0)
            hit_sub = False
            if mod is not None and mod.name == "__init__.py":
                for alias in node.names:
                    sub = callees._resolve_in(mod.parent, [alias.name], root)
                    if sub is not None and sub.name != "__init__.py":
                        add(sub)
                        hit_sub = True
            if not hit_sub:
                add(mod)
    return out


def _defining_files(names: list, root: Path, budget_s: float = DEF_SCAN_BUDGET_S) -> dict:
    """name -> [rel paths that define it] (bounded scan, same matcher as orient/near_miss)."""
    found: dict = {n: [] for n in names}
    if not names:
        return found
    deadline = time.monotonic() + budget_s
    spent = 0
    for f in closure._project_files(root):
        if time.monotonic() > deadline or spent > DEF_SCAN_MAX_BYTES:
            break
        body = closure._read(f)
        if body is None:
            continue
        spent += len(body)
        present = [n for n in names if n in body]
        if not present:
            continue
        rel = f.relative_to(root).as_posix()
        for dn, _line in near_miss.defined_names(body):
            if dn in present and rel not in found[dn]:
                found[dn].append(rel)
    return found


def _test_files_for(sources: list, root: Path) -> list:
    """For each source ``x.py`` the unique project test file ``test_x.py`` / ``x_test.py``, if there is one."""
    want = {}
    for rel in sources:
        stem = rel.rsplit("/", 1)[-1][:-3]
        want[f"test_{stem}.py"] = rel
        want[f"{stem}_test.py"] = rel
    hits: dict = {}
    for i, f in enumerate(closure._project_files(root)):
        if i >= MAX_FILES_SCANNED:
            break
        if f.name in want:
            hits.setdefault(want[f.name], []).append(f.relative_to(root).as_posix())
    return [h[0] for rel in sources for h in [hits.get(rel) or []] if len(h) == 1]


def _stem(word: str) -> str:
    w = word.lower()
    if len(w) > 5 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _stem_index(root: Path) -> tuple[dict, dict]:
    """(top-level file stem -> [rel], source-file stem (lower) -> [rel]) for T1b/T4."""
    top: dict = {}
    src: dict = {}
    for i, f in enumerate(closure._project_files(root)):
        if i >= MAX_FILES_SCANNED:
            break
        rel = f.relative_to(root).as_posix()
        stem = f.name.split(".", 1)[0]
        if "/" not in rel:
            top.setdefault(stem, []).append(rel)
        if f.suffix.lower() in SOURCE_SUFFIXES and not is_test(rel) and "/tests/" not in "/" + rel:
            src.setdefault(stem.lower(), []).append(rel)
    return top, src


def plan(text: str, root: Path, max_reads: int = MAX_READS, total_max: int = TOTAL_MAX_CHARS) -> list[dict]:
    """Tool argument dicts (see ``tool_of``) the request makes predictable, highest precision first."""
    root = root.resolve()
    text = text or ""
    names, paths = orient.request_names(text)
    reads: list[dict] = []
    seen: set = set()
    budget = [total_max]

    def take(rel: str, size: int, focus: str = "") -> bool:
        if len(reads) >= max_reads or rel in seen:
            return False
        cost = size if not focus else min(size, 2_400)
        if cost > budget[0]:
            return False
        budget[0] -= cost
        seen.add(rel)
        reads.append({"path": rel, "focus": focus} if focus else {"path": rel})
        return True

    tests: list[Path] = []
    for p in paths:  # T1
        f = root / p
        rel = _inside(f, root)
        if rel is None or not f.is_file():
            continue
        size = _text_size(f)
        if size is None or size == 0 or size > WHOLE_MAX_CHARS:
            continue
        if take(rel, size) and is_test(rel):
            tests.append(f.resolve())
    words = [m.group(1) for m in _WORD.finditer(text)]
    index: Optional[tuple] = None
    tops: list = []
    if words:
        index = _stem_index(root)
        for w in words:  # T1b: README, Makefile, CHANGELOG ... (taken below, as a complement)
            if w.islower() or w.lower() in GENERIC_STEMS:
                continue
            hit = index[0].get(w) or []
            if len(hit) == 1 and hit[0] not in tops:
                tops.append(hit[0])
    for t in tests:  # T2
        for sub in subjects(t, root):
            rel = _inside(sub, root)
            size = _text_size(sub) if rel else None
            if rel and size and size <= WHOLE_MAX_CHARS:
                take(rel, size)
    sources = [r["path"] for r in reads if r["path"].endswith(".py") and not is_test(r["path"])]
    if sources and not tests and _WANTS_TEST.search(text):  # T2b: a named source -> its test file
        for rel in _test_files_for(sources, root):
            size = _text_size(root / rel)
            if size and size <= WHOLE_MAX_CHARS:
                take(rel, size)
    defs: dict = {}
    if names and len(reads) < max_reads:  # T3
        defs = _defining_files(names, root)
        for n in names:
            where = defs.get(n) or []
            if len(where) != 1:
                continue  # undefined or ambiguous: a guess, not a prediction
            rel = where[0]
            if rel in seen:
                continue
            size = _text_size(root / rel)
            if not size:
                continue
            if size <= WHOLE_MAX_CHARS:
                take(rel, size)
            else:
                take(rel, size, focus=n)
    if not paths and not reads and index is not None:  # T4: only when nothing explicit was named
        lowered = {t.lower() for t in re.findall(r"[A-Za-z_][\w-]*", text)}
        for w in words:
            st = _stem(w)
            if not w.islower() or st in GENERIC_STEMS or len(st) < 4:
                continue
            hit = index[1].get(st) or index[1].get(w.lower()) or []
            if len(hit) != 1:
                continue
            if not (set(hit[0].lower().split("/")[:-1]) & lowered) and not re.search(
                    rf"\b{re.escape(w)}\s+{_CODE_NOUN}", text, re.IGNORECASE):
                continue  # names the folder ("counts ... in textkit") or a code noun ("the slug helper")
            size = _text_size(root / hit[0])
            if not size or size > WHOLE_MAX_CHARS:
                continue
            take(hit[0], size)
            if len(reads) >= 2:
                break
    if reads:  # T1b only next to a real prediction
        for rel in tops:
            size = _text_size(root / rel)
            if size and size <= WHOLE_MAX_CHARS:
                take(rel, size)
    if _ALL_REFS.search(text):  # T5
        for n in names:
            where = defs.get(n) or []
            if len(where) == 1:
                top = where[0].split("/", 1)[0] if "/" in where[0] else ""
                search = {"query": n, "max_matches": SEARCH_MAX_MATCHES}
                if top:
                    search["path"] = top
                reads.append(search)
                break
    return reads


def tool_of(args: dict) -> str:
    """The tool a plan entry is for: SearchContent entries carry ``query``."""
    return "SearchContent" if "query" in args else "ReadFile"


def ghost_id(turn: int, index: int) -> str:
    return f"{GHOST_PREFIX}{turn}_{index}"


def prose_block(pairs: Iterable[tuple]) -> str:
    """The prose form: one block per (args, result) appended to the request."""
    parts = [PROSE_HEAD]
    for args, result in pairs:
        if tool_of(args) == "SearchContent":
            label = f"SearchContent {args['query']}" + (f" in {args['path']}" if args.get("path") else "")
        else:
            label = "ReadFile " + args.get("path", "") + (f" (focus {args['focus']})" if args.get("focus") else "")
        parts.append(f"--- {label}\n{result}")
    return "\n\n".join(parts)


def used(ghost_paths: Iterable[str], later_args: Iterable[dict], answer: str = "") -> dict:
    """Which ghost-read paths the model then touched (edited, re-read, cited). For hop_stats."""
    ghost = list(dict.fromkeys(ghost_paths))
    touched = set()
    for a in later_args:
        p = str(a.get("path") or "")
        for g in ghost:
            if p and (p == g or p.endswith("/" + g) or g.endswith("/" + p)):
                touched.add(g)
        cmd = str(a.get("command") or "")
        for g in ghost:
            if g in cmd:
                touched.add(g)
    for g in ghost:
        if g in (answer or "") or g.rsplit("/", 1)[-1] in (answer or ""):
            touched.add(g)
    return {"ghost": len(ghost), "used": len(touched)}

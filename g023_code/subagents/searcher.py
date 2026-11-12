"""
Searcher Subagent

SearchContent uses ``rg --json`` when ripgrep is on PATH, else ``grep -R``,
else a Python walk. Results report a total occurrence count separately from a
sample list so a sample cap is not presented as a total cap. ``.gitignore`` is
honoured. Glob/brace file find lives here too (Glob tool).
"""

from __future__ import annotations

import asyncio
import fnmatch
import glob as globmod
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Iterable, List, Optional

from .. import docindex, near_miss
from ..config import get_project_root
from ..paths import confine_or_error


IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".g023", ".deepcode", ".idea", ".vscode", "target", "vendor",
}
IGNORE_EXTS = {
    ".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".o", ".a",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
    ".zip", ".tar", ".gz",
}

_BRACE = re.compile(r"\{([^{}]+)\}")
DEFAULT_MAX_SAMPLES = 12
MAX_SAMPLES_CAP = 60
COUNT_CEILING = 50_000


def search_backend_name() -> str:
    if shutil.which("rg"):
        return "rg"
    if shutil.which("grep"):
        return "grep"
    return "walk"


def expand_braces(pattern: str) -> List[str]:
    """One-level-or-nested brace expansion: ``*.{ts,js}`` → ``['*.ts', '*.js']``."""
    match = _BRACE.search(pattern or "")
    if not match:
        return [pattern]
    out: List[str] = []
    for alt in match.group(1).split(","):
        expanded = pattern[: match.start()] + alt.strip() + pattern[match.end() :]
        out.extend(expand_braces(expanded))
    return out or [pattern]


def _split_glob_list(file_glob: str) -> List[str]:
    """Split on commas that are not inside ``{...}``."""
    parts: List[str] = []
    buf: List[str] = []
    depth = 0
    for ch in file_glob or "":
        if ch == "{":
            depth += 1
            buf.append(ch)
        elif ch == "}":
            depth = max(0, depth - 1)
            buf.append(ch)
        elif ch == "," and depth == 0:
            piece = "".join(buf).strip()
            if piece:
                parts.append(piece)
            buf = []
        else:
            buf.append(ch)
    piece = "".join(buf).strip()
    if piece:
        parts.append(piece)
    return parts


def _glob_patterns(file_glob: str) -> List[str]:
    """
    Normalise what a caller might plausibly pass as a glob.

    Accepts ``*.py``, ``py``, ``.py``, ``*.py,*.md``, ``src/**/*.ts``,
    ``**/*.{ts,js}``.
    """
    patterns: List[str] = []
    for raw in _split_glob_list(file_glob or ""):
        for expanded in expand_braces(raw):
            piece = expanded.strip()
            if not piece:
                continue
            if "*" not in piece and "?" not in piece and "[" not in piece:
                piece = f"*.{piece.lstrip('.')}"
            patterns.append(piece)
    return patterns


# Readable islands inside otherwise-ignored `.g023/`. Posts and notebook
# bodies stay out of the standing prompt; SearchContent still has to find them.
OVERLAY_G023_DIRS = frozenset({"memory", "bbs"})


def _under_g023_overlay(rel: Path) -> bool:
    parts = rel.parts
    return len(parts) >= 2 and parts[0] == ".g023" and parts[1] in OVERLAY_G023_DIRS


def _parents_blocked(rel: Path) -> bool:
    parent = rel.parts[:-1]
    if len(parent) >= 2 and parent[0] == ".g023" and parent[1] in OVERLAY_G023_DIRS:
        return False
    if parent[:1] == (".g023",):
        return True
    return bool(set(parent) & IGNORE_DIRS)


def _should_skip(path: Path, root: Optional[Path] = None) -> bool:
    relative = path
    if root is not None:
        try:
            relative = path.relative_to(root)
        except ValueError:
            pass
    if _parents_blocked(relative):
        return True
    if path.suffix.lower() in IGNORE_EXTS:
        return True
    return False


def _walk_files(root: Path):
    root_res = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        directory = Path(dirpath)
        try:
            rel = directory.resolve().relative_to(root_res)
            rel_s = rel.as_posix()
        except ValueError:
            rel_s = "."
        if rel_s in (".", ""):
            keep = [d for d in dirnames if d not in IGNORE_DIRS or d == ".g023"]
        elif rel_s == ".g023":
            keep = [d for d in dirnames if d in OVERLAY_G023_DIRS]
        elif rel_s.startswith(".g023/") and rel_s.split("/")[1] in OVERLAY_G023_DIRS:
            keep = [d for d in dirnames if d not in IGNORE_DIRS]
            if rel_s == ".g023/bbs":
                keep = [d for d in keep if d != "bbs_tools"]
        else:
            keep = [d for d in dirnames if d not in IGNORE_DIRS]
        dirnames[:] = sorted(keep)
        for name in sorted(filenames):
            yield directory / name


def _git_match(rel: str, pat: str) -> bool:
    pat = pat.lstrip("/")
    if pat.endswith("/"):
        pat = pat.rstrip("/")
        return (
            rel == pat
            or rel.startswith(pat + "/")
            or fnmatch.fnmatch(rel, pat)
            or fnmatch.fnmatch(rel, pat + "/**")
        )
    name = Path(rel).name
    if "/" not in pat:
        return fnmatch.fnmatch(name, pat) or fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(
            rel, f"**/{pat}"
        )
    return fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(rel, f"**/{pat}")


def build_ignorer(
    root: Path, *, allow_dirs: frozenset = frozenset(), allow_exts: frozenset = frozenset()
) -> Callable[[Path], bool]:
    """True when the path should be omitted (IGNORE_DIRS / .gitignore).

    ``allow_dirs`` / ``allow_exts`` lift the defaults for names a caller asked
    for literally (Glob ``vendor/**/*.png`` wants vendor/ and .png files).
    """
    rules: list[tuple[str, bool]] = []
    gi = root / ".gitignore"
    if gi.is_file():
        try:
            text = gi.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        for line in text.splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            negate = raw.startswith("!")
            if negate:
                raw = raw[1:]
            rules.append((raw, negate))

    root_s = os.path.realpath(root)
    root_prefix = root_s.rstrip(os.sep) + os.sep

    def ignored(path: Path) -> bool:
        # os.path string ops: Path.resolve()/relative_to() cost ~0.5 ms per call
        # on Python 3.12, which made Glob over 4,000 hits take seconds.
        real = os.path.realpath(path)
        if real == root_s:
            rel = Path(".")
        elif real.startswith(root_prefix):
            rel = Path(real[len(root_prefix):])
        else:
            return True
        if _under_g023_overlay(rel):
            if "bbs_tools" in rel.parts:
                return True
            if path.is_file() and path.suffix.lower() in IGNORE_EXTS - allow_exts:
                return True
            return False
        if (set(rel.parts) & IGNORE_DIRS) - allow_dirs:
            return True
        if path.is_file() and path.suffix.lower() in IGNORE_EXTS - allow_exts:
            return True
        rel_s = rel.as_posix()
        prefixes = [rel_s]
        parts = rel.parts
        for i in range(len(parts)):
            prefixes.append("/".join(parts[: i + 1]))
        matched = False
        for prefix in prefixes:
            if prefix.rsplit("/", 1)[-1] in allow_dirs:
                continue
            for pat, negate in rules:
                if _git_match(prefix, pat):
                    matched = not negate
        return matched

    return ignored


def _matches_glob(path: Path, root: Path, patterns: List[str]) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    for pattern in patterns:
        for expanded in expand_braces(pattern):
            if path.match(expanded) or relative.match(expanded):
                return True
            name = path.name
            if fnmatch.fnmatch(name, expanded) or fnmatch.fnmatch(
                relative.as_posix(), expanded
            ):
                return True
            if "/" not in expanded.rstrip("/") and fnmatch.fnmatch(
                relative.as_posix(), f"**/{expanded}"
            ):
                return True
    return False


def _compile_query(query: str) -> re.Pattern:
    try:
        return re.compile(query, re.IGNORECASE)
    except re.error:
        return re.compile(re.escape(query), re.IGNORECASE)


# (path, mtime_ns) -> (outline or None, symbols) for the files samples came from.
_WHERE_CACHE: dict[tuple[str, int], tuple] = {}
WHERE_MAX_BYTES = 2_000_000  # code: regex symbol scan
WHERE_TEXT_MAX_BYTES = 32_000_000  # text: heading induction is ~0.1 s per 5 MB


def _where_index(path: Path, lines: Optional[list]) -> tuple:
    """Outline (text) or symbol spans (code) of ``path``, cached by mtime. Local, no API."""
    try:
        st = path.stat()
    except OSError:
        return None, {}
    key = (str(path), st.st_mtime_ns)
    hit = _WHERE_CACHE.get(key)
    if hit is not None:
        return hit
    out: tuple = (None, {})
    from .file_reader import TEXT_LANGUAGES, _detect_language, _local_metadata

    lang = _detect_language(path)
    cap = WHERE_TEXT_MAX_BYTES if lang in TEXT_LANGUAGES else WHERE_MAX_BYTES
    if lines is not None and st.st_size <= cap:
        content = "\n".join(lines)
        if lang in TEXT_LANGUAGES:
            outline = docindex.doc_for(path, content, lang).outline
            out = (outline if outline else None, {})
        else:
            out = (None, _local_metadata(path, content).get("symbols") or {})
    if len(_WHERE_CACHE) > 64:
        _WHERE_CACHE.clear()
    _WHERE_CACHE[key] = out
    return out


def _where(path: Path, lines: Optional[list], line_no: int, text: str) -> str:
    """Breadcrumb of a hit: its section path (text) or enclosing function (code)."""
    outline, symbols = _where_index(path, lines)
    if outline is not None:
        return outline.address(line_no)
    name = docindex.enclosing_symbol(symbols, line_no)
    # A definition's own first line needs no "in itself".
    if name and int(symbols[name][0]) != line_no:
        return name
    return ""


def _sample_from_line(path: Path, root: Path, line_no: int, line: str, lines: Optional[list] = None) -> dict:
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        rel = str(path)
    before: list[str] = []
    after: list[str] = []
    if lines is not None:
        before = [l.strip()[:120] for l in lines[max(0, line_no - 2) : line_no - 1]]
        after = [l.strip()[:120] for l in lines[line_no : line_no + 2]]
    sample = {
        "file": rel,
        "line": line_no,
        "match": line.strip()[:200],
        "context_before": before,
        "context_after": after,
    }
    where = _where(path, lines, line_no, line) if lines is not None else ""
    if where:
        sample["in"] = where
    return sample


# Candidates kept per search for ranking; the total keeps counting past it.
CANDIDATE_CAP = 2_000
UNSHOWN_FILES_MAX = 20
_DEF_LINE = re.compile(
    r"^\s*(?:export\s+(?:default\s+)?)?(?:pub\s+)?"
    r"(?:async\s+def|def|class|function|func|fn|interface|struct|enum|trait|type|impl"
    r"|const|let|var)\b"
)
_ASSIGN_LINE = re.compile(r"^\s*[A-Za-z_][\w.]*\s*(?::[^=]+)?=[^=]")
# C/C++ at column 0: ``void do_goto( CHAR_DATA *ch, ... )`` (not ``;``-terminated),
# and ``#define NAME``. A call or prototype never starts a definition line like that.
_C_DEF_LINE = re.compile(
    r"^(?:[A-Za-z_][\w*&:<>, \t]*?[ \t*&])(~?[A-Za-z_]\w*(?:::~?\w+)*)[ \t]*\("
)
_C_DEFINE = re.compile(r"^\s*#\s*define\s+\w")
_C_NOT_NAMES = frozenset("if while for switch return sizeof else do case".split())
# A statement, not a definition head: ``return True, f(x)``, ``await g(y)``.
_C_NOT_LEADS = frozenset("return else case new delete throw await yield raise assert print not".split())
_HEAD_END = re.compile(r"[(=:{<]")
_DOC_EXTS = {".md", ".txt", ".rst", ".adoc", ".log", ".csv"}
# Copies of the tree (release snapshots, backups) rank after the live source.
_ARCHIVE_DIRS = {"releases", "release", "backup", "backups", "archive", "old", "dist"}


def _is_definition(text: str, pattern: Optional[re.Pattern]) -> bool:
    """The match names what the line defines: ``def q(``, ``class Q``, ``Q = ...``."""
    c_def = _C_DEFINE.match(text) or (
        (m := _C_DEF_LINE.match(text)) is not None
        and m.group(1) not in _C_NOT_NAMES
        and text.split(None, 1)[0] not in _C_NOT_LEADS
        and not text.rstrip().endswith(";")
        # A C definition head ends in ``)``, ``{`` or ``,`` (args wrap); prose
        # like "DeepSeek surge (peak/off-peak) pricing windows." does not.
        and text.rstrip()[-1:] in (")", "{", ",", "(")
    )
    if not (c_def or _DEF_LINE.match(text) or _ASSIGN_LINE.match(text)):
        return False
    if pattern is None:
        return True
    cut = _HEAD_END.search(text)
    head = text[: cut.start()] if cut else text
    return bool(pattern.search(head))


_KEYWORD_DEF = re.compile(
    r"^(?:export\s+(?:default\s+)?)?(?:pub\s+)?"
    r"(?:async\s+def|def|class|function|func|fn|interface|struct|enum|trait|type|impl)\b"
)
_IDENT = re.compile(r"[A-Za-z_]\w*")


def _definition_tier(text: str, pattern: Optional[re.Pattern]) -> tuple[int, int]:
    """(tier, exact): what the matched line defines, lower is stronger.

    0 a def/class/function (or a C function body) named by the query; 1 a
    module-level name (``NAME = ...``, ``const``, ``#define``); 2 an indented
    local assignment; 3 a plain use. ``exact`` is 0 when a defined name equals
    the query instead of merely containing it (``surge`` vs ``surge_badge``).
    """
    if not _is_definition(text, pattern):
        return 3, 1
    body = text.lstrip()
    indented = len(body) < len(text)
    if _KEYWORD_DEF.match(body):
        tier = 0
    elif _C_DEFINE.match(text):
        tier = 1
    elif not indented and _C_DEF_LINE.match(text) and not _ASSIGN_LINE.match(text):
        tier = 0
    else:
        tier = 2 if indented else 1
    exact = 1
    if pattern is not None:
        cut = _HEAD_END.search(text)
        head = text[: cut.start()] if cut else text
        if any(pattern.fullmatch(name) for name in _IDENT.findall(head)):
            exact = 0
    return tier, exact


def search_tiers_enabled() -> bool:
    from ..config import settings

    return bool(getattr(settings, "search_tiers", False))


def _candidate_rank(
    rel: str, text: str, pattern: Optional[re.Pattern] = None, tiers: bool = False
) -> tuple[int, ...]:
    """Lower is better: live tree, then code over prose, then definitions.

    With ``tiers`` the definition flag becomes :func:`_definition_tier`, so a
    ``def`` named by the query outranks a local ``name = ...`` that merely
    mentions it (before, both tied and file order decided).
    """
    parts = set(Path(rel).parts[:-1])
    archive = 1 if parts & _ARCHIVE_DIRS else 0
    doc = 1 if Path(rel).suffix.lower() in _DOC_EXTS else 0
    if tiers:
        return (archive, doc) + _definition_tier(text, pattern)
    kind = 0 if _is_definition(text, pattern) else 1
    return archive, doc, kind


# Definition peek: the body of the one definition a search plainly points at.
PEEK_MAX_LINES = 30
PEEK_MAX_CHARS = 2_400


def def_peek_enabled() -> bool:
    from ..config import settings

    return bool(getattr(settings, "def_peek", False))


def _body_span(lines: list[str], start: int) -> tuple[int, int]:
    """1-based inclusive span of the block headed at ``start`` (indentation scope).

    The block ends before the first non-blank line indented no deeper than the
    head; a closing ``}``/``)``/``]`` at the head's depth is kept (C, JS, Rust).
    """
    head = lines[start - 1]
    base = len(head) - len(head.lstrip())
    end = start
    j = start
    while j < len(lines):
        line = lines[j]
        j += 1
        body = line.lstrip()
        if not body:
            continue
        if len(line) - len(body) <= base:
            if body[0] in ")}]":
                end = j
            break
        end = j
    return start, end


def definition_peek(samples: list, pattern: Optional[re.Pattern], root: Path) -> Optional[dict]:
    """Body of the definition a search names unambiguously, or None.

    Fires when exactly one shown definition's name *equals* a query term, or
    when only one shown line is a definition at all. Any other shape (two
    exact names, several partial ones) is a guess, and a wrong guess costs
    the tokens without saving the follow-up read.
    """
    defs: list[tuple[int, dict]] = []
    for smp in samples:
        if not isinstance(smp, dict):
            continue
        rel = str(smp.get("file") or "")
        rank = _candidate_rank(rel, str(smp.get("match") or ""), pattern, True)
        if rank[0] or rank[1] or rank[2] != 0:
            continue
        defs.append((rank[3], smp))
    exact = [smp for e, smp in defs if e == 0]
    if len(exact) == 1:
        pick = exact[0]
    elif not exact and len(defs) == 1:
        pick = defs[0][1]
    else:
        return None
    path = root / str(pick["file"])
    lines = _file_lines(path)
    try:
        start = int(pick.get("line") or 0)
    except (TypeError, ValueError):
        return None
    if not lines or not 1 <= start <= len(lines):
        return None
    a, b = _body_span(lines, start)
    if b <= start + 1:
        return None  # the sample's context line already shows the whole body
    indent = len(lines[start - 1]) - len(lines[start - 1].lstrip())
    while a > 1 and lines[a - 2].lstrip().startswith("@") and (
        len(lines[a - 2]) - len(lines[a - 2].lstrip()) == indent
    ):
        a -= 1  # decorators belong to the definition
    shown = min(b, a + PEEK_MAX_LINES - 1)
    body: list[str] = []
    used = 0
    for n in range(a, shown + 1):
        text = lines[n - 1].rstrip()[:200]
        if used + len(text) > PEEK_MAX_CHARS and body:
            shown = n - 1
            break
        body.append(text)
        used += len(text) + 1
    out = {"file": pick["file"], "start": a, "end": shown, "lines": body}
    if shown < b:
        out["more_lines"] = b - shown
    return out


def _rel(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return str(path)


def _pick_samples(
    candidates: list[tuple[Path, int, str]],
    root: Path,
    max_matches: int,
    pattern: Optional[re.Pattern] = None,
) -> tuple[list[dict], dict[str, int]]:
    """Definition-first, file-diverse choice of ``max_matches`` candidates.

    Round-robin over files ordered by their best candidate, so a cap of 12
    covers up to 12 files instead of the first file's 12 hits. Returns the
    samples (with ±2 context lines) and ``{file: count}`` for matching files
    that got no sample.
    """
    per_file: dict[str, list[tuple[tuple, Path, int, str]]] = {}
    counts: dict[str, int] = {}
    tiers = search_tiers_enabled()
    for path, line_no, text in candidates:
        rel = _rel(path, root)
        counts[rel] = counts.get(rel, 0) + 1
        per_file.setdefault(rel, []).append(
            (_candidate_rank(rel, text, pattern, tiers), path, line_no, text)
        )
    for rows in per_file.values():
        rows.sort(key=lambda r: (r[0], r[2]))

    def _file_key(f: str) -> tuple:
        if not tiers:
            return per_file[f][0][0], f
        # Same best tier: a file named by the query (surge.py for "surge") first.
        stem = 0 if pattern is not None and pattern.search(Path(f).stem) else 1
        return per_file[f][0][0], stem, f

    order = sorted(per_file, key=_file_key)
    chosen: dict[str, list] = {f: [] for f in order}
    taken = 0
    depth = 0
    while taken < max_matches:
        progressed = False
        for f in order:
            rows = per_file[f]
            if depth < len(rows) and taken < max_matches:
                chosen[f].append(rows[depth])
                taken += 1
                progressed = True
        if not progressed:
            break
        depth += 1
    samples: list[dict] = []
    for f in order:
        if not chosen[f]:
            continue
        lines = _file_lines(chosen[f][0][1])
        for _, path, line_no, text in sorted(chosen[f], key=lambda r: r[2]):
            samples.append(_sample_from_line(path, root, line_no, text, lines))
    unshown = {f: counts[f] for f in order if not chosen[f]}
    if len(unshown) > UNSHOWN_FILES_MAX:
        unshown = dict(list(unshown.items())[:UNSHOWN_FILES_MAX])
    return samples, unshown


def _file_lines(path: Path) -> Optional[list[str]]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _search_file_candidates(
    path: Path,
    pattern: re.Pattern,
    room: int,
) -> tuple[int, list[tuple[Path, int, str]]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0, []
    total = 0
    found: list[tuple[Path, int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        if not pattern.search(line):
            continue
        total += 1
        if len(found) < room:
            found.append((path, i, line))
        if total >= COUNT_CEILING:
            break
    return total, found


def _payload(
    query: str,
    search_root: Path,
    file_glob: Optional[str],
    max_matches: int,
    backend: str,
    total: int,
    candidates: list[tuple[Path, int, str]],
    files_scanned: int = 0,
    project_root: Optional[Path] = None,
) -> str:
    samples, unshown = _pick_samples(
        candidates, project_root or search_root, max_matches, _compile_query(query)
    )
    files = sorted({m["file"] for m in samples})
    truncated = total > len(samples)
    summary = f"Found {total} matches"
    if samples:
        summary += f" (showing {len(samples)} samples)"
    summary += f" via {backend}."
    if files:
        summary += f" Occurrences in: {', '.join(files[:8])}" + ("…" if len(files) > 8 else "")
    if unshown:
        summary += f" {len(unshown)} more file(s) without samples."
    if truncated:
        summary += f" Sample cap {max_matches}; total is {total}."
    return json.dumps(
        {
            "query": query,
            "path": str(search_root),
            "file_glob": file_glob,
            "max_matches": max_matches,
            "backend": backend,
            "total": total,
            "files": files,
            "samples": samples,
            "unshown_files": unshown,
            "truncated": truncated,
            "files_scanned": files_scanned,
            "metadata_summary": summary,
        },
        ensure_ascii=False,
    )


def _rg_search(
    query: str,
    search_root: Path,
    project_root: Path,
    max_matches: int,
    patterns: List[str],
    ignored: Callable[[Path], bool],
) -> Optional[str]:
    rg = shutil.which("rg")
    if not rg:
        return None
    cmd = [
        rg,
        "--json",
        "-i",
        "--no-heading",
        "--hidden",
        "--glob",
        "!.git",
    ]
    # Honour .gitignore even when the fixture is not a git repo.
    cmd.append("--no-require-git")
    for pat in patterns:
        cmd.extend(["-g", pat])
    cmd.extend(["--", query, str(search_root)])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        # Flag unknown (old rg) or bad pattern — caller falls through.
        if "unrecognized" in (proc.stderr or "").lower() or proc.returncode == 2:
            if "--no-require-git" in cmd:
                cmd = [c for c in cmd if c != "--no-require-git"]
                try:
                    proc = subprocess.run(
                        cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=60,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    return None
                if proc.returncode not in (0, 1):
                    return None
            else:
                return None
        else:
            return None
    total = 0
    candidates: list[tuple[Path, int, str]] = []
    for line in (proc.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("type") != "match":
            if ev.get("type") == "summary":
                stats = ((ev.get("data") or {}).get("stats") or {}).get("matches") or {}
                reported = stats.get("total")
                if isinstance(reported, int) and reported > total:
                    total = reported
            continue
        data = ev.get("data") or {}
        path_text = ((data.get("path") or {}).get("text")) or ""
        p = Path(path_text) if path_text else None
        if p is not None and ignored(p):
            continue
        total += 1
        line_no = int(data.get("line_number") or 0)
        text = ((data.get("lines") or {}).get("text") or "").rstrip("\n")
        if p is not None and len(candidates) < CANDIDATE_CAP:
            candidates.append((p, line_no, text))
        if total >= COUNT_CEILING:
            break
    return _payload(query, search_root, None if not patterns else ",".join(patterns),
                    max_matches, "rg", total, candidates, project_root=project_root)


def _grep_search(
    query: str,
    search_root: Path,
    project_root: Path,
    max_matches: int,
    patterns: List[str],
    ignored: Callable[[Path], bool],
) -> Optional[str]:
    grep = shutil.which("grep")
    if not grep:
        return None
    cmd = [
        grep,
        "-R",
        "-n",
        "-H",
        "-I",
        "-i",
        "-E",
        "--binary-files=without-match",
    ]
    for d in sorted(IGNORE_DIRS):
        cmd.extend(["--exclude-dir", d])
    for pat in patterns:
        # grep --include matches the basename.
        base = pat.split("/")[-1] if "/" in pat else pat
        cmd.extend(["--include", base])
    # Use a pattern grep accepts; fall back to a literal.
    ere = query
    try:
        re.compile(query)
    except re.error:
        ere = re.escape(query)
    cmd.extend(["--", ere, str(search_root)])
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):
        # Invalid ERE — retry escaped.
        cmd[-2] = re.escape(query)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode not in (0, 1):
            return None
    total = 0
    candidates: list[tuple[Path, int, str]] = []
    line_re = re.compile(r"^(.*?):(\d+):(.*)$")
    for raw in (proc.stdout or "").splitlines():
        m = line_re.match(raw)
        if not m:
            continue
        path_s, line_s, text = m.group(1), m.group(2), m.group(3)
        p = Path(path_s)
        if not p.is_absolute():
            p = search_root / p
        try:
            p = p.resolve()
        except OSError:
            continue
        if ignored(p):
            continue
        total += 1
        if len(candidates) < CANDIDATE_CAP:
            candidates.append((p, int(line_s), text))
        if total >= COUNT_CEILING:
            break
    return _payload(query, search_root, None if not patterns else ",".join(patterns),
                    max_matches, "grep", total, candidates, project_root=project_root)


def _walk_search(
    query: str,
    search_root: Path,
    project_root: Path,
    max_matches: int,
    patterns: List[str],
    ignored: Callable[[Path], bool],
) -> str:
    pattern = _compile_query(query)
    total = 0
    cands: list[tuple[Path, int, str]] = []
    files_scanned = 0
    if search_root.is_file():
        candidates: Iterable[Path] = [search_root]
    else:
        candidates = _walk_files(search_root)
    for item in candidates:
        if not item.is_file() or _should_skip(item, search_root) or ignored(item):
            continue
        if patterns and not _matches_glob(item, search_root, patterns):
            continue
        files_scanned += 1
        n, found = _search_file_candidates(
            item, pattern, max(0, CANDIDATE_CAP - len(cands))
        )
        total += n
        cands.extend(found)
        if total >= COUNT_CEILING:
            break
    return _payload(query, search_root, None if not patterns else ",".join(patterns),
                    max_matches, "walk", total, cands, files_scanned,
                    project_root=project_root)


def search_sync(
    query: str,
    path: Optional[str] = None,
    max_matches: int = DEFAULT_MAX_SAMPLES,
    file_glob: Optional[str] = None,
    backend: Optional[str] = None,
) -> str:
    if not query:
        # An empty pattern matches every line: a context bill, never an answer.
        return json.dumps({"error": "query is empty; pass a word, symbol or phrase "
                                    "(Glob or ListDir lists files)"})
    project_root = get_project_root()
    search_root, err = confine_or_error(path, root=project_root)
    if err:
        return json.dumps({"error": err})
    assert search_root is not None
    if not search_root.exists():
        return json.dumps({"error": f"Path does not exist: {search_root}"})

    # Bounded: search results are never vaulted, so the cap is the size limit.
    max_matches = min(MAX_SAMPLES_CAP, max(1, int(max_matches or DEFAULT_MAX_SAMPLES)))
    patterns = _glob_patterns(file_glob) if file_glob else []
    ignored = build_ignorer(project_root)
    chosen = backend or search_backend_name()

    def _finish(raw: str, used: str) -> str:
        data = json.loads(raw)
        data["file_glob"] = file_glob
        if not data.get("error"):
            _add_elsewhere(data, query, search_root, project_root, patterns, ignored, used)
        if used != "walk":
            for name in sorted(OVERLAY_G023_DIRS):
                extra_root = project_root / ".g023" / name
                overlay = _overlay_dir_search(
                    query,
                    search_root,
                    project_root,
                    extra_root,
                    max_matches,
                    patterns,
                    ignored,
                    used,
                )
                if overlay:
                    data = _merge_search_payloads(data, overlay, max_matches)
        if not data.get("error") and int(data.get("total") or 0) == 0:
            _add_near_misses(data, query, search_root, project_root, ignored)
        if not data.get("error") and data.get("samples") and def_peek_enabled():
            peek = definition_peek(data["samples"], _compile_query(query), project_root)
            if peek:
                data["definition"] = peek
                for smp in data["samples"]:
                    # The peek shows these lines; the hit row keeps its "@ where".
                    if smp.get("file") == peek["file"] and peek["start"] <= smp.get("line", 0) <= peek["end"]:
                        smp["context_before"] = []
                        smp["context_after"] = []
        return json.dumps(data, ensure_ascii=False)

    if chosen == "rg":
        result = _rg_search(query, search_root, project_root, max_matches, patterns, ignored)
        if result is not None:
            return _finish(result, "rg")
        chosen = "grep" if shutil.which("grep") else "walk"
    if chosen == "grep":
        result = _grep_search(query, search_root, project_root, max_matches, patterns, ignored)
        if result is not None:
            return _finish(result, "grep")
    walk = _walk_search(query, search_root, project_root, max_matches, patterns, ignored)
    return _finish(walk, "walk")


def scope_count_enabled() -> bool:
    from ..config import settings

    return bool(getattr(settings, "scope_count", False))


def _add_elsewhere(
    data: dict,
    query: str,
    search_root: Path,
    project_root: Path,
    patterns: List[str],
    ignored: Callable[[Path], bool],
    used: str,
) -> None:
    """Scoped search: how many hits the rest of the project holds.

    A search scoped to a subdirectory leaves "is it used anywhere else?"
    open, and the usual answer is a second, project-wide search. One count
    over the project (same backend, one sample) closes it:
    ``elsewhere_in_project: 0`` means the scoped result is the whole story.
    """
    if not scope_count_enabled():
        return
    try:
        if search_root.resolve() == project_root.resolve():
            return
    except OSError:
        return
    try:
        if used == "rg":
            raw = _rg_search(query, project_root, project_root, 1, patterns, ignored)
        elif used == "grep":
            raw = _grep_search(query, project_root, project_root, 1, patterns, ignored)
        else:
            raw = _walk_search(query, project_root, project_root, 1, patterns, ignored)
        whole = json.loads(raw) if raw else {}
    except Exception:  # a hint must never break a search
        return
    if whole.get("error") or "total" not in whole:
        return
    try:
        outside = int(whole["total"]) - int(data.get("total") or 0)
    except (TypeError, ValueError):
        return
    if outside >= 0:
        data["elsewhere_in_project"] = outside


def _add_near_misses(
    data: dict, query: str, search_root: Path, project_root: Path, ignored: Callable[[Path], bool]
) -> None:
    """Zero hits: suggest defined names spelled like the query's (``near_miss``).

    The model searched for the name it expected; one hop to the real
    spelling replaces a ListDir/ReadFile/re-grep chain. Bounded by
    ``near_miss`` time and byte budgets; off with G023_NEAR_MISS=0.
    """
    if not near_miss.enabled():
        return
    root = search_root if search_root.is_dir() else search_root.parent
    if search_root.is_file():
        files = iter([search_root])
    else:
        files = (
            f for f in _walk_files(root)
            if f.is_file() and not _should_skip(f, root) and not ignored(f)
        )
    try:
        found = near_miss.suggest(query, files, project_root)
    except Exception:  # a hint must never break a search
        return
    hits = found.get("did_you_mean") or []
    if hits:
        data["did_you_mean"] = hits
        names = ", ".join(h["name"] for h in hits[:3])
        data["metadata_summary"] = (
            str(data.get("metadata_summary") or "Found 0 matches")
            + f" Similar defined names: {names}."
        )


def _overlay_dir_search(
    query: str,
    search_root: Path,
    project_root: Path,
    extra: Path,
    max_matches: int,
    patterns: List[str],
    ignored: Callable[[Path], bool],
    backend: str,
) -> Optional[dict]:
    if not extra.is_dir():
        return None
    try:
        extra.resolve().relative_to(search_root.resolve())
    except ValueError:
        return None
    try:
        search_root.resolve().relative_to(extra.resolve())
        return None
    except ValueError:
        pass
    if backend == "rg":
        raw = _rg_search(query, extra, project_root, max_matches, patterns, ignored)
    elif backend == "grep":
        raw = _grep_search(query, extra, project_root, max_matches, patterns, ignored)
    else:
        raw = _walk_search(query, extra, project_root, max_matches, patterns, ignored)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _merge_search_payloads(primary: dict, extra: dict, max_matches: int) -> dict:
    samples = list(primary.get("samples") or [])
    seen = {(s.get("file"), s.get("line")) for s in samples if isinstance(s, dict)}
    for item in extra.get("samples") or []:
        if not isinstance(item, dict):
            continue
        key = (item.get("file"), item.get("line"))
        if key in seen:
            continue
        if len(samples) < max_matches:
            samples.append(item)
            seen.add(key)
    primary_files = set(primary.get("files") or [])
    extra_files = set(extra.get("files") or [])
    if extra_files and extra_files.issubset(primary_files):
        total = int(primary.get("total") or 0)
    else:
        total = int(primary.get("total") or 0) + int(extra.get("total") or 0)
    files = sorted({m.get("file") for m in samples if isinstance(m, dict) and m.get("file")})
    primary["samples"] = samples
    unshown = dict(primary.get("unshown_files") or {})
    for f, n in (extra.get("unshown_files") or {}).items():
        if f not in files and len(unshown) < UNSHOWN_FILES_MAX:
            unshown[f] = n
    primary["unshown_files"] = unshown
    primary["total"] = total
    primary["files"] = files
    primary["truncated"] = total > len(samples)
    return primary


async def run_searcher(
    query: str,
    path: Optional[str] = None,
    max_matches: int = DEFAULT_MAX_SAMPLES,
    file_glob: Optional[str] = None,
) -> str:
    return await asyncio.to_thread(search_sync, query, path, max_matches, file_glob)


def _normalize_find_pattern(pat: str) -> str:
    if "/" not in pat and not pat.startswith("**"):
        return f"**/{pat}"
    return pat


_WILD = re.compile(r"[*?\[\]{}]")


def literal_allowances(pattern: str, search_rel: str = "") -> tuple[frozenset, frozenset]:
    """Directory names and extensions a glob names literally.

    ``frames/*.png`` names ``.png``; ``vendor/**/*.js`` names ``vendor``. The
    default ignore lists exist to keep noise out of broad searches, not to
    contradict an explicit request — that turned ``Glob frames/*.png`` into
    "0 files" for a directory of 4,000 PNGs.
    """
    dirs: set[str] = set()
    exts: set[str] = set()
    for alt in expand_braces(pattern or ""):
        segs = alt.replace("\\", "/").split("/")
        for seg in segs[:-1]:
            if seg and not _WILD.search(seg):
                dirs.add(seg)
        m = re.search(r"(\.[A-Za-z0-9]+)$", segs[-1])
        if m:
            exts.add(m.group(1).lower())
    for seg in (search_rel or "").split("/"):
        if seg and seg != ".":
            dirs.add(seg)
    return frozenset(dirs & IGNORE_DIRS), frozenset(exts & IGNORE_EXTS)


def _collect_glob_hits(
    glob_pat: str,
    from_root: Path,
    project_root: Path,
    ignored: Callable[[Path], bool],
    seen: set[str],
    files: list[str],
    allow_dirs: frozenset = frozenset(),
    allow_exts: frozenset = frozenset(),
) -> None:
    try:
        hits = globmod.glob(glob_pat, root_dir=str(from_root), recursive=True)
    except (OSError, ValueError):
        hits = []
    from_s = os.path.realpath(from_root)
    root_s = os.path.realpath(project_root)
    root_prefix = root_s.rstrip(os.sep) + os.sep
    for hit in hits:
        real = os.path.realpath(os.path.join(from_s, hit))
        if not real.startswith(root_prefix) or not os.path.isfile(real):
            continue
        fp = Path(real)
        if ignored(fp):
            continue
        if not (allow_dirs or allow_exts):
            if _should_skip(fp, from_root):
                continue
        else:
            rel_parts = Path(os.path.relpath(real, from_s)).parts
            blocked = set(rel_parts[:-1]) & (IGNORE_DIRS | {".g023"})
            if blocked - allow_dirs or fp.suffix.lower() in IGNORE_EXTS - allow_exts:
                continue
        rel = real[len(root_prefix):].replace(os.sep, "/")
        if rel not in seen:
            seen.add(rel)
            files.append(rel)


def glob_sync(pattern: str, path: Optional[str] = None) -> str:
    project_root = get_project_root()
    search_root, err = confine_or_error(path, root=project_root)
    if err:
        return json.dumps({"error": err})
    assert search_root is not None
    if not search_root.exists():
        return json.dumps({"error": f"Path does not exist: {search_root}"})
    try:
        search_rel = search_root.resolve().relative_to(project_root.resolve()).as_posix()
    except ValueError:
        search_rel = ""
    allow_dirs, allow_exts = literal_allowances(pattern, search_rel)
    allow_dirs -= {".g023"}  # .g023 stays private apart from its overlay islands
    ignored = build_ignorer(project_root, allow_dirs=allow_dirs, allow_exts=allow_exts)
    files: list[str] = []
    seen: set[str] = set()
    for raw in _glob_patterns(pattern) or expand_braces(pattern):
        glob_pat = _normalize_find_pattern(raw)
        _collect_glob_hits(glob_pat, search_root, project_root, ignored, seen, files, allow_dirs, allow_exts)
        # Python glob skips hidden names (`.g023`). Overlay the readable islands.
        for name in sorted(OVERLAY_G023_DIRS):
            extra = project_root / ".g023" / name
            if not extra.is_dir():
                continue
            try:
                extra.resolve().relative_to(search_root.resolve())
            except ValueError:
                continue
            try:
                search_root.resolve().relative_to(extra.resolve())
                continue
            except ValueError:
                pass
            _collect_glob_hits(glob_pat, extra, project_root, ignored, seen, files)
    files.sort()
    cap = GLOB_FILES_CAP
    payload = {
        "pattern": pattern,
        "path": str(search_root),
        "total": len(files),
        "files": files[:cap],
        "truncated": len(files) > cap,
    }
    if len(files) > cap:
        # Every hit accounted for: numbered names collapse into templates per
        # directory, so "how many / which range" needs no paging.
        from ..shapes import listing_lines

        payload["files"] = files[:GLOB_FILES_WHEN_DIGEST]
        payload["digest"] = listing_lines(files, budget=40)
        payload["note"] = (f"{len(files):,} matches: digest covers all of them; files lists the first "
                           f"{GLOB_FILES_WHEN_DIGEST}. Narrow the pattern for exact names.")
    if allow_dirs or allow_exts:
        payload["included_ignored"] = sorted(allow_dirs | allow_exts)
    return json.dumps(payload, ensure_ascii=False)


GLOB_FILES_CAP = 200
GLOB_FILES_WHEN_DIGEST = 20


async def run_glob(pattern: str, path: Optional[str] = None) -> str:
    return await asyncio.to_thread(glob_sync, pattern, path)

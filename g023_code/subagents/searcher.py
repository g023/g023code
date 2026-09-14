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

from ..config import get_project_root
from ..paths import confine_or_error, rel_to_root


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


def _under_g023_memory(rel: Path) -> bool:
    parts = rel.parts
    return len(parts) >= 2 and parts[0] == ".g023" and parts[1] == "memory"


def _parents_blocked(rel: Path) -> bool:
    parent = rel.parts[:-1]
    if len(parent) >= 2 and parent[0] == ".g023" and parent[1] == "memory":
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
            keep = [d for d in dirnames if d == "memory"]
        elif rel_s == ".g023/memory" or rel_s.startswith(".g023/memory/"):
            keep = [d for d in dirnames if d not in IGNORE_DIRS]
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


def build_ignorer(root: Path) -> Callable[[Path], bool]:
    """True when the path should be omitted (IGNORE_DIRS / .gitignore)."""
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

    def ignored(path: Path) -> bool:
        try:
            rel = path.resolve().relative_to(root.resolve())
        except ValueError:
            return True
        if _under_g023_memory(rel):
            if path.is_file() and path.suffix.lower() in IGNORE_EXTS:
                return True
            return False
        if set(rel.parts) & IGNORE_DIRS:
            return True
        if path.is_file() and path.suffix.lower() in IGNORE_EXTS:
            return True
        rel_s = rel.as_posix()
        prefixes = [rel_s]
        parts = rel.parts
        for i in range(len(parts)):
            prefixes.append("/".join(parts[: i + 1]))
        matched = False
        for prefix in prefixes:
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


def _sample_from_line(path: Path, root: Path, line_no: int, line: str, lines: Optional[list] = None) -> dict:
    try:
        rel = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        rel = str(path)
    before: list[str] = []
    after: list[str] = []
    if lines is not None:
        before = [l.strip()[:120] for l in lines[max(0, line_no - 3) : line_no - 1]]
        after = [l.strip()[:120] for l in lines[line_no : line_no + 2]]
    return {
        "file": rel,
        "line": line_no,
        "match": line.strip()[:200],
        "context_before": before,
        "context_after": after,
    }


def _search_file_count_and_sample(
    path: Path,
    root: Path,
    pattern: re.Pattern,
    remaining_samples: int,
) -> tuple[int, list[dict]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return 0, []
    lines = text.splitlines()
    total = 0
    samples: list[dict] = []
    for i, line in enumerate(lines, 1):
        if not pattern.search(line):
            continue
        total += 1
        if len(samples) < remaining_samples:
            samples.append(_sample_from_line(path, root, i, line, lines))
        if total >= COUNT_CEILING:
            break
    return total, samples


def _payload(
    query: str,
    search_root: Path,
    file_glob: Optional[str],
    max_matches: int,
    backend: str,
    total: int,
    samples: list[dict],
    files_scanned: int = 0,
) -> str:
    files = sorted({m["file"] for m in samples})
    truncated = total > len(samples)
    summary = f"Found {total} matches"
    if samples:
        summary += f" (showing {len(samples)} samples)"
    summary += f" via {backend}."
    if files:
        summary += f" Occurrences in: {', '.join(files[:8])}" + ("…" if len(files) > 8 else "")
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
            "matches": samples,
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
    samples: list[dict] = []
    files: set[str] = set()
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
        if p is not None and len(samples) < max_matches:
            samples.append(_sample_from_line(p, project_root, line_no, text))
            files.add(samples[-1]["file"])
        if total >= COUNT_CEILING:
            break
    return _payload(query, search_root, None if not patterns else ",".join(patterns), max_matches, "rg", total, samples)


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
    samples: list[dict] = []
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
        if len(samples) < max_matches:
            samples.append(_sample_from_line(p, project_root, int(line_s), text))
        if total >= COUNT_CEILING:
            break
    return _payload(
        query,
        search_root,
        None if not patterns else ",".join(patterns),
        max_matches,
        "grep",
        total,
        samples,
    )


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
    samples: list[dict] = []
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
        n, found = _search_file_count_and_sample(
            item, project_root, pattern, max(0, max_matches - len(samples))
        )
        total += n
        samples.extend(found)
        if total >= COUNT_CEILING:
            break
    return _payload(query, search_root, None if not patterns else ",".join(patterns), max_matches, "walk", total, samples, files_scanned)


def search_sync(
    query: str,
    path: Optional[str] = None,
    max_matches: int = DEFAULT_MAX_SAMPLES,
    file_glob: Optional[str] = None,
    backend: Optional[str] = None,
) -> str:
    project_root = get_project_root()
    search_root, err = confine_or_error(path, root=project_root)
    if err:
        return json.dumps({"error": err})
    assert search_root is not None
    if not search_root.exists():
        return json.dumps({"error": f"Path does not exist: {search_root}"})

    max_matches = max(1, int(max_matches or DEFAULT_MAX_SAMPLES))
    patterns = _glob_patterns(file_glob) if file_glob else []
    ignored = build_ignorer(project_root)
    chosen = backend or search_backend_name()

    def _finish(raw: str, used: str) -> str:
        data = json.loads(raw)
        data["file_glob"] = file_glob
        if used != "walk":
            mem = project_root / ".g023" / "memory"
            overlay = _overlay_memory_search(
                query, search_root, project_root, mem, max_matches, patterns, ignored, used
            )
            if overlay:
                data = _merge_search_payloads(data, overlay, max_matches)
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


def _overlay_memory_search(
    query: str,
    search_root: Path,
    project_root: Path,
    mem: Path,
    max_matches: int,
    patterns: List[str],
    ignored: Callable[[Path], bool],
    backend: str,
) -> Optional[dict]:
    if not mem.is_dir():
        return None
    try:
        mem.resolve().relative_to(search_root.resolve())
    except ValueError:
        return None
    try:
        search_root.resolve().relative_to(mem.resolve())
        return None
    except ValueError:
        pass
    if backend == "rg":
        raw = _rg_search(query, mem, project_root, max_matches, patterns, ignored)
    elif backend == "grep":
        raw = _grep_search(query, mem, project_root, max_matches, patterns, ignored)
    else:
        raw = _walk_search(query, mem, project_root, max_matches, patterns, ignored)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _merge_search_payloads(primary: dict, extra: dict, max_matches: int) -> dict:
    samples = list(primary.get("samples") or primary.get("matches") or [])
    seen = {(s.get("file"), s.get("line")) for s in samples if isinstance(s, dict)}
    for item in extra.get("samples") or extra.get("matches") or []:
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
    primary["matches"] = samples
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


def glob_sync(pattern: str, path: Optional[str] = None) -> str:
    project_root = get_project_root()
    search_root, err = confine_or_error(path, root=project_root)
    if err:
        return json.dumps({"error": err})
    assert search_root is not None
    if not search_root.exists():
        return json.dumps({"error": f"Path does not exist: {search_root}"})
    ignored = build_ignorer(project_root)
    files: list[str] = []
    seen: set[str] = set()
    for raw in _glob_patterns(pattern) or expand_braces(pattern):
        glob_pat = _normalize_find_pattern(raw)
        try:
            hits = globmod.glob(glob_pat, root_dir=str(search_root), recursive=True)
        except (OSError, ValueError):
            hits = []
        for hit in hits:
            fp = (search_root / hit).resolve()
            if not fp.is_file():
                continue
            if ignored(fp) or _should_skip(fp, search_root):
                continue
            try:
                fp.relative_to(project_root.resolve())
            except ValueError:
                continue
            rel = rel_to_root(fp, root=project_root)
            if rel not in seen:
                seen.add(rel)
                files.append(rel)
    files.sort()
    cap = 200
    return json.dumps(
        {
            "pattern": pattern,
            "path": str(search_root),
            "total": len(files),
            "files": files[:cap],
            "truncated": len(files) > cap,
        },
        ensure_ascii=False,
    )


async def run_glob(pattern: str, path: Optional[str] = None) -> str:
    return await asyncio.to_thread(glob_sync, pattern, path)

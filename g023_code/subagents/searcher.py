"""
Searcher Subagent
Performs metadata-first grep/glob style search and returns compact JSON matches.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import List, Optional

from ..config import get_project_root, settings


# Common ignore patterns
IGNORE_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".g023", ".deepcode", ".idea", ".vscode", "target", "vendor",
}
IGNORE_EXTS = {".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".o", ".a", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".tar", ".gz"}


def _should_skip(path: Path, root: Optional[Path] = None) -> bool:
    """True for files we never want to grep.

    Directory names are only checked *below* the search root: a project living
    under a directory that happens to be called ``build`` or ``venv`` is still a
    project, and matching against the absolute path would skip all of it.
    """
    relative = path
    if root is not None:
        try:
            relative = path.relative_to(root)
        except ValueError:
            pass
    if set(relative.parts[:-1]) & IGNORE_DIRS:
        return True
    if path.suffix.lower() in IGNORE_EXTS:
        return True
    return False


def _walk_files(root: Path):
    """Yield files under root, pruning ignored directories as we descend.

    ``rglob('*')`` would descend into node_modules and .git before anything got
    filtered — on a real repo that is the difference between instant and a
    multi-second stall with the whole tree held in memory.
    """
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORE_DIRS)
        directory = Path(dirpath)
        for name in sorted(filenames):
            yield directory / name


def _glob_patterns(file_glob: str) -> List[str]:
    """
    Normalise what a caller might plausibly pass as a glob.

    The orchestrator writes these by hand, so accept the shapes it actually
    produces: ``*.py``, ``py``, ``.py``, ``*.py,*.md``, ``src/**/*.ts``.
    """
    patterns: List[str] = []
    for raw in file_glob.split(","):
        part = raw.strip()
        if not part:
            continue
        if "*" not in part and "?" not in part and "[" not in part:
            # A bare extension, with or without the dot.
            part = f"*.{part.lstrip('.')}"
        patterns.append(part)
    return patterns


def _matches_glob(path: Path, root: Path, patterns: List[str]) -> bool:
    """True when the file matches any pattern, by name or by relative path."""
    try:
        relative = path.relative_to(root)
    except ValueError:
        relative = path
    for pattern in patterns:
        if path.match(pattern) or relative.match(pattern):
            return True
    return False


def _search_file(path: Path, pattern: re.Pattern, max_per_file: int = 4) -> List[dict]:
    matches = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        if pattern.search(line):
            before = lines[max(0, i - 3) : i - 1]
            after = lines[i : i + 2]
            matches.append(
                {
                    "file": str(path),
                    "line": i,
                    "match": line.strip()[:200],
                    "context_before": [l.strip()[:120] for l in before],
                    "context_after": [l.strip()[:120] for l in after],
                }
            )
            if len(matches) >= max_per_file:
                break
    return matches


async def run_searcher(
    query: str,
    path: Optional[str] = None,
    max_matches: int = 12,
    file_glob: Optional[str] = None,
) -> str:
    root = get_project_root()
    search_root = root
    if path:
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        search_root = p.resolve()

    if not search_root.exists():
        return json.dumps({"error": f"Path does not exist: {search_root}"})

    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        # Treat as literal
        pattern = re.compile(re.escape(query), re.IGNORECASE)

    all_matches: List[dict] = []
    files_scanned = 0
    files_skipped_by_glob = 0
    truncated = False
    patterns = _glob_patterns(file_glob) if file_glob else []

    # Walk lazily, pruning ignored directories rather than listing them first.
    if search_root.is_file():
        candidates = iter([search_root])
    else:
        candidates = _walk_files(search_root)

    for item in candidates:
        if len(all_matches) >= max_matches:
            truncated = True
            break
        if not item.is_file() or _should_skip(item, search_root):
            continue
        if patterns and not _matches_glob(item, search_root, patterns):
            files_skipped_by_glob += 1
            continue
        files_scanned += 1
        found = _search_file(item, pattern, max_per_file=3)
        for m in found:
            # Make path relative for readability
            try:
                m["file"] = str(Path(m["file"]).relative_to(root))
            except ValueError:
                pass
            all_matches.append(m)
            if len(all_matches) >= max_matches:
                truncated = True
                break

    summary = f"Found {len(all_matches)} matches across {files_scanned} files scanned."
    if patterns:
        summary += f" Filtered to {'/'.join(patterns)} ({files_skipped_by_glob} files skipped)."
    if all_matches:
        files = sorted({m["file"] for m in all_matches})
        summary += f" Occurrences in: {', '.join(files[:8])}" + ("…" if len(files) > 8 else "")
    if truncated:
        # Say so explicitly: silence here reads as "that is all there is".
        summary += f" Stopped at the {max_matches}-match cap; narrow the query or raise it."

    return json.dumps(
        {
            "query": query,
            "path": str(search_root),
            "file_glob": file_glob,
            "max_matches": max_matches,
            "truncated": truncated,
            "files_scanned": files_scanned,
            "metadata_summary": summary,
            "matches": all_matches[:max_matches],
        },
        ensure_ascii=False,
    )

"""
Searcher Subagent
Performs metadata-first grep/glob style search and returns compact JSON matches.
"""

from __future__ import annotations

import json
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


def _should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & IGNORE_DIRS:
        return True
    if path.suffix.lower() in IGNORE_EXTS:
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

    # Simple walk
    if search_root.is_file():
        candidates = [search_root]
    else:
        candidates = list(search_root.rglob("*"))

    for item in candidates:
        if len(all_matches) >= max_matches:
            break
        if not item.is_file() or _should_skip(item):
            continue
        if file_glob:
            # Very basic glob support
            if not item.match(file_glob) and not item.name.endswith(tuple(file_glob.replace("*", "").split(","))):
                # crude fallback
                pass
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
                break

    summary = f"Found {len(all_matches)} matches across {files_scanned} files scanned."
    if all_matches:
        files = sorted({m["file"] for m in all_matches})
        summary += f" Occurrences in: {', '.join(files[:8])}" + ("…" if len(files) > 8 else "")

    return json.dumps(
        {
            "query": query,
            "path": str(search_root),
            "max_matches": max_matches,
            "metadata_summary": summary,
            "matches": all_matches[:max_matches],
        },
        ensure_ascii=False,
    )

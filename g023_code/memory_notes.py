"""Project notebook under .g023/memory/. Never injected into instructions."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import get_project_root

INDEX_NAME = "MEMORY.md"
INDEX_CAP = 200


def memory_root() -> Path:
    return get_project_root() / ".g023" / "memory"


def index_path() -> Path:
    return memory_root() / INDEX_NAME


def is_memory_index_path(path: Path, root: Optional[Path] = None) -> bool:
    root_p = (root or get_project_root()).resolve()
    try:
        rel = Path(path).resolve().relative_to(root_p)
    except ValueError:
        return False
    return rel.as_posix() == f".g023/memory/{INDEX_NAME}"


def write_index(text: str) -> dict:
    """Write MEMORY.md, spilling lines past INDEX_CAP into topics/overflow.md."""
    root = memory_root()
    root.mkdir(parents=True, exist_ok=True)
    dest = index_path()
    normalized = (text or "").replace("\r\n", "\n")
    if not normalized.endswith("\n") and normalized:
        normalized += "\n"
    lines = normalized.splitlines()
    if len(lines) <= INDEX_CAP:
        dest.write_text(normalized, encoding="utf-8")
        return {
            "ok": True,
            "path": str(dest),
            "lines": len(lines),
            "bytes": len(normalized.encode("utf-8")),
        }
    head = "\n".join(lines[:INDEX_CAP]) + "\n"
    tail = "\n".join(lines[INDEX_CAP:]) + "\n"
    topics = root / "topics"
    topics.mkdir(parents=True, exist_ok=True)
    overflow = topics / "overflow.md"
    dest.write_text(head, encoding="utf-8")
    overflow.write_text(tail, encoding="utf-8")
    return {
        "ok": True,
        "path": str(dest),
        "lines": INDEX_CAP,
        "bytes": len(head.encode("utf-8")),
        "spilled": str(overflow),
        "spilled_lines": len(lines) - INDEX_CAP,
    }


def status_line() -> str:
    dest = index_path()
    if not dest.is_file():
        return "none"
    try:
        n = len(dest.read_text(encoding="utf-8").splitlines())
    except OSError:
        return "unreadable"
    return f"{INDEX_NAME} {n} line(s)"

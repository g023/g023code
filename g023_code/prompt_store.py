"""Persist user prompts as incrementing ``.g023/prompts/{n}.txt`` files.

A small pure store: next index, write, read last. The REPL binds Ctrl+Up to
:func:`last_prompt`; it does not own numbering or I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import get_project_root


def prompts_dir() -> Path:
    """Project ``.g023/prompts/`` directory, created on demand."""
    path = get_project_root() / ".g023" / "prompts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _indices(directory: Optional[Path] = None) -> list[int]:
    folder = directory if directory is not None else prompts_dir()
    found: list[int] = []
    try:
        entries = folder.iterdir()
    except OSError:
        return found
    for path in entries:
        if path.suffix == ".txt" and path.stem.isdigit():
            found.append(int(path.stem))
    return found


def next_index(directory: Optional[Path] = None) -> int:
    """1-based index of the next prompt file (1 if the folder is empty)."""
    nums = _indices(directory)
    return (max(nums) + 1) if nums else 1


def save_prompt(text: str, directory: Optional[Path] = None) -> Path:
    """Write ``text`` to the next ``{#}.txt`` and return that path."""
    folder = directory if directory is not None else prompts_dir()
    folder.mkdir(parents=True, exist_ok=True)
    index = next_index(folder)
    path = folder / f"{index}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def last_prompt(directory: Optional[Path] = None) -> Optional[str]:
    """Contents of the highest-numbered prompt file, or None if none exist."""
    folder = directory if directory is not None else prompts_dir()
    nums = _indices(folder)
    if not nums:
        return None
    path = folder / f"{max(nums)}.txt"
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None

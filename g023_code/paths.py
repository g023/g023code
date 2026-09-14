"""Resolve tool paths and refuse anything outside the project root."""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from .config import get_project_root

PathLike = Union[str, Path]


class PathOutsideProject(ValueError):
    """The resolved path is not inside G023_PROJECT_ROOT."""


def resolve_in_project(
    path: Optional[PathLike] = None,
    *,
    root: Optional[Path] = None,
) -> Path:
    """Resolve ``path`` against the project root.

    Empty / ``.`` / ``None`` means the project root. Absolute paths are
    accepted only when they stay inside the root after ``resolve()``
    (symlink escapes included).
    """
    root_p = (root or get_project_root()).resolve()
    if path is None or str(path).strip() in ("", "."):
        return root_p
    p = Path(path)
    if not p.is_absolute():
        p = root_p / p
    p = p.resolve()
    try:
        p.relative_to(root_p)
    except ValueError as exc:
        raise PathOutsideProject(f"Path is outside project root: {p}") from exc
    return p


def confine_or_error(
    path: Optional[PathLike] = None,
    *,
    root: Optional[Path] = None,
) -> tuple[Optional[Path], Optional[str]]:
    """``(resolved, None)`` or ``(None, error message)``. Does not read the path."""
    try:
        return resolve_in_project(path, root=root), None
    except PathOutsideProject as exc:
        return None, str(exc)


def rel_to_root(path: PathLike, *, root: Optional[Path] = None) -> str:
    """POSIX-ish relative path for working-set / search results."""
    root_p = (root or get_project_root()).resolve()
    p = Path(path)
    if not p.is_absolute():
        p = root_p / p
    p = p.resolve()
    try:
        return p.relative_to(root_p).as_posix()
    except ValueError:
        return str(p)

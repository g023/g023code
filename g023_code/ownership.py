"""Code ownership: whose structure the orchestrator may improve while it works.

Prompting guides for Claude report that one sentence ("This codebase is yours
...") makes the model merge repeated code and fix nearby defects. A live A/B on
deepseek-flash (12 runs per framing) found that *identity* framing does nothing
on this model (0/12 merges), while the same behaviour stated as an explicit
*directive* merged the duplicate 6/6 and fixed a nearby defect 3/6, with every
existing behaviour kept, for about +28% cost per task.

The user owns their code, so the directive is scoped by provenance:

* ``user``  — no directive; the standing prompt is byte-identical to before.
* ``auto``  — (default) the directive covers only files this program created
  in the project. Those files are recorded when WriteFile creates them
  (``.g023/provenance.json``), and ReadFile/Edit/WriteFile results for them
  carry ``"yours": true``. Everything else stays minimal-change.
* ``agent`` — the directive covers the whole project.

Files created by Bash (``cat > x``, generators) are not recorded: only a
WriteFile that created the file proves who wrote it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .config import get_project_root, settings
from .fsutil import atomic_write_text, file_lock

MODES = ("auto", "user", "agent")
MARK_TOOLS = frozenset({"ReadFile", "Edit", "WriteFile"})
PROVENANCE_CAP = 5000  # paths kept; oldest dropped first

_RULES = (
    "When code you touch repeats logic that your change would repeat again, merge it into one "
    "helper instead of adding another copy. Fix defects you notice in the code you touch (for "
    "example, a function that breaks its own docstring). Keep existing behaviour and tests passing, "
    "and name every such change in your answer."
)

AGENT_ADDENDUM = "\nCode ownership: this codebase is yours to keep healthy. " + _RULES + "\n"

AUTO_ADDENDUM = (
    "\nCode ownership: a ReadFile, Edit or WriteFile result marked \"yours\":true is a file you "
    "created in this project, so its structure is yours to keep healthy. In those files: " + _RULES
    + " Files without that mark belong to the user: change what the request needs and report other "
    "defects instead of fixing them.\n"
)


def mode() -> str:
    value = str(getattr(settings, "ownership", "auto") or "auto").lower()
    return value if value in MODES else "auto"


def system_addendum(value: Optional[str] = None) -> str:
    """Standing-prompt text for the ownership mode (empty for ``user``)."""
    value = value or mode()
    if value == "agent":
        return AGENT_ADDENDUM
    if value == "auto":
        return AUTO_ADDENDUM
    return ""


def _store(root: Optional[Path] = None) -> Path:
    return (root or get_project_root()) / ".g023" / "provenance.json"


def _rel(path: str | Path, root: Path) -> Optional[str]:
    p = Path(path)
    if not p.is_absolute():
        p = root / p
    try:
        return p.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return None


def _load(store: Path) -> dict:
    try:
        data = json.loads(store.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    files = data.get("created") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def record_created(path: str | Path, *, root: Optional[Path] = None) -> bool:
    """Remember that WriteFile created ``path`` (project-relative). Never raises."""
    try:
        root = root or get_project_root()
        rel = _rel(path, root)
        if not rel or rel.startswith(".g023/"):
            return False
        store = _store(root)
        store.parent.mkdir(parents=True, exist_ok=True)
        with file_lock(store):
            files = _load(store)
            if rel in files:
                return False
            files[rel] = {"by": "WriteFile"}
            if len(files) > PROVENANCE_CAP:
                files = dict(list(files.items())[-PROVENANCE_CAP:])
            atomic_write_text(store, json.dumps({"created": files}, indent=1, sort_keys=False) + "\n")
        return True
    except Exception:
        return False


def created(path: str | Path, *, root: Optional[Path] = None) -> bool:
    try:
        root = root or get_project_root()
        rel = _rel(path, root)
        return bool(rel) and rel in _load(_store(root))
    except Exception:
        return False


def forget(path: str | Path, *, root: Optional[Path] = None) -> bool:
    """Hand a file back to the user (it stops being marked ``yours``)."""
    try:
        root = root or get_project_root()
        rel = _rel(path, root)
        store = _store(root)
        with file_lock(store):
            files = _load(store)
            if not rel or rel not in files:
                return False
            del files[rel]
            atomic_write_text(store, json.dumps({"created": files}, indent=1) + "\n")
        return True
    except Exception:
        return False


def listing(*, root: Optional[Path] = None) -> list[str]:
    return sorted(_load(_store(root or get_project_root())))


def mark_result(tool: str, args: dict, result: str) -> str:
    """Add ``"yours": true`` to a JSON tool result about a file this program created.

    Only in ``auto`` mode, only for ReadFile/Edit/WriteFile, only for a JSON
    object result without an error. Anything else is returned unchanged.
    """
    if tool not in MARK_TOOLS or mode() != "auto":
        return result
    path = str((args or {}).get("path") or "")
    if not path or not result or not result.lstrip().startswith("{"):
        return result
    if not created(path):
        return result
    try:
        data = json.loads(result)
    except ValueError:
        return result
    if not isinstance(data, dict) or data.get("error") or data.get("yours"):
        return result
    data["yours"] = True
    return json.dumps(data, ensure_ascii=False)

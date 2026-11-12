"""Rename tool: one call renames an identifier across the project, all or nothing.

Bench evidence (rename tasks): the model renamed a function with a loop of
Bash hops — ``grep -rn`` to find uses, ``sed -i`` per file, ``grep`` again to
confirm none were left — 4 to 7 hops, the costliest task of the bench. Each
hop re-sends the whole prefix and pays output for the shell plumbing. A
Rename tool does the find / replace / confirm in one step and returns the
per-file counts, so the next call can be the test run.

Semantics: whole-identifier matches only (``old`` not preceded or followed by
``[A-Za-z0-9_]``), in every project text file under ``path`` (ignore rules,
binary, data and oversized files skipped, as for the closure receipt),
optionally narrowed by an ``include`` glob. Every file is written or none:
on any write error the files already written are put back. Each changed file
is backed up to ``.g023/backups/`` like an Edit. Setting ``rename_tool``
(G023_RENAME_TOOL).
"""

from __future__ import annotations

import copy
import fnmatch
import keyword
import re
from pathlib import Path
from typing import Iterable, List, Optional

from .config import settings

NAME = "Rename"
MAX_FILES = 200
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")

SCHEMA = {
    "type": "function",
    "name": NAME,
    "description": (
        "Rename an identifier (function, class, variable, constant) everywhere in the project in "
        "one step: every whole-word occurrence in every text file under path, all files or none. "
        "Returns the files changed with their counts and refs_left (0 when no whole-word "
        "occurrence remains). Use it instead of grep/sed loops, then run the tests. Each changed "
        "file is backed up to .g023/backups/. Requires confirmation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "old": {"type": "string", "description": "Current identifier (letters, digits, _)"},
            "new": {"type": "string", "description": "New identifier"},
            "path": {"type": "string", "description": "File or directory to rename within. Default: the project root."},
            "include": {"type": "string", "description": "Optional glob on file names or paths, e.g. '*.py'."},
        },
        "required": ["old", "new"],
        "additionalProperties": False,
    },
}


def enabled() -> bool:
    return bool(getattr(settings, "rename_tool", False))


def add_to_schemas(tools: Iterable[dict]) -> List[dict]:
    """Return ``tools`` plus the Rename schema, placed right after Edit."""
    out: List[dict] = []
    added = False
    for schema in tools:
        out.append(schema)
        if schema.get("name") == "Edit":
            out.append(copy.deepcopy(SCHEMA))
            added = True
    if not added:
        out.append(copy.deepcopy(SCHEMA))
    return out


def _pattern(old: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9_])" + re.escape(old) + r"(?![A-Za-z0-9_])")


def validate(old: str, new: str) -> Optional[str]:
    if not _IDENT.match(old or ""):
        return f"old must be an identifier (letters, digits, _): {old!r}"
    if not _IDENT.match(new or ""):
        return f"new must be an identifier (letters, digits, _): {new!r}"
    if old == new:
        return "old and new are the same"
    if keyword.iskeyword(new):
        return f"new is a Python keyword: {new!r}"
    return None


def _candidates(root: Path, scope: Path, include: str) -> Iterable[Path]:
    from .closure import _project_files

    if scope.is_file():
        yield scope
        return
    for f in _project_files(root):
        try:
            f.relative_to(scope)
        except ValueError:
            continue
        rel = f.relative_to(root).as_posix()
        if include and not (fnmatch.fnmatch(f.name, include) or fnmatch.fnmatch(rel, include)):
            continue
        yield f


def plan(root: Path, old: str, new: str, scope: Path, include: str = "") -> dict:
    """{rel: (count, old_text, new_text)} of the files the rename changes, or {"error": ...}."""
    from .closure import _read

    pat = _pattern(old)
    out: dict = {}
    for f in _candidates(root, scope, include):
        text = _read(f)
        if not text or old not in text:
            continue
        new_text, n = pat.subn(new, text)
        if n:
            out[f.relative_to(root).as_posix()] = (n, text, new_text)
            if len(out) > MAX_FILES:
                return {"error": f"more than {MAX_FILES} files would change; narrow path or include"}
    return out


def planned_files(old: str, new: str, path: str = ".", include: str = "") -> list:
    """Project-relative files a Rename call would write (for pre-write capture)."""
    from .config import get_project_root
    from .paths import confine_or_error

    if validate(old, new):
        return []
    root = Path(get_project_root()).resolve()
    scope, err = confine_or_error(path or ".", root=root)
    if err or scope is None or not scope.exists():
        return []
    try:
        changes = plan(root, old, new, scope, include or "")
    except Exception:
        return []
    return [] if "error" in changes else sorted(changes)


def run(old: str, new: str, path: str = ".", include: str = "") -> dict:
    """Do the rename. Returns the tool result dict."""
    from . import edit_engine
    from .cache import get_cache
    from .config import get_project_root
    from .paths import confine_or_error
    from .tools.registry import backup_before_write

    problem = validate(old, new)
    if problem:
        return {"error": problem}
    root = Path(get_project_root()).resolve()
    scope, err = confine_or_error(path or ".", root=root)
    if err or scope is None:
        return {"error": err or "Path is outside project root"}
    if not scope.exists():
        return {"error": f"Path not found: {path}"}
    changes = plan(root, old, new, scope, include or "")
    if "error" in changes:
        return {"error": changes["error"]}
    if not changes:
        return {"ok": True, "unchanged": True, "old": old, "new": new, "files": {}, "total": 0,
                "note": f"no whole-word occurrence of {old} under {path or '.'}"}
    pat_new = _pattern(new)
    written: list = []  # (path, original bytes)
    backups = 0
    try:
        pat = _pattern(old)
        for rel in sorted(changes):
            p = root / rel
            ft = edit_engine.load(p)  # strict UTF-8; keeps BOM / line endings
            new_text = pat.sub(new, ft.text)
            if new_text == ft.text:
                continue
            original = p.read_bytes()
            if backup_before_write(p, root):
                backups += 1
            edit_engine.save(p, ft.encode(new_text))
            written.append((p, original))
            get_cache().invalidate_file(p)
    except Exception as e:
        for p, original in reversed(written):
            try:
                edit_engine.save(p, original)
                get_cache().invalidate_file(p)
            except Exception:
                pass
        return {"error": f"rename failed, no file changed: {e}"}
    files = {rel: n for rel, (n, _t, _nt) in sorted(changes.items())}
    out: dict = {"ok": True, "old": old, "new": new, "files": files,
                 "total": sum(files.values()), "refs_left": 0}
    clash = sorted(rel for rel, (_n, text, _nt) in changes.items() if pat_new.search(text))
    if clash:
        out["new_existed_in"] = clash[:10]
    if backups:
        out["backups"] = backups
    broken = {}
    for rel, (_n, text, new_text) in changes.items():
        b = edit_engine.introduced_syntax_error(root / rel, text, new_text)
        if b:
            broken[rel] = b.get("syntax_error")
    if broken:
        out["syntax_error"] = broken
    return out


def evidence(data: dict) -> str:
    files = data.get("files") or {}
    return f"Rename {data.get('old')} → {data.get('new')}: {data.get('total', 0)} ref(s) in {len(files)} file(s)"

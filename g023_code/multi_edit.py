"""Multi-hunk Edit: ``more_edits`` on Edit applies several replacements to one file in one call.

Bench evidence (s28 rows): feature tasks spent two write hops on the same two
files — constant + import first, then function + test — although both files
were already in view and the hunks are independent. Telling the model in prose
that batching is safe moved nothing in an earlier A/B; an optional schema field
did (behaviour follows the schema). ``more_edits`` is that field: the hunks
apply in order against the text the earlier ones produced, all or nothing, and
the result lists every hunk's line span. Setting ``multi_edit``
(G023_MULTI_EDIT).

An item may name another ``path``: live, the model put a README hunk in
``more_edits`` of an ``app/config.py`` Edit although the schema said "same
file". Rather than fail that (and pay a hop), hunks are grouped per file and
every file is written or none.
"""

from __future__ import annotations

import copy
from typing import Iterable, List

from .config import settings

FIELD = "more_edits"

SCHEMA_PROPERTY = {
    "type": "array",
    "description": (
        "Optional. Further replacements applied after the first one, all or nothing (no file is "
        "written if any replacement fails). Each applies to this Edit's file unless it names "
        "another path. Use it to make every change the request needs in one call instead of one "
        "Edit per response."
    ),
    "items": {
        "type": "object",
        "properties": {
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
            "path": {"type": "string", "description": "Another file. Default: this Edit's path."},
        },
        "required": ["old_string", "new_string"],
    },
}


def enabled() -> bool:
    return bool(getattr(settings, "multi_edit", False))


def add_to_schemas(tools: Iterable[dict]) -> List[dict]:
    """Return schemas with ``more_edits`` on Edit (copies, never in place)."""
    out: List[dict] = []
    for schema in tools:
        if schema.get("name") == "Edit":
            schema = copy.deepcopy(schema)
            schema.setdefault("parameters", {}).setdefault("properties", {})[FIELD] = copy.deepcopy(SCHEMA_PROPERTY)
        out.append(schema)
    return out


def hunks(args: dict) -> list:
    """Every (path, old, new) of an Edit call's arguments, first hunk included."""
    path = str(args.get("path") or "")
    out = [(path, str(args.get("old_string") or ""), str(args.get("new_string") or ""))]
    for h in args.get(FIELD) or []:
        if isinstance(h, dict):
            out.append((str(h.get("path") or path), str(h.get("old_string") or ""),
                        str(h.get("new_string") or "")))
    return out


def paths(args: dict) -> list:
    """Distinct files an Edit call writes, its own path first."""
    out: list = []
    for p, _old, _new in hunks(args):
        if p and p not in out:
            out.append(p)
    return out


def groups(path: str, first: dict, more) -> "dict[str, list]":
    """{path: [hunk, ...]} in first-seen order; items without a path go to ``path``."""
    items = list(more) if isinstance(more, list) else [more]
    out: dict = {path: [first]}
    for h in items:
        target = str(h.get("path") or path) if isinstance(h, dict) else path
        out.setdefault(target, []).append(h)
    return out

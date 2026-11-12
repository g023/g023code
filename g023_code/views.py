"""View-coherent Bash deltas: show the model what a shell command changed in files it has seen.

The harness keeps, per project file, the text as it was when the model last
looked at it (a ReadFile, or its own Edit/WriteFile). A Bash call is already
bracketed by two stat fingerprints (attribution.Preimages.snapshot), so the
files it changed are *observed*, not guessed from the command text. For each
changed file the model has a view of, the Bash result carries the new text
(small files) or a compact unified diff from that view to it; other changed files are listed
by name. A formatter, codemod or ``sed -i`` then needs no re-read or
confirming grep, and the next Edit is built from the current text instead of
a stale one.

Setting ``view_deltas`` (env G023_VIEW_DELTAS). Local, no API calls.
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Optional

VIEW_MAX_FILES = 64
VIEW_MAX_BYTES = 256_000
DELTA_FILE_LINES = 40
DELTA_FILE_CHARS = 1_600
DELTA_TOTAL_CHARS = 3_200
UNSEEN_LIST_MAX = 8
# At or under this size a changed file is shown whole (its current text), not
# as a diff: live runs re-read a file after a hunk-only delta before editing it
# again, so for small files the diff saved nothing.
VIEW_FULL_CHARS = 2_400


def enabled() -> bool:
    from .config import settings

    return bool(getattr(settings, "view_deltas", False))


def _read_text(p: Path) -> Optional[str]:
    try:
        if not p.is_file() or p.stat().st_size > VIEW_MAX_BYTES:
            return None
        return p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _compact_diff(old: str, new: str) -> list[str]:
    """Unified diff rows (one context line), without the ---/+++ file header."""
    rows = list(
        difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=1)
    )
    return [r[:200] for r in rows[2:]]


class ViewStore:
    """Last-seen text per project-relative path, oldest evicted first."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root
        self._seen: dict[str, str] = {}

    def root_dir(self) -> Path:
        return self._root()

    def _root(self) -> Path:
        if self.root is None:
            from .config import get_project_root

            self.root = Path(get_project_root()).resolve()
        return self.root

    def _rel(self, path: str) -> Optional[str]:
        if not path:
            return None
        root = self._root()
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        try:
            return p.resolve().relative_to(root).as_posix()
        except (ValueError, OSError):
            return None

    def see(self, path: str) -> None:
        """Record the file's current text as the model's view of it."""
        rel = self._rel(path)
        if rel is None:
            return
        text = _read_text(self._root() / rel)
        self._seen.pop(rel, None)
        if text is None:
            return
        self._seen[rel] = text
        while len(self._seen) > VIEW_MAX_FILES:
            self._seen.pop(next(iter(self._seen)))

    def has(self, rel: str) -> bool:
        return rel in self._seen

    def clear(self) -> None:
        self._seen.clear()

    def drift(self) -> Optional[str]:
        """Changes to seen files made outside the session since the model saw them.

        Every write the agent makes (Edit, WriteFile, an observed Bash change)
        moves the view forward, so any difference left at turn start was made by
        someone else — the user's editor, a watcher, a git checkout. No guess
        about who wrote it is needed.
        """
        changed: list[str] = []
        removed: list[str] = []
        root = self._root()
        for rel, old in list(self._seen.items()):
            p = root / rel
            if not p.exists():
                removed.append(rel)
            elif _read_text(p) != old:
                changed.append(rel)
        if not changed and not removed:
            return None
        return self.deltas(changed, removed)

    def deltas(self, changed: list[str], removed: list[str] = ()) -> Optional[str]:
        """Diff text for ``changed``/``removed`` files against their views; updates the views.

        None when nothing the model could act on changed.
        """
        out: list[str] = []
        used = 0
        unseen: list[str] = []
        for rel in changed:
            old = self._seen.get(rel)
            if old is None:
                unseen.append(rel)
                continue
            new = _read_text(self._root() / rel)
            if new is None:
                self._seen.pop(rel, None)
                out.append(f"## {rel} (now binary or too large to diff)")
                continue
            self._seen[rel] = new
            if new == old:
                continue  # touched, same text as the model's view
            room = DELTA_TOTAL_CHARS - used
            if len(new) <= min(VIEW_FULL_CHARS, room):
                lines = new.splitlines()
                out.append(f"## {rel} (current text, {len(lines)} line{'' if len(lines) == 1 else 's'})")
                out.extend(lines)
                used += len(new)
                continue
            rows = _compact_diff(old, new)
            if room < 200:
                out.append(f"## {rel} (changed; diff omitted, budget spent)")
                continue
            body: list[str] = []
            size = 0
            cap = min(DELTA_FILE_CHARS, room)
            for row in rows:
                if len(body) >= DELTA_FILE_LINES or size + len(row) > cap:
                    break
                body.append(row)
                size += len(row) + 1
            more = len(rows) - len(body)
            out.append(f"## {rel}")
            out.extend(body)
            if more:
                out.append(f"(+{more} more diff lines; ReadFile for the rest)")
            used += size
        for rel in removed:
            if self._seen.pop(rel, None) is not None:
                out.append(f"## {rel} (deleted)")
        if unseen:
            names = ", ".join(unseen[:UNSEEN_LIST_MAX])
            extra = len(unseen) - UNSEEN_LIST_MAX
            out.append("also changed: " + names + (f" (+{extra} more)" if extra > 0 else ""))
        return "\n".join(out) if out else None


def changed_and_removed(
    before: Optional[dict], after: Optional[dict], root: Optional[Path] = None
) -> tuple[list[str], list[str]]:
    """Files a command changed or created, and files it deleted, from two stat fingerprints."""
    if before is None or after is None:
        return [], []
    from .paths import harness_subdir

    skip = harness_subdir(root)

    def keep(rel: str) -> bool:
        return not (skip and rel.startswith(skip))

    changed = sorted(r for r in after if before.get(r) != after[r] and keep(r))
    removed = sorted(r for r in before if r not in after and keep(r))
    return changed, removed

"""Directory digests: a bounded, templated map of a directory too big to list.

ListDir keeps its exact entry list while a listing fits. Past that it returns
this digest instead of the first N names in alphabetical order (which, in a
directory of 4,000 frames, is 200 frame names and nothing else):

- every direct child is accounted for: numbered/hashed names collapse into
  templates (``frame_{00000..03999}.png x4000  3.1 MB``), the rest are listed;
- sibling directories with numbered names collapse the same way, with their
  shared layout (``pkg{0..29}/ x30  each: lib/ (20 .js)``);
- recursive listings disclose breadth-first under a row budget: each
  subdirectory gets a fair share, and a directory that does not fit is
  summarised (counts, extension mix, size) rather than silently dropped;
- ignored directories (``.git``, ``node_modules``, ...) are one-line notes.

Scans are bounded (entries per directory, total time); no API call.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path
from typing import Optional

from .shapes import ext_histogram, group_names, human_size

SCAN_MAX_ENTRIES = 50_000
SCAN_SECONDS = 2.0
# Rows of output (each ~40-80 chars) for flat and recursive digests.
FLAT_ROWS = 60
RECURSIVE_ROWS = 80
LAYOUT_SAMPLE = 200


class _Scan:
    __slots__ = ("dirs", "files", "sizes", "capped", "error", "links")

    def __init__(self):
        self.dirs: list[str] = []
        self.files: list[str] = []
        self.sizes: dict[str, Optional[int]] = {}
        self.capped = False
        self.error: Optional[str] = None
        self.links: set[str] = set()


def scan(path: Path, *, sizes: bool = True, cap: int = SCAN_MAX_ENTRIES) -> _Scan:
    s = _Scan()
    try:
        with os.scandir(path) as it:
            for n, e in enumerate(it):
                if n >= cap:
                    s.capped = True
                    break
                try:
                    is_dir = e.is_dir()
                except OSError:
                    is_dir = False
                if is_dir:
                    s.dirs.append(e.name)
                    if e.is_symlink():
                        s.links.add(e.name)
                else:
                    s.files.append(e.name)
                    if sizes:
                        try:
                            s.sizes[e.name] = e.stat().st_size
                        except OSError:
                            s.sizes[e.name] = None
    except OSError as e:
        s.error = str(e)
    s.dirs.sort(key=str.lower)
    s.files.sort(key=str.lower)
    return s


def _count(s: _Scan, n: int) -> str:
    return f"{n:,}+" if s.capped else f"{n:,}"


def _plural(n: str, word: str) -> str:
    return f"{n} {word}" + ("" if n == "1" else "s")


def summarize_dir(path: Path, ignore: frozenset, _hops: int = 0) -> str:
    """``12 files (.py 10, .md 2), 3 dirs: a/, b/, c/, 48.0 KB`` for one directory.

    A directory holding nothing but one subdirectory is followed (``lib/: 20
    files (20 .js)``), as editors compact single-child folder chains.
    """
    s = scan(path, cap=5_000)
    if s.error:
        return f"unreadable: {s.error}"
    if not s.dirs and not s.files:
        return "empty"
    if not s.files and len(s.dirs) == 1 and s.dirs[0] not in ignore and _hops < 3:
        return f"{s.dirs[0]}/: " + summarize_dir(path / s.dirs[0], ignore, _hops + 1)
    bits = []
    if s.files:
        bits.append(_plural(_count(s, len(s.files)), "file") + f" ({ext_histogram(s.files, 3)})")
    if s.dirs:
        shown = [d for d in s.dirs if d not in ignore]
        names = ", ".join(d + "/" for d in shown[:4]) + (", ..." if len(shown) > 4 else "")
        bits.append(_plural(_count(s, len(s.dirs)), "dir") + (f": {names}" if names else ""))
    total = sum(v for v in s.sizes.values() if v)
    if total:
        bits.append(human_size(total))
    return ", ".join(bits)


def _layout(path: Path) -> tuple:
    s = scan(path, sizes=False, cap=2_000)
    exts = Counter(Path(f).suffix.lower() for f in s.files)
    return tuple(s.dirs), tuple(sorted(exts.items()))


def _file_rows(prefix: str, s: _Scan, limit: int) -> list[str]:
    groups, singles = group_names(s.files, s.sizes)
    rows = []
    for g in groups:
        row = f"{prefix}{g['template']}  x{g['count']}"
        if g.get("size"):
            row += f"  {human_size(g['size'])}"
        if g.get("gap"):
            row += f"  ({g['gap']})"
        rows.append(row)
    keep = max(0, limit - len(rows))
    for name in singles[:keep]:
        size = s.sizes.get(name)
        rows.append(f"{prefix}{name}" + (f"  {human_size(size)}" if size is not None else ""))
    if len(singles) > keep:
        rest = singles[keep:]
        rows.append(f"{prefix}... +{len(rest)} more files ({ext_histogram(rest, 4)})")
    return rows


class _Budget:
    def __init__(self, rows: int, seconds: float):
        self.rows = rows
        self.deadline = time.monotonic() + seconds

    def expired(self) -> bool:
        return time.monotonic() > self.deadline


def _dir_rows(path: Path, prefix: str, depth: int, max_depth: int, ignore: frozenset,
              budget: _Budget, share: int, skipped: list[str]) -> list[str]:
    """Rows for the children of ``path`` (already scanned by the caller's caller)."""
    s = scan(path)
    if s.error:
        return [f"{prefix}(unreadable: {s.error})"]
    rows: list[str] = []
    dirs = []
    for d in s.dirs:
        if d in ignore:
            skipped.append(prefix + d)
        else:
            dirs.append(d)
    # Sibling directories with templated names: one row with their shared layout.
    groups, singles = group_names(dirs)
    for g in groups:
        members = g["members"]
        layouts = Counter(_layout(path / m) for m in members[:LAYOUT_SAMPLE])
        common, freq = layouts.most_common(1)[0] if layouts else ((), 0)
        each = summarize_dir(path / members[0], ignore) if members else ""
        sampled = min(len(members), LAYOUT_SAMPLE)
        same = "same layout" if freq == sampled else f"{freq}/{sampled} share a layout"
        rows.append(f"{prefix}{g['template']}/  x{g['count']}  ({same}; e.g. {members[0]}/: {each})")
    expandable: list[str] = []
    for d in singles:
        if depth < max_depth and d not in s.links and not budget.expired():
            expandable.append(d)
        else:
            rows.append(f"{prefix}{d}/  ({summarize_dir(path / d, ignore)})")
    # Files of this directory get what is left of this level's share after dirs.
    file_rows = _file_rows(prefix, s, max(8, share - len(rows) - len(expandable)))
    if expandable:
        child_share = max(4, (share - len(rows) - len(file_rows)) // max(1, len(expandable)))
        for d in expandable:
            sub = _dir_rows(path / d, f"{prefix}{d}/", depth + 1, max_depth, ignore, budget, child_share, skipped)
            if len(sub) <= child_share:
                rows.append(f"{prefix}{d}/")
                rows.extend("  " + r for r in sub)
            else:
                rows.append(f"{prefix}{d}/  ({summarize_dir(path / d, ignore)})")
                rows.extend("  " + r for r in sub[: max(0, child_share - 1)])
                rows.append(f"  {prefix}{d}/... ({len(sub) - child_share + 1} more rows; ListDir {prefix}{d} for all)")
    if s.capped:
        file_rows.append(f"{prefix}(scan stopped at {SCAN_MAX_ENTRIES:,} entries)")
    return rows + file_rows


def digest(path: Path, *, recursive: bool, ignore: frozenset) -> dict:
    """Templated map of ``path`` within the row budget (see module doc)."""
    max_depth = 2 if recursive else 1
    budget = _Budget(RECURSIVE_ROWS if recursive else FLAT_ROWS, SCAN_SECONDS)
    skipped: list[str] = []
    top = scan(path)
    rows = _dir_rows(path, "", 1, max_depth, ignore, budget, budget.rows, skipped)
    out = {
        "digest": True,
        "dirs": _count(top, len(top.dirs)),
        "files": _count(top, len(top.files)),
        "size": human_size(sum(v for v in top.sizes.values() if v)) if top.files else None,
        "lines": rows,
        "legend": "name{a..b} = numbered range; xN = count; (dir: files, ext mix, size)",
    }
    if out["size"] is None:
        del out["size"]
    if skipped:
        out["skipped"] = skipped[:20]
    if budget.expired():
        out["partial"] = "time budget hit; some directories summarised without expansion"
    return out

"""Turn-zero orientation card: where the names in the request are defined.

A request that names code (``rename load_cfg in config.py``,
``cart_total_usd``) usually spends its first model call on a search for those
names. When the request names distinctive identifiers or project paths, this
module looks them up locally (one bounded scan of defined names, the same
matcher near-miss suggestions use) and appends one short line to the user
message:

    [where: load_cfg def app/config.py:12 (+5 uses in 2 files); app/main.py 40 lines]

The card costs a few dozen input tokens; a search hop it replaces costs a
model call (output plus a replay of the context). It is added only when at
least one name resolves, and never when the request names nothing.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from . import closure, near_miss

MAX_NAMES = 4
MAX_PATHS = 4
TIME_BUDGET_S = 0.4
TOTAL_MAX_BYTES = 12_000_000

_PATH = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w-]+\.[A-Za-z0-9]{1,5})(?![\w/])")
_IDENT = re.compile(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)(?![\w])")


def enabled() -> bool:
    from .config import settings

    return bool(getattr(settings, "orient_card", False))


def request_names(text: str) -> tuple[list, list]:
    """(identifiers, paths) the request names.

    Identifiers must look like code, not prose: snake_case, digits, or
    lowerCamel (``DeepSeek`` and ``README`` are words in a sentence).
    """
    paths: list = []
    for m in _PATH.finditer(text or ""):
        p = m.group(1)
        if p not in paths and not p[0].isdigit():
            paths.append(p)
    path_bits = {b for p in paths for b in re.split(r"[/.]", p)}
    names: list = []
    for m in _IDENT.finditer(text or ""):
        n = m.group(1)
        code_like = "_" in n.strip("_") or any(c.isdigit() for c in n) or (
            n[0].islower() and any(c.isupper() for c in n))
        if n in names or n in path_bits or len(n) < 4 or not code_like:
            continue
        names.append(n)
    return names[:MAX_NAMES], paths[:MAX_PATHS]


def card(text: str, root: Path, budget_s: float = TIME_BUDGET_S) -> Optional[str]:
    """One ``[where: ...]`` line, or None when nothing named resolves."""
    names, paths = request_names(text)
    if not names and not paths:
        return None
    rows: list = []
    root = root.resolve()
    for p in paths:
        f = (root / p)
        try:
            f = f.resolve()
            f.relative_to(root)
        except (ValueError, OSError):
            continue
        if f.is_file():
            try:
                n = f.read_bytes().count(b"\n")
            except OSError:
                continue
            rows.append(f"{p} {n} lines")
    if names:
        defs: dict = {}
        uses: dict = {n: [0, set()] for n in names}
        pats = {n: closure._pattern(n) for n in names}
        deadline = time.monotonic() + budget_s
        spent = 0
        for f in closure._project_files(root):
            if time.monotonic() > deadline or spent > TOTAL_MAX_BYTES:
                break
            body = closure._read(f)
            if body is None:
                continue
            spent += len(body)
            present = [n for n in names if n in body]
            if not present:
                continue
            rel = f.relative_to(root).as_posix()
            for n in present:
                k = len(pats[n].findall(body))
                if k:
                    uses[n][0] += k
                    uses[n][1].add(rel)
            for dn, line in near_miss.defined_names(body):
                if dn in present and dn not in defs:
                    defs[dn] = f"{rel}:{line}"
        for n in names:
            count, files = uses[n]
            if n in defs:
                extra = count - 1
                tail = f" (+{extra} uses in {len(files)} file{'s' if len(files) != 1 else ''})" if extra > 0 else ""
                rows.append(f"{n} def {defs[n]}{tail}")
            elif count:
                rows.append(f"{n} {count} uses, no def ({', '.join(sorted(files)[:3])})")
            else:
                rows.append(f"{n} not found")
    if not rows or all(r.endswith("not found") for r in rows):
        return None
    return "[where: " + "; ".join(rows) + "]"

"""
Project standing instructions — this program's AGENTS.md equivalent.

A ``g023.md`` in the project root is appended to the orchestrator's system
prompt. ``/g023-compact`` can write a denser ``g023_compact.md`` from it
(deepseek-flash, reasoning off) and leaves the original in place. When the
compact file exists, *that* is what the prompt uses. A project-root
``AGENTS.md``, when readable, is appended as well.

Neither file is required. Missing or unreadable files are treated as absent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .api import output_text, reasoning_param
from .config import get_project_root
from .ui import Glyph, console
from .usage import format_cost, get_usage

FULL_NAME = "g023.md"
COMPACT_NAME = "g023_compact.md"
AGENTS_NAME = "AGENTS.md"
COMPACT_MODEL = "deepseek-flash"

# Whole-string fence unwrap only — an instruction file that *contains* fences
# must keep them. The compact model sometimes wraps the whole reply anyway.
_OUTER_FENCE = re.compile(
    r"^```(?:markdown|md)?\s*\n(.*)\n```\s*$",
    re.DOTALL | re.IGNORECASE,
)

COMPACT_SYSTEM = """You compact a project's standing instruction file (g023.md) into g023_compact.md.

g023.md is this project's equivalent of AGENTS.md: persistent instructions a
coding assistant must follow on every turn. Your output is what gets appended
to the system prompt, so every rule, constraint, convention, named path,
command, and preference must survive. Drop only repetition, filler, and
examples that do not change behaviour. Do not invent new rules.

Output compacted markdown only. No preamble, no wrapping code fence.
"""


@dataclass(frozen=True)
class CompactReport:
    ok: bool
    message: str


def full_path() -> Path:
    return get_project_root() / FULL_NAME


def compact_path() -> Path:
    return get_project_root() / COMPACT_NAME


def agents_path() -> Path:
    return get_project_root() / AGENTS_NAME


def active_file() -> Optional[Path]:
    """The file the system prompt would use, or None if neither exists."""
    compact = compact_path()
    if compact.is_file():
        return compact
    full = full_path()
    if full.is_file():
        return full
    return None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _unwrap_markdown(text: str) -> str:
    text = text.strip()
    match = _OUTER_FENCE.match(text)
    return match.group(1).strip() if match else text


def _format_addendum(path: Path, text: str) -> str:
    return (
        f"\n\n# Project instructions ({path.name})\n"
        "Follow these standing project instructions.\n\n"
        f"{text}\n"
    )


def agents_addendum() -> str:
    """Project-root AGENTS.md, or empty when absent/unreadable."""
    path = agents_path()
    if not path.is_file():
        return ""
    text = _read(path).strip()
    if not text:
        return ""
    return _format_addendum(path, text)


def system_addendum() -> str:
    """Text to append to the orchestrator system prompt. Empty when unused."""
    parts: list[str] = []
    path = active_file()
    if path is not None:
        text = _read(path).strip()
        if text:
            parts.append(_format_addendum(path, text))
    agents = agents_addendum()
    if agents:
        parts.append(agents)
    return "".join(parts)


async def compact(client: Any, *, overwrite: bool = False) -> CompactReport:
    """
    Compact ``g023.md`` into ``g023_compact.md`` with Flash and no reasoning.

    Leaves ``g023.md`` untouched. ``overwrite=False`` refuses to replace an
    existing compact file.
    """
    source = full_path()
    dest = compact_path()
    if not source.is_file():
        return CompactReport(
            False,
            f"No {FULL_NAME} in the project root — nothing to compact.",
        )
    original = _read(source)
    if not original.strip():
        return CompactReport(False, f"{FULL_NAME} is empty — nothing to compact.")
    if dest.is_file() and not overwrite:
        return CompactReport(
            False,
            f"{COMPACT_NAME} already exists. Re-run and confirm to overwrite.",
        )

    try:
        with console.status(
            f"[accent]Compacting {FULL_NAME}{Glyph.ELLIPSIS}[/accent]",
            spinner="dots",
        ):
            resp = await client.create(
                model=COMPACT_MODEL,
                instructions=COMPACT_SYSTEM,
                input=[{"role": "user", "content": original}],
                temperature=0.1,
                reasoning=reasoning_param(enabled=False),
            )
    except Exception as exc:
        return CompactReport(False, f"Compaction failed: {exc}")

    delta = get_usage().record(COMPACT_MODEL, resp.get("usage"), scope="subagent")
    summary_cost = delta.cost(COMPACT_MODEL)
    summary = _unwrap_markdown(output_text(resp))
    if not summary:
        return CompactReport(
            False,
            "Compaction produced an empty file — original left untouched.",
        )

    try:
        dest.write_text(summary + "\n", encoding="utf-8")
    except OSError as exc:
        return CompactReport(False, f"Could not write {COMPACT_NAME}: {exc}")

    before, after = len(original), len(summary)
    change = (
        f"{before:,} {Glyph.ARROW} {after:,} chars"
        if after <= before
        else f"{before:,} {Glyph.ARROW} {after:,} chars (longer than the original)"
    )
    return CompactReport(
        True,
        f"Wrote {COMPACT_NAME} ({change} {Glyph.DOT} "
        f"summary cost {format_cost(summary_cost)}). {FULL_NAME} is unchanged.",
    )

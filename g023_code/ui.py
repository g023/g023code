"""
Presentation layer — the single place that decides how g023 looks.

Everything that prints to the terminal goes through here: the theme, the
glyphs, the gauges, and the compact renderings of tool calls and their results.
Keeping it in one module means the orchestrator can stay about orchestration
and still narrate what it is doing in real time.

Two rules shape the design:

* **Say what happened, not what was sent.** A tool trace shows
  ``read cli.py → 708 lines, 4 classes``, not a JSON blob.
* **Degrade quietly.** Terminals that cannot draw box characters get ASCII;
  nothing here should ever be the reason output is unreadable.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

THEME = Theme(
    {
        "brand": "bold cyan",
        "accent": "cyan",
        "muted": "dim",
        "ok": "green",
        "warn": "yellow",
        "bad": "red",
        "tool": "magenta",
        "sub": "blue",
        "cost": "yellow",
        "user": "bold green",
        "heading": "bold",
        "key": "cyan",
    }
)

console = Console(theme=THEME, highlight=False)


def _unicode_ok() -> bool:
    """False on terminals that would mangle box drawing and pictographs."""
    forced = os.environ.get("G023_ASCII", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return False
    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return "utf" in encoding


UNICODE = _unicode_ok()


class Glyph:
    """Named glyphs with an ASCII fallback, resolved once at import."""

    ARROW = "→" if UNICODE else "->"
    BACK = "←" if UNICODE else "<-"
    BULLET = "•" if UNICODE else "*"
    CHECK = "✓" if UNICODE else "ok"
    CROSS = "✗" if UNICODE else "x"
    DOT = "·" if UNICODE else "-"
    FULL = "█" if UNICODE else "#"
    EMPTY = "░" if UNICODE else "."
    SPARK = "▁▂▃▄▅▆▇█" if UNICODE else "12345678"
    ELLIPSIS = "…" if UNICODE else "..."


# Per-tool colour, icon and the verb used when narrating a call.
@dataclass(frozen=True)
class ToolStyle:
    icon: str
    color: str
    verb: str
    subagent: bool = False


TOOL_STYLES: dict[str, ToolStyle] = {
    "ReadFile": ToolStyle("📄" if UNICODE else "R", "sub", "read", subagent=True),
    "SearchContent": ToolStyle("🔎" if UNICODE else "S", "sub", "search", subagent=True),
    "AnalyzeImage": ToolStyle("👁" if UNICODE else "V", "sub", "look at", subagent=True),
    "Agent": ToolStyle("🧠" if UNICODE else "A", "sub", "delegate", subagent=True),
    "Bash": ToolStyle("⚙" if UNICODE else "$", "tool", "run"),
    "WriteFile": ToolStyle("✎" if UNICODE else "W", "warn", "write"),
    "ListDir": ToolStyle("📁" if UNICODE else "L", "tool", "list"),
    "FetchUrl": ToolStyle("🌐" if UNICODE else "F", "tool", "fetch"),
    "WebSearch": ToolStyle("🔍" if UNICODE else "?", "tool", "web search"),
}

_FALLBACK_STYLE = ToolStyle(Glyph.BULLET, "tool", "call")


def tool_style(name: str) -> ToolStyle:
    return TOOL_STYLES.get(name, _FALLBACK_STYLE)


# ---------------------------------------------------------------------------
# Small formatters
# ---------------------------------------------------------------------------

def human_bytes(n: int) -> str:
    step = 1024.0
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < step or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= step
    return f"{value:.1f} GB"


def human_ms(ms: float) -> str:
    if ms < 1000:
        return f"{ms:.0f} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s"
    minutes, seconds = divmod(int(ms / 1000), 60)
    return f"{minutes}m {seconds:02d}s"


def truncate(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + Glyph.ELLIPSIS


def shorten_path(path: str, root: Optional[str] = None) -> str:
    """Show a path relative to the project when possible — it reads far better."""
    if not path:
        return ""
    if root:
        try:
            import pathlib

            return str(pathlib.Path(path).resolve().relative_to(pathlib.Path(root).resolve()))
        except (ValueError, OSError):
            pass
    return path


def gauge(fraction: float, width: int = 24, warn: float = 0.6, danger: float = 0.85) -> Text:
    """A coloured fill bar. Colour is the signal; the number is the detail."""
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    color = "bad" if fraction >= danger else ("warn" if fraction >= warn else "ok")
    bar = Text()
    bar.append(Glyph.FULL * filled, style=color)
    bar.append(Glyph.EMPTY * (width - filled), style="muted")
    return bar


def sparkline(values: Iterable[float]) -> str:
    """Tiny inline trend for per-turn numbers (cost, tokens, latency)."""
    vals = [float(v) for v in values]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    span = hi - lo
    chars = Glyph.SPARK
    if span <= 0:
        return chars[0] * len(vals)
    return "".join(chars[min(int((v - lo) / span * (len(chars) - 1)), len(chars) - 1)] for v in vals)


def kv_table(rows: Iterable[tuple[str, Any]], *, note_column: bool = False) -> Table:
    """Two- or three-column key/value table with consistent spacing."""
    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(style="key", no_wrap=True)
    table.add_column(overflow="fold")
    if note_column:
        table.add_column(style="muted", overflow="fold")
    for row in rows:
        table.add_row(*[str(c) for c in row])
    return table


def panel(renderable: Any, title: str = "", *, style: str = "accent", subtitle: str = "") -> Panel:
    return Panel(
        renderable,
        title=f"[brand]{title}[/brand]" if title else None,
        subtitle=f"[muted]{subtitle}[/muted]" if subtitle else None,
        border_style=style,
        padding=(0, 1),
    )


# ---------------------------------------------------------------------------
# Tool call / result summarisation
# ---------------------------------------------------------------------------

def describe_call(name: str, args: dict, root: Optional[str] = None) -> str:
    """One readable phrase for a tool call — the salient argument, not the JSON."""
    args = args or {}

    def s(key: str) -> str:
        return str(args.get(key) or "")

    if name == "ReadFile":
        detail = shorten_path(s("path"), root)
        if args.get("start_line") or args.get("end_line"):
            detail += f"[muted]:{s('start_line') or 1}-{s('end_line') or 'end'}[/muted]"
        if args.get("focus"):
            detail += f" [muted]({truncate(s('focus'), 30)})[/muted]"
        return detail
    if name == "SearchContent":
        detail = f'"{truncate(s("query"), 40)}"'
        where = s("path") or s("file_glob")
        if where:
            detail += f" [muted]in {shorten_path(where, root)}[/muted]"
        return detail
    if name == "AnalyzeImage":
        return f"{shorten_path(s('path_or_url'), root)} [muted]{truncate(s('question'), 40)}[/muted]"
    if name == "Bash":
        return truncate(s("command"), 70)
    if name == "WriteFile":
        size = len(s("content").encode("utf-8"))
        return f"{shorten_path(s('path'), root)} [muted]({human_bytes(size)})[/muted]"
    if name == "ListDir":
        return shorten_path(s("path") or ".", root) + (" [muted](recursive)[/muted]" if args.get("recursive") else "")
    if name == "FetchUrl":
        return truncate(s("url"), 60)
    if name == "WebSearch":
        return f'"{truncate(s("query"), 50)}"'
    if name == "Agent":
        return f"{s('kind') or 'explore'} [muted]{truncate(s('objective'), 50)}[/muted]"
    return truncate(json.dumps(args, ensure_ascii=False), 70)


def describe_result(name: str, result: str) -> tuple[str, bool]:
    """
    Summarise what a tool actually returned: ``(phrase, ok)``.

    Falls back to a size in characters when the payload is not the JSON we
    expect, so an unrecognised tool still reports *something* useful.
    """
    size = len(result or "")
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return f"{size:,} chars", True

    if not isinstance(data, dict):
        return f"{size:,} chars", True

    if data.get("error"):
        return truncate(str(data["error"]), 70), False

    if name == "ReadFile":
        if data.get("start_line"):
            span = data["end_line"] - data["start_line"] + 1
            return f"lines {data['start_line']}-{data['end_line']} of {data['total_lines']:,} ({span} lines)", True
        meta = data.get("metadata") or {}
        bits = []
        if meta.get("loc"):
            bits.append(f"{meta['loc']:,} lines")
        if meta.get("classes"):
            bits.append(f"{len(meta['classes'])} classes")
        if meta.get("functions"):
            bits.append(f"{len(meta['functions'])} functions")
        if data.get("from_cache"):
            bits.append("[ok]cached[/ok]")
        return (", ".join(bits) or f"{size:,} chars"), True

    if name == "SearchContent":
        matches = data.get("matches") or []
        files = len({m.get("file") for m in matches if isinstance(m, dict)})
        return f"{len(matches)} matches in {files} file(s)", True

    if name == "AnalyzeImage":
        bits = [f"{len(data.get('answer') or '')} chars"]
        if data.get("cached"):
            bits.append("[ok]cached[/ok]")
        elif data.get("elapsed_ms"):
            bits.append(human_ms(data["elapsed_ms"]))
        if data.get("model"):
            bits.append(str(data["model"]))
        return f" {Glyph.DOT} ".join(bits), True

    if name == "Bash":
        code = data.get("exit_code")
        out = len(data.get("stdout") or "")
        err = len(data.get("stderr") or "")
        phrase = f"exit {code} {Glyph.DOT} {out:,} chars out"
        if err:
            phrase += f" {Glyph.DOT} {err:,} err"
        return phrase, code == 0

    if name == "WriteFile":
        return f"wrote {human_bytes(int(data.get('bytes') or 0))}", bool(data.get("ok"))

    if name == "ListDir":
        return f"{len(data.get('entries') or [])} entries", True

    if name == "FetchUrl":
        content = data.get("content") or data.get("text") or ""
        src = data.get("source", "")
        return f"HTTP {data.get('status', '?')} {Glyph.DOT} {len(content):,} chars {Glyph.DOT} {src}", True

    if name == "WebSearch":
        return f"{len(data.get('results') or [])} results", True

    if name == "Agent":
        return f"{len(data.get('plan_or_exploration') or '')} chars", True

    return f"{size:,} chars", True


# ---------------------------------------------------------------------------
# Live activity trace
# ---------------------------------------------------------------------------

@dataclass
class ToolEvent:
    name: str
    args: dict
    elapsed_ms: float = 0.0
    result_chars: int = 0
    ok: bool = True
    summary: str = ""
    denied: bool = False


@dataclass
class TurnTrace:
    """Everything worth telling the user about one user turn."""

    iterations: int = 0
    events: list[ToolEvent] = field(default_factory=list)
    thinking_ms: float = 0.0
    tool_ms: float = 0.0
    total_ms: float = 0.0

    @property
    def tool_count(self) -> int:
        return len(self.events)

    def tools_by_name(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for e in self.events:
            counts[e.name] = counts.get(e.name, 0) + 1
        return counts

    def slowest(self) -> Optional[ToolEvent]:
        return max(self.events, key=lambda e: e.elapsed_ms, default=None)


class ActivityPrinter:
    """
    Streams the trace as it happens.

    Verbosity decides how much: ``low`` shows one line per tool call (you always
    want to know a command ran), ``mid`` adds the outcome and timing, ``high``
    adds reasoning excerpts and a per-iteration token line.
    """

    def __init__(self, out: Optional[Console] = None, root: Optional[str] = None):
        self.console = out or console
        self.root = root

    def tool_start(self, name: str, args: dict) -> None:
        st = tool_style(name)
        self.console.print(
            f"  [{st.color}]{st.icon}[/{st.color}] [{st.color}]{st.verb}[/{st.color}] "
            f"{describe_call(name, args, self.root)}"
        )

    def tool_end(self, event: ToolEvent, show_timing: bool = True) -> None:
        mark = f"[ok]{Glyph.CHECK}[/ok]" if event.ok else f"[bad]{Glyph.CROSS}[/bad]"
        timing = f" [muted]{Glyph.DOT} {human_ms(event.elapsed_ms)}[/muted]" if show_timing else ""
        self.console.print(f"     {mark} [muted]{event.summary}[/muted]{timing}")

    def tool_denied(self, name: str) -> None:
        self.console.print(f"     [warn]{Glyph.CROSS} denied by user[/warn]")

    def iteration(self, n: int, tokens_in: int, tokens_out: int, cost: str) -> None:
        self.console.print(
            f"  [muted]iter {n} {Glyph.DOT} {tokens_in:,} in {Glyph.DOT} "
            f"{tokens_out:,} out {Glyph.DOT} {cost}[/muted]"
        )

    def note(self, message: str) -> None:
        self.console.print(f"  [muted]{message}[/muted]")


def trace_summary(trace: TurnTrace) -> Text:
    """One dense line closing out a turn: what ran, how long, what was slowest."""
    parts: list[str] = [f"{trace.iterations} iteration(s)"]
    if trace.tool_count:
        by_name = ", ".join(f"{n}×{c}" for n, c in sorted(trace.tools_by_name().items()))
        parts.append(f"{trace.tool_count} tool call(s): {by_name}")
        slow = trace.slowest()
        if slow is not None and slow.elapsed_ms > 0:
            parts.append(f"slowest {slow.name} {human_ms(slow.elapsed_ms)}")
    parts.append(f"model {human_ms(trace.thinking_ms)} / tools {human_ms(trace.tool_ms)}")
    return Text(f" {Glyph.DOT} ".join(parts), style="dim")


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

def status_bar(
    *,
    context_fraction: float,
    context_tokens: int,
    max_context: int,
    turn_cost: str,
    session_cost: str,
    hit_rate: float,
    elapsed_ms: float,
    model: str,
) -> Text:
    """
    The line printed under every answer.

    Cache hit rate sits next to cost on purpose: a hit is roughly fifty times
    cheaper than a miss, so the two numbers only make sense together.
    """
    bar = gauge(context_fraction, width=16)
    line = Text()
    line.append(f"{model} ", style="accent")
    line.append(f"{Glyph.DOT} ", style="dim")
    line.append_text(bar)
    line.append(f" {context_tokens:,}/{max_context:,} ({context_fraction:.0%})", style="dim")
    line.append(f" {Glyph.DOT} ", style="dim")
    line.append(f"cache {hit_rate:.0%}", style="ok" if hit_rate >= 0.5 else "dim")
    line.append(f" {Glyph.DOT} ", style="dim")
    line.append(f"turn {turn_cost}", style="cost")
    line.append(f" {Glyph.DOT} session {session_cost}", style="dim")
    line.append(f" {Glyph.DOT} {human_ms(elapsed_ms)}", style="dim")
    return line


def banner(
    *,
    version: str,
    project: str,
    home: str,
    model: str,
    effort: str,
    verbose: str,
    vision: str,
    ollama: str,
) -> Panel:
    """The startup card — orientation in one glance, no scrolling."""
    title = Text()
    title.append("g023 Code", style="brand")
    title.append(f"  v{version}", style="dim")
    title.append("\nDeepSeek V4 ", style="dim")
    title.append(f"{Glyph.DOT} Subagent-First {Glyph.DOT} Context is Currency", style="dim")

    info = kv_table(
        [
            ("project", project),
            ("home", f"[dim]{home}[/dim]"),
            ("model", f"{model}  [dim]{Glyph.DOT} thinking {effort} {Glyph.DOT} verbose {verbose}[/dim]"),
            ("vision", vision),
            ("ollama", ollama),
        ]
    )

    hint = Text()
    hint.append(f"{Glyph.ARROW} ", style="accent")
    hint.append("/help", style="brand")
    hint.append(" for commands", style="dim")
    hint.append(f"  {Glyph.DOT}  ", style="dim")
    hint.append("/ollama", style="brand")
    hint.append(" to point vision at another machine", style="dim")
    hint.append(f"  {Glyph.DOT}  ", style="dim")
    hint.append("/exit", style="brand")
    hint.append(" to quit", style="dim")

    return Panel(Group(title, "", info, "", hint), border_style="accent", padding=(1, 2))

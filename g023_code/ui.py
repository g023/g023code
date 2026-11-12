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

import difflib
import json
import os
import re
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


TOOL_STYLES: dict[str, ToolStyle] = {
    "ReadFile": ToolStyle("📄" if UNICODE else "R", "sub", "read"),
    "SearchContent": ToolStyle("🔎" if UNICODE else "S", "sub", "search"),
    "AnalyzeImage": ToolStyle("👁" if UNICODE else "V", "sub", "look at"),
    "Agent": ToolStyle("🧠" if UNICODE else "A", "sub", "delegate"),
    "Bash": ToolStyle("⚙" if UNICODE else "$", "tool", "run"),
    "WriteFile": ToolStyle("✎" if UNICODE else "W", "warn", "write"),
    "Edit": ToolStyle("✂" if UNICODE else "E", "warn", "edit"),
    "Rename": ToolStyle("✎" if UNICODE else "R", "warn", "rename"),
    "ListDir": ToolStyle("📁" if UNICODE else "L", "tool", "list"),
    "Glob": ToolStyle("⊞" if UNICODE else "G", "tool", "find"),
    "TodoWrite": ToolStyle("☑" if UNICODE else "T", "tool", "todo"),
    "LearnSearch": ToolStyle("≡" if UNICODE else "Ls", "tool", "learn-search"),
    "LearnGet": ToolStyle("≡" if UNICODE else "Lg", "tool", "learn-get"),
    "LearnPut": ToolStyle("≡" if UNICODE else "Lp", "tool", "learn-put"),
    "SessionSearch": ToolStyle("▤" if UNICODE else "Ss", "tool", "session-search"),
    "LocalAsk": ToolStyle("⌂" if UNICODE else "La", "sub", "ask local"),
    "FetchUrl": ToolStyle("🌐" if UNICODE else "F", "tool", "fetch"),
    "WebSearch": ToolStyle("🔍" if UNICODE else "?", "tool", "search"),
    # Server-side searches from sessions recorded before 1.5.0 still replay.
    "web_search": ToolStyle("🔍" if UNICODE else "?", "tool", "web search"),
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


def display_path(path: Any) -> str:
    """A path for humans: ``~`` stands for the home directory.

    Banners, ``/status`` and ``/config`` are screenshotted and pasted into issues; the
    absolute home path would publish the local account name with them.
    """
    if path is None or str(path) == "":
        return ""
    import pathlib

    text = str(path)
    try:
        home = pathlib.Path(os.path.expanduser("~"))
        rel = pathlib.Path(text).relative_to(home)
    except (ValueError, RuntimeError, OSError):
        return text
    if str(home) in ("", "/", "."):
        return text
    return "~" if str(rel) == "." else "~" + os.sep + str(rel)


def short_url(url: str) -> str:
    """Host plus a hint of the path — enough to recognise a page in one line."""
    if not url:
        return ""
    stripped = re.sub(r"^https?://(www\.)?", "", url).rstrip("/")
    host, _, path = stripped.partition("/")
    if not path:
        return host
    return f"{host}/{path}" if len(path) <= 28 else f"{host}/{Glyph.ELLIPSIS}{path[-24:]}"


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
    if name == "LocalAsk":
        files = args.get("paths") if isinstance(args.get("paths"), list) else []
        scope = f"{len(files)} file(s)" if files else "whole project"
        return f'"{truncate(s("question"), 40)}" [muted]{scope}[/muted]'
    if name == "Bash":
        return truncate(s("command"), 70)
    if name == "WriteFile":
        size = len(s("content").encode("utf-8"))
        return f"{shorten_path(s('path'), root)} [muted]({human_bytes(size)})[/muted]"
    if name == "Edit":
        return f"{shorten_path(s('path'), root)} [muted]({len(s('old_string'))} → {len(s('new_string'))} chars)[/muted]"
    if name == "Rename":
        where = shorten_path(s("path"), root) if s("path") not in ("", ".") else "project"
        return f"{truncate(s('old'), 40)} → {truncate(s('new'), 40)} [muted]in {where}[/muted]"
    if name == "Glob":
        detail = f'"{truncate(s("pattern"), 40)}"'
        if s("path"):
            detail += f" [muted]in {shorten_path(s('path'), root)}[/muted]"
        return detail
    if name == "TodoWrite":
        leaves = args.get("leaves") or []
        return f"{len(leaves)} leaf(s)"
    if name == "LearnSearch":
        return truncate(s("query"), 60)
    if name == "LearnGet":
        return s("id")
    if name == "LearnPut":
        return f"{s('id')} [muted]{s('scope')}[/muted]"
    if name == "SessionSearch":
        return truncate(s("query"), 60)
    if name == "ListDir":
        return shorten_path(s("path") or ".", root) + (" [muted](recursive)[/muted]" if args.get("recursive") else "")
    if name == "FetchUrl":
        extra = f" [muted]focus={truncate(s('focus'), 24)}[/muted]" if args.get("focus") else ""
        if args.get("offset"):
            extra += f" [muted]@{args.get('offset')}[/muted]"
        return truncate(s("url"), 60) + extra
    if name == "WebSearch":
        return truncate(s("query"), 70)
    if name == "web_search":
        # A server-side search action: either the queries it ran, or the page it
        # went to (optionally with the pattern it looked for there).
        queries = args.get("queries") or []
        if queries:
            return truncate(", ".join(f'"{q}"' for q in queries), 60)
        detail = short_url(s("url"))
        if args.get("pattern"):
            detail += f" [muted]/{truncate(s('pattern'), 24)}/[/muted]"
        return detail
    if name == "Agent":
        return f"{s('kind') or 'explore'} [muted]{truncate(s('objective'), 50)}[/muted]"
    return truncate(json.dumps(args, ensure_ascii=False), 70)


MAX_PERMISSION_DIFF_CHARS = 8000


def unified_change_diff(path: str, old: str, new: str) -> str:
    """Unified diff of ``old`` → ``new`` for a permission prompt."""
    a = old.splitlines(keepends=True)
    b = new.splitlines(keepends=True)
    if not a and not old:
        a = []
    if a and not a[-1].endswith("\n"):
        a[-1] = a[-1] + "\n"
    if b and not b[-1].endswith("\n"):
        b[-1] = b[-1] + "\n"
    rendered = "".join(
        difflib.unified_diff(a, b, fromfile=path or "old", tofile=path or "new", n=3)
    )
    if not rendered:
        rendered = "(no textual difference)"
    if len(rendered) > MAX_PERMISSION_DIFF_CHARS:
        rendered = (
            rendered[:MAX_PERMISSION_DIFF_CHARS]
            + f"\n{Glyph.ELLIPSIS}[diff truncated at {MAX_PERMISSION_DIFF_CHARS} chars]"
        )
    return rendered


def permission_diff(name: str, args: dict, root: Optional[str] = None) -> Optional[str]:
    """Diff shown at the ASK prompt for Edit/WriteFile. None for other tools."""
    args = args or {}
    path = str(args.get("path") or "")
    display = shorten_path(path, root) if path else path
    if name == "Edit":
        from .multi_edit import hunks

        diffs = [unified_change_diff(shorten_path(hp, root) if hp else display, old, new)
                 for hp, old, new in hunks(args)]
        return "\n".join(d for d in diffs if d) or None
    if name == "Rename":
        from .rename import planned_files

        files = planned_files(str(args.get("old") or ""), str(args.get("new") or ""),
                              path or ".", str(args.get("include") or ""))
        if not files:
            return None
        shown = "\n".join(f"  {f}" for f in files[:20])
        more = f"\n  ... and {len(files) - 20} more" if len(files) > 20 else ""
        return f"Files to change ({len(files)}):\n{shown}{more}"
    if name == "WriteFile":
        new = str(args.get("content") or "")
        old = ""
        if path:
            try:
                from .config import get_project_root
                from .paths import confine_or_error

                p, err = confine_or_error(path, root=get_project_root())
                if err is None and p is not None and p.is_file():
                    old = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                old = ""
        return unified_change_diff(display, old, new)
    return None


def _describe_web(name: str, result: str) -> tuple[str, bool]:
    """WebSearch/FetchUrl results start with a one-line JSON header (web.py)."""
    first = result.split("\n", 1)[0]
    try:
        head = json.loads(first)
    except (json.JSONDecodeError, ValueError):
        head = None
    if not isinstance(head, dict):
        return f"{len(result):,} chars", True
    if head.get("error"):
        return truncate(str(head["error"]), 70), False
    src = str(head.get("source", "")).split(" ")[0]
    if name == "WebSearch":
        return f"{head.get('results', 0)} results {Glyph.DOT} {src}", True
    shown = str(head.get("shown", ""))
    shown = "focus" if shown.startswith("focus") else shown
    return f"{int(head.get('chars') or 0):,} chars {Glyph.DOT} {shown} {Glyph.DOT} {src}", True


def describe_result(name: str, result: str) -> tuple[str, bool]:
    """
    Summarise what a tool actually returned: ``(phrase, ok)``.

    Falls back to a size in characters when the payload is not the JSON we
    expect, so an unrecognised tool still reports *something* useful.
    """
    size = len(result or "")
    if name in ("WebSearch", "FetchUrl"):
        return _describe_web(name, result or "")
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return f"{size:,} chars", True

    if not isinstance(data, dict):
        return f"{size:,} chars", True

    if data.get("error"):
        return truncate(str(data["error"]), 70), False

    if name == "ReadFile":
        if data.get("mode") == "raw_full":
            body = data.get("content") or ""
            return f"full file, {len(body):,} chars", bool(data.get("verbatim"))
        if data.get("summary_source") == "local_focus":
            matches = data.get("matches") or []
            if not matches:
                return "focus: no section matched", True
            best = matches[0]
            how = "verbatim" if data.get("verbatim") else "ranges"
            return (
                f"focus {Glyph.ARROW} {truncate(str(best.get('section')), 40)} "
                f"(lines {best.get('start_line')}-{best.get('end_line')}, {len(matches)} match(es), {how})"
            ), True
        if data.get("start_line"):
            start = data["start_line"]
            end = data.get("end_line") or start
            span = end - start + 1
            total = data.get("total_lines")
            of = f" of {total:,}" if isinstance(total, int) else ""
            return f"lines {start}-{end}{of} ({span} lines)", True
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
        samples = data.get("samples") or data.get("matches") or []
        total = data.get("total")
        if not isinstance(total, int):
            total = len(samples)
        files = data.get("files") or list({m.get("file") for m in samples if isinstance(m, dict)})
        return f"{total} matches ({len(samples)} shown) in {len(files)} file(s)", True

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
        size = human_bytes(int(data.get("bytes") or 0))
        if data.get("unchanged"):
            return f"unchanged ({size})", True
        phrase = f"wrote {size}"
        if data.get("backup"):
            phrase += f" \u2022 backup \u2192 {data['backup']}"
        return phrase, bool(data.get("ok"))

    if name == "Edit":
        size = human_bytes(int(data.get("bytes") or 0))
        n = int(data.get("replacements") or 0)
        phrase = f"{n} replacement(s), {size}"
        if data.get("backup"):
            phrase += f" \u2022 backup \u2192 {data['backup']}"
        return phrase, bool(data.get("ok"))

    if name == "Rename":
        files = data.get("files") or {}
        phrase = f"{int(data.get('total') or 0)} ref(s) in {len(files)} file(s)"
        if data.get("syntax_error"):
            phrase += " \u2022 syntax error"
        return phrase, bool(data.get("ok"))

    if name == "Glob":
        total = int(data.get("total") or 0)
        shown = len(data.get("files") or [])
        return f"{total} file(s) ({shown} listed)", True

    if name == "TodoWrite":
        open_n = len((data.get("working_set") or {}).get("open") or [])
        return f"{open_n} open leaf(s)", bool(data.get("ok"))

    if name == "LocalAsk":
        n = len(data.get("evidence") or [])
        return f"{n} cited line(s) {Glyph.DOT} {data.get('backend', 'local')}", bool(data.get("evidence"))

    if name in ("LearnSearch", "SessionSearch"):
        n = int(data.get("count") or len(data.get("hits") or []))
        return f"{n} hit(s)", bool(data.get("ok", True))

    if name == "LearnGet":
        return str(data.get("id") or data.get("error") or "guide"), bool(data.get("ok"))

    if name == "LearnPut":
        return str(data.get("id") or data.get("error") or "put"), bool(data.get("ok"))

    if name == "ListDir":
        if data.get("digest"):
            return f"digest, {len(data.get('lines') or [])} rows", True
        return f"{len(data.get('entries') or [])} entries", True

    # Replayed server-side web_search traces have no tool result: the
    # orchestrator builds their events straight from the web_search_call items.

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

    def step_notice(
        self, n: int, limit: int, elapsed_ms: float, cost: str, tools: int = 0
    ) -> None:
        """
        Where a long turn has got to — printed at every verbosity on purpose.

        A turn with no step cap can run as long as the model keeps calling tools,
        and the only other sign of that is a spinner that repaints over itself.
        This line stays in the scrollback, so the count, the clock and the money
        are all visible while the turn is still running rather than after it.
        """
        of = f"/{limit}" if limit else ""
        tail = "" if limit else f"  [muted]no step cap {Glyph.DOT} Ctrl-C stops[/muted]"
        self.console.print(
            f"  [warn]{Glyph.BULLET} step {n}{of}[/warn] "
            f"[muted]{Glyph.DOT} {human_ms(elapsed_ms)} {Glyph.DOT} "
            f"{tools} tool call(s) {Glyph.DOT} {cost} this turn[/muted]{tail}"
        )

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
) -> Panel:
    """The startup card — orientation in one glance, no scrolling."""
    title = Text()
    title.append("g023 Code", style="brand")
    title.append(f"  v{version}", style="dim")
    title.append("\nDeepSeek V4.1 Flash ", style="dim")
    title.append(f"{Glyph.DOT} Single-Agent by Default {Glyph.DOT} Context is Currency", style="dim")

    info = kv_table(
        [
            ("project", project),
            ("home", f"[dim]{home}[/dim]"),
            ("model", f"{model}  [dim]{Glyph.DOT} thinking {effort} {Glyph.DOT} verbose {verbose}[/dim]"),
            ("vision", vision),
        ]
    )

    hint = Text()
    hint.append(f"{Glyph.ARROW} ", style="accent")
    hint.append("/help", style="brand")
    hint.append(" for commands", style="dim")
    hint.append(f"  {Glyph.DOT}  ", style="dim")
    hint.append("/model", style="brand")
    hint.append(" for limits and prices", style="dim")
    hint.append(f"  {Glyph.DOT}  ", style="dim")
    hint.append("/exit", style="brand")
    hint.append(" to quit", style="dim")

    return Panel(Group(title, "", info, "", hint), border_style="accent", padding=(1, 2))

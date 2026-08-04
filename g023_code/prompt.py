"""
The input line.

When ``prompt_toolkit`` is installed you get command completion, persistent
history, history search, and a live status toolbar pinned to the bottom of the
terminal. When it is not, everything still works through ``rich``'s prompt —
the feature is additive, never required. ``pip install prompt_toolkit``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Callable, Optional

from rich.prompt import Prompt

from . import commands as cmd_registry
from .ui import Glyph, console

try:  # optional dependency
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import HTML
    from prompt_toolkit.history import FileHistory
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.styles import Style

    HAS_PROMPT_TOOLKIT = True
except ImportError:  # pragma: no cover - exercised only on minimal installs
    HAS_PROMPT_TOOLKIT = False


PROMPT_STYLE_DEFS = {
    "prompt": "bold ansigreen",
    "bottom-toolbar": "bg:#1c1c1c #8a8a8a",
    "bottom-toolbar.accent": "bg:#1c1c1c #5fd7ff",
    "completion-menu.completion": "bg:#1c1c1c #b2b2b2",
    "completion-menu.completion.current": "bg:#5fd7ff #000000 bold",
    "completion-menu.meta.completion": "bg:#1c1c1c #6c6c6c",
    "completion-menu.meta.completion.current": "bg:#00afd7 #000000",
}


if HAS_PROMPT_TOOLKIT:

    class SlashCompleter(Completer):
        """Completes slash commands and their first-argument choices."""

        def get_completions(self, document, complete_event):
            text = document.text_before_cursor
            candidates = cmd_registry.completions(text)
            if not candidates:
                return

            # Replace only the word being typed, not the whole line.
            tail = "" if text.endswith((" ", "\t")) else text.split()[-1] if text.split() else ""
            start = -len(tail)

            for candidate in candidates:
                command = cmd_registry.lookup(candidate)
                meta = command.summary if command else ""
                if not meta and text.split():
                    parent = cmd_registry.lookup(text.split()[0])
                    if parent and parent.args:
                        meta = parent.args[0].description
                yield Completion(candidate, start_position=start, display_meta=meta)


class Reader:
    """
    One place that knows how to read a line from the user.

    ``status`` is called right before each prompt to render the bottom toolbar,
    so the context gauge and running cost stay visible without being reprinted
    into the scrollback after every turn.
    """

    def __init__(
        self,
        history_file: Optional[Path] = None,
        status: Optional[Callable[[], str]] = None,
    ):
        self.status = status
        self.session = None
        # prompt_toolkit needs a real terminal; piped or redirected input (a
        # smoke test, a heredoc, a CI run) has to go through the plain prompt.
        if not HAS_PROMPT_TOOLKIT or not sys.stdin.isatty():
            return

        history = None
        if history_file is not None:
            try:
                history_file.parent.mkdir(parents=True, exist_ok=True)
                history = FileHistory(str(history_file))
            except OSError:
                history = None

        bindings = KeyBindings()

        @bindings.add("escape", "enter")
        def _(event):  # noqa: ANN001 - prompt_toolkit callback signature
            """Alt/Esc then Enter inserts a newline instead of submitting."""
            event.current_buffer.insert_text("\n")

        self.session = PromptSession(
            history=history,
            completer=SlashCompleter(),
            complete_while_typing=True,
            key_bindings=bindings,
            style=Style.from_dict(PROMPT_STYLE_DEFS),
            bottom_toolbar=self._toolbar if status else None,
            enable_history_search=True,
        )

    def _toolbar(self):
        if self.status is None:
            return None
        try:
            return HTML(self.status())
        except Exception:
            return None

    def _message(self, label: str):
        return [("class:prompt", f"{label} {Glyph.ARROW} ")]

    def ask(self, label: str = "you") -> str:
        """
        Read one line (or block). Raises EOFError/KeyboardInterrupt to the caller.

        Only safe outside an event loop — ``PromptSession.prompt`` starts one of
        its own. Async callers want :meth:`ask_async`.
        """
        if self.session is not None:
            return self.session.prompt(self._message(label))
        return Prompt.ask(f"[user]{label}[/user]")

    async def ask_async(self, label: str = "you") -> str:
        """The same read, driven by the caller's already-running event loop."""
        if self.session is not None:
            return await self.session.prompt_async(self._message(label))
        # rich blocks the loop while it waits, so keep it off the loop thread.
        return await asyncio.to_thread(Prompt.ask, f"[user]{label}[/user]")

    @property
    def enhanced(self) -> bool:
        return self.session is not None


def install_hint() -> str:
    """Shown once at startup when the optional dependency is missing."""
    if HAS_PROMPT_TOOLKIT:
        return ""
    return (
        f"[muted]{Glyph.BULLET} Tab-completion, history and the status bar need "
        "[bold]pip install prompt_toolkit[/bold][/muted]"
    )


def confirm(question: str, *, default: bool = False) -> bool:
    """
    A yes/no question that treats Ctrl-C / Ctrl-D as "no".

    Cancelling a prompt is an answer, not a failure: letting the EOFError out
    would surface to the user as a traceback-ish error for what was really just
    a change of mind.
    """
    from rich.prompt import Confirm

    try:
        return Confirm.ask(question, default=default)
    except (KeyboardInterrupt, EOFError):
        console.print("[muted]Cancelled.[/muted]")
        return False


def choose(
    title: str,
    options: list[tuple[str, str]],
    *,
    default: int = 1,
    allow_cancel: bool = True,
) -> Optional[int]:
    """
    A numbered picker used wherever a command could have been given an argument.

    ``options`` is a list of (label, description). Returns the 1-based index, or
    None when cancelled. Kept in this module so every picker in the app looks
    and behaves identically.
    """
    from rich.table import Table

    table = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
    table.add_column(justify="right", style="key", no_wrap=True)
    table.add_column(overflow="fold")
    table.add_column(style="muted", overflow="fold")
    for i, (label, description) in enumerate(options, start=1):
        marker = f"{i}" if i != default else f"{i}{Glyph.ARROW}"
        table.add_row(marker, label, description)
    if allow_cancel:
        table.add_row("0", "[muted]cancel[/muted]", "")

    console.print()
    console.print(f"[heading]{title}[/heading]")
    console.print(table)

    choices = [str(i) for i in range(0 if allow_cancel else 1, len(options) + 1)]
    try:
        picked = Prompt.ask("Choose", choices=choices, default=str(default), show_choices=False)
    except (KeyboardInterrupt, EOFError):
        console.print("[muted]Cancelled.[/muted]")
        return None

    index = int(picked)
    return None if index == 0 else index

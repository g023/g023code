"""Ctrl-C as a scoped cancellation, not a process-wide exception.

Why this exists: since Python 3.11 ``asyncio.run`` turns the first Ctrl-C into
``CancelledError`` inside the main task, so ``except KeyboardInterrupt`` in a
coroutine never fires. Raising ``KeyboardInterrupt`` inside a Task is worse: it
escapes the event loop. The REPL needs "stop what is running, keep the
session", so this module makes that contract explicit:

* Work that Ctrl-C may stop runs through :meth:`InterruptController.run`, which
  executes it as a child task. SIGINT cancels the innermost such task; the
  awaiting parent gets :class:`UserInterrupt` (an ordinary exception) and the
  child's own ``CancelledError`` cleanup (turn rollback, process-group kill)
  has already run.
* A synchronous prompt on the loop thread (permission ask, picker) is wrapped
  in :meth:`InterruptController.blocking_prompt`; there SIGINT raises
  ``KeyboardInterrupt`` synchronously, which the prompt treats as "no".
* Three Ctrl-C presses within two seconds while a task ignores cancellation
  raise ``KeyboardInterrupt`` at once — the escape hatch for a wedged step.
* With nothing interruptible running, SIGINT keeps its default behaviour.

``signal.signal`` + ``call_soon_threadsafe`` works on POSIX and Windows
(``loop.add_signal_handler`` is POSIX-only).
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys
import threading
import time
from typing import Any, Awaitable, Iterator, Optional, TypeVar

from rich.prompt import Confirm as _RichConfirm, Prompt as _RichPrompt

T = TypeVar("T")

FORCE_PRESSES = 3
FORCE_WINDOW_S = 2.0


class UserInterrupt(Exception):
    """The user pressed Ctrl-C and the interruptible work was cancelled."""


class InterruptController:
    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stack: list[asyncio.Task] = []
        self._requested: set[int] = set()  # id() of tasks we cancelled
        self._blocking = 0
        self._presses: list[float] = []
        self._previous: Any = None
        self._installed = False

    # -- installation ---------------------------------------------------------

    def install(self) -> bool:
        """Route SIGINT here. Only the main thread may install; returns success."""
        if self._installed or threading.current_thread() is not threading.main_thread():
            return self._installed
        try:
            self._loop = asyncio.get_running_loop()
            self._previous = signal.signal(signal.SIGINT, self._on_sigint)
        except (RuntimeError, ValueError):
            return False
        self._installed = True
        return True

    def uninstall(self) -> None:
        if not self._installed:
            return
        try:
            signal.signal(signal.SIGINT, self._previous or signal.default_int_handler)
        except (ValueError, TypeError):
            pass
        self._installed = False

    @property
    def installed(self) -> bool:
        return self._installed

    # -- the handler ----------------------------------------------------------

    def _on_sigint(self, signum: int, frame: Any) -> None:
        if self._blocking:
            raise KeyboardInterrupt
        now = time.monotonic()
        self._presses = [t for t in self._presses if now - t < FORCE_WINDOW_S] + [now]
        task = self._stack[-1] if self._stack else None
        if task is None or task.done() or self._loop is None or self._loop.is_closed():
            raise KeyboardInterrupt
        if len(self._presses) >= FORCE_PRESSES:
            self._presses.clear()
            raise KeyboardInterrupt
        self._loop.call_soon_threadsafe(self._cancel, task)

    def _cancel(self, task: asyncio.Task) -> None:
        if not task.done():
            self._requested.add(id(task))
            task.cancel()

    def interrupt_current(self) -> bool:
        """Cancel the innermost interruptible task (same as one Ctrl-C). For tests/UI."""
        task = self._stack[-1] if self._stack else None
        if task is None or task.done():
            return False
        self._cancel(task)
        return True

    # -- scopes ---------------------------------------------------------------

    async def run(self, awaitable: Awaitable[T]) -> T:
        """Await ``awaitable`` as a Ctrl-C-cancellable child task.

        Raises :class:`UserInterrupt` when Ctrl-C cancelled it; a cancellation
        of the caller itself still propagates as ``CancelledError``.
        """
        task = asyncio.ensure_future(awaitable)
        self._stack.append(task)
        try:
            try:
                return await task
            except asyncio.CancelledError:
                me = asyncio.current_task()
                if me is not None and me.cancelling():
                    raise  # we were cancelled, not just the child
                if task.cancelled() and id(task) in self._requested:
                    raise UserInterrupt() from None
                raise
        finally:
            self._requested.discard(id(task))
            if self._stack and self._stack[-1] is task:
                self._stack.pop()
            elif task in self._stack:
                self._stack.remove(task)

    @contextlib.contextmanager
    def blocking_prompt(self) -> Iterator[None]:
        """Mark a synchronous prompt on the loop thread: Ctrl-C raises KI there."""
        self._blocking += 1
        try:
            yield
        finally:
            self._blocking -= 1


# One per process: the SIGINT handler is process-wide too.
controller = InterruptController()


def blocking_prompt():
    return controller.blocking_prompt()


# ---------------------------------------------------------------------------
# Prompts that ask once when nobody is at the keyboard
# ---------------------------------------------------------------------------
# rich re-asks after an invalid answer. With piped stdin every re-ask eats the
# next input line, so one confirm swallowed the rest of a scripted session
# ("/settings reset" then 21 commands read as bad y/n answers). Without a tty
# an unusable answer is a decline (EOFError: every caller treats it as "no").


def _on_bad_answer(self, value, error) -> None:
    if not sys.stdin.isatty():
        raise EOFError(f"unusable answer {value!r} on non-interactive stdin")
    self.console.print(error, markup=True)


class Confirm(_RichConfirm):
    on_validate_error = _on_bad_answer


class Prompt(_RichPrompt):
    on_validate_error = _on_bad_answer

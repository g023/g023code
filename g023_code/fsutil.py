"""Crash- and race-safe file primitives shared by every on-disk store.

Invariants these helpers make explicit:

* ``atomic_write_text`` / ``atomic_write_bytes``: a reader sees either the old file or the new file,
  never a torn one (temp file in the same directory + ``os.replace``).
* ``create_exclusive``: allocating a "unique" name is a single atomic step
  (``O_CREAT | O_EXCL``), so two processes can never claim the same name.
* ``file_lock``: read-modify-write cycles on a shared file are serialised
  across processes (advisory lock on a sidecar ``.lock`` file; on platforms
  without ``fcntl``/``msvcrt`` it degrades to a no-op).
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Iterator, Optional

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt as _msvcrt  # type: ignore[import-not-found]
except ImportError:
    _msvcrt = None  # type: ignore[assignment]


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> None:
    """Replace ``path`` with ``text`` atomically; keeps an existing file's mode."""
    atomic_write_bytes(path, text.encode(encoding))


def atomic_write_bytes(path: Path | str, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically; keeps an existing file's mode."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode: Optional[int] = None
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def create_exclusive(
    directory: Path | str,
    stem: str,
    suffix: str,
    *,
    text: str = "",
    encoding: str = "utf-8",
    attempts: int = 1000,
) -> Path:
    """Create ``{stem}{suffix}`` (or ``{stem}-N{suffix}``) that did not exist before.

    The existence check and the creation are one ``O_EXCL`` syscall, so two
    writers racing for the same name get different files.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for n in range(attempts):
        name = f"{stem}{suffix}" if n == 0 else f"{stem}-{n}{suffix}"
        path = directory / name
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        return path
    raise FileExistsError(f"no free name for {stem}{suffix} in {directory}")


def create_next_numbered(
    directory: Path | str,
    name_for: Callable[[int], str],
    start: int,
    *,
    text: str = "",
    encoding: str = "utf-8",
    attempts: int = 10000,
) -> tuple[int, Path]:
    """Create the first ``name_for(n)`` (n >= start) that does not exist, atomically."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for n in range(start, start + attempts):
        path = directory / name_for(n)
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
            fh.write(text)
        return n, path
    raise FileExistsError(f"no free numbered name in {directory}")


# In-process serialisation: flock locks belong to the open file description,
# so two threads of one process would each "get" the lock. A per-path thread
# lock closes that gap.
_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock(key: str) -> threading.RLock:
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = _THREAD_LOCKS[key] = threading.RLock()
        return lock


@contextlib.contextmanager
def file_lock(path: Path | str, *, timeout: float = 10.0) -> Iterator[None]:
    """Hold an advisory cross-process lock for ``path`` (hidden sidecar ``.{name}.lock`` file).

    Times out after ``timeout`` seconds and then proceeds unlocked: a stuck
    lock must never wedge the CLI, and the writes it guards are atomic anyway.
    """
    path = Path(path)
    lock_path = path.with_name(f".{path.name}.lock")  # hidden: stays out of listings
    tlock = _thread_lock(os.path.abspath(lock_path))
    with tlock:
        fh = None
        locked = False
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(lock_path, "a+b")
            deadline = time.monotonic() + timeout
            while True:
                try:
                    if _fcntl is not None:
                        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
                    elif _msvcrt is not None:  # pragma: no cover - Windows
                        fh.seek(0)
                        _msvcrt.locking(fh.fileno(), _msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(0.02)
        except OSError:
            fh = None
        try:
            yield
        finally:
            if fh is not None:
                if locked:
                    with contextlib.suppress(OSError):
                        if _fcntl is not None:
                            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
                        elif _msvcrt is not None:  # pragma: no cover
                            fh.seek(0)
                            _msvcrt.locking(fh.fileno(), _msvcrt.LK_UNLCK, 1)
                fh.close()

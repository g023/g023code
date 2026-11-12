"""Counterfactual failure attribution: did this turn's edits cause the failure?

After a fix, the model usually runs a broader check (the full suite). A failure
there looks the same whether this turn's edits caused it or it was already
there, so the model goes to find out: it reads the failing test and module and
re-runs things. A 1.5.0 development benchmark measured about a third of all calls landing after
the target check had already passed, most of them spent on that question.

The harness can answer it with no model call. Re-run the same command in a
disposable *shadow* copy of the project, where every file this turn wrote is
restored to its pre-turn bytes (and files the turn created are absent). Then
diff the failure sets:

  new           fails now, passes with the edits reverted -> caused by this turn
  pre_existing  fails both ways                           -> not caused by it
  fixed         fails only with the edits reverted        -> fixed by this turn

The answer is attached to the check's own tool result, so it costs a few dozen
tokens instead of 1-4 hops.

Soundness rules:
  * Pre-images are captured before the first write of each path in a turn.
    Every Bash command runs between two stat fingerprints of the project; if it
    changed a file that has no pre-image, it *taints* the turn and attribution
    is skipped from then on. (Decided by effect, not by parsing the command:
    `cp -r . "$tmp"` is fine, `python fix.py` that writes is not.)
  * The shadow tree is a real copy (never hard links), so nothing the shadow
    run does can touch the project. Large dependency directories (.venv,
    node_modules) are symlinked read-through. Trees over the size budget are
    skipped.
  * Occurrences of the project's absolute path in the command are rewritten
    to the shadow root.
  * The result is a measurement, not a verdict about intent. Flaky tests read
    as new or fixed.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Iterable, Optional

# Directories never copied into the shadow tree (VCS, caches, g023's own state).
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", ".g023", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".tox", ".nox", ".cache", ".idea", ".vscode",
})
# Large, read-mostly dependency trees: symlinked into the shadow, not copied.
LINK_DIRS = frozenset({".venv", "venv", "node_modules", ".gradle"})
MAX_FILES = 5_000
MAX_BYTES = 64 * 1024 * 1024
MAX_LISTED = 8

_FAILURE_PATTERNS = (
    # pytest short summary: "FAILED tests/test_x.py::test_a - AssertionError"
    re.compile(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s.*)?$", re.M),
    # unittest: "FAIL: test_a (tests.test_x.T)"
    re.compile(r"^(?:FAIL|ERROR):\s+(\S+)\s+\(([\w.]+)\)", re.M),
    # go test: "--- FAIL: TestX (0.00s)"
    re.compile(r"^\s*--- FAIL:\s+(\S+)", re.M),
    # cargo test: "test tests::x ... FAILED"
    re.compile(r"^test\s+(\S+)\s+\.\.\.\s+FAILED", re.M),
    # jest / vitest: "  ✕ adds numbers (3 ms)"
    re.compile(r"^\s*[✕×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", re.M),
)


def failure_ids(text: str) -> set[str]:
    """Names of the failing tests a runner printed (empty when none are named)."""
    found: set[str] = set()
    for pat in _FAILURE_PATTERNS:
        for m in pat.finditer(text or ""):
            if m.lastindex and m.lastindex >= 2:
                found.add(f"{m.group(2)}.{m.group(1)}")
            else:
                found.add(m.group(1).strip())
    return found


class Preimages:
    """Pre-turn bytes of every file this turn wrote (``None`` = did not exist)."""

    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root).resolve() if root else None
        self.files: dict[str, Optional[bytes]] = {}
        self.tainted = False

    def reset(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.files = {}
        self.tainted = False

    def capture(self, path: str) -> None:
        """Remember ``path``'s current bytes, once per turn, before a write."""
        if not path:
            return
        if self.root is None:
            from .config import get_project_root

            self.root = Path(get_project_root()).resolve()
        p = Path(path)
        if not p.is_absolute():
            p = self.root / p
        try:
            p = p.resolve()
            rel = p.relative_to(self.root).as_posix()
        except (OSError, ValueError):
            return
        if rel in self.files:
            return
        try:
            self.files[rel] = p.read_bytes() if p.is_file() else None
        except OSError:
            self.tainted = True

    def snapshot(self) -> Optional[dict[str, tuple[int, int]]]:
        """Stat fingerprint {rel: (mtime_ns, size)} of the project, or None over budget."""
        if self.root is None:
            from .config import get_project_root

            self.root = Path(get_project_root()).resolve()
        return _stat_tree(self.root)

    def note_bash(self, before: Optional[dict], after: Optional[dict]) -> list[str]:
        """Taint the turn if a shell command changed a file that has no pre-image.

        Bash leaves no pre-image, so a project file it created, changed or
        removed would make the shadow tree's "pre-turn" state wrong. Writes to
        files that already have a pre-image are fine: the pre-image is older.
        Returns the project files the command created or changed (sorted).
        """
        if before is None or after is None:
            self.tainted = True
            return []
        changed = sorted(rel for rel in before.keys() | after.keys() if before.get(rel) != after.get(rel))
        if any(rel not in self.files for rel in changed):
            self.tainted = True
        return [rel for rel in changed if rel in after]

    def changed(self) -> dict[str, Optional[bytes]]:
        """Only the paths whose bytes now differ from the pre-image."""
        out: dict[str, Optional[bytes]] = {}
        for rel, before in self.files.items():
            p = self.root / rel
            try:
                now = p.read_bytes() if p.is_file() else None
            except OSError:
                now = b""
            if now != before:
                out[rel] = before
        return out

    def usable(self) -> bool:
        return self.root is not None and not self.tainted and bool(self.changed())


def _harness_abs(root: Path) -> Optional[str]:
    """Absolute path of the harness-owned subtree inside ``root`` (see paths.harness_subdir)."""
    from .paths import harness_subdir

    sub = harness_subdir(root)
    return os.path.normpath(os.path.join(str(root), sub)) if sub else None


def _walk_dirs(dirpath: str, dirnames: list[str], harness: Optional[str]) -> list[str]:
    return [d for d in dirnames if d not in SKIP_DIRS and os.path.join(dirpath, d) != harness]


def _stat_tree(root: Path) -> Optional[dict[str, tuple[int, int]]]:
    out: dict[str, tuple[int, int]] = {}
    harness = _harness_abs(root)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in _walk_dirs(dirpath, dirnames, harness) if d not in LINK_DIRS]
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root).replace(os.sep, "/")] = (st.st_mtime_ns, st.st_size)
            if len(out) > MAX_FILES:
                return None
    return out


def _build_shadow(root: Path, dest: Path) -> bool:
    """Copy ``root`` into ``dest`` within budget. False when over budget."""
    files = 0
    size = 0
    harness = _harness_abs(root)
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        target_dir = dest / rel_dir
        keep = []
        for d in _walk_dirs(dirpath, dirnames, harness):
            src = Path(dirpath) / d
            if d in LINK_DIRS or src.is_symlink():
                try:
                    os.symlink(os.path.realpath(src), target_dir / d)
                except OSError:
                    return False
                continue
            keep.append(d)
            (target_dir / d).mkdir(exist_ok=True)
        dirnames[:] = keep
        for name in filenames:
            src = Path(dirpath) / name
            dst = target_dir / name
            try:
                if src.is_symlink():
                    os.symlink(os.readlink(src), dst)
                    continue
                st = src.stat()
            except OSError:
                continue
            files += 1
            size += st.st_size
            if files > MAX_FILES or size > MAX_BYTES:
                return False
            try:
                shutil.copy2(src, dst)
            except OSError:
                return False
    return True


def _restore(dest: Path, preimages: dict[str, Optional[bytes]]) -> None:
    for rel, data in preimages.items():
        p = dest / rel
        if data is None:
            try:
                p.unlink()
            except FileNotFoundError:
                pass
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.is_symlink():
            p.unlink()
        p.write_bytes(data)


def rewrite_command(command: str, root: Path, shadow: Path) -> str:
    for form in {str(root), str(root.resolve())}:
        if form and form != "/":
            command = command.replace(form, str(shadow))
    return command


async def shadow_run(
    command: str,
    root: Path,
    preimages: dict[str, Optional[bytes]],
    timeout: float,
    probe=None,
) -> Optional[tuple[Optional[int], str, float]]:
    """Run ``command`` against the pre-turn tree. (exit_code, output, ms) or None."""
    from .tools.registry import bash_env, kill_process_group, strip_ansi

    root = Path(root)
    tmp = Path(tempfile.mkdtemp(prefix="g023_shadow_"))
    started = time.perf_counter()
    try:
        ok = await asyncio.to_thread(_build_shadow, root, tmp)
        if not ok:
            return None
        _restore(tmp, preimages)
        bash = shutil.which("bash")
        cmd = rewrite_command(command, root, tmp)
        argv = [bash, "-o", "pipefail", "-c", cmd] if bash else ["/bin/sh", "-c", cmd]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(tmp),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=probe.env(bash_env(tmp)) if probe is not None else bash_env(tmp),
            start_new_session=(os.name == "posix"),
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            # Kill before the finally removes the directory the group runs in.
            kill_process_group(proc)
            raise
        except asyncio.TimeoutError:
            kill_process_group(proc)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            return None
        text = strip_ansi(out.decode("utf-8", errors="replace")).replace(str(tmp), str(root))
        return proc.returncode, text, (time.perf_counter() - started) * 1000
    except OSError:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# `| tail -20`, `| head -n 5`: output views that hide failure ids. Removing
# them changes only how much is printed (exit codes come from the runner
# under pipefail), so both sides of the counterfactual see the full list.
_VIEW_FILTER = re.compile(
    r"\|\s*(?:tail|head)(?:\s+-{1,2}[\w=+-]+)*(?:\s+[+-]?\d+)?\s*(?=$|;|&&|\|\||\n|\))"
)
_INTERRUPTED = re.compile(r"Interrupted: \d+ errors? during collection|error: could not compile|"
                          r"no tests ran|FAILED \(errors=\d+\)\s*$", re.M)


def strip_view_filters(command: str) -> str:
    """The command without trailing ``| tail``/``| head`` views."""
    return _VIEW_FILTER.sub("", command)


def _listed(names: Iterable[str]) -> list[str]:
    names = sorted(names)
    if len(names) > MAX_LISTED:
        return names[:MAX_LISTED] + [f"... +{len(names) - MAX_LISTED} more"]
    return names


def attribute(
    after_text: str,
    after_code: Optional[int],
    before_text: str,
    before_code: Optional[int],
) -> dict:
    """Diff a failing check against the same check with this turn's edits reverted."""
    after = failure_ids(after_text)
    before = failure_ids(before_text)
    out: dict = {"baseline": "same command with this turn's edits reverted (shadow copy)"}
    if _INTERRUPTED.search(after_text or ""):
        out["note"] = "The runner stopped early (collection/compile error); other tests did not run."
    if after or before:
        new, pre, fixed = after - before, after & before, before - after
        if new:
            out["new"] = _listed(new)
        if pre:
            out["pre_existing"] = _listed(pre)
        if fixed:
            out["fixed_by_edits"] = _listed(fixed)
        if new and pre:
            out["verdict"] = (
                f"{len(new)} failure(s) are new (caused by this turn's edits); "
                f"{len(pre)} also fail without them (pre-existing)."
            )
        elif new:
            out["verdict"] = f"{len(new)} failure(s) are new: they pass with this turn's edits reverted."
        elif pre:
            out["verdict"] = (
                f"All {len(pre)} failure(s) also fail with this turn's edits reverted: "
                "pre-existing, not caused by them."
            )
        else:
            out["verdict"] = "No named failures remain; the edits fixed the ones that failed before."
        return out
    if before_code not in (0, None):
        out["verdict"] = (
            f"The command also fails (exit {before_code}) with this turn's edits reverted: "
            "the failure predates them."
        )
    elif before_code == 0:
        out["verdict"] = "The command passes with this turn's edits reverted: the failure is caused by them."
    else:
        out["verdict"] = "Baseline run gave no exit code; attribution unknown."
    return out

"""
Tool registry: permission checks + dispatch to local executors or subagents.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import shutil
import sys
import time
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from ..interrupts import Confirm, Prompt

from ..finish import FINISH_ARG
from ..hops import CHECK_ARG
from ..config import get_project_root, settings
from ..cache import get_cache
from .. import closure, edit_engine
from ..interrupts import blocking_prompt
from ..paths import confine_or_error, rel_to_root
from ..subagents.searcher import IGNORE_DIRS
from ..ui import console
from .schemas import TOOL_SCHEMAS
from .. import testoracle


def _describe_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


# Permission levels
ALLOW = "allow"
ASK = "ask"
BLOCK = "block"

# Read-only and cheap: these stay allowed whatever the default is. WebSearch
# is here because it only sends a query to DeepSeek (as the retired server-side
# search did, unprompted) and repeats are served from the machine-wide cache.
SAFE_TOOLS = (
    "WebSearch",
    "ReadFile",
    "SearchContent",
    "ListDir",
    "Glob",
    "TodoWrite",
    "LearnSearch",
    "LearnGet",
    "LearnPut",
    "SessionSearch",
    "LocalAsk",
)

# Everything else follows settings.permission_default. FetchUrl included: the
# page is fetched by DeepSeek's Fetch API, never by this machine, and forcing a
# prompt made unattended runs (permission_default=allow) unable to read any page.
DEFAULT_ASK_TOOLS = ("AnalyzeImage", "Bash", "WriteFile", "Edit", "Rename", "Agent", "FetchUrl")


def default_policy() -> Dict[str, str]:
    """The starting permission table, honouring settings.permission_default."""
    fallback = settings.permission_default
    if fallback not in (ALLOW, ASK, BLOCK):
        fallback = ASK
    policy: Dict[str, str] = {name: ALLOW for name in SAFE_TOOLS}
    policy.update({name: fallback for name in DEFAULT_ASK_TOOLS})
    return policy


# ---------------------------------------------------------------------------
# Bash output hygiene (context is currency)
# ---------------------------------------------------------------------------
# Raw command dumps are a direct context cost. Strip ANSI, keep pytest
# failures, cap at the source — then the vault sees a smaller body.
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|].*?(?:\x07|\x1b\\))")
_BLANK_RUN = re.compile(r"\n{3,}")
_PYTEST_FAIL_BLOCK = re.compile(r"^={5,}\s*(FAILURES|ERRORS)\s*={5,}", re.M)
BASH_STDOUT_CAP = 6_000
BASH_STDERR_CAP = 1_500


def strip_ansi(text: str) -> str:
    if not text or "\x1b" not in text:
        return text
    return _ANSI_RE.sub("", text)


def compact_command_output(text: str) -> str:
    """Decision-ready command output: no ANSI, no passing-test chatter."""
    text = strip_ansi(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = _BLANK_RUN.sub("\n\n", text)
    fail = _PYTEST_FAIL_BLOCK.search(text)
    if fail:
        head = text[: min(600, fail.start())].rstrip()
        tail = text[fail.start() :]
        if len(head) + len(tail) + 40 < len(text):
            return head + "\n\n… [passing tests elided] …\n\n" + tail
        return text
    if len(text) > 4_000 and re.search(r"\d+ passed", text):
        lines = text.splitlines()
        if len(lines) > 40:
            return "\n".join(lines[:6] + ["… [passing output elided] …"] + lines[-20:])
    return text


def cap_command_output(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    keep_tail = max(0, limit - 280)
    return text[:200] + f"\n… [{len(text) - limit} chars elided] …\n" + text[-keep_tail:]


def prepare_bash_payload(stdout: str, stderr: str, exit_code: int) -> dict:
    return {
        "exit_code": exit_code,
        "stdout": cap_command_output(compact_command_output(stdout), BASH_STDOUT_CAP),
        "stderr": cap_command_output(compact_command_output(stderr), BASH_STDERR_CAP),
    }


def keep_full_output(payload: dict, stdout: str, stderr: str) -> None:
    """When capping/compaction dropped text, save the raw streams to a vault file.

    ``payload["full_output"]`` then names a project-relative file the model can
    read with ReadFile ranges or ``grep -n`` instead of re-running the command.
    """
    def norm(t: str) -> str:
        t = strip_ansi(t or "").replace("\r\n", "\n").replace("\r", "\n")
        return _BLANK_RUN.sub("\n\n", t)

    if payload.get("stdout") == norm(stdout) and payload.get("stderr") == norm(stderr):
        return
    try:
        from .. import salience

        rel, lines = salience.write_vault_file(
            salience.bash_full_body(strip_ansi(stdout or ""), strip_ansi(stderr or ""))
        )
    except Exception:
        return
    if rel:
        payload["full_output"] = rel
        payload["full_lines"] = lines


# ---------------------------------------------------------------------------
# Bash environment: process groups, the python shim, exit-127 hints
# ---------------------------------------------------------------------------
# Measured (1.5.0 development bench): 7 of 10 runs typed `python ...` on a box that only
# has python3, got "python: not found" and spent ~2 hops recovering. When
# `python` is absent but python3 exists, a shim directory (outside the
# project, in the machine-wide cache) is prepended to PATH so the predictable
# recovery never costs a hop.
# Memo of the last shim directory that was verified to hold a live ``python``
# link. Keyed by the directory itself, so a changed G023_HOME / global cache dir
# (or a deleted link) is re-checked instead of serving a stale PATH entry.
_SHIM_OK: Optional[Path] = None

# command -> what to use instead, when the command itself is missing.
_NOT_FOUND_ALTERNATIVES = {
    "python": ("python3",),
    "pip": ("pip3", "python3 -m pip"),
    "pip3": ("python3 -m pip",),
    "pytest": ("python3 -m pytest",),
    "py.test": ("python3 -m pytest",),
}
_NOT_FOUND_RE = re.compile(r"(?:^|\n)(?:[^\n:]*: )?(?:(?:line )?\d+: )?([\w.+-]+): (?:command )?not found")


def _python_shim_dir() -> Optional[Path]:
    """A directory whose ``python`` points at ``python3``, when ``python`` is missing.

    Idempotent and safe under concurrent callers: the link is created via a
    per-process-and-thread temp name and an atomic rename; an existing link is reused.
    """
    global _SHIM_OK
    if os.name != "posix" or shutil.which("python"):
        return None
    py3 = shutil.which("python3")
    if not py3:
        return None
    try:
        from ..config import get_global_cache_dir

        d = get_global_cache_dir() / "shims"
        link = d / "python"
        if _SHIM_OK == d and link.exists():
            return d
        d.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            tmp = d / f".python.{os.getpid()}.{threading.get_ident()}"
            if tmp.is_symlink() or tmp.exists():
                tmp.unlink()
            tmp.symlink_to(py3)
            os.replace(tmp, link)
        _SHIM_OK = d
        return d
    except OSError:
        _SHIM_OK = None
        return None


def bash_env(root: Path) -> dict:
    env = {**os.environ, "PWD": str(root)}
    shim = _python_shim_dir()
    if shim is not None:
        env["PATH"] = str(shim) + os.pathsep + env.get("PATH", "")
    return env


def kill_process_group(proc) -> None:
    """SIGKILL the whole group started with start_new_session (fallback: the shell)."""
    if os.name == "posix":
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        proc.kill()
    except ProcessLookupError:
        pass


def not_found_hint(exit_code: Optional[int], stderr: str) -> Optional[str]:
    """For exit 127, name an installed alternative so the retry needs no probing hop."""
    if exit_code != 127:
        return None
    m = _NOT_FOUND_RE.search(stderr or "")
    if not m:
        return None
    name = m.group(1)
    for alt in _NOT_FOUND_ALTERNATIVES.get(name, ()):
        if shutil.which(alt.split()[0]):
            return f"{name} is not installed here; use `{alt}`."
    return f"{name} is not installed here."


# ---------------------------------------------------------------------------
# WriteFile backups
# ---------------------------------------------------------------------------
# Before overwriting a file, keep the previous version under .g023/backups/
# so a bad write is one copy away. The layout mirrors the file tree (one
# folder per file, timestamped copies inside), and only the newest few copies
# per file are kept, so the folder stays browsable instead of accumulating
# junk. Backups are never taken for .g023/ itself, except .g023/bbs/
# (bulletin-board threads are recoverable that way).
BACKUP_DIR = "backups"
BACKUP_SUFFIX = ".bak"
UNDONE_SUFFIX = ".undone"
BACKUP_KEEP_DEFAULT = 5

# Order is part of the contract: /undo restores "the newest" backup. Names
# used to carry only whole seconds plus a content hash, so two writes in one
# second were ordered by the random hash. Names now carry nanoseconds, strictly
# increasing within the process, and ordering is parsed, never left to luck.
_BACKUP_NAME = re.compile(r"^(\d{8}-\d{6})(?:\.(\d{9}))?-")
_last_backup_ns = 0


def _backup_stamp() -> str:
    global _last_backup_ns
    ns = max(time.time_ns(), _last_backup_ns + 1)
    _last_backup_ns = ns
    secs, frac = divmod(ns, 1_000_000_000)
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(secs)) + f".{frac:09d}"


def _backup_sort_key(path: Path) -> tuple:
    m = _BACKUP_NAME.match(path.name)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    if not m:
        return ("", -1, mtime)
    # Legacy whole-second names sort before any sub-second name of that
    # second; among themselves the file mtime breaks the tie.
    return (m.group(1), int(m.group(2)) if m.group(2) else -1, mtime)


def _versions(backup_dir: Path, suffix: str = BACKUP_SUFFIX) -> list[Path]:
    """Snapshots in ``backup_dir``, newest first."""
    return sorted(backup_dir.glob(f"*{suffix}"), key=_backup_sort_key, reverse=True)


def _prune(backup_dir: Path, suffix: str, keep: int) -> int:
    versions = _versions(backup_dir, suffix)
    for stale in versions[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass
    return min(len(versions), keep)


def _keep_count() -> int:
    return max(1, int(getattr(settings, "write_backup_keep", BACKUP_KEEP_DEFAULT) or BACKUP_KEEP_DEFAULT))


def _snapshot(data: bytes, backup_dir: Path, suffix: str) -> Path:
    """Write one snapshot atomically (a crash leaves no half-written backup)."""
    digest = hashlib.sha256(data).hexdigest()[:8]
    target = backup_dir / f"{_backup_stamp()}-{digest}{suffix}"
    edit_engine.save(target, data)
    return target


def backup_before_write(path: Path, root: Path) -> Optional[dict]:
    """Snapshot ``path`` into ``root/.g023/backups/`` before it is overwritten.

    Returns ``None`` when there is nothing worth preserving (a brand-new file,
    a path inside ``.g023/`` itself, backups disabled), else a dict describing
    the backup that was taken. Older backups for the same file are pruned down
    to the newest ``settings.write_backup_keep`` (default 5).
    """
    if not settings.write_backup:
        return None
    if not path.exists() or not path.is_file():
        return None
    try:
        rel_g023 = path.relative_to(root / ".g023")
    except ValueError:
        rel_g023 = None
    # .g023/ is cache, sessions, logs — not worth snapshotting. .g023/bbs/
    # is the bulletin board: a clobber of a thread is recoverable this way.
    if rel_g023 is not None and rel_g023.parts[:1] != ("bbs",):
        return None
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None

    parent_rel = "" if rel.parent == Path(".") else str(rel.parent)
    backup_dir = root / ".g023" / BACKUP_DIR / parent_rel / path.name
    backup_dir.mkdir(parents=True, exist_ok=True)

    old = path.read_bytes()
    backup_path = _snapshot(old, backup_dir, BACKUP_SUFFIX)
    count = _prune(backup_dir, BACKUP_SUFFIX, _keep_count())

    return {
        "backup": str(backup_path.relative_to(root)),
        "backup_count": count,
        "previous_bytes": len(old),
    }


def backup_dir_for(path: Path, root: Path) -> Optional[Path]:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    parent_rel = "" if rel.parent == Path(".") else str(rel.parent)
    return root / ".g023" / BACKUP_DIR / parent_rel / path.name


def newest_backup_for(path: Path, root: Path) -> Optional[Path]:
    backup_dir = backup_dir_for(path, root)
    if backup_dir is None or not backup_dir.is_dir():
        return None
    versions = _versions(backup_dir)
    return versions[0] if versions else None


def restore_newest_backup(path: str) -> dict:
    """Restore the newest ``.g023/backups/`` snapshot for ``path``.

    /undo is a stack pop: the restored snapshot is consumed, so a second
    /undo goes one version further back instead of re-applying the same
    one. The version being replaced is kept as ``*.undone`` (never lost), and
    the file is replaced atomically with its mode preserved.
    """
    root = get_project_root()
    resolved, err = confine_or_error(path, root=root)
    if err or resolved is None:
        return {"error": err or "Path is outside project root"}
    backup = newest_backup_for(resolved, root)
    if backup is None:
        return {"error": f"No backup found for {resolved}"}
    data = backup.read_bytes()
    previous_hash = None
    saved_current = None
    if resolved.exists() and resolved.is_file():
        current = resolved.read_bytes()
        previous_hash = hashlib.sha256(current).hexdigest()
        if current != data:
            kept = _snapshot(current, backup.parent, UNDONE_SUFFIX)
            _prune(backup.parent, UNDONE_SUFFIX, _keep_count())
            saved_current = str(kept.relative_to(root))
    edit_engine.save(resolved, data)
    restored_from = str(backup.relative_to(root))
    try:
        backup.unlink()
    except OSError:
        pass
    get_cache().invalidate_file(resolved)
    return {
        "ok": True,
        "path": str(resolved),
        "restored_from": restored_from,
        "bytes": len(data),
        "previous_hash": previous_hash,
        "saved_current": saved_current,
    }


# A complete listing above this many entries is replaced by its digest when that is 2x+ smaller.
DIGEST_MIN_ENTRIES = 40


class ToolRegistry:
    def __init__(self):
        self.policy = default_policy()
        self._executors: Dict[str, Callable] = {
            "Bash": self._exec_bash,
            "WriteFile": self._exec_write_file,
            "Edit": self._exec_edit,
            "ListDir": self._exec_list_dir,
            "Glob": self._exec_glob,
            "TodoWrite": self._exec_todo_write,
            "FetchUrl": self._exec_fetch_url,
            "WebSearch": self._exec_web_search,
            "LearnSearch": self._exec_learn_search,
            "LearnGet": self._exec_learn_get,
            "LearnPut": self._exec_learn_put,
            "SessionSearch": self._exec_session_search,
            "LocalAsk": self._exec_local_ask,
            "Rename": self._exec_rename,
            # Heavy tools are routed to subagents by the orchestrator
            "ReadFile": None,
            "SearchContent": None,
            "AnalyzeImage": None,
            "Agent": None,
        }
        # Test oracle (testoracle.py): the last pytest report a Bash call
        # produced, taken by the orchestrator right after the call.
        self.last_oracle: Optional[dict] = None
        self._oracle_off = False

    def get_schemas(self) -> list:
        """Tool schemas offered to the orchestrator.

        AnalyzeImage is hidden while vision is disabled so the model never
        proposes a call it cannot fulfil (and the static prefix stays stable
        for as long as the config does).
        """
        tools = [
            s
            for s in TOOL_SCHEMAS
            if (settings.vision_enabled or s.get("name") != "AnalyzeImage")
            # Single-agent by default: Agent is offered only after /subagents on.
            and (getattr(settings, "subagents_enabled", False) or s.get("name") != "Agent")
            # LocalAsk only while the local helper model is on (/local on).
            and (getattr(settings, "local_enabled", False) or s.get("name") != "LocalAsk")
        ]
        return tools

    def set_permission(self, tool_name: str, level: str):
        if level not in (ALLOW, ASK, BLOCK):
            raise ValueError(f"Invalid permission: {level}")
        self.policy[tool_name] = level

    async def check_permission(self, tool_name: str, args: dict) -> bool:
        level = self.policy.get(tool_name, ASK)
        if level == ALLOW:
            return True
        if level == BLOCK:
            console.print(f"[red]Blocked tool:[/red] {tool_name}")
            return False
        if tool_name == "FetchUrl":
            return self.confirm_fetch(args)

        # ASK — lead with the readable phrase, and keep the raw arguments
        # available underneath: a decision needs both the gist and the specifics.
        from .. import ui

        style = ui.tool_style(tool_name)
        console.print()
        console.print(
            f"[warn]Permission required[/warn] [{style.color}]{style.icon} {style.verb}[/{style.color}] "
            f"{ui.describe_call(tool_name, args, str(get_project_root()))}"
        )
        diff_text = ui.permission_diff(tool_name, args, str(get_project_root()))
        skip_keys: set[str] = set()
        if diff_text:
            console.print(diff_text)
            skip_keys = {"content", "old_string", "new_string", "more_edits"}
        for key, value in (args or {}).items():
            if key in skip_keys:
                continue
            console.print(f"  [key]{key}[/key] [muted]{ui.truncate(str(value), 100)}[/muted]")
        try:
            with blocking_prompt():
                answer = Prompt.ask(
                    "Allow? [dim](y = once, a = always for this tool, n = no)[/dim]",
                    choices=["y", "a", "n"],
                    default="y",
                    show_choices=False,
                )
        except (EOFError, KeyboardInterrupt):
            # Ctrl-D / Ctrl-C at a permission prompt means "not this one" — it
            # should decline the call, not blow up the turn that asked.
            # One-shot / piped stdin also lands here: there is no operator to
            # answer, so the call is declined rather than hanging forever.
            # Unattended project runs must pin permission_default to allow.
            hint = ""
            if not sys.stdin.isatty():
                hint = (
                    " Non-interactive stdin cannot answer ASK prompts — "
                    "set permission_default to allow (project .g023/config.json "
                    "or /tools <name> allow) for unattended runs."
                )
            console.print(f"\n[warn]Declined.{hint}[/warn]")
            return False
        # Piped/default answers can leave the cursor on the prompt line, so the
        # following tool trace would print on top of "Allow? (y):".
        console.print()
        if answer == "a":
            self.set_permission(tool_name, ALLOW)
            console.print(f"[ok]{tool_name} will run without asking for the rest of this session.[/ok]")
            return True
        return answer == "y"

    def confirm_fetch(self, args: dict) -> bool:
        """Approve a fetch, and let the user pick the cached copy or the network.

        The choice is written back into `args` — the orchestrator hands the same
        dict to execute(), so the answer reaches the executor as `cache_mode`.
        """
        from .. import web

        url = str(args.get("url", "")).strip()
        if not url:
            console.print("[red]FetchUrl called without a url.[/red]")
            return False

        requested = str(args.get("cache_mode", "auto")).lower()
        try:
            cached = web.get_store().get_page(web.canonical_url(url), touch=False)
        except Exception:
            cached = None
        if cached and cached["status"] != 200:
            cached = None

        console.print(f"\n[yellow]Permission required[/yellow] to fetch [bold]{url}[/bold]")
        console.print(f"[dim]DeepSeek Fetch API · requested mode: {requested}[/dim]")

        def _declined() -> bool:
            hint = ""
            if not sys.stdin.isatty():
                hint = (
                    " Non-interactive stdin cannot answer ASK prompts — "
                    "set permission_default to allow for unattended runs."
                )
            console.print(f"\n[warn]Declined.{hint}[/warn]")
            return False

        if cached is None:
            if requested == "cache":
                console.print("[dim]Nothing cached for this URL — this would be a network fetch.[/dim]")
            try:
                with blocking_prompt():
                    approved = Confirm.ask("Fetch it?", default=True)
            except (EOFError, KeyboardInterrupt):
                return _declined()
            if not approved:
                return False
            args["cache_mode"] = "fresh" if requested == "cache" else requested
            return True

        origin = "search prefetch" if cached["source"] == "search" else cached["source"]
        console.print(
            f"[green]Cached copy available[/green] — {_describe_age(cached['age'])}, "
            f"{cached['chars']:,} chars ({origin}, machine-wide cache)"
        )
        try:
            with blocking_prompt():
                choice = Prompt.ask(
                    # Square brackets are rich markup, so spell the options out instead.
                    "Use cached copy, fetch fresh, or deny?",
                    choices=["c", "f", "d"],
                    default="c" if requested != "fresh" else "f",
                )
        except (EOFError, KeyboardInterrupt):
            return _declined()
        if choice == "d":
            return False
        args["cache_mode"] = "cache" if choice == "c" else "fresh"
        return True

    async def execute(self, tool_name: str, arguments: dict, tool_call_id: str) -> str:
        """Execute a lightweight tool. Heavy tools must be routed to subagents first."""
        executor = self._executors.get(tool_name)
        if executor is None:
            return json.dumps(
                {
                    "error": f"Tool '{tool_name}' is a subagent-routed tool and should not be executed directly."
                }
            )
        try:
            # Models invent keys the schema does not list (a live /goal sent
            # TodoWrite `verify`). Splatting those is a TypeError and a wasted
            # model round. Drop them, run the call, and name what was ignored.
            extra: list[str] = []
            # Predicated finish is the orchestrator's business (finish.py); a
            # nested Agent loop shares the schemas, so drop it here silently.
            arguments = {k: v for k, v in arguments.items() if k not in (FINISH_ARG, CHECK_ARG)}
            params = inspect.signature(executor).parameters
            if not any(
                p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
            ):
                extra = sorted(k for k in arguments if k not in params)
                arguments = {k: v for k, v in arguments.items() if k in params}
            result = await executor(**arguments)
            if not isinstance(result, str):
                result = json.dumps(result, ensure_ascii=False)
            if extra:
                try:
                    data = json.loads(result)
                except json.JSONDecodeError:
                    data = None
                if isinstance(data, dict):
                    data["ignored_arguments"] = extra
                    result = json.dumps(data, ensure_ascii=False)
            return result
        except Exception as e:
            return json.dumps({"error": str(e), "tool": tool_name})

    # ------------------------------------------------------------------
    # Lightweight executors
    # ------------------------------------------------------------------

    async def _exec_bash(self, command: str, timeout: int = 60) -> str:
        root = get_project_root()
        # Safety: basic blocklist
        dangerous = ["rm -rf /", "mkfs", ":(){", "dd if=/dev/zero"]
        if any(d in command for d in dangerous):
            return json.dumps({"error": "Command blocked by safety filter"})
        try:
            # GPU work (CUDA, model training/serving): the local helper model
            # gives the card back for the rest of the turn (local_llm.py).
            from .. import local_llm

            local_llm.note_command(command)
        except Exception:
            pass

        try:
            # The tool is Bash. The default shell is /bin/sh, which rejects
            # bash syntax (PIPESTATUS) and reports a failure for a command
            # that actually succeeded. Prefer bash when it is installed.
            # pipefail: `pytest -q | tail` must not report tail's exit 0
            # after pytest failed. That false green used to clear the goal.
            bash = shutil.which("bash")
            argv = [bash, "-o", "pipefail", "-c", command] if bash else ["/bin/sh", "-c", command]
            env = bash_env(root)
            probe = None
            if not self._oracle_off and testoracle.enabled() and testoracle.wants(command):
                probe = testoracle.Probe()
                env = probe.env(env)
            # Own process group: a timeout must kill the whole pipeline
            # (pytest workers, servers started with &), not just the shell,
            # or the children outlive the call and keep the pipes open.
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(root),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                start_new_session=(os.name == "posix"),
            )
            out_buf: list = []
            err_buf: list = []

            async def pump(stream, buf):
                while True:
                    chunk = await stream.read(65536)
                    if not chunk:
                        return
                    buf.append(chunk)

            readers = asyncio.gather(pump(proc.stdout, out_buf), pump(proc.stderr, err_buf))
            timed_out = False
            try:
                await asyncio.wait_for(asyncio.shield(readers), timeout=timeout)
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.CancelledError:
                # Ctrl-C / turn cancel: the group was started in its own
                # session, so the terminal's SIGINT never reached it. Kill it
                # here or it outlives the turn (orphaned servers, test runs).
                kill_process_group(proc)
                readers.cancel()
                if probe is not None:
                    probe.close()
                raise
            except asyncio.TimeoutError:
                timed_out = True
                kill_process_group(proc)
                try:
                    await asyncio.wait_for(readers, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    readers.cancel()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass

            out = b"".join(out_buf).decode("utf-8", errors="replace")
            err = b"".join(err_buf).decode("utf-8", errors="replace")
            if probe is not None:
                report = probe.collect()
                probe.close()
                if report is None and not timed_out and testoracle.plugin_failed(out + err):
                    # The project's pytest could not load the probe: run the
                    # command as written and leave the oracle off for the session.
                    self._oracle_off = True
                    return await self._exec_bash(command, timeout)
                if report is not None:
                    self.last_oracle = {"command": command, "report": report}
            payload = prepare_bash_payload(out, err, proc.returncode if not timed_out else None)
            keep_full_output(payload, out, err)
            if timed_out:
                # Partial output is the observation the model needs (how far
                # it got); the error alone forces a blind re-run.
                payload = {"error": f"Command timed out after {timeout}s (process group killed)", **payload}
            else:
                hint = not_found_hint(proc.returncode, err)
                if hint:
                    payload["hint"] = hint
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e)})

    async def _exec_local_ask(self, question: str, paths: Optional[list] = None) -> str:
        from .. import local_ask

        return await local_ask.run(question, paths)

    async def _exec_write_file(self, path: str, content: str) -> str:
        root = get_project_root()
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        p = p.resolve()
        # Prevent writing outside project
        try:
            p.relative_to(root)
        except ValueError:
            return json.dumps({"error": "Refusing to write outside project root"})

        # A write with identical bytes changes nothing and would only burn a
        # pointless backup slot; report it as the no-op it is. An existing
        # file keeps its line-ending style (CRLF) and BOM when the model sends
        # plain LF text, and nothing is translated on Windows.
        existing = None
        previous_text: Optional[str] = None
        was_new = not p.exists()
        if p.exists() and p.is_file():
            try:
                existing = edit_engine.load(p)
                previous_text = existing.text
            except (UnicodeDecodeError, OSError):
                existing = None
        data = edit_engine.conform_new_content(content, existing)
        if p.exists() and p.is_file():
            try:
                same = p.read_bytes() == data
            except OSError:
                same = False
            if same:
                return json.dumps({
                    "ok": True,
                    "unchanged": True,
                    "path": str(p),
                    "bytes": len(data),
                })

        # WriteFile REPLACES THE WHOLE FILE, so before touching it snapshot the
        # previous version under .g023/backups/ — a write based on a ReadFile
        # *summary* (not raw content) is how files get silently truncated.
        backup = backup_before_write(p, root)

        from .. import memory_notes

        if memory_notes.is_memory_index_path(p, root):
            report = memory_notes.write_index(content)
            get_cache().invalidate_file(p)
            return json.dumps(report, ensure_ascii=False)

        edit_engine.save(p, data)
        if was_new:
            from .. import ownership

            ownership.record_created(p, root=root)
        # Drop the stale summaries for this path. Writing a placeholder row here
        # instead (as this used to) invalidated nothing — summaries are keyed by
        # content hash, so the junk row could never be hit or evicted.
        get_cache().invalidate_file(p)

        new_bytes = len(data)
        payload = {"ok": True, "path": str(p), "bytes": new_bytes}
        if existing is not None and existing.eol == "\r\n":
            payload["line_endings"] = "crlf (kept)"
        if backup:
            payload.update(backup)
        broken = edit_engine.introduced_syntax_error(p, previous_text, content)
        if broken:
            payload.update(broken)
        # A ReadFile summary omits bodies. Rewriting the file from that
        # summary is how a game loses its loop. Say so in the tool result
        # so the same turn can restore the backup instead of shipping it.
        prev_n = int(payload.get("previous_bytes") or 0)
        if prev_n >= 800 and new_bytes * 3 < prev_n:
            restore = str(payload.get("backup") or "")
            where = f"{restore} and " if restore else "the backup and "
            payload["warning"] = (
                f"file shrank from {prev_n} to {new_bytes} bytes. "
                "WriteFile replaces the entire file. If this used a ReadFile "
                f"summary, the body was truncated — restore {where}"
                "Edit, or ReadFile with raw=true before rewriting."
            )
        return json.dumps(payload, ensure_ascii=False)

    async def _exec_edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
        line: Optional[int] = None,
        more_edits: Optional[list] = None,
    ) -> str:
        root = get_project_root()
        p, err = confine_or_error(path, root=root)
        if err or p is None:
            return json.dumps({"error": err or "Path is outside project root"})
        if not p.exists() or not p.is_file():
            return json.dumps({"error": f"File not found: {p}"})
        try:
            ft = edit_engine.load(p)
        except UnicodeDecodeError:
            return json.dumps({"error": f"File is not UTF-8 text: {p}"})
        except OSError as e:
            return json.dumps({"error": f"Cannot read file: {e}"})
        try:
            line_no = int(line) if line not in (None, "") else None
        except (TypeError, ValueError):
            line_no = None

        def symbols() -> dict:
            from ..subagents.file_reader import _local_metadata

            return _local_metadata(p, ft.text).get("symbols") or {}

        spans: list[str] = []
        if more_edits:
            # Multi-hunk edit (multi_edit.py): every hunk in every file, or none.
            from .. import multi_edit

            first = {"old_string": old_string, "new_string": new_string, "replace_all": replace_all, "line": line_no}
            grouped = multi_edit.groups(path, first, more_edits)
            if len(grouped) > 1:
                return await self._exec_multi_file_edit(grouped, root)
            hunks = grouped[path]
            res, spans = edit_engine.apply_edits(ft.text, hunks, symbols_for=symbols)
            old_string = "\n".join(str(h.get("old_string") or "") for h in hunks if isinstance(h, dict))
            new_string = "\n".join(str(h.get("new_string") or "") for h in hunks if isinstance(h, dict))
        else:
            res = edit_engine.apply_edit(
                ft.text, old_string, new_string,
                replace_all=bool(replace_all), line=line_no, symbols_for=symbols,
            )
        if res.error is not None or res.text is None:
            payload = {"error": res.error, "path": str(p)}
            if res.hint:
                payload.update(res.hint)
            return json.dumps(payload, ensure_ascii=False)
        if res.text == ft.text:
            return json.dumps({
                "ok": True,
                "unchanged": True,
                "path": str(p),
                "replacements": 0,
            })
        backup = backup_before_write(p, root)
        data = ft.encode(res.text)
        edit_engine.save(p, data)
        get_cache().invalidate_file(p)
        payload = {
            "ok": True,
            "path": str(p),
            "replacements": res.replacements,
            "lines": f"{res.start_line}-{res.end_line}",
        }
        if len(spans) > 1:
            payload["lines"] = ", ".join(spans)
            payload["edits"] = len(spans)
        if res.matched != "exact":
            payload["matched"] = res.matched
        if ft.eol == "\r\n":
            payload["line_endings"] = "crlf (kept)"
        if backup:
            payload["backup"] = backup["backup"]
        broken = edit_engine.introduced_syntax_error(p, ft.text, res.text)
        if broken:
            payload.update(broken)
        if closure.enabled():
            try:
                extra = await asyncio.to_thread(closure.receipt, old_string, new_string, root)
            except Exception:  # a receipt must never fail a write that landed
                extra = None
            if extra:
                payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    async def _exec_multi_file_edit(self, grouped: dict, root: Path) -> str:
        """Edit hunks grouped per file (multi_edit.groups): all files written or none."""
        resolved: dict = {}  # one entry per real file: "a.py" and "./a.py" are the same file
        for arg, hunks in grouped.items():
            p, err = confine_or_error(arg, root=root)
            if err or p is None:
                return json.dumps({"error": f"{arg}: {err or 'Path is outside project root'}; nothing written"})
            resolved.setdefault(p, (arg, []))[1].extend(hunks)
        planned = []  # (path arg, resolved, FileText, EditResult, spans, hunks)
        for p, (arg, hunks) in resolved.items():
            if not p.is_file():
                return json.dumps({"error": f"{arg}: file not found; nothing written", "path": str(p)})
            try:
                ft = edit_engine.load(p)
            except (UnicodeDecodeError, OSError) as e:
                return json.dumps({"error": f"{arg}: cannot read as UTF-8 text ({e}); nothing written"})
            res, spans = edit_engine.apply_edits(ft.text, hunks)
            if res.error is not None or res.text is None:
                payload = {"error": f"{arg}: {res.error}; nothing written", "path": str(p)}
                if res.hint:
                    payload.update(res.hint)
                return json.dumps(payload, ensure_ascii=False)
            planned.append((arg, p, ft, res, spans, hunks))
        written: list = []
        backups: list = []
        try:
            for _arg, p, ft, res, _spans, _hunks in planned:
                if res.text == ft.text:
                    continue
                original = p.read_bytes()
                backup = backup_before_write(p, root)
                if backup:
                    backups.append(backup["backup"])
                edit_engine.save(p, ft.encode(res.text))
                written.append((p, original))
                get_cache().invalidate_file(p)
        except Exception as e:
            for p, original in reversed(written):
                try:
                    edit_engine.save(p, original)
                    get_cache().invalidate_file(p)
                except Exception:
                    pass
            return json.dumps({"error": f"write failed, no file changed: {e}"})
        first_arg, first_p, _ft, first_res, first_spans, _h = planned[0]
        payload: dict = {
            "ok": True,
            "path": str(first_p),
            "replacements": sum(r.replacements for _a, _p, _f, r, _s, _hh in planned),
            "lines": ", ".join(first_spans),
            "edits": sum(len(sp) for *_x, sp, _hh in planned),
            "files": {rel_to_root(p, root=root): ", ".join(sp) for _a, p, _f, _r, sp, _hh in planned},
        }
        if backups:
            payload["backups"] = len(backups)
        broken = {}
        for _a, p, ft, res, _s, _hh in planned:
            b = edit_engine.introduced_syntax_error(p, ft.text, res.text)
            if b:
                broken[rel_to_root(p, root=root)] = b.get("syntax_error")
        if broken:
            payload["syntax_error"] = broken
        if closure.enabled():
            olds = "\n".join(str(h.get("old_string") or "") for *_x, hh in planned for h in hh if isinstance(h, dict))
            news = "\n".join(str(h.get("new_string") or "") for *_x, hh in planned for h in hh if isinstance(h, dict))
            try:
                extra = await asyncio.to_thread(closure.receipt, olds, news, root)
            except Exception:
                extra = None
            if extra:
                payload.update(extra)
        return json.dumps(payload, ensure_ascii=False)

    async def _exec_rename(self, old: str, new: str, path: str = ".", include: str = "") -> str:
        from .. import rename

        data = await asyncio.to_thread(rename.run, old, new, path or ".", include or "")
        return json.dumps(data, ensure_ascii=False)

    async def _exec_glob(self, pattern: str, path: str = ".") -> str:
        from ..subagents.searcher import run_glob

        return await run_glob(pattern, path)

    async def _exec_todo_write(
        self,
        leaves: Optional[list] = None,
        goal: Optional[str] = None,
        constraints: Optional[list] = None,
    ) -> str:
        from ..working_set import (
            evidence_hint,
            evidence_satisfies_done,
            MAX_OPEN_LEAVES,
            load as load_ws,
            split_open_overflow,
            update_todos,
        )

        incoming = leaves or []
        ws = load_ws()
        was_open = any(str(leaf.get("status")) != "done" for leaf in ws.get("open") or [])
        accepted: list = []
        rejected: list = []
        for leaf in incoming:
            if not isinstance(leaf, dict):
                continue
            if str(leaf.get("status") or "") == "done" and not evidence_satisfies_done(
                ws, leaf
            ):
                rejected.append(leaf)
                continue
            accepted.append(leaf)
        # The open-leaf cap is enforced here, explicitly: normalize used to
        # truncate silently, so leaves 9+ vanished and the goal looked done.
        accepted, overflow = split_open_overflow(ws.get("open") or [], accepted)
        # A mixed batch used to reject every leaf, so the model re-sent the
        # ones that were already valid. Apply those. Only the rejects stay open.
        if accepted or goal or constraints is not None:
            ws = update_todos(accepted, goal=goal, constraints=constraints)
        if overflow:
            return json.dumps(
                {
                    "ok": False,
                    "error": "too_many_open_leaves",
                    "max_open": MAX_OPEN_LEAVES,
                    "rejected": [leaf.get("id") for leaf in overflow],
                    "applied": [leaf.get("id") for leaf in accepted],
                    "hint": (
                        f"At most {MAX_OPEN_LEAVES} leaves may be open. The listed ids "
                        "were NOT recorded. Finish or cancel open leaves, or merge "
                        "work into fewer leaves, then send them again."
                    ),
                    # Both rejections in one reply: evidence-less "done" leaves
                    # were not applied either.
                    **(
                        {
                            "done_requires_evidence": [leaf.get("id") for leaf in rejected],
                            "evidence_hint": evidence_hint(ws, rejected[0]),
                        }
                        if rejected
                        else {}
                    ),
                    "working_set": ws,
                },
                ensure_ascii=False,
            )
        if rejected:
            return json.dumps(
                {
                    "ok": False,
                    "error": "done_requires_evidence",
                    "id": rejected[0].get("id"),
                    "rejected": [leaf.get("id") for leaf in rejected],
                    "applied": [leaf.get("id") for leaf in accepted],
                    "hint": evidence_hint(ws, rejected[0]),
                    "working_set": ws,
                },
                ensure_ascii=False,
            )
        out: dict = {"ok": True, "working_set": ws}
        leaves_now = ws.get("open") or []
        if was_open and leaves_now and all(str(leaf.get("status")) == "done" for leaf in leaves_now):
            # The harness, not the model, decides that the last todo just closed
            # (a rule the model had to evaluate fired on turns with no todos).
            out["note"] = "[handoff due]"
        return json.dumps(out, ensure_ascii=False)

    async def _exec_learn_search(self, query: str, scope: str = "all") -> str:
        from .. import learn

        hits = learn.search(query, scope=scope)
        return json.dumps({"ok": True, "hits": hits, "count": len(hits)}, ensure_ascii=False)

    async def _exec_learn_get(self, id: str) -> str:
        from .. import learn

        return json.dumps(learn.get(id), ensure_ascii=False)

    async def _exec_learn_put(
        self,
        id: str,
        kind: str,
        scope: str,
        title: str,
        when: str,
        body: str,
        evidence: str,
        not_when: str = "",
        requires: Optional[list] = None,
    ) -> str:
        from .. import learn

        meta = {
            "id": id,
            "kind": kind,
            "scope": scope,
            "title": title,
            "when": when,
            "not_when": not_when,
            "requires": requires or [],
            "evidence": evidence,
        }
        result = learn.put(meta, body, evidence=evidence, source="orchestrator")
        return json.dumps(result, ensure_ascii=False)

    async def _exec_session_search(self, query: str, max_hits: int = 8) -> str:
        from ..session_log import search_records

        hits = search_records(query, max_hits=int(max_hits or 8))
        return json.dumps({"ok": True, "hits": hits, "count": len(hits)}, ensure_ascii=False)

    async def _exec_list_dir(self, path: str = ".", recursive: bool = False) -> str:  # noqa: C901
        root = get_project_root()
        p, err = confine_or_error(path, root=root)
        if err or p is None:
            return json.dumps({"error": err or "Path is outside project root"})
        if not p.exists():
            return json.dumps({"error": f"Path does not exist: {p}"})
        if not p.is_dir():
            return json.dumps({"error": f"Not a directory: {p}"})

        limit = 80 if recursive else 200
        max_depth = 2 if recursive else 1  # the schema promises depth 2; honour it

        def describe(item: Path, name: str) -> dict:
            is_dir = item.is_dir()
            try:
                size = item.stat().st_size if not is_dir else None
            except OSError:  # broken symlink, or a race with something deleting it
                size = None
            return {"name": name + ("/" if is_dir else ""), "type": "dir" if is_dir else "file", "size": size}

        entries: list[dict] = []
        skipped_dirs: list[str] = []
        truncated = False

        def walk(directory: Path, depth: int, prefix: str) -> None:
            nonlocal truncated
            try:
                children = sorted(directory.iterdir(), key=lambda c: (c.is_file(), c.name.lower()))
            except OSError as e:
                entries.append({"name": prefix or ".", "type": "dir", "error": str(e)})
                return
            for item in children:
                if len(entries) >= limit:
                    truncated = True
                    return
                # Noise directories would eat the entry budget before anything useful.
                if item.is_dir() and item.name in IGNORE_DIRS:
                    skipped_dirs.append(prefix + item.name)
                    continue
                entries.append(describe(item, prefix + item.name))
                if item.is_dir() and depth < max_depth and not item.is_symlink():
                    walk(item, depth + 1, f"{prefix}{item.name}/")

        walk(p, 1, "")

        dg = None
        if truncated or len(entries) > DIGEST_MIN_ENTRIES:
            # Too many entries for an exact list: the first N names in
            # alphabetical order would hide everything after them. A templated
            # digest accounts for every child; below the limit it is used only
            # when it is at least twice as compact (numbered/sibling names).
            from ..dirmap import digest

            dg = await asyncio.to_thread(digest, p, recursive=recursive, ignore=frozenset(IGNORE_DIRS))
            if not truncated and len(dg["lines"]) * 2 > len(entries):
                dg = None
        if dg is not None:
            return json.dumps({
                "path": str(p),
                "recursive": recursive,
                "max_depth": max_depth,
                **dg,
                "note": (
                    (f"More than {limit} entries: templated digest instead of a truncated list. "
                     if truncated else "Repetitive names: templated digest of the listing. ")
                    + "ListDir a subdirectory, or Glob a template (e.g. 'frames/frame_0001*.png'), for exact names."
                ),
            }, ensure_ascii=False)

        payload = {
            "path": str(p),
            "recursive": recursive,
            "max_depth": max_depth,
            "entry_count": len(entries),
            "entries": entries,
        }
        if truncated:
            payload["truncated"] = f"Stopped at {limit} entries — list a subdirectory for the rest."
        if skipped_dirs:
            payload["skipped"] = skipped_dirs[:20]
        return json.dumps(payload, ensure_ascii=False)

    async def _exec_fetch_url(
        self,
        url: str,
        focus: str = "",
        offset: int = 0,
        max_chars: int = 8000,
        cache_mode: str = "auto",
        max_age: Optional[int] = None,
    ) -> str:
        from .. import web

        try:
            page = await asyncio.to_thread(web.fetch, url, mode=cache_mode, max_age=max_age)
        except web.WebError as e:
            return json.dumps({"url": url, "error": str(e)}, ensure_ascii=False)
        return web.render_page(page, focus=focus or "", offset=int(offset or 0), max_chars=int(max_chars or 8000))

    async def _exec_web_search(self, query: str, count: int = 5, fresh: bool = False) -> str:
        from .. import web

        try:
            out = await asyncio.to_thread(web.search, query, count, fresh=bool(fresh))
        except web.WebError as e:
            return json.dumps({"query": query, "error": str(e)}, ensure_ascii=False)
        return web.render_search(out, count)


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry

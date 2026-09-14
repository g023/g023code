"""
Tool registry: permission checks + dispatch to local executors or subagents.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from rich.prompt import Confirm, Prompt

from ..config import get_project_root, settings
from ..cache import get_cache
from ..paths import confine_or_error
from ..subagents.searcher import IGNORE_DIRS
from ..ui import console
from .schemas import TOOL_SCHEMAS, WEB_SEARCH_TOOL


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

# Read-only, local, and cheap: these stay allowed whatever the default is.
# Web search is absent because it is no longer a tool we execute: DeepSeek
# runs it server-side inside the model call, so there is no point at which we
# could interpose a prompt.
SAFE_TOOLS = (
    "ReadFile",
    "SearchContent",
    "ListDir",
    "Glob",
    "TodoWrite",
    "LearnSearch",
    "LearnGet",
    "LearnPut",
    "SessionSearch",
)

# Everything else follows settings.permission_default, except FetchUrl: network
# fetches leave the machine and touch a third party, so they always surface to
# the user unless they explicitly relax it with /tools.
DEFAULT_ASK_TOOLS = ("AnalyzeImage", "Bash", "WriteFile", "Edit", "Agent")


def default_policy() -> Dict[str, str]:
    """The starting permission table, honouring settings.permission_default."""
    fallback = settings.permission_default
    if fallback not in (ALLOW, ASK, BLOCK):
        fallback = ASK
    policy: Dict[str, str] = {name: ALLOW for name in SAFE_TOOLS}
    policy.update({name: fallback for name in DEFAULT_ASK_TOOLS})
    policy["FetchUrl"] = BLOCK if fallback == BLOCK else ASK
    return policy


# Kept for callers that want the shape of the table without the live setting.
DEFAULT_POLICY: Dict[str, str] = default_policy()


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


# ---------------------------------------------------------------------------
# WriteFile backups
# ---------------------------------------------------------------------------
# Before overwriting a file, keep the previous version under .g023/backups/
# so a bad write is one copy away. The layout mirrors the file tree (one
# folder per file, timestamped copies inside), and only the newest few copies
# per file are kept, so the folder stays browsable instead of accumulating
# junk. Backups are never taken for .g023/ itself.
BACKUP_DIR = "backups"
BACKUP_SUFFIX = ".bak"
BACKUP_KEEP_DEFAULT = 5


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
        path.relative_to(root / ".g023")
        return None
    except ValueError:
        pass
    try:
        rel = path.relative_to(root)
    except ValueError:
        return None

    parent_rel = "" if rel.parent == Path(".") else str(rel.parent)
    backup_dir = root / ".g023" / BACKUP_DIR / parent_rel / path.name
    backup_dir.mkdir(parents=True, exist_ok=True)

    old = path.read_bytes()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    digest = hashlib.sha256(old).hexdigest()[:8]
    backup_path = backup_dir / f"{stamp}-{digest}{BACKUP_SUFFIX}"
    backup_path.write_bytes(old)

    keep = max(1, getattr(settings, "write_backup_keep", BACKUP_KEEP_DEFAULT))
    versions = sorted(backup_dir.glob(f"*{BACKUP_SUFFIX}"), key=lambda v: v.name, reverse=True)
    for stale in versions[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass

    return {
        "backup": str(backup_path.relative_to(root)),
        "backup_count": min(len(versions), keep),
        "previous_bytes": len(old),
    }


def apply_unique_edit(
    text: str,
    old_string: str,
    new_string: str,
    replace_all: bool = False,
) -> tuple[Optional[str], Optional[str], int]:
    """Return ``(new_text, error, n_replacements)``. Fail closed on 0 or >1 unless replace_all."""
    if old_string == "":
        return None, "old_string is empty", 0
    n = text.count(old_string)
    if n == 0:
        return None, "old_string not found in file", 0
    if n > 1 and not replace_all:
        return None, f"old_string is not unique ({n} occurrences); refuse to edit", 0
    if replace_all:
        return text.replace(old_string, new_string), None, n
    return text.replace(old_string, new_string, 1), None, 1


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
    versions = sorted(
        backup_dir.glob(f"*{BACKUP_SUFFIX}"),
        key=lambda v: v.name,
        reverse=True,
    )
    return versions[0] if versions else None


def restore_newest_backup(path: str) -> dict:
    """Copy the newest ``.g023/backups/`` snapshot for ``path`` back onto the file."""
    root = get_project_root()
    resolved, err = confine_or_error(path, root=root)
    if err or resolved is None:
        return {"error": err or "Path is outside project root"}
    backup = newest_backup_for(resolved, root)
    if backup is None:
        return {"error": f"No backup found for {resolved}"}
    previous_hash = None
    if resolved.exists() and resolved.is_file():
        previous_hash = hashlib.sha256(resolved.read_bytes()).hexdigest()
    data = backup.read_bytes()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_bytes(data)
    get_cache().invalidate_file(resolved)
    return {
        "ok": True,
        "path": str(resolved),
        "restored_from": str(backup.relative_to(root)),
        "bytes": len(data),
        "previous_hash": previous_hash,
    }


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
            "LearnSearch": self._exec_learn_search,
            "LearnGet": self._exec_learn_get,
            "LearnPut": self._exec_learn_put,
            "SessionSearch": self._exec_session_search,
            # Heavy tools are routed to subagents by the orchestrator
            "ReadFile": None,
            "SearchContent": None,
            "AnalyzeImage": None,
            "Agent": None,
        }

    def get_schemas(self) -> list:
        """Tool schemas offered to the orchestrator.

        AnalyzeImage is hidden while vision is disabled so the model never
        proposes a call it cannot fulfil (and the static prefix stays stable
        for as long as the config does).
        """
        tools = [
            s for s in TOOL_SCHEMAS
            if settings.vision_enabled or s.get("name") != "AnalyzeImage"
        ]
        # DeepSeek's own search tool rides along with ours. It has no schema and
        # no executor: the model calls it, the server runs the search loop, and
        # the results come back inside the same response.
        return tools + [WEB_SEARCH_TOOL]

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
            skip_keys = {"content", "old_string", "new_string"}
        for key, value in (args or {}).items():
            if key in skip_keys:
                continue
            console.print(f"  [key]{key}[/key] [muted]{ui.truncate(str(value), 100)}[/muted]")
        try:
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
        """Approve a fetch, and let the user pick cache or network.

        The choice is written back into `args` — the orchestrator hands the same
        dict to execute(), so the answer reaches the executor as `cache_mode`.
        """
        from ..web_fetch import engine_name

        url = str(args.get("url", "")).strip()
        if not url:
            console.print("[red]FetchUrl called without a url.[/red]")
            return False

        requested = str(args.get("cache_mode", "auto")).lower()
        cached = get_cache().get_web(url, touch=False)

        console.print(f"\n[yellow]Permission required[/yellow] to fetch [bold]{url}[/bold]")
        console.print(f"[dim]engine: {engine_name()} · requested mode: {requested}[/dim]")

        if cached is None:
            if requested == "cache":
                console.print("[dim]Nothing cached for this URL — this would be a network fetch.[/dim]")
            try:
                approved = Confirm.ask("Fetch it?", default=True)
            except (EOFError, KeyboardInterrupt):
                hint = ""
                if not sys.stdin.isatty():
                    hint = (
                        " Non-interactive stdin cannot answer ASK prompts — "
                        "set permission_default to allow for unattended runs."
                    )
                console.print(f"\n[warn]Declined.{hint}[/warn]")
                return False
            if not approved:
                return False
            args["cache_mode"] = "fresh"
            return True

        age = _describe_age(cached["age_seconds"])
        size = len(cached["body"])
        console.print(
            f"[green]Cached copy available[/green] — fetched {age}, "
            f"HTTP {cached['status']}, {size:,} chars"
        )
        try:
            choice = Prompt.ask(
                # Square brackets are rich markup, so spell the options out instead.
                "Use cached copy, fetch fresh, or deny?",
                choices=["c", "f", "d"],
                default="c" if requested != "fresh" else "f",
            )
        except (EOFError, KeyboardInterrupt):
            hint = ""
            if not sys.stdin.isatty():
                hint = (
                    " Non-interactive stdin cannot answer ASK prompts — "
                    "set permission_default to allow for unattended runs."
                )
            console.print(f"\n[warn]Declined.{hint}[/warn]")
            return False
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
            result = await executor(**arguments)
            return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
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
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "PWD": str(root)},
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return json.dumps({"error": f"Command timed out after {timeout}s"})

            out = stdout.decode("utf-8", errors="replace")
            err = stderr.decode("utf-8", errors="replace")
            return json.dumps(
                prepare_bash_payload(out, err, proc.returncode),
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e)})

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
        # pointless backup slot; report it as the no-op it is.
        if p.exists() and p.is_file():
            previous = p.read_text(encoding="utf-8", errors="replace")
            if previous == content:
                return json.dumps({
                    "ok": True,
                    "unchanged": True,
                    "path": str(p),
                    "bytes": len(content.encode("utf-8")),
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

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # Drop the stale summaries for this path. Writing a placeholder row here
        # instead (as this used to) invalidated nothing — summaries are keyed by
        # content hash, so the junk row could never be hit or evicted.
        get_cache().invalidate_file(p)

        payload = {"ok": True, "path": str(p), "bytes": len(content.encode("utf-8"))}
        if backup:
            payload.update(backup)
        return json.dumps(payload, ensure_ascii=False)

    async def _exec_edit(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        root = get_project_root()
        p, err = confine_or_error(path, root=root)
        if err or p is None:
            return json.dumps({"error": err or "Path is outside project root"})
        if not p.exists() or not p.is_file():
            return json.dumps({"error": f"File not found: {p}"})
        try:
            previous = p.read_text(encoding="utf-8")
        except OSError as e:
            return json.dumps({"error": f"Cannot read file: {e}"})
        updated, problem, n = apply_unique_edit(
            previous, old_string, new_string, replace_all=bool(replace_all)
        )
        if problem is not None or updated is None:
            return json.dumps({"error": problem, "path": str(p)})
        if updated == previous:
            return json.dumps({
                "ok": True,
                "unchanged": True,
                "path": str(p),
                "bytes": len(previous.encode("utf-8")),
                "replacements": 0,
            })
        backup = backup_before_write(p, root)
        p.write_text(updated, encoding="utf-8")
        get_cache().invalidate_file(p)
        payload = {
            "ok": True,
            "path": str(p),
            "bytes": len(updated.encode("utf-8")),
            "replacements": n,
        }
        if backup:
            payload.update(backup)
        return json.dumps(payload, ensure_ascii=False)

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
            load as load_ws,
            update_todos,
        )

        incoming = leaves or []
        ws = load_ws()
        for leaf in incoming:
            if not isinstance(leaf, dict):
                continue
            if str(leaf.get("status") or "") != "done":
                continue
            if not evidence_satisfies_done(ws, leaf):
                return json.dumps(
                    {
                        "ok": False,
                        "error": "done_requires_evidence",
                        "id": leaf.get("id"),
                        "hint": evidence_hint(ws, leaf),
                        "working_set": ws,
                    },
                    ensure_ascii=False,
                )
        ws = update_todos(incoming, goal=goal, constraints=constraints)
        return json.dumps({"ok": True, "working_set": ws}, ensure_ascii=False)

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

    async def _exec_list_dir(self, path: str = ".", recursive: bool = False) -> str:
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
        cache_mode: str = "auto",
        max_age: int = 3600,
        extract: str = "text",
        max_chars: int = 20000,
    ) -> str:
        from .. import web_fetch

        cache = get_cache()
        cache_mode = (cache_mode or "auto").lower()
        cached = cache.get_web(url, touch=False)

        def _render(result: web_fetch.FetchResult) -> str:
            data = web_fetch.extract(result, extract)
            content = data.get("content")
            truncated = False
            if isinstance(content, str) and len(content) > max_chars:
                data["content"] = content[:max_chars]
                truncated = True
            payload = {
                "url": result.url,
                "final_url": result.final_url,
                "status": result.status,
                "source": "cache" if result.from_cache else "network",
                "fetched_at": result.fetched_at,
                "age_seconds": int(time.time() - result.fetched_at),
                "engine": result.engine,
                "truncated": truncated or result.truncated,
                **data,
            }
            if truncated:
                payload["truncation_note"] = (
                    f"Content cut to {max_chars} chars. Re-call with a larger max_chars "
                    "and cache_mode='cache' to read more without another network request."
                )
            return json.dumps(payload, ensure_ascii=False)

        # Serve from cache when the user (or the policy) asked for it.
        if cached and (
            cache_mode == "cache"
            or (cache_mode == "auto" and cached["age_seconds"] <= max_age)
        ):
            result = web_fetch.FetchResult(
                url=cached["url"],
                final_url=cached["final_url"] or cached["url"],
                status=cached["status"] or 0,
                headers=cached["headers"],
                body=cached["body"],
                engine=cached["engine"] or "cache",
                profile=cached["profile"] or "",
                elapsed_ms=0,
                fetched_at=cached["fetched_at"],
                from_cache=True,
            )
            cache.touch_web(url)
            return _render(result)

        if cache_mode == "cache":
            return json.dumps(
                {
                    "url": url,
                    "error": "cache_miss",
                    "message": "Nothing cached for this URL. Re-call with cache_mode='fresh' to fetch it.",
                }
            )

        try:
            result = await asyncio.to_thread(web_fetch.fetch, url)
        except Exception as e:
            payload = {"url": url, "error": str(e)}
            if cached:
                payload["stale_cache_available"] = True
                payload["stale_age_seconds"] = int(cached["age_seconds"])
                payload["message"] = "Network fetch failed; a stale cached copy exists (cache_mode='cache')."
            return json.dumps(payload, ensure_ascii=False)

        cache.put_web(
            url,
            body=result.body,
            status=result.status,
            headers=result.headers,
            final_url=result.final_url,
            engine=result.engine,
            profile=result.profile,
        )
        return _render(result)


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry

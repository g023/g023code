"""
Tool registry: permission checks + dispatch to local executors or subagents.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from html import unescape
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import parse_qs, urlparse

from rich.prompt import Confirm, Prompt

from ..config import get_project_root, settings
from ..cache import get_cache
from ..subagents.searcher import IGNORE_DIRS
from ..ui import console
from .schemas import TOOL_SCHEMAS



# DuckDuckGo's HTML endpoint wraps every hit in a /l/?uddg=<escaped-url>
# redirector, so the raw href never starts with "http" and has to be unwrapped.
_DDG_RESULT = re.compile(
    r'<a[^>]+class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL
)
_DDG_SNIPPET = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def _strip_tags(html_fragment: str) -> str:
    return unescape(_TAG.sub("", html_fragment)).strip()


def _ddg_target(href: str) -> str:
    """Unwrap a DuckDuckGo redirect into the destination URL."""
    href = unescape(href)
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            return target
    return href if href.startswith("http") else ""


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
SAFE_TOOLS = ("ReadFile", "SearchContent", "ListDir", "WebSearch")

# Everything else follows settings.permission_default, except FetchUrl: network
# fetches leave the machine and touch a third party, so they always surface to
# the user unless they explicitly relax it with /tools.
DEFAULT_ASK_TOOLS = ("AnalyzeImage", "Bash", "WriteFile", "Agent")


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


class ToolRegistry:
    def __init__(self):
        self.policy = default_policy()
        self._executors: Dict[str, Callable] = {
            "Bash": self._exec_bash,
            "WriteFile": self._exec_write_file,
            "ListDir": self._exec_list_dir,
            "WebSearch": self._exec_web_search,
            "FetchUrl": self._exec_fetch_url,
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
        if settings.vision_enabled:
            return TOOL_SCHEMAS
        return [
            s for s in TOOL_SCHEMAS
            if s.get("function", {}).get("name") != "AnalyzeImage"
        ]

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
        for key, value in (args or {}).items():
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
            console.print("\n[warn]Declined.[/warn]")
            return False
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
                console.print("\n[warn]Declined.[/warn]")
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
            console.print("\n[warn]Declined.[/warn]")
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
                {
                    "exit_code": proc.returncode,
                    "stdout": out[-8000:] if len(out) > 8000 else out,
                    "stderr": err[-2000:] if len(err) > 2000 else err,
                },
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

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # Drop the stale summaries for this path. Writing a placeholder row here
        # instead (as this used to) invalidated nothing — summaries are keyed by
        # content hash, so the junk row could never be hit or evicted.
        get_cache().invalidate_file(p)
        return json.dumps({"ok": True, "path": str(p), "bytes": len(content.encode("utf-8"))})

    async def _exec_list_dir(self, path: str = ".", recursive: bool = False) -> str:
        root = get_project_root()
        p = Path(path)
        if not p.is_absolute():
            p = root / p
        p = p.resolve()
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

    async def _exec_web_search(self, query: str, num_results: int = 5) -> str:
        # Lightweight free search via DuckDuckGo HTML (no key required)
        # For production one could swap to Serper / Brave / Tavily
        try:
            import httpx
            from urllib.parse import quote_plus

            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
                        ),
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                text = r.text

            results = []
            for m in _DDG_RESULT.finditer(text):
                href = _ddg_target(m.group(1))
                title = _strip_tags(m.group(2))
                if not href or not title:
                    continue
                results.append({"title": title, "url": href})
                if len(results) >= num_results:
                    break

            # Snippets are rendered in document order, so they line up with the
            # titles above; a result without one is still worth returning.
            snippets = [_strip_tags(s) for s in _DDG_SNIPPET.findall(text)]
            for i, item in enumerate(results):
                if i < len(snippets) and snippets[i]:
                    item["snippet"] = snippets[i][:300]

            payload = {"query": query, "results": results}
            if not results:
                payload["note"] = (
                    f"No results parsed from DuckDuckGo (HTTP {r.status_code}). "
                    "The endpoint may be rate-limiting; try FetchUrl on a known URL."
                )
            return json.dumps(payload, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"Web search failed: {e}"})


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry

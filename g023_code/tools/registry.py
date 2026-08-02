"""
Tool registry: permission checks + dispatch to local executors or subagents.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from rich.console import Console
from rich.prompt import Confirm

from ..config import get_project_root, settings
from ..cache import get_cache
from .schemas import TOOL_SCHEMAS

console = Console()


# Permission levels
ALLOW = "allow"
ASK = "ask"
BLOCK = "block"

# Default policy
DEFAULT_POLICY: Dict[str, str] = {
    "ReadFile": ALLOW,
    "SearchContent": ALLOW,
    "ListDir": ALLOW,
    "WebSearch": ALLOW,
    "AnalyzeImage": ASK,
    "Bash": ASK,
    "WriteFile": ASK,
    "Agent": ASK,
}


class ToolRegistry:
    def __init__(self):
        self.policy = dict(DEFAULT_POLICY)
        self._executors: Dict[str, Callable] = {
            "Bash": self._exec_bash,
            "WriteFile": self._exec_write_file,
            "ListDir": self._exec_list_dir,
            "WebSearch": self._exec_web_search,
            # Heavy tools are routed to subagents by the orchestrator
            "ReadFile": None,
            "SearchContent": None,
            "AnalyzeImage": None,
            "Agent": None,
        }

    def get_schemas(self) -> list:
        return TOOL_SCHEMAS

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
        # ASK
        preview = json.dumps(args, indent=2, ensure_ascii=False)[:400]
        console.print(f"\n[yellow]Permission required[/yellow] for [bold]{tool_name}[/bold]")
        console.print(f"[dim]{preview}[/dim]")
        return Confirm.ask("Allow this tool call?", default=True)

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
        # Invalidate cache
        get_cache().put_file_summary(str(p), summary="[file rewritten]", metadata={"rewritten": True})
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

        entries = []
        if recursive:
            for item in sorted(p.rglob("*"))[:80]:
                rel = item.relative_to(p)
                entries.append(
                    {
                        "name": str(rel),
                        "type": "dir" if item.is_dir() else "file",
                        "size": item.stat().st_size if item.is_file() else None,
                    }
                )
        else:
            for item in sorted(p.iterdir())[:100]:
                entries.append(
                    {
                        "name": item.name,
                        "type": "dir" if item.is_dir() else "file",
                        "size": item.stat().st_size if item.is_file() else None,
                    }
                )
        return json.dumps({"path": str(p), "entries": entries}, ensure_ascii=False)

    async def _exec_web_search(self, query: str, num_results: int = 5) -> str:
        # Lightweight free search via DuckDuckGo HTML (no key required)
        # For production one could swap to Serper / Brave / Tavily
        try:
            import httpx
            from urllib.parse import quote_plus

            url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "g023-code/1.0"})
                text = r.text
            # Very crude extraction
            import re
            results = []
            for m in re.finditer(r'<a rel="nofollow" class="result__a" href="(.*?)">(.*?)</a>', text):
                href, title = m.group(1), re.sub(r"<.*?>", "", m.group(2))
                if href.startswith("http"):
                    results.append({"title": title.strip(), "url": href})
                if len(results) >= num_results:
                    break
            return json.dumps({"query": query, "results": results}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"Web search failed: {e}"})


_registry: Optional[ToolRegistry] = None


def get_registry() -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry

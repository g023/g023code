"""
Interactive CLI for g023 Code.
Supports slash commands and multiline input.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from . import __version__
from .config import get_home, get_project_root, settings, load_api_key
from .orchestrator import Orchestrator
from .cache import get_cache

console = Console()


BANNER = f"""
[bold cyan]g023 Code[/bold cyan]  v{__version__}
[dim]DeepSeek V4 · Subagent-First · Context is Currency[/dim]
Project: [green]{get_project_root()}[/green]
Home:    [dim]{get_home()}[/dim]
Type /help for commands.  Ctrl+C or /exit to quit.
"""


HELP_TEXT = """
[bold]Slash Commands[/bold]

  /help, /?              Show this help
  /model flash|pro       Switch orchestrator model
  /thinking low|high|max Set reasoning effort
  /compact               Force conversation compaction (placeholder)
  /clear                 Reset conversation state
  /cache clear           Purge SQLite caches
  /cost                  Show token usage & rough cost
  /goal <text>           Set a high-level goal (uses max effort)
  /vision backend <name> Set vision backend (none|glm|openai|local)
  /verbose               Toggle verbose (show reasoning excerpts)
  /exit, /quit           Exit

[bold]Philosophy[/bold]
  Heavy work (file reads, searches, vision) is always delegated to
  isolated subagents that return only compact summaries. The
  orchestrator stays clean for high-level reasoning.
"""


class CLI:
    def __init__(self):
        self.orch = Orchestrator()
        self.running = True
        self._pending_goal: str | None = None

    def print_banner(self):
        console.print(Panel(BANNER.strip(), border_style="cyan"))

    def handle_slash(self, line: str) -> bool:
        """Return True if the command was handled (no further processing)."""
        parts = line.strip().split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd in ("/help", "/?"):
            console.print(HELP_TEXT)
            return True

        if cmd == "/model":
            m = arg.strip().lower()
            if m in ("flash", "deepseek-v4-flash"):
                settings.orchestrator_model = "deepseek-v4-flash"
                console.print("[green]Orchestrator → deepseek-v4-flash[/green]")
            elif m in ("pro", "deepseek-v4-pro"):
                settings.orchestrator_model = "deepseek-v4-pro"
                console.print("[green]Orchestrator → deepseek-v4-pro[/green]")
            else:
                console.print("Usage: /model flash|pro")
            return True

        if cmd == "/thinking":
            e = arg.strip().lower()
            if e in ("low", "high", "max"):
                settings.reasoning_effort = e  # type: ignore
                settings.thinking_enabled = True
                console.print(f"[green]Reasoning effort → {e}[/green]")
            elif e in ("off", "none", "disabled"):
                settings.thinking_enabled = False
                console.print("[yellow]Thinking mode disabled[/yellow]")
            else:
                console.print("Usage: /thinking low|high|max|off")
            return True

        if cmd == "/clear":
            self.orch.reset()
            console.print("[green]Conversation cleared.[/green]")
            return True

        if cmd == "/cache":
            if arg.strip() == "clear":
                get_cache().clear_all()
                console.print("[green]Cache purged.[/green]")
            else:
                console.print("Usage: /cache clear")
            return True

        if cmd == "/cost":
            console.print(self.orch.cost_summary())
            return True

        if cmd == "/verbose":
            settings.verbose = not settings.verbose
            console.print(f"Verbose = {settings.verbose}")
            return True

        if cmd == "/vision":
            if arg.startswith("backend"):
                b = arg.split(maxsplit=1)[-1].strip() if " " in arg else "none"
                settings.vision_backend = b
                console.print(f"[green]Vision backend → {b}[/green]")
            else:
                console.print(f"Current vision backend: {settings.vision_backend}")
            return True

        if cmd == "/goal":
            if not arg:
                console.print("Usage: /goal <objective>")
                return True
            # Force max effort for goals — handled specially in the main loop
            self._pending_goal = arg
            return True

        if cmd in ("/exit", "/quit"):
            self.running = False
            return True

        console.print(f"[yellow]Unknown command:[/yellow] {cmd}  (try /help)")
        return True

    async def loop(self):
        self.print_banner()
        # Quick key check
        try:
            load_api_key()
        except Exception as e:
            console.print(f"[red]{e}[/red]")
            return

        while self.running:
            try:
                console.print()
                user = Prompt.ask("[bold green]you[/bold green]")
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Bye.[/dim]")
                break

            line = user.strip()
            if not line:
                continue

            if line.startswith("/"):
                self.handle_slash(line)
                if self._pending_goal:
                    goal = self._pending_goal
                    self._pending_goal = None
                    old_effort = settings.reasoning_effort
                    settings.reasoning_effort = "max"
                    console.print(f"[cyan]Goal (max reasoning):[/cyan] {goal}")
                    try:
                        result = await self.orch.run_turn(
                            f"High-level goal (use tools freely, break into steps):\n\n{goal}"
                        )
                        console.print()
                        console.print(Panel(Markdown(result), title="[bold cyan]g023 · goal[/bold cyan]", border_style="cyan"))
                    except Exception as e:
                        console.print(f"[red]Error:[/red] {e}")
                    finally:
                        settings.reasoning_effort = old_effort
                continue

            # Normal turn
            try:
                result = await self.orch.run_turn(line)
                console.print()
                console.print(Panel(Markdown(result), title="[bold cyan]g023[/bold cyan]", border_style="cyan"))
            except KeyboardInterrupt:
                console.print("\n[yellow]Interrupted.[/yellow]")
            except Exception as e:
                console.print(f"[red]Error:[/red] {e}")
                if settings.verbose:
                    import traceback
                    traceback.print_exc()


def main():
    cli = CLI()
    try:
        asyncio.run(cli.loop())
    except KeyboardInterrupt:
        console.print("\n[dim]Bye.[/dim]")

"""
Orchestrator — the high-level reasoning loop.
Keeps context clean by delegating all heavy data work to subagents.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.live import Live
from rich.spinner import Spinner

from .config import load_api_key, settings
from .tools.registry import get_registry
from .subagents.router import get_router

console = Console()


ORCHESTRATOR_SYSTEM = """You are g023 Code — an elite terminal-native AI programming assistant powered by DeepSeek V4.

Core philosophy: Context is Currency. Subagents are the Treasury.

You NEVER dump raw file contents or large search results into your own context.
Instead you always use the provided tools:
- ReadFile → returns a compact structural summary + metadata (use this instead of asking for full files)
- SearchContent → returns metadata-first match list
- AnalyzeImage → external vision (when configured)
- Agent → spawn Explore or Plan subagents for complex work
- Bash / WriteFile / ListDir / WebSearch for direct actions

When you need information, call the appropriate tool. After receiving the compact result, reason and continue.
Prefer multiple precise tool calls over one giant request.
Be concise in final answers unless the user asks for detail.
You are operating inside a real project directory. Respect the user's codebase.
"""


@dataclass
class OrchestratorState:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    turn: int = 0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    last_tool_results: List[str] = field(default_factory=list)

    def add_user(self, content: str):
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, msg: Any):
        # msg can be a ChatCompletionMessage or dict
        if hasattr(msg, "model_dump"):
            d = msg.model_dump(exclude_none=True)
        elif hasattr(msg, "dict"):
            d = msg.dict()
        else:
            d = msg
        self.messages.append(d)

    def add_tool_result(self, tool_call_id: str, content: str):
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": content,
            }
        )


class Orchestrator:
    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=load_api_key(),
            base_url=settings.base_url,
        )
        self.registry = get_registry()
        self.router = get_router()
        self.state = OrchestratorState()
        # Static system + tools are the prefix for cache friendliness
        self._system = ORCHESTRATOR_SYSTEM

    def reset(self):
        self.state = OrchestratorState()

    def _build_messages(self) -> list:
        """System prompt is always first (static → high cache hit rate)."""
        msgs = [{"role": "system", "content": self._system}]
        msgs.extend(self.state.messages)
        return msgs

    async def run_turn(self, user_input: str) -> str:
        self.state.add_user(user_input)
        self.state.turn += 1

        max_iterations = 25
        final_content = ""

        for iteration in range(max_iterations):
            messages = self._build_messages()
            tools = self.registry.get_schemas()

            kwargs: Dict[str, Any] = {
                "model": settings.orchestrator_model,
                "messages": messages,
                "tools": tools,
                "max_tokens": settings.max_tokens,
                "temperature": settings.temperature,
            }
            if settings.thinking_enabled:
                kwargs["reasoning_effort"] = settings.reasoning_effort
                kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

            with console.status(f"[bold cyan]Thinking…[/bold cyan] (turn {self.state.turn}, iter {iteration+1})", spinner="dots"):
                try:
                    response = await self.client.chat.completions.create(**kwargs)
                except Exception as e:
                    console.print(f"[red]API error:[/red] {e}")
                    return f"API error: {e}"

            choice = response.choices[0]
            msg = choice.message
            self.state.add_assistant(msg)

            # Token accounting (approximate)
            if response.usage:
                self.state.total_input_tokens += response.usage.prompt_tokens or 0
                self.state.total_output_tokens += response.usage.completion_tokens or 0

            # Tool calls?
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    name = tc.function.name
                    try:
                        args = json.loads(tc.function.arguments or "{}")
                    except json.JSONDecodeError:
                        args = {}

                    # Permission
                    allowed = await self.registry.check_permission(name, args)
                    if not allowed:
                        self.state.add_tool_result(tc.id, json.dumps({"error": "User denied permission"}))
                        continue

                    console.print(f"[dim]→ {name}({json.dumps(args)[:80]}…)[/dim]")

                    # Route heavy tools to subagents
                    if name in ("ReadFile", "SearchContent", "AnalyzeImage", "Agent"):
                        result = await self.router.delegate(name, args)
                    else:
                        result = await self.registry.execute(name, args, tc.id)

                    self.state.add_tool_result(tc.id, result)
                    # Micro-compact very long results
                    if len(result) > 6000:
                        result = result[:5500] + "\n…[truncated for orchestrator context]"
                continue  # loop back with tool results

            # Final text response
            final_content = msg.content or ""
            # Show reasoning if present and verbose
            reasoning = getattr(msg, "reasoning_content", None)
            if reasoning and settings.verbose:
                console.print(Panel(reasoning[:2000], title="[dim]reasoning[/dim]", border_style="dim"))

            break
        else:
            final_content = "Reached maximum tool iterations. Stopping."

        return final_content

    def cost_summary(self) -> str:
        # Rough cost estimate using Flash prices
        in_cost = (self.state.total_input_tokens / 1_000_000) * 0.14  # miss price as upper bound
        out_cost = (self.state.total_output_tokens / 1_000_000) * 0.28
        return (
            f"Tokens — in: {self.state.total_input_tokens:,}  out: {self.state.total_output_tokens:,}\n"
            f"Est. cost (cache-miss upper bound): ${in_cost + out_cost:.4f}"
        )

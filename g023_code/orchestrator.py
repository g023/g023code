"""
Orchestrator — the high-level reasoning loop.

Keeps context clean by delegating all heavy data work to subagents, and narrates
what it is doing while it does it. Every tool call is timed, summarised, and
folded into a :class:`~g023_code.ui.TurnTrace` so the CLI can close each turn
with an honest account of where the time and the money went.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI
from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from . import ui
from .config import get_project_root, load_api_key, settings
from .tools.registry import get_registry
from .subagents.router import get_router
from .ui import ActivityPrinter, Glyph, ToolEvent, TurnTrace, console
from .usage import format_cost, get_usage

ORCHESTRATOR_SYSTEM = """You are g023 Code — an elite terminal-native AI programming assistant powered by DeepSeek V4.

Core philosophy: Context is Currency. Subagents are the Treasury.

You NEVER dump raw file contents or large search results into your own context.
Instead you always use the provided tools:
- ReadFile → returns a compact structural summary + metadata (use this instead of asking for full files)
- SearchContent → returns metadata-first match list
- Agent → spawn Explore or Plan subagents for complex work
- FetchUrl → read a web page as readable text (asks the user first; the user
  decides between a cached copy and a fresh fetch, so never assume either)
- Bash / WriteFile / ListDir / WebSearch for direct actions

When you need information, call the appropriate tool. After receiving the compact result, reason and continue.
Prefer multiple precise tool calls over one giant request.
Be concise in final answers unless the user asks for detail.
You are operating inside a real project directory. Respect the user's codebase.
"""

VISION_SYSTEM_ADDENDUM = """
- AnalyzeImage → vision model ({model}) for screenshots, diagrams, and photos of code.
  You cannot see images yourself; always use AnalyzeImage and ask a specific question.
"""

VISION_DISABLED_ADDENDUM = """
You have no vision capability in this session. If the user references an image,
tell them to run /vision to enable a vision model (and /ollama host if the
Ollama daemon runs on another machine).
"""

COMPACT_SYSTEM = """You are a Compaction Subagent. You are given a transcript of a
coding session between a user and an AI assistant (including tool calls).

Produce a dense summary that lets the assistant continue the session with no other
context. Preserve, in this order:

1. The user's goals and any explicit instructions or constraints still in force.
2. Files read or modified, with the key facts learned about each (paths, symbols, structure).
3. Decisions made and their rationale.
4. Work completed so far.
5. What remains to be done / the immediate next step.

Be specific — keep exact file paths, function names, and commands. Drop pleasantries,
superseded attempts, and raw file dumps. Output markdown under those five headings.
"""

CLEARED_TOOL_RESULT = "[Old tool result content cleared]"

# Tools whose work belongs in an isolated subagent context, never in ours.
SUBAGENT_TOOLS = ("ReadFile", "SearchContent", "AnalyzeImage", "Agent")


# ---------------------------------------------------------------------------
# One normalised model reply, whether it arrived streamed or in one piece
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str = ""

    def parsed(self) -> dict:
        try:
            value = json.loads(self.arguments or "{}")
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def argument_error(self) -> Optional[str]:
        """Why the arguments could not be used, if they could not.

        A long WriteFile that runs into max_tokens arrives as truncated JSON.
        Executing that as an empty argument dict produces a baffling
        "missing positional argument" — the model needs to be told what actually
        happened so it can re-send a smaller call.
        """
        raw = self.arguments or "{}"
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as e:
            return (
                f"Malformed tool arguments ({e.msg} at char {e.pos} of {len(raw)}). "
                "They were most likely cut off mid-generation — re-send this call "
                "with a smaller payload (for a large file, write it in parts)."
            )
        if not isinstance(value, dict):
            return f"Tool arguments must be a JSON object, got {type(value).__name__}."
        return None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class Reply:
    content: str = ""
    reasoning: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Any = None
    streamed: bool = False

    def to_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        return msg


@dataclass
class TurnRecord:
    """A compact record of a finished turn, for /context and trend sparklines."""

    prompt: str
    cost: float
    tokens_in: int
    tokens_out: int
    tools: int
    elapsed_ms: float


@dataclass
class OrchestratorState:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    turn: int = 0
    compactions: int = 0
    history: List[TurnRecord] = field(default_factory=list)

    def add_user(self, content: str):
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, msg: Any):
        if isinstance(msg, dict):
            self.messages.append(msg)
        elif hasattr(msg, "model_dump"):
            self.messages.append(msg.model_dump(exclude_none=True))
        else:
            self.messages.append(dict(msg))

    def add_tool_result(self, tool_call_id: str, content: str):
        self.messages.append(
            {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        )

    def role_breakdown(self) -> dict[str, tuple[int, int]]:
        """role -> (message count, characters) — what /context reports."""
        out: dict[str, tuple[int, int]] = {}
        for m in self.messages:
            role = m.get("role", "?")
            size = len(m.get("content") or "")
            for call in m.get("tool_calls") or []:
                if isinstance(call, dict):
                    size += len((call.get("function") or {}).get("arguments") or "")
            count, chars = out.get(role, (0, 0))
            out[role] = (count + 1, chars + size)
        return out


class Orchestrator:
    def __init__(self, printer: Optional[ActivityPrinter] = None):
        self.client = AsyncOpenAI(api_key=load_api_key(), base_url=settings.base_url)
        self.registry = get_registry()
        self.router = get_router()
        self.state = OrchestratorState()
        self.usage = get_usage()
        self.printer = printer or ActivityPrinter(root=str(get_project_root()))
        self.trace = TurnTrace()
        self._streamed_answer = False

    def reset(self):
        self.state = OrchestratorState()
        self.usage.reset()
        self.trace = TurnTrace()
        self._streamed_answer = False

    @property
    def _system(self) -> str:
        """
        System prompt. Static except for the vision clause, which only changes
        when the user reconfigures vision — so the prefix stays cache-friendly.
        """
        if settings.vision_enabled:
            return ORCHESTRATOR_SYSTEM + VISION_SYSTEM_ADDENDUM.format(
                model=settings.vision_model or settings.vision_backend
            )
        return ORCHESTRATOR_SYSTEM + VISION_DISABLED_ADDENDUM

    def _build_messages(self) -> list:
        """System prompt is always first (static → high cache hit rate)."""
        return [{"role": "system", "content": self._system}] + self.state.messages

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------

    def _request_kwargs(self) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {
            "model": settings.orchestrator_model,
            "messages": self._build_messages(),
            "tools": self.registry.get_schemas(),
            "max_tokens": settings.max_tokens,
            "temperature": settings.temperature,
        }
        if settings.thinking_enabled:
            kwargs["reasoning_effort"] = settings.reasoning_effort
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        return kwargs

    async def _call_blocking(self, kwargs: Dict[str, Any], label: str) -> Reply:
        """Non-streaming call, with a spinner that counts the seconds."""
        started = time.perf_counter()
        task = asyncio.create_task(self.client.chat.completions.create(**kwargs))
        with console.status(f"[accent]{label}[/accent]", spinner="dots") as status:
            while not task.done():
                await asyncio.sleep(0.25)
                status.update(
                    f"[accent]{label}[/accent] "
                    f"[muted]{Glyph.DOT} {time.perf_counter() - started:.1f}s[/muted]"
                )
        response = await task

        msg = response.choices[0].message
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=tc.function.arguments or "")
            for tc in (msg.tool_calls or [])
        ]
        return Reply(
            content=msg.content or "",
            reasoning=getattr(msg, "reasoning_content", None) or "",
            tool_calls=calls,
            usage=response.usage,
        )

    async def _call_streaming(self, kwargs: Dict[str, Any], label: str) -> Reply:
        """
        Streaming call — the answer appears as it is written.

        Reasoning deltas drive the spinner text (so a long think looks like
        progress rather than a hang) and the answer renders into a live panel.
        """
        kwargs = dict(kwargs)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        stream = await self.client.chat.completions.create(**kwargs)

        content: List[str] = []
        reasoning: List[str] = []
        partial: Dict[int, Dict[str, str]] = {}
        usage = None
        started = time.perf_counter()
        live: Optional[Live] = None
        reasoning_chars = 0
        counter_shown = False
        last_tick = 0.0

        def clear_counter() -> None:
            """Wipe the in-place thinking line so nothing prints on top of it."""
            nonlocal counter_shown
            if counter_shown:
                console.print(" " * 64, end="\r")
                counter_shown = False

        try:
            async for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta

                piece = getattr(delta, "reasoning_content", None)
                if piece:
                    reasoning.append(piece)
                    reasoning_chars += len(piece)
                    now = time.perf_counter()
                    # Redraw ~10x a second: often enough to look live, rare enough
                    # that a fast stream does not spend its time on the terminal.
                    # The counter redraws in place with \r, which only means
                    # anything on a terminal — piped or redirected output would
                    # collect every tick as literal noise.
                    if live is None and console.is_terminal and now - last_tick >= 0.1:
                        last_tick = now
                        counter_shown = True
                        console.print(
                            f"  [muted]{Glyph.DOT} thinking {reasoning_chars:,} chars"
                            f" {Glyph.DOT} {now - started:.1f}s[/muted]",
                            end="\r",
                        )

                for tc in getattr(delta, "tool_calls", None) or []:
                    slot = partial.setdefault(tc.index, {"id": "", "name": "", "arguments": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function is not None:
                        if tc.function.name:
                            slot["name"] = tc.function.name
                        if tc.function.arguments:
                            slot["arguments"] += tc.function.arguments

                if delta.content:
                    content.append(delta.content)
                    if live is None:
                        clear_counter()
                        live = Live(console=console, refresh_per_second=8, vertical_overflow="visible")
                        live.start()
                    live.update(ui.panel(Markdown("".join(content)), "g023"))
        finally:
            if live is not None:
                live.stop()
            clear_counter()

        calls = [
            ToolCall(id=slot["id"], name=slot["name"], arguments=slot["arguments"])
            for _, slot in sorted(partial.items())
            if slot["name"]
        ]

        return Reply(
            content="".join(content),
            reasoning="".join(reasoning),
            tool_calls=calls,
            usage=usage,
            streamed=bool(content),
        )

    async def _call_model(self, iteration: int) -> Reply:
        kwargs = self._request_kwargs()
        label = f"Thinking{Glyph.ELLIPSIS} (turn {self.state.turn}, step {iteration})"
        if settings.stream:
            try:
                return await self._call_streaming(kwargs, label)
            except Exception as e:
                # Some proxies reject stream_options; one quiet retry unstreamed
                # is better than failing a turn over a transport detail.
                console.print(f"[muted]streaming unavailable ({e}); falling back[/muted]")
        return await self._call_blocking(kwargs, label)

    # ------------------------------------------------------------------
    # The turn
    # ------------------------------------------------------------------

    async def run_turn(self, user_input: str) -> str:
        self.usage.start_turn()
        self.trace = TurnTrace()
        turn_started = time.perf_counter()

        if settings.auto_compact and self.context_fraction() >= settings.compact_threshold:
            console.print(
                f"[warn]Context at {self.context_fraction():.0%} "
                f"{Glyph.ARROW} auto-compacting.[/warn]"
            )
            console.print(f"[muted]{await self.compact()}[/muted]")

        self.state.add_user(user_input)
        self.state.turn += 1

        max_iterations = 25
        final_content = ""

        for iteration in range(1, max_iterations + 1):
            self.trace.iterations = iteration

            think_started = time.perf_counter()
            try:
                reply = await self._call_model(iteration)
            except Exception as e:
                console.print(f"[bad]API error:[/bad] {e}")
                # Drop the dangling user message so the next turn starts clean.
                self._rollback_to_last_complete()
                return f"API error: {e}"
            self.trace.thinking_ms += (time.perf_counter() - think_started) * 1000

            self.state.add_assistant(reply.to_message())
            delta = self.usage.record(settings.orchestrator_model, reply.usage, scope="orchestrator")

            if reply.reasoning and settings.show_thinking:
                console.print(
                    ui.panel(
                        Text(reply.reasoning[:4000], style="dim"),
                        f"reasoning {Glyph.DOT} step {iteration}",
                        style="dim",
                    )
                )

            if settings.show_thinking and delta.calls:
                self.printer.iteration(
                    iteration,
                    delta.input_tokens,
                    delta.output_tokens,
                    format_cost(delta.cost(settings.orchestrator_model)),
                )

            if reply.tool_calls:
                await self._run_tool_calls(reply.tool_calls)
                continue

            final_content = reply.content
            self._streamed_answer = reply.streamed
            break
        else:
            final_content = (
                f"Stopped after {max_iterations} tool iterations without a final answer. "
                "Try narrowing the request, or run /compact and ask again."
            )
            self._streamed_answer = False

        self.trace.total_ms = (time.perf_counter() - turn_started) * 1000
        self.state.history.append(
            TurnRecord(
                prompt=user_input[:80],
                cost=self.usage.turn_cost,
                tokens_in=self.usage.turn.input_tokens,
                tokens_out=self.usage.turn.output_tokens,
                tools=self.trace.tool_count,
                elapsed_ms=self.trace.total_ms,
            )
        )
        return final_content

    @property
    def answer_was_streamed(self) -> bool:
        """True when the last answer already rendered live — don't print it twice."""
        return self._streamed_answer

    async def _run_tool_calls(self, calls: List[ToolCall]) -> None:
        for tc in calls:
            args = tc.parsed()

            problem = tc.argument_error()
            if problem is not None:
                console.print(f"  [bad]{Glyph.CROSS} {tc.name}: {problem}[/bad]")
                self.trace.events.append(
                    ToolEvent(name=tc.name, args=args, ok=False, summary="bad arguments")
                )
                self.state.add_tool_result(tc.id, json.dumps({"error": problem}))
                continue

            self.printer.tool_start(tc.name, args)

            allowed = await self.registry.check_permission(tc.name, args)
            if not allowed:
                self.printer.tool_denied(tc.name)
                self.trace.events.append(
                    ToolEvent(name=tc.name, args=args, ok=False, denied=True, summary="denied")
                )
                self.state.add_tool_result(
                    tc.id, json.dumps({"error": "User denied permission"})
                )
                continue

            started = time.perf_counter()
            if tc.name in SUBAGENT_TOOLS:
                result = await self.router.delegate(tc.name, args)
            else:
                result = await self.registry.execute(tc.name, args, tc.id)
            elapsed = (time.perf_counter() - started) * 1000
            self.trace.tool_ms += elapsed

            summary, ok = ui.describe_result(tc.name, result)
            event = ToolEvent(
                name=tc.name,
                args=args,
                elapsed_ms=elapsed,
                result_chars=len(result),
                ok=ok,
                summary=summary,
            )
            self.trace.events.append(event)

            if settings.show_answer_detail:
                self.printer.tool_end(event, show_timing=settings.show_tool_timing)

            # Micro-compact very long results before they enter context.
            if len(result) > 6000:
                result = (
                    result[:5500]
                    + f"\n{Glyph.ELLIPSIS}[truncated for orchestrator context; "
                    "re-run the tool with a narrower scope for the rest]"
                )
            self.state.add_tool_result(tc.id, result)

    def _rollback_to_last_complete(self) -> None:
        """
        Drop a half-finished exchange after an API failure.

        An assistant message with tool_calls and no matching tool results is a
        400 on the *next* request, so a failed turn must not leave one behind.
        """
        while self.state.messages:
            last = self.state.messages[-1]
            if last.get("role") == "user":
                self.state.messages.pop()
                break
            if last.get("role") == "assistant" and last.get("tool_calls"):
                self.state.messages.pop()
                continue
            break

    # ------------------------------------------------------------------
    # Context accounting
    # ------------------------------------------------------------------

    def context_fraction(self) -> float:
        """How full the context window is, 0.0–1.0, from the last real call."""
        if not settings.max_context_tokens:
            return 0.0
        return min(self.usage.context_tokens / settings.max_context_tokens, 1.0)

    def status_line(self) -> Text:
        """The one-line footer printed under every answer."""
        return ui.status_bar(
            context_fraction=self.context_fraction(),
            context_tokens=self.usage.context_tokens,
            max_context=settings.max_context_tokens,
            turn_cost=format_cost(self.usage.turn_cost),
            session_cost=format_cost(self.usage.cost()),
            hit_rate=self.usage.totals().hit_rate(),
            elapsed_ms=self.trace.total_ms,
            model=settings.orchestrator_model,
        )

    def turn_summary(self) -> Text:
        """What ran during the turn that just finished (verbose ≥ mid)."""
        return ui.trace_summary(self.trace)

    def cost_lines(self) -> list[str]:
        u = self.usage
        total = u.totals()
        lines = [
            f"Context   {u.context_tokens:,} / {settings.max_context_tokens:,} tokens "
            f"({self.context_fraction():.0%})  {Glyph.DOT}  last call: "
            f"{u.context_cache_hit_tokens:,} cache hit / {u.context_cache_miss_tokens:,} miss",
            f"Session   in {total.input_tokens:,} "
            f"({total.cache_hit_tokens:,} hit / {total.cache_miss_tokens:,} miss, "
            f"{total.hit_rate():.0%} hit rate)  {Glyph.DOT}  out {total.output_tokens:,}",
        ]
        for model, mu in sorted(u.per_model().items()):
            lines.append(
                f"  {model:<20} {mu.calls:>3} calls  {mu.total_tokens:>10,} tok  "
                f"{format_cost(mu.cost(model))}"
            )
        lines.append(
            f"Est. cost {format_cost(u.cost())}  "
            f"(orchestrator {format_cost(u.scope_cost('orchestrator'))} {Glyph.DOT} "
            f"subagents {format_cost(u.scope_cost('subagent'))})"
        )
        return lines

    def cost_summary(self) -> str:
        return "\n".join(self.cost_lines())

    # ------------------------------------------------------------------
    # Compaction (SPEC §10)
    # ------------------------------------------------------------------

    def micro_compact(self, keep_recent: int = 6) -> int:
        """
        Layer 1: locally blank out old tool results. Free — no API call.
        Returns the number of characters reclaimed.
        """
        tool_indices = [i for i, m in enumerate(self.state.messages) if m.get("role") == "tool"]
        reclaimed = 0
        for i in tool_indices[:-keep_recent] if keep_recent else tool_indices:
            content = self.state.messages[i].get("content") or ""
            if content == CLEARED_TOOL_RESULT:
                continue
            reclaimed += len(content)
            self.state.messages[i]["content"] = CLEARED_TOOL_RESULT
        return reclaimed

    def _render_transcript(self, max_chars: int = 60_000) -> str:
        """Flatten the message list into something a summarizer can read."""
        parts: List[str] = []
        for m in self.state.messages:
            role = m.get("role", "?")
            content = m.get("content") or ""
            if role == "tool":
                parts.append(f"[tool result] {content[:1500]}")
                continue
            calls = m.get("tool_calls") or []
            if calls:
                names = ", ".join(
                    f"{c['function']['name']}({(c['function'].get('arguments') or '')[:160]})"
                    for c in calls
                    if isinstance(c, dict) and c.get("function")
                )
                parts.append(f"[assistant tool calls] {names}")
            if content:
                parts.append(f"[{role}] {content[:4000]}")
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            # Keep the tail — the most recent work matters most.
            text = f"{Glyph.ELLIPSIS}[earlier transcript elided]{Glyph.ELLIPSIS}\n\n" + text[-max_chars:]
        return text

    async def compact(self, instructions: str = "") -> str:
        """
        Layer 3: summarize the conversation with the cheap model in an isolated
        context and replace the history with that summary.
        """
        if not self.state.messages:
            return "Nothing to compact — the conversation is empty."

        before_msgs = len(self.state.messages)
        before_chars = sum(len(m.get("content") or "") for m in self.state.messages)

        # Render *before* touching the history: micro-compacting first would hand
        # the summarizer a transcript of blanked placeholders instead of the work
        # it is supposed to summarise. The history is replaced wholesale below
        # anyway, so there is nothing to reclaim up front.
        user_msg = self._render_transcript()
        if instructions:
            user_msg = f"Focus the summary on: {instructions}\n\n{user_msg}"

        try:
            with console.status("[accent]Compacting conversation…[/accent]", spinner="dots"):
                resp = await self.client.chat.completions.create(
                    model=settings.subagent_model,
                    messages=[
                        {"role": "system", "content": COMPACT_SYSTEM},
                        {"role": "user", "content": user_msg},
                    ],
                    max_tokens=2048,
                    temperature=0.1,
                    extra_body={"thinking": {"type": "disabled"}},
                )
        except Exception as e:
            return f"Compaction failed: {e}"

        delta = self.usage.record(settings.subagent_model, resp.usage, scope="subagent")
        summary_cost = delta.cost(settings.subagent_model)
        summary = resp.choices[0].message.content or ""
        if not summary.strip():
            return "Compaction produced an empty summary — history left untouched."

        self.state.messages = [
            {
                "role": "user",
                "content": (
                    "Summary of the conversation so far (the full history was compacted "
                    f"to save context):\n\n{summary}"
                ),
            },
            {
                "role": "assistant",
                "content": "Understood — I have the prior context and will continue from there.",
            },
        ]
        self.state.compactions += 1

        after_chars = sum(len(m.get("content") or "") for m in self.state.messages)
        saved = max(before_chars - after_chars, 0)
        # The next call re-reads a fresh prefix; reflect that estimate now.
        self.usage.context_tokens = max(self.usage.context_tokens - saved // 4, 0)
        return (
            f"Compacted {before_msgs} messages {Glyph.ARROW} 2 "
            f"(~{saved // 4:,} tokens reclaimed {Glyph.DOT} summary cost {format_cost(summary_cost)})."
        )

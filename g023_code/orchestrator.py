"""
Orchestrator — the high-level reasoning loop.

Runs on DeepSeek's Responses API. That choice is what lets ``web_search`` be a
real tool of this loop: DeepSeek only exposes its server-side search on
``/responses``, so the model can search the web mid-turn and keep going, in the
same call, without anything here brokering it.

The conversation is stored as Responses *items* rather than chat messages —
``function_call`` / ``function_call_output`` pairs, plus the model's own
``reasoning``, ``message`` and ``web_search_call`` items echoed back verbatim.
Echoing them unchanged is both the highest-fidelity way to carry history and the
most prefix-cache friendly, since the prefix stays byte-identical between turns.

Keeps context clean by delegating all heavy data work to subagents, and narrates
what it is doing while it does it. Every tool call is timed, summarised, and
folded into a :class:`~g023_code.ui.TurnTrace` so the CLI can close each turn
with an honest account of where the time and the money went.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from . import ui
from .api import DeepSeekError, get_client, reasoning_param
from .config import get_project_root, settings
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
- web_search → your own built-in web search. Use it whenever the answer depends on
  current facts, or on a library or API you are not certain about. It runs during
  your turn: you search, read what you find, and carry straight on.
- FetchUrl → read a specific web page as readable text, in full (asks the user
  first; the user decides between a cached copy and a fresh fetch, so never
  assume either). Use web_search to find a page, FetchUrl to study one.
- Bash / WriteFile / ListDir for direct actions

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

# DeepSeek tags every search action with the id of the call that produced it,
# appended to the URL as a fragment ("…/downloads/#ws_call_id=call_01_ab…") and
# pushed in as a trailing pseudo-query. It is bookkeeping, not part of either.
_WS_CALL_ID = re.compile(r"[#&?]?ws_call_id=[A-Za-z0-9_]+")


def _strip_call_id(value: str) -> str:
    return _WS_CALL_ID.sub("", value or "").rstrip("#&?").strip()


def describe_search(item: dict) -> tuple[dict, str]:
    """Turn a ``web_search_call`` item into (args, one-line summary) for the trace.

    The server reports what it did as an ``action``: a ``search`` carries the
    queries it ran, while ``open_page`` and ``find_in_page`` carry the URL it
    went to. Opening a page can fail — the server meets timeouts and blocks like
    any other client — so the status is worth surfacing rather than assuming.
    """
    action = item.get("action") or {}
    kind = action.get("type") or "search"
    url = _strip_call_id(action.get("url") or "")
    queries = [q for q in (_strip_call_id(q) for q in action.get("queries") or []) if q]

    if kind == "search":
        return {"queries": queries}, ", ".join(queries) or "search"
    args: dict[str, Any] = {"url": url}
    if action.get("pattern"):
        args["pattern"] = action["pattern"]
        return args, f"find {action['pattern']!r} in {ui.short_url(url)}"
    return args, f"read {ui.short_url(url)}"


# ---------------------------------------------------------------------------
# One normalised model reply, whether it arrived streamed or in one piece
# ---------------------------------------------------------------------------

@dataclass
class ToolCall:
    """A ``function_call`` item the model produced."""

    id: str  # call_id — what a function_call_output must quote back
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

        A long WriteFile that runs into max_output_tokens arrives as truncated
        JSON. Executing that as an empty argument dict produces a baffling
        "missing positional argument" — the model needs to be told what actually
        happened so it can re-send a smaller call.
        """
        raw = self.arguments or "{}"
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as e:
            return (
                f"Malformed tool arguments — {e.msg} (char {e.pos} of {len(raw)}). "
                "They were most likely cut off mid-generation — re-send this call "
                "with a smaller payload (for a large file, write it in parts)."
            )
        if not isinstance(value, dict):
            return f"Tool arguments must be a JSON object, got {type(value).__name__}."
        return None


@dataclass
class Reply:
    """A parsed ``/responses`` result."""

    output: List[Dict[str, Any]] = field(default_factory=list)
    content: str = ""
    # Mid-flight narration ("let me check the docs page"). Not the answer, but
    # the only thing said when a turn ends without one — see run_turn.
    commentary: str = ""
    reasoning: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    searches: List[Dict[str, Any]] = field(default_factory=list)
    usage: Any = None
    status: str = "completed"
    incomplete_reason: str = ""
    streamed: bool = False


def parse_reply(response: Dict[str, Any], rendered_ids: Optional[set] = None) -> Reply:
    """Split a response's ``output`` list into the parts the loop acts on."""
    reply = Reply(
        output=list(response.get("output") or []),
        usage=response.get("usage"),
        status=response.get("status") or "completed",
        incomplete_reason=((response.get("incomplete_details") or {}).get("reason") or ""),
    )
    answer: List[str] = []
    commentary: List[str] = []
    reasoning: List[str] = []
    # Message ids that were rendered live, split by which bucket they landed in,
    # so "was it already on screen?" can be answered for whichever one we end up
    # presenting as the turn's result.
    streamed_answer = False
    streamed_commentary = False

    for item in reply.output:
        kind = item.get("type")
        if kind == "function_call":
            reply.tool_calls.append(
                ToolCall(
                    id=item.get("call_id") or "",
                    name=item.get("name") or "",
                    arguments=item.get("arguments") or "",
                )
            )
        elif kind == "web_search_call":
            reply.searches.append(item)
        elif kind == "reasoning":
            for part in item.get("content") or []:
                if part.get("type") == "reasoning_text" and part.get("text"):
                    reasoning.append(part["text"])
        elif kind == "message" and item.get("role") == "assistant":
            # 'commentary' is the model narrating mid-flight ("let me check the
            # docs page"); only 'final_answer' is the answer itself.
            is_answer = item.get("phase") in (None, "final_answer")
            bucket = answer if is_answer else commentary
            for part in item.get("content") or []:
                if part.get("type") == "output_text" and part.get("text"):
                    bucket.append(part["text"])
            if rendered_ids is not None and item.get("id") in rendered_ids:
                if is_answer:
                    streamed_answer = True
                else:
                    streamed_commentary = True

    reply.content = "".join(answer)
    reply.commentary = "".join(commentary)
    reply.reasoning = "\n".join(reasoning)
    # A turn can legitimately end with narration and no final answer. Whichever
    # of the two the caller ends up showing is what "already streamed" has to
    # refer to, or the CLI reprints it — or, worse, prints "(no answer)" over
    # text the user just watched arrive.
    if reply.content:
        reply.streamed = streamed_answer
    elif reply.commentary:
        reply.streamed = streamed_commentary
    else:
        reply.streamed = False
    return reply


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
    # Responses input items, in order. Not chat messages: see the module docstring.
    items: List[Dict[str, Any]] = field(default_factory=list)
    turn: int = 0
    compactions: int = 0
    history: List[TurnRecord] = field(default_factory=list)

    def add_user(self, content: str):
        self.items.append({"role": "user", "content": content})

    def add_output(self, output: List[Dict[str, Any]]):
        """Echo the model's own output items back into the history verbatim."""
        self.items.extend(output)

    def add_tool_result(self, call_id: str, content: str):
        self.items.append(
            {"type": "function_call_output", "call_id": call_id, "output": content}
        )

    def role_breakdown(self) -> dict[str, tuple[int, int]]:
        """bucket -> (item count, characters) — what /context reports."""
        out: dict[str, tuple[int, int]] = {}

        def add(bucket: str, size: int) -> None:
            count, chars = out.get(bucket, (0, 0))
            out[bucket] = (count + 1, chars + size)

        for item in self.items:
            kind = item.get("type")
            if kind == "function_call_output":
                add("tool", len(item.get("output") or ""))
            elif kind == "function_call":
                add("assistant", len(item.get("arguments") or "") + len(item.get("name") or ""))
            elif kind == "web_search_call":
                add("web search", len(json.dumps(item.get("action") or {})))
            elif kind == "reasoning":
                add(
                    "reasoning",
                    sum(len(p.get("text") or "") for p in item.get("content") or []),
                )
            else:
                role = item.get("role") or "assistant"
                content = item.get("content")
                if isinstance(content, str):
                    size = len(content)
                else:
                    size = sum(len(p.get("text") or "") for p in content or [])
                add(role, size)
        return out


class Orchestrator:
    def __init__(self, printer: Optional[ActivityPrinter] = None):
        self.client = get_client()
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
        System prompt, sent as the request's ``instructions``. Static except for
        the vision clause, which only changes when the user reconfigures vision —
        so the prefix stays cache-friendly.
        """
        if settings.vision_enabled:
            return ORCHESTRATOR_SYSTEM + VISION_SYSTEM_ADDENDUM.format(
                model=settings.vision_model or settings.vision_backend
            )
        return ORCHESTRATOR_SYSTEM + VISION_DISABLED_ADDENDUM

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------

    def _request_body(self) -> Dict[str, Any]:
        return {
            "model": settings.orchestrator_model,
            "instructions": self._system,
            "input": list(self.state.items),
            "tools": self.registry.get_schemas(),
            "max_output_tokens": settings.max_tokens,
            "temperature": settings.temperature,
            "reasoning": reasoning_param(),
        }

    async def _call_blocking(self, body: Dict[str, Any], label: str) -> Reply:
        """Non-streaming call, with a spinner that counts the seconds."""
        started = time.perf_counter()
        task = asyncio.create_task(self.client.create(**body))
        with console.status(f"[accent]{label}[/accent]", spinner="dots") as status:
            while not task.done():
                await asyncio.sleep(0.25)
                status.update(
                    f"[accent]{label}[/accent] "
                    f"[muted]{Glyph.DOT} {time.perf_counter() - started:.1f}s[/muted]"
                )
        return parse_reply(await task)

    async def _call_streaming(self, body: Dict[str, Any], label: str) -> Reply:
        """
        Streaming call — the answer appears as it is written.

        Reasoning deltas drive the thinking counter (so a long think looks like
        progress rather than a hang) and each assistant message renders into its
        own live panel. Searches are announced twice — once when the server
        starts one, so a slow search is visibly a search rather than a hang, and
        again with the queries it actually ran once it finishes.

        The deltas are for the eye only: the authoritative result is taken from
        the response object embedded in the terminal event, which makes stream
        reassembly bugs impossible.
        """
        final: Optional[Dict[str, Any]] = None
        rendered_ids: set = set()

        text_by_item: Dict[str, List[str]] = {}
        current_item: Optional[str] = None
        live: Optional[Live] = None
        started = time.perf_counter()
        reasoning_chars = 0
        counter_shown = False
        last_tick = 0.0

        def clear_counter() -> None:
            """Wipe the in-place thinking line so nothing prints on top of it."""
            nonlocal counter_shown
            if counter_shown:
                console.print(" " * 72, end="\r")
                counter_shown = False

        def show_counter(text: str) -> None:
            """Draw an in-place progress line, on a terminal only."""
            nonlocal counter_shown
            if live is not None or not console.is_terminal:
                return
            counter_shown = True
            console.print(f"  [muted]{Glyph.DOT} {text}[/muted]", end="\r")

        def render_panel(item_id: str, title: str = "g023") -> None:
            if live is not None:
                live.update(ui.panel(Markdown("".join(text_by_item.get(item_id, []))), title))

        def close_panel(title: Optional[str] = None) -> None:
            """Stop the live panel, optionally re-titling it on the way out.

            ``Live.stop`` leaves the last frame on screen, so a final update
            here is what the user is left looking at.
            """
            nonlocal live, current_item
            if live is not None:
                if title is not None and current_item is not None:
                    render_panel(current_item, title)
                live.stop()
                live = None
            current_item = None

        try:
            async for event in self.client.stream(**body):
                kind = event.get("type") or ""

                if kind == "response.reasoning_text.delta":
                    piece = event.get("delta") or ""
                    reasoning_chars += len(piece)
                    now = time.perf_counter()
                    # Redraw ~10x a second: often enough to look live, rare
                    # enough that a fast stream does not spend its time on the
                    # terminal. The counter redraws in place with \r, which only
                    # means anything on a terminal — piped or redirected output
                    # would collect every tick as literal noise.
                    if now - last_tick >= 0.1:
                        last_tick = now
                        show_counter(
                            f"thinking {reasoning_chars:,} chars "
                            f"{Glyph.DOT} {now - started:.1f}s"
                        )

                elif kind == "response.output_text.delta":
                    item_id = event.get("item_id") or ""
                    if item_id != current_item:
                        close_panel()
                        clear_counter()
                        current_item = item_id
                        rendered_ids.add(item_id)
                        live = Live(
                            console=console,
                            refresh_per_second=8,
                            vertical_overflow="visible",
                        )
                        live.start()
                    text_by_item.setdefault(item_id, []).append(event.get("delta") or "")
                    render_panel(item_id)

                elif kind == "response.output_item.added":
                    item = event.get("item") or {}
                    if item.get("type") == "web_search_call":
                        # The server can spend a while out on the web without
                        # emitting anything else. Say so, or it reads as a hang.
                        show_counter("searching the web…")

                elif kind == "response.output_item.done":
                    item = event.get("item") or {}
                    if item.get("type") == "message":
                        # The phase is only settled here — an item announces
                        # itself as a final answer and may still turn out to be
                        # narration — so this is where the panel gets its title.
                        close_panel(
                            "g023"
                            if item.get("phase") in (None, "final_answer")
                            else f"g023 {Glyph.DOT} thinking aloud"
                        )
                    elif item.get("type") == "web_search_call":
                        # Announce it now; the trace event is recorded once the
                        # whole reply is parsed, so both stay in step.
                        clear_counter()
                        _, summary = describe_search(item)
                        style = ui.tool_style("web_search")
                        console.print(
                            f"  [{style.color}]{style.icon} {style.verb}[/{style.color}] "
                            f"[muted]{ui.truncate(summary, 88)}[/muted]"
                        )

                elif kind in (
                    "response.completed",
                    "response.incomplete",
                    "response.failed",
                ):
                    final = event.get("response") or {}
        finally:
            close_panel()
            clear_counter()

        if final is None:
            raise DeepSeekError(0, "the stream ended before the response completed")
        if final.get("status") == "failed":
            error = final.get("error") or {}
            raise DeepSeekError(0, error.get("message") or "the model reported a failure")
        return parse_reply(final, rendered_ids=rendered_ids)

    async def _call_model(self, iteration: int) -> Reply:
        body = self._request_body()
        label = f"Thinking{Glyph.ELLIPSIS} (turn {self.state.turn}, step {iteration})"
        if settings.stream:
            try:
                return await self._call_streaming(body, label)
            except DeepSeekError:
                raise
            except Exception as e:
                # A transport-level streaming failure should not cost the turn;
                # one quiet retry unstreamed is the cheaper answer.
                console.print(f"[muted]streaming unavailable ({e}); falling back[/muted]")
        return await self._call_blocking(body, label)

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
                # Drop the dangling exchange so the next turn starts clean.
                self._rollback_to_last_complete()
                return f"API error: {e}"
            self.trace.thinking_ms += (time.perf_counter() - think_started) * 1000

            self.state.add_output(reply.output)
            delta = self.usage.record(settings.orchestrator_model, reply.usage, scope="orchestrator")

            for item in reply.searches:
                args, summary = describe_search(item)
                self.trace.events.append(
                    ToolEvent(
                        name="web_search",
                        args=args,
                        ok=item.get("status") == "completed",
                        summary=summary,
                    )
                )

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

            if reply.incomplete_reason == "max_output_tokens":
                # Say so rather than presenting a sentence that stops mid-word
                # as if it were the whole answer.
                console.print(
                    f"[warn]The reply hit the {settings.max_tokens:,}-token output "
                    "limit and was cut off.[/warn]"
                )

            # The model can finish a turn having only narrated. That narration is
            # all it said, so present it rather than an empty panel.
            final_content = reply.content or reply.commentary
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

        The API enforces the tool-call pairing in both directions: a
        ``function_call`` with no matching ``function_call_output`` is a 400, and
        so is an output whose call was never made. A failed turn must not leave
        either behind, or every later turn fails too.
        """
        while self.state.items:
            last = self.state.items[-1]
            if last.get("role") == "user":
                self.state.items.pop()
                break
            self.state.items.pop()
        self._repair_pairing()

    def _repair_pairing(self) -> None:
        """Drop any ``function_call`` / ``function_call_output`` left unpaired."""
        called = {
            i.get("call_id") for i in self.state.items if i.get("type") == "function_call"
        }
        answered = {
            i.get("call_id")
            for i in self.state.items
            if i.get("type") == "function_call_output"
        }
        orphans = called.symmetric_difference(answered)
        if not orphans:
            return
        self.state.items = [
            i
            for i in self.state.items
            if i.get("type") not in ("function_call", "function_call_output")
            or i.get("call_id") not in orphans
        ]

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

        Only the ``output`` text goes; the items themselves stay, because
        removing one would orphan its ``function_call`` and make the next
        request a 400.
        """
        indices = [
            i
            for i, item in enumerate(self.state.items)
            if item.get("type") == "function_call_output"
        ]
        reclaimed = 0
        for i in indices[:-keep_recent] if keep_recent else indices:
            content = self.state.items[i].get("output") or ""
            if content == CLEARED_TOOL_RESULT:
                continue
            reclaimed += len(content)
            self.state.items[i]["output"] = CLEARED_TOOL_RESULT
        return reclaimed

    def _render_transcript(self, max_chars: int = 60_000) -> str:
        """Flatten the item list into something a summarizer can read."""
        parts: List[str] = []
        for item in self.state.items:
            kind = item.get("type")
            if kind == "function_call_output":
                parts.append(f"[tool result] {(item.get('output') or '')[:1500]}")
                continue
            if kind == "function_call":
                parts.append(
                    f"[assistant tool call] {item.get('name')}"
                    f"({(item.get('arguments') or '')[:160]})"
                )
                continue
            if kind == "web_search_call":
                _, summary = describe_search(item)
                parts.append(f"[web search] {summary}")
                continue
            if kind == "reasoning":
                continue  # the model's scratch work, not the record of the session
            role = item.get("role") or "assistant"
            content = item.get("content")
            if not isinstance(content, str):
                content = "".join(p.get("text") or "" for p in content or [])
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
        if not self.state.items:
            return "Nothing to compact — the conversation is empty."

        before_items = len(self.state.items)
        before_chars = len(self._render_transcript(max_chars=10**9))

        # Render *before* touching the history: micro-compacting first would hand
        # the summarizer a transcript of blanked placeholders instead of the work
        # it is supposed to summarise. The history is replaced wholesale below
        # anyway, so there is nothing to reclaim up front.
        user_msg = self._render_transcript()
        if instructions:
            user_msg = f"Focus the summary on: {instructions}\n\n{user_msg}"

        try:
            with console.status("[accent]Compacting conversation…[/accent]", spinner="dots"):
                resp = await self.client.create(
                    model=settings.subagent_model,
                    instructions=COMPACT_SYSTEM,
                    input=[{"role": "user", "content": user_msg}],
                    max_output_tokens=2048,
                    temperature=0.1,
                    reasoning=reasoning_param(enabled=False),
                )
        except Exception as e:
            return f"Compaction failed: {e}"

        from .api import output_text

        delta = self.usage.record(settings.subagent_model, resp.get("usage"), scope="subagent")
        summary_cost = delta.cost(settings.subagent_model)
        summary = output_text(resp)
        if not summary.strip():
            return "Compaction produced an empty summary — history left untouched."

        self.state.items = [
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

        after_chars = len(self._render_transcript(max_chars=10**9))
        saved = before_chars - after_chars
        # The next call re-reads a fresh prefix; reflect that estimate now.
        self.usage.context_tokens = max(self.usage.context_tokens - saved // 4, 0)
        # A short conversation can summarise to something longer than itself.
        # Report that rather than clamping it to a flattering "0 reclaimed".
        change = (
            f"~{saved // 4:,} tokens reclaimed"
            if saved > 0
            else f"no reduction — the summary is ~{-saved // 4:,} tokens larger"
        )
        return (
            f"Compacted {before_items} items {Glyph.ARROW} 2 "
            f"({change} {Glyph.DOT} summary cost {format_cost(summary_cost)})."
        )

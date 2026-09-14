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
import hashlib
import itertools
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from . import project_instructions
from . import ui
from .api import (
    DeepSeekError,
    get_client,
    reasoning_param,
    silent_degradation,
    unknown_item_types,
)
from .effort import (
    split_reply_meta,
    strip_reply_meta_from_items,
    visible_reply_text,
)
from .cache import get_cache, is_vault_pointer
from .cost_policy import (
    VAULT_TOOLS,
    should_micro_cached_prefix,
    should_post_turn_compact,
    should_vault_tool_output,
)
from .config import (
    ITERATION_NOTICE_EVERY,
    MAX_OUTPUT_CONTINUES,
    UNLIMITED_OUTPUT_TOKENS,
    get_project_root,
    settings,
)
from .session_log import (
    SessionLog,
    load_latest_objective_log,
    load_latest_view,
    load_prefix_fingerprint,
    prefix_hashes,
    write_objective_log,
)
from .signals import get_signals
from .tools.registry import ALLOW, get_registry
from .subagents.router import get_router
from .ui import ActivityPrinter, Glyph, ToolEvent, TurnTrace, console
from .usage import format_cost, get_usage
from .working_set import (
    is_working_set_item,
    judge_goal_completion,
    load as load_working_set,
    looks_like_test_command,
    mirror_working_set,
    note_file,
    note_verify,
    reset as reset_working_set,
    save as save_working_set,
    working_set_item_content,
)

ORCHESTRATOR_SYSTEM = """You are g023 Code — an elite terminal-native AI programming assistant powered by DeepSeek V4.

Core philosophy: Context is Currency. Subagents are the Treasury.

You NEVER dump raw file contents or large search results into your own context.
Instead you always use the provided tools:
- ReadFile → returns a compact structural summary + metadata (use this instead of asking for full files);
  pass raw=true to get the FULL file verbatim when you need exact bytes
- SearchContent → returns metadata-first match list (total count plus a sample)
- Glob → find files by glob/brace pattern (e.g. *.{ts,js})
- TodoWrite → update the working-set leaves (remaining work lives there)
- Agent → spawn research, planning, or todos subagents (explore/plan still work).
  research relays findings; planning relays a plan; todos writes a list,
  orchestrates task workers (each receives issues from the previous task),
  and relays a final summary. Pass prior/issues so workers communicate both ways.
- web_search → your own built-in web search. Use it whenever the answer depends on
  current facts, or on a library or API you are not certain about. It runs during
  your turn: you search, read what you find, and carry straight on.
- FetchUrl → read a specific web page as readable text, in full (asks the user
  first; the user decides between a cached copy and a fresh fetch, so never
  assume either). Use web_search to find a page, FetchUrl to study one.
- Bash / WriteFile / ListDir for direct actions

When you need information, call the appropriate tool. After receiving the compact result, reason and continue.

Editing files safely:
- Edit replaces a unique old_string with new_string in an existing file. If the
  span is missing or not unique, the tool refuses and writes nothing. Prefer Edit
  for small changes.
- WriteFile REPLACES THE ENTIRE FILE. Never rewrite a file from a ReadFile summary
  or from memory — summaries omit code bodies, and a summary-based rewrite silently
  truncates the file. Use WriteFile to create a file or to rewrite it in full.
  Before a full rewrite, ReadFile with raw=true (or read every range you will touch)
  so you hold the complete current content.
- Every WriteFile/Edit overwrite automatically backs up the previous version to
  .g023/backups/ (newest few kept per file), so a bad write is one copy away.
Prefer multiple precise tool calls over one giant request.
Be concise in final answers unless the user asks for detail.
You are operating inside a real project directory. Respect the user's codebase.

Learned procedures from earlier sessions live in the learn store (machine-level and per-project).
They are not in this prompt. Call LearnSearch when (a) the task uses a tool, host, or workflow
you have not used yet this session, (b) a tool returns an error, or (c) the user mentions a
browser, MCP, Ollama, or other machine-local setup. LearnGet loads one guide body. Do not
treat a guide as permission to run a blocked tool.

Project facts from earlier sessions live in .g023/memory/ (MEMORY.md and topics/).
They are not in this prompt. SearchContent can read that directory. Do not copy those
notes into standing instructions.

SessionSearch greps this project's .g023/sessions/*.jsonl, including text dropped from
the current view by compact. Omitted write bodies come back as hashes, not contents.

Same-response trailers (after the user-visible answer, nothing after them):
When this turn used tools or changed the project, emit
[handoff]
## Goals
## Files
## Decisions
## Done
[/handoff]
with specific paths and names. Omit remaining-work guesses (the working-set item holds those). Omit the block for a short chat reply. The next turn reuses this instead of a second summarizer call.
Then end with exactly one last line:
[next_effort: N]
N is an integer 0-75 for how hard the NEXT user request looks — not this turn's remaining tool steps. 75 is the thinking-on default and the ceiling (values above 75 are capped to 75); use 25-50 for routine follow-up; 0 to disable thinking. Pick the number that matches the next request; do not default to 75. Do not mention these trailers in the answer. If you omit next_effort, the current effort stays as-is.
"""


def _load_a_improve() -> str:
    """A_Improve.txt communication/reasoning-style markers for the standing prompt.

    Loaded from the install root next to this package. If the file is missing,
    a fallback with the four named sections and their marker phrases is used
    so instructions still build.
    """
    path = Path(__file__).resolve().parent.parent / "A_Improve.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        text = ""
    if not text:
        text = (
            "## Communication & Reasoning Style\n\n"
            "When working through complex concepts, explaining decisions, or engaging "
            "in a dialogue with the user, structure your reasoning using the following "
            "conversational markers.\n\n"
            "### 1. Flagging Caveats & Limitations\n"
            '- "One honest caveat:"\n'
            '- "I don\'t want to paper over this:"\n'
            '- "Worth naming:"\n'
            '- "I don\'t want to bury this:"\n\n'
            "### 2. Acknowledging Pushback & Admitting Error\n"
            '- "Fair pushback:"\n'
            '- "That\'s a fair hit:"\n'
            '- "That\'s on me:"\n'
            '- "I\'m not going to defend that:"\n\n'
            "### 3. Introducing Precision & Key Distinctions\n"
            '- "Let me be precise:"\n'
            '- "The sharper distinction:"\n'
            '- "The part that matters:"\n'
            '- "Worth stating plainly:"\n'
            '- "One thing worth stating precisely:"\n\n'
            "### 4. Framing & Emphasis\n"
            '- "I\'d rather say this now than have it surface later:"\n'
            '- "I\'d be doing you a disservice:"\n'
            '- "One thing worth flagging:"\n'
            '- "The key insight is:"\n'
        )
    return "\n\n" + text + "\n"


ORCHESTRATOR_SYSTEM = ORCHESTRATOR_SYSTEM + _load_a_improve()

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
5. Decisions not already recorded in the working-set item. Do not invent remaining
   work; remaining work lives in the working-set item immediately after the original
   goal.

Be specific — keep exact file paths, function names, and commands. Drop pleasantries,
superseded attempts, and raw file dumps. Output markdown under those five headings.
"""

CLEARED_TOOL_RESULT = "[Old tool result content cleared]"

# Session-compact policy. The summarizer sees the start of the session (goals)
# and the recent tail, not only a truncated tail of tool noise. After a compact
# the original user-goal item stays byte-stable (prefix cache) and recent
# function_call / function_call_output pairs stay as the original dicts.
COMPACT_TRANSCRIPT_MAX_CHARS = 60_000
# Dense 5-heading summary. 8192 let Flash fill a novel of billed output
# tokens; 2048 is enough and keeps the next turn's prefix smaller.
# Prefix-stable compact does not send this (the orchestrator omits
# max_output_tokens too — adding it busts the standing prefix). The
# stored summary is still clipped so a rambling reply cannot bloat the
# next turn.
COMPACT_MAX_OUTPUT_TOKENS = 2048
COMPACT_SUMMARY_STORE_CHARS = 8_000
COMPACT_KEEP_TAIL = 12
MICRO_KEEP_RECENT = 6
TOOL_SHARE_MICRO_THRESHOLD = 0.4
COMPACT_SUMMARY_LEADIN = (
    "Summary of the conversation so far (the full history was compacted "
    "to save context):\n\n"
)
TRANSCRIPT_ELISION_MARKER = "[middle transcript elided]"
# Appended to the live item list so Layer-3 compact hits the standing
# prefix (instructions + tools + history) instead of flattening into a
# new COMPACT_SYSTEM prompt that misses from token 0.
COMPACT_USER_PROMPT = (
    "Do not call tools. Write a dense summary of this conversation that a "
    "fresh assistant turn can continue from, under these headings: "
    "Goals / Files / Decisions / Done. Keep exact paths, symbols, and "
    "commands. Drop pleasantries and raw dumps."
)
# Post-turn compact pays for a summarizer call only when there is no summary
# already in hand. Prefer, in order: a [handoff] block on the same final
# answer, an existing compact-summary item (local fold), then Flash. Skip
# Flash unless leftover history is large *and uncached* (a hot prefix is
# cheaper to keep than to bust — see cost_policy), the window is full, or
# the working-set judge says the objective is complete. Small tool rows stay
# in place so the next request can still hit the prefix cache.
POST_TURN_COMPACT_MIN_CHARS = 4_000
POST_TURN_COMPACT_TOOL_TYPES = frozenset(
    {"function_call", "function_call_output", "web_search_call"}
)

# Tools whose work belongs in an isolated subagent context, never in ours.
SUBAGENT_TOOLS = ("ReadFile", "SearchContent", "AnalyzeImage", "Agent")

# ALLOW read-only calls in one model step may overlap. Writes and ASK tools stay serial.
READ_ONLY_PARALLEL = frozenset(
    {"ReadFile", "SearchContent", "ListDir", "Glob", "LearnSearch", "LearnGet", "SessionSearch"}
)
WRITE_TOOLS = frozenset({"WriteFile", "Edit"})

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
    # Drift signals, recorded but never acted on — see g023_code.signals.
    unknown_types: set = field(default_factory=set)
    degradation: Optional[str] = None


def parse_reply(response: Dict[str, Any], rendered_ids: Optional[set] = None) -> Reply:
    """Split a response's ``output`` list into the parts the loop acts on."""
    reply = Reply(
        output=list(response.get("output") or []),
        usage=response.get("usage"),
        status=response.get("status") or "completed",
        incomplete_reason=((response.get("incomplete_details") or {}).get("reason") or ""),
        unknown_types=unknown_item_types(response),
        degradation=silent_degradation(response),
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
    # Item count immediately after the last successful Layer-3 compact. Used to
    # skip a second summarizer rewrite of a history that has not grown since.
    compacted_len: Optional[int] = None
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
        # Do not open a new jsonl here: that would rewrite .g023/sessions/CURRENT
        # to an empty file and a later process could not /resume the previous
        # session. The first logged event of a *new* conversation opens a file;
        # /resume attaches to the existing one.
        self.session_log: Optional[SessionLog] = None
        self._frozen_addendum: Optional[str] = None
        self._frozen_tool_schemas: Optional[list] = None
        self._frozen_tool_key: Optional[tuple] = None
        self._frozen_instructions_text: Optional[str] = None
        self._frozen_instructions_key: Optional[tuple] = None
        self.prefix_mismatch: Optional[str] = None
        self._layer3_this_turn = False
        self._items_at_turn_start = 0
        self._last_objective_log: Optional[Path] = None
        self._last_handoff: Optional[str] = None
        self._next_effort_applied = False
        # Wire ``reasoning.effort`` is pinned to the first request of this
        # prefix. A live probe (2026-09-14) found high→low on a continuation
        # misses from token 0, including standing instructions and tools.
        # ``[next_effort]`` still updates settings; the wire follows when the
        # prefix is already new: /thinking, /goal, /clear, or a successful
        # post-turn compact (output is ~200× a cache hit, so retuning there
        # is the remaining bill once the prefix is hot).
        self._prefix_effort_wire: Optional[str] = None
        # Temperature is also a /responses cache-key field. Pin it with the
        # standing prefix so a /settings mutation cannot miss from token 0.
        self._prefix_temperature: Optional[float] = None

    def reset(self):
        self.state = OrchestratorState()
        self.usage.reset()
        self.trace = TurnTrace()
        self._streamed_answer = False
        self._frozen_addendum = None
        self._frozen_tool_schemas = None
        self._frozen_tool_key = None
        self._frozen_instructions_text = None
        self._frozen_instructions_key = None
        self.prefix_mismatch = None
        self._layer3_this_turn = False
        self._items_at_turn_start = 0
        self._last_objective_log = None
        self._last_handoff = None
        self._next_effort_applied = False
        self._prefix_effort_wire = None
        self._prefix_temperature = None
        try:
            self.router.clear_shared_prefix()
        except Exception:
            pass
        get_signals().reset()
        try:
            reset_working_set()
        except Exception:
            pass
        try:
            from . import learn

            learn.reset_session_budget()
        except Exception:
            pass
        try:
            self.session_log = SessionLog.open_new()
            self._write_prefix_fingerprint()
        except Exception:
            self.session_log = None

    def _ensure_session_log(self) -> Optional[SessionLog]:
        log = getattr(self, "session_log", None)
        if log is not None:
            return log
        try:
            self.session_log = SessionLog.open_new()
            self._write_prefix_fingerprint()
        except Exception:
            self.session_log = None
        return self.session_log

    def _write_prefix_fingerprint(self) -> None:
        log = getattr(self, "session_log", None)
        if log is None:
            return
        try:
            body = self._request_body()
            inst, tools = prefix_hashes(body["instructions"], body["tools"])
            log.log_prefix_fingerprint(inst, tools)
        except Exception:
            pass

    def _session_log_call(self, method: str, *args, **kwargs) -> None:
        log = self._ensure_session_log()
        if log is None:
            return
        try:
            getattr(log, method)(*args, **kwargs)
        except Exception:
            pass

    def resume_from_latest(self) -> tuple[list, Optional[dict], Optional[Any]]:
        """Load the latest JSONL view into this process and append later events to it.

        Attaches ``session_log`` to that file (does not open a new one) and
        records a ``compact_boundary`` of the loaded view so a third process
        can continue the same session.
        """
        from pathlib import Path as _Path

        items, ws, path = load_latest_view()
        if path is None:
            return [], None, None
        self.state.items = list(items)
        self._repair_pairing()
        if ws is not None:
            save_working_set(ws)
            mirror_working_set(self.state.items, ws, self._sticky_goal_item())
        try:
            self.session_log = SessionLog(_Path(path))
            stored = load_prefix_fingerprint(_Path(path))
            body = self._request_body()
            inst, tools = prefix_hashes(body["instructions"], body["tools"])
            self.prefix_mismatch = None
            if stored and (
                stored.get("instructions_sha256") != inst
                or stored.get("tools_sha256") != tools
            ):
                self.prefix_mismatch = (
                    "prefix fingerprint mismatch: g023.md or tools/vision changed "
                    "since this session started; DeepSeek prefix cache will miss "
                    "from token 0. Standing instructions were not rewritten to compensate."
                )
            self._write_prefix_fingerprint()
            self.session_log.log_compact(
                self.state.items, ws if ws is not None else load_working_set()
            )
        except Exception:
            pass
        return self.state.items, ws, path

    def seed_from_objective_log(
        self, path: Optional[Any] = None
    ) -> tuple[list, Optional[dict], Optional[Any]]:
        """Start a new orchestration from a finished-objective handoff log.

        ``instructions`` stay the standing system prompt. ``input`` becomes the
        working-set item (if any) plus the compact summary. A new JSONL session
        is opened — this is a fresh orchestration, not an attach to the old
        transcript.
        """
        from pathlib import Path as _Path

        rec = None
        if path is not None:
            try:
                from .session_log import parse_objective_log

                rec = parse_objective_log(_Path(path))
            except OSError:
                rec = None
        else:
            rec = load_latest_objective_log()
        if rec is None or not (rec.get("summary") or rec.get("working_set")):
            return [], None, None
        summary = str(rec.get("summary") or "").strip()
        ws = rec.get("working_set") if isinstance(rec.get("working_set"), dict) else None
        if ws is not None:
            save_working_set(ws)
        else:
            try:
                ws = load_working_set()
            except Exception:
                ws = None
        items = self._items_from_handoff(summary, ws)
        self.state = OrchestratorState(items=list(items))
        self.state.compactions = 1
        self.state.compacted_len = len(self.state.items)
        try:
            self.session_log = SessionLog.open_new()
            self._write_prefix_fingerprint()
            self.session_log.log_compact(
                self.state.items, ws if ws is not None else load_working_set()
            )
        except Exception:
            self.session_log = None
        self._last_objective_log = rec.get("path")
        return self.state.items, ws, rec.get("path")

    def _items_from_handoff(self, summary: str, ws: Optional[dict] = None) -> List[dict]:
        out: List[dict] = []
        if ws is not None:
            out.append({"role": "user", "content": working_set_item_content(ws)})
        if summary:
            body = summary if summary.startswith(COMPACT_SUMMARY_LEADIN.rstrip("\n")) else (
                COMPACT_SUMMARY_LEADIN + summary
            )
            out.append({"role": "user", "content": body})
        return out

    def handle_interrupt(self) -> None:
        """Pairing-safe rollback used on KeyboardInterrupt mid-turn."""
        self._rollback_to_last_complete()
        self._session_log_call("log_interrupt", self.state.items)

    @property
    def _system(self) -> str:
        """
        System prompt, sent as the request's ``instructions``. Static except for
        the vision clause (changes when the user reconfigures vision) and the
        project-instruction addendum (``g023_compact.md`` if present, else
        ``g023.md``, plus a project-root ``AGENTS.md`` when readable) — so the
        prefix stays cache-friendly until those change.
        """
        if settings.vision_enabled:
            base = ORCHESTRATOR_SYSTEM + VISION_SYSTEM_ADDENDUM.format(
                model=settings.effective_vision_model
            )
        else:
            base = ORCHESTRATOR_SYSTEM + VISION_DISABLED_ADDENDUM
        frozen = getattr(self, "_frozen_addendum", None)
        if frozen is None:
            frozen = project_instructions.system_addendum()
            self._frozen_addendum = frozen
        return base + frozen

    def _instructions_key(self) -> tuple:
        return (
            bool(settings.vision_enabled),
            settings.vision_backend,
            settings.effective_vision_model,
        )

    def _tools_key(self) -> tuple:
        return (bool(settings.vision_enabled),)

    def _frozen_instructions(self) -> str:
        """Standing instructions, frozen until the user changes vision."""
        key = self._instructions_key()
        cached = getattr(self, "_frozen_instructions_text", None)
        if cached is None or getattr(self, "_frozen_instructions_key", None) != key:
            self._frozen_instructions_text = self._system
            self._frozen_instructions_key = key
        return self._frozen_instructions_text

    def _frozen_tools(self) -> list:
        """Tool schemas, frozen until the user changes vision (AnalyzeImage)."""
        key = self._tools_key()
        cached = getattr(self, "_frozen_tool_schemas", None)
        if cached is None or getattr(self, "_frozen_tool_key", None) != key:
            self._frozen_tool_schemas = self.registry.get_schemas()
            self._frozen_tool_key = key
        return list(self._frozen_tool_schemas)

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------

    def release_prefix_effort(self) -> None:
        """Drop the pinned wire effort so the next request re-reads settings.

        Used by explicit user actions that already start a new prefix
        (/thinking, /goal, /clear) and by a successful post-turn compact
        (history was replaced). Automatic ``[next_effort]`` must not call
        this on a hot continuation — retuning the alias mid-prefix misses
        from token 0.
        """
        self._prefix_effort_wire = None
        self._prefix_temperature = None

    def _wire_reasoning(self) -> Dict[str, str]:
        wanted = reasoning_param()
        if self._prefix_effort_wire is None:
            self._prefix_effort_wire = wanted["effort"]
        return {"effort": self._prefix_effort_wire}

    def _wire_temperature(self) -> float:
        if getattr(self, "_prefix_temperature", None) is None:
            self._prefix_temperature = float(settings.temperature)
        return self._prefix_temperature

    def _request_body(self, items: Optional[List[dict]] = None) -> Dict[str, Any]:
        instructions = self._frozen_instructions()
        tools = self._frozen_tools()
        reasoning = self._wire_reasoning()
        temperature = self._wire_temperature()
        # Children reuse this standing prefix so Agent steps hit the same
        # cache units as the parent (instructions + tools + effort). Isolated
        # input items still miss from the first user row — that is the point
        # of the branch. Publish even if the router ignores it.
        try:
            self.router.set_shared_prefix(
                instructions, tools, reasoning, temperature=temperature
            )
        except Exception:
            pass
        body = {
            "model": settings.orchestrator_model,
            "instructions": instructions,
            "input": list(self.state.items if items is None else items),
            "tools": tools,
            "temperature": temperature,
            "reasoning": reasoning,
        }
        # Omitted rather than sent as 0 when the budget is unlimited: the field
        # is optional, and leaving it out is what hands the ceiling back to the
        # model's own maximum. Sending 0 would ask for an empty reply.
        if settings.max_tokens != UNLIMITED_OUTPUT_TOKENS:
            body["max_output_tokens"] = settings.max_tokens
        return body

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
                raw = "".join(text_by_item.get(item_id, []))
                live.update(ui.panel(Markdown(visible_reply_text(raw)), title))

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

    async def _call_model(self, iteration: int, limit: int = 0) -> Reply:
        # A user/working-set item between a parallel function_call batch and
        # its outputs is a DeepSeek 400 ("No tool output found for tool call").
        self._reseat_items_splitting_tool_pairs()
        body = self._request_body()
        step = f"step {iteration}/{limit}" if limit else f"step {iteration}"
        label = f"Thinking{Glyph.ELLIPSIS} (turn {self.state.turn}, {step})"
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

    def _apply_reply_meta(self, reply: Reply) -> None:
        """Parse trailing handoff/next-effort meta, strip them, apply in-memory.

        A complete ``[handoff]`` on a final (no-tool-call) reply is stored as
        ``_last_handoff`` so compact can reuse it instead of a second Flash
        call. A valid ``[next_effort: N]`` on that same final reply updates
        ``settings.reasoning_effort`` (and thinking on/off) in memory, capped
        at 75. The wire alias stays pinned to this prefix — high→low on the
        next user request misses from token 0. ``/thinking`` / ``/goal`` /
        ``/clear`` / a successful post-turn compact release the pin. Sets
        ``_next_effort_applied`` so ``/goal``
        can honour the trailer instead of restoring the pre-goal dial.
        Mid-turn tool-call replies still strip trailers from display/history
        so they never flash, but they must not change effort for the remaining
        steps of this turn and must not overwrite a previous turn's handoff.
        Neither trailer is written to config — ``/thinking`` remains the
        persisted session default. Missing or invalid meta leaves the current
        effort unchanged and does not raise.
        """
        item_handoff, item_effort = strip_reply_meta_from_items(reply.output)
        content_handoff = comment_handoff = None
        parsed = None
        if reply.content:
            reply.content, content_handoff, content_effort = split_reply_meta(
                reply.content
            )
            parsed = content_effort
        if reply.commentary:
            reply.commentary, comment_handoff, comment_effort = split_reply_meta(
                reply.commentary
            )
            if parsed is None:
                parsed = comment_effort
        if parsed is None:
            parsed = item_effort
        if reply.tool_calls:
            return
        self._last_handoff = content_handoff or comment_handoff or item_handoff
        if parsed is None:
            return
        self._next_effort_applied = True
        if parsed <= 0:
            settings.thinking_enabled = False
            settings.reasoning_effort = 0
            return
        settings.thinking_enabled = True
        settings.reasoning_effort = parsed

    # ------------------------------------------------------------------
    # The turn
    # ------------------------------------------------------------------

    async def run_turn(self, user_input: str) -> str:
        self.usage.start_turn()
        self.trace = TurnTrace()
        try:
            from .prompt_store import save_prompt

            save_prompt(user_input)
        except Exception:
            pass
        try:
            from . import learn

            learn.reset_turn()
        except Exception:
            pass
        turn_started = time.perf_counter()

        self._layer3_this_turn = False
        self._next_effort_applied = False
        report = await self._maybe_auto_compact()
        self._items_at_turn_start = len(self.state.items)
        if report:
            # Layer-1 blanking of a cached prefix only runs at window
            # pressure (see cost_policy). Don't banner a micro as
            # "auto-compacting" unless we are at threshold or just rewrote.
            over = self.context_fraction() >= settings.compact_threshold
            if over or report.startswith("Compacted"):
                console.print(
                    f"[warn]Context at {self.context_fraction():.0%} "
                    f"{Glyph.ARROW} auto-compacting.[/warn]"
                )
            console.print(f"[muted]{report}[/muted]")

        self.state.add_user(user_input)
        self._session_log_call("log_user", self.state.items[-1])
        self.state.turn += 1

        # Read per turn, so /steps takes effect on the next thing you ask.
        # 0 is "no limit": count up forever and let the model, or Ctrl-C, end it.
        configured = max(0, int(settings.max_iterations))
        steps = itertools.count(1) if not configured else iter(range(1, configured + 1))

        try:
            return await self._run_turn_loop(
                user_input, steps, configured, turn_started
            )
        except KeyboardInterrupt:
            self.handle_interrupt()
            raise

    async def _run_turn_loop(
        self,
        user_input: str,
        steps,
        configured: int,
        turn_started: float,
    ) -> str:
        final_content = ""
        output_continues = 0
        for iteration in steps:
            self.trace.iterations = iteration

            # Progress the user can see at any verbosity. The spinner shows the
            # step while a call is in flight and then repaints over itself, so
            # without this an uncapped turn leaves nothing behind saying how far
            # in it is or what it has cost.
            if iteration > 1 and iteration % ITERATION_NOTICE_EVERY == 0:
                self.printer.step_notice(
                    iteration,
                    configured,
                    (time.perf_counter() - turn_started) * 1000,
                    format_cost(self.usage.turn_cost),
                    tools=self.trace.tool_count,
                )

            think_started = time.perf_counter()
            try:
                reply = await self._call_model(iteration, configured)
            except Exception as e:
                console.print(f"[bad]API error:[/bad] {e}")
                # Drop the dangling exchange so the next turn starts clean.
                self._rollback_to_last_complete()
                return f"API error: {e}"
            self.trace.thinking_ms += (time.perf_counter() - think_started) * 1000

            self._apply_reply_meta(reply)
            self.state.add_output(reply.output)
            self._session_log_call("log_output", reply.output)
            delta = self.usage.record(settings.orchestrator_model, reply.usage, scope="orchestrator")
            self._record_signals(reply, delta)

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
                # as if it were the whole answer. Which limit was hit depends on
                # who set it: with no budget configured we never sent one, so the
                # ceiling is the model's own and /max-tokens cannot raise it.
                if settings.max_tokens == UNLIMITED_OUTPUT_TOKENS:
                    console.print(
                        "[warn]The reply hit the model's own output limit and was "
                        "cut off.[/warn] [muted]No budget was sent with the request; "
                        "ask for the rest, or for the work in smaller pieces.[/muted]"
                    )
                else:
                    console.print(
                        f"[warn]The reply hit the {settings.max_tokens:,}-token output "
                        "limit and was cut off.[/warn] [muted]Raise it with "
                        "/max-tokens, or /max-tokens off for the model maximum."
                        "[/muted]"
                    )
                # A /goal that thinks until the output ceiling used to end the
                # turn with no tools run. Nudge once per cut-off, capped, so a
                # long project can keep writing smaller payloads instead of
                # dying mid-thought. Tool-call steps already `continue` above.
                if output_continues < MAX_OUTPUT_CONTINUES:
                    output_continues += 1
                    nudge = (
                        "Your previous reply hit the output limit and was cut off. "
                        "Continue the same task. Prefer tool calls with smaller "
                        "payloads (write files in parts) over a long written answer."
                    )
                    console.print(
                        f"[muted]Continuing this turn "
                        f"({output_continues}/{MAX_OUTPUT_CONTINUES}) "
                        f"with a smaller-payload nudge.[/muted]"
                    )
                    self.state.add_user(nudge)
                    self._session_log_call("log_user", self.state.items[-1])
                    continue

            # The model can finish a turn having only narrated. That narration is
            # all it said, so present it rather than an empty panel.
            final_content = reply.content or reply.commentary
            self._streamed_answer = reply.streamed
            break
        else:
            # Unreachable when the step count is unlimited — that loop only ends
            # by answering, by an API error, or by Ctrl-C.
            final_content = (
                f"Stopped after {configured} tool iterations without a final answer. "
                "Try narrowing the request, run /compact and ask again, or raise the "
                "cap with /steps (/steps off removes it)."
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
        report = await self._post_turn_compact()
        if report:
            console.print(f"[muted]{report}[/muted]")
        return final_content

    def _record_signals(self, reply: Reply, delta) -> None:
        """Log what looked off about a response, and bank the hit rate.

        Recording only. None of these change the loop's behaviour: an unknown
        item type is not an error, and an empty response is not necessarily a
        broken one. What they buy is that when the answers get worse, there is
        something to look at other than the answers — see /signals.
        """
        signals = get_signals()
        if reply.unknown_types:
            signals.note_unknown_types(reply.unknown_types, self.state.turn)
            if settings.show_answer_detail:
                console.print(
                    f"[warn]{Glyph.BULLET} unrecognised response item(s): "
                    f"{', '.join(sorted(reply.unknown_types))} — carried through "
                    "untouched, but not shown in the trace.[/warn]"
                )
        if reply.degradation and not reply.tool_calls:
            signals.note_empty_response(reply.degradation, self.state.turn)
            if settings.show_answer_detail:
                console.print(f"[warn]{Glyph.BULLET} {reply.degradation}[/warn]")

        if delta.calls and (delta.cache_hit_tokens or delta.cache_miss_tokens):
            try:
                get_cache().record_prefix_stats(
                    settings.orchestrator_model,
                    delta.cache_hit_tokens,
                    delta.cache_miss_tokens,
                    delta.calls,
                )
            except Exception:
                # Observability must never be able to cost someone a turn.
                pass

    @property
    def answer_was_streamed(self) -> bool:
        """True when the last answer already rendered live — don't print it twice."""
        return self._streamed_answer

    async def _invoke_tool(self, tc: ToolCall, args: dict) -> str:
        """Run one permitted tool. Tests stub this to control await overlap."""
        if tc.name in SUBAGENT_TOOLS:
            return await self.router.delegate(tc.name, args)
        return await self.registry.execute(tc.name, args, tc.id)

    def _maybe_parallel(self, name: str) -> bool:
        return name in READ_ONLY_PARALLEL and self.registry.policy.get(name) == ALLOW

    async def _run_tool_calls(self, calls: List[ToolCall]) -> None:
        prepared: list[tuple[str, ToolCall, dict, Optional[str]]] = []
        for tc in calls:
            args = tc.parsed()
            problem = tc.argument_error()
            if problem is not None:
                console.print(f"  [bad]{Glyph.CROSS} {tc.name}: {problem}[/bad]")
                prepared.append(("error", tc, args, problem))
                continue
            self.printer.tool_start(tc.name, args)
            allowed = await self.registry.check_permission(tc.name, args)
            if not allowed:
                self.printer.tool_denied(tc.name)
                prepared.append(("denied", tc, args, None))
                continue
            prepared.append(("run", tc, args, None))

        serial_lock = asyncio.Lock()
        path_locks: dict[str, asyncio.Lock] = {}
        path_guard = asyncio.Lock()

        async def lock_for_path(path: str) -> asyncio.Lock:
            async with path_guard:
                lock = path_locks.get(path)
                if lock is None:
                    lock = asyncio.Lock()
                    path_locks[path] = lock
                return lock

        async def execute_prepared(
            entry: tuple[str, ToolCall, dict, Optional[str]],
        ) -> tuple[ToolCall, dict, str, float, bool, bool]:
            status, tc, args, problem = entry
            if status == "error":
                return tc, args, json.dumps({"error": problem}), 0.0, False, False
            if status == "denied":
                return (
                    tc,
                    args,
                    json.dumps({"error": "User denied permission"}),
                    0.0,
                    False,
                    True,
                )
            started = time.perf_counter()
            try:
                result = await self._invoke_tool(tc, args)
            except Exception as e:
                result = json.dumps({"error": str(e), "tool": tc.name})
            elapsed = (time.perf_counter() - started) * 1000
            return tc, args, result, elapsed, True, False

        async def guarded(
            entry: tuple[str, ToolCall, dict, Optional[str]],
        ) -> tuple[ToolCall, dict, str, float, bool, bool]:
            status, tc, args, problem = entry
            if status != "run":
                return await execute_prepared(entry)
            if self._maybe_parallel(tc.name):
                return await execute_prepared(entry)
            async with serial_lock:
                if tc.name in WRITE_TOOLS:
                    lock = await lock_for_path(str(args.get("path") or ""))
                    async with lock:
                        return await execute_prepared(entry)
                return await execute_prepared(entry)

        results = await asyncio.gather(*[guarded(e) for e in prepared])

        # Record every function_call_output first, then mirror the working
        # set. Appending a user-role working-set item after the first write
        # of a parallel batch splits the remaining outputs from their calls
        # and DeepSeek 400s with "No tool output found for tool call …".
        pending_ws: list[tuple[ToolCall, dict, str]] = []
        for tc, args, result, elapsed, ran, denied in results:
            if not ran:
                self.trace.events.append(
                    ToolEvent(
                        name=tc.name,
                        args=args,
                        ok=False,
                        denied=denied,
                        summary="denied" if denied else "bad arguments",
                    )
                )
                self.state.add_tool_result(tc.id, result)
                self._session_log_call("log_tool_result", tc.id, result)
                continue

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

            full_read = (
                tc.name == "ReadFile"
                and bool(args.get("raw"))
                and not args.get("start_line")
                and not args.get("end_line")
            )
            stored = result
            if should_vault_tool_output(tc.name, stored, raw_read=full_read):
                try:
                    stored = get_cache().offload_tool_output(stored)
                except Exception:
                    stored = result
            # Compact payloads (ReadFile summaries, Agent JSON) *are* the
            # record — slicing them mid-JSON drops the symbol map / findings
            # and forces extra reads. Vault already offloaded bulky
            # Bash/FetchUrl/ListDir. Only slice a vault-eligible body that
            # failed to offload.
            if (
                len(stored) > 6000
                and not full_read
                and not is_vault_pointer(stored)
                and tc.name in VAULT_TOOLS
            ):
                stored = (
                    stored[:5500]
                    + f"\n{Glyph.ELLIPSIS}[truncated for orchestrator context; "
                    "re-run the tool with a narrower scope for the rest]"
                )
            if not ok:
                try:
                    from .learn import catalog_nudge

                    extra = catalog_nudge(tc.name, result)
                except Exception:
                    extra = None
                if extra:
                    stored = stored + "\n" + extra
            self.state.add_tool_result(tc.id, stored)
            self._session_log_call("log_tool_result", tc.id, stored)
            pending_ws.append((tc, args, result))
        for tc, args, result in pending_ws:
            self._apply_working_set_from_tool(tc, args, result)

    def _apply_working_set_from_tool(self, tc: ToolCall, args: dict, result: str) -> None:
        """Local working-set updater. Compact does not maintain this object."""
        try:
            data = json.loads(result) if result else {}
        except json.JSONDecodeError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        ws = None
        try:
            if tc.name in WRITE_TOOLS and data.get("ok"):
                path = str(data.get("path") or args.get("path") or "")
                digest = ""
                if path:
                    from pathlib import Path as _Path

                    p = _Path(path)
                    if p.is_file():
                        digest = hashlib.sha256(p.read_bytes()).hexdigest()
                ws = note_file(path, digest)
            elif tc.name == "Bash" and looks_like_test_command(str(args.get("command") or "")):
                excerpt = str(data.get("stdout") or data.get("error") or "")[-400:]
                ws = note_verify(
                    str(args.get("command") or ""),
                    data.get("exit_code"),
                    excerpt,
                )
            elif tc.name == "TodoWrite" and data.get("ok"):
                ws = data.get("working_set") or load_working_set()
            if ws is not None:
                mirror_working_set(self.state.items, ws, self._sticky_goal_item())
                self._session_log_call("log_working_set", ws)
        except Exception:
            pass

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

    def _reseat_items_splitting_tool_pairs(self) -> None:
        """Park user/working-set items that split a function_call batch.

        DeepSeek requires every ``function_call_output`` for an open batch
        to arrive before the next user message. A working-set mirror (role
        user) appended after the first WriteFile of a parallel group is
        enough to 400 the following request. Items are moved, not rewritten,
        so prefix bytes of the legal prefix stay the same objects.
        """
        pending: set = set()
        kept: List[dict] = []
        stray: List[dict] = []
        for item in self.state.items:
            kind = item.get("type")
            if kind == "function_call":
                pending.add(item.get("call_id"))
                kept.append(item)
                continue
            if kind == "function_call_output":
                pending.discard(item.get("call_id"))
                kept.append(item)
                continue
            if pending and (
                is_working_set_item(item) or item.get("role") == "user"
            ):
                stray.append(item)
                continue
            kept.append(item)
        if stray:
            self.state.items = kept + stray

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

    def micro_compact(self, keep_recent: int = MICRO_KEEP_RECENT) -> int:
        """
        Layer 1: locally blank out old tool results. Free — no API call.
        Returns the number of characters reclaimed.

        Only the ``output`` text goes; the items themselves stay, because
        removing one would orphan its ``function_call`` and make the next
        request a 400. Already-cleared placeholders stay the same string so
        the prefix behind the moving blanking boundary stays still. The most
        recent keep-window results and the latest verbatim/raw ReadFile are
        left intact.
        """
        indices = [
            i
            for i, item in enumerate(self.state.items)
            if item.get("type") == "function_call_output"
        ]
        spare = set(indices[-keep_recent:] if keep_recent else [])
        raw_idx = self._most_recent_verbatim_read_index()
        if raw_idx is not None:
            spare.add(raw_idx)

        reclaimed = 0
        for i in indices:
            if i in spare:
                continue
            content = self.state.items[i].get("output") or ""
            if content == CLEARED_TOOL_RESULT:
                continue
            reclaimed += len(content)
            self.state.items[i]["output"] = CLEARED_TOOL_RESULT
        return reclaimed

    def _most_recent_verbatim_read_index(self) -> Optional[int]:
        """Index of the latest ReadFile raw=true or range-read output, if any."""
        calls: Dict[str, dict] = {}
        for item in self.state.items:
            if item.get("type") == "function_call":
                cid = item.get("call_id")
                if cid:
                    calls[cid] = item
        last: Optional[int] = None
        for i, item in enumerate(self.state.items):
            if item.get("type") != "function_call_output":
                continue
            call = calls.get(item.get("call_id") or "") or {}
            if call.get("name") != "ReadFile":
                continue
            try:
                args = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):
                continue
            if (
                args.get("raw") is True
                or args.get("start_line") is not None
                or args.get("end_line") is not None
            ):
                last = i
        return last

    def _tool_result_share(self) -> float:
        """Fraction of history characters that live in tool-result items."""
        breakdown = self.state.role_breakdown()
        total = sum(chars for _, chars in breakdown.values()) or 1
        return breakdown.get("tool", (0, 0))[1] / total

    def _already_compacted_short(self) -> bool:
        """Layer 3 already ran and the history has not grown past that shape."""
        if self.state.compactions <= 0:
            return False
        ceiling = self.state.compacted_len
        if ceiling is None:
            ceiling = 2 + COMPACT_KEEP_TAIL
        return len(self.state.items) <= ceiling

    def _should_auto_compact(self) -> bool:
        """Whether the automatic path may fire on this turn.

        A post-compact ``len//4`` gauge is not a trigger — wait for the next
        real orchestrator ``input_tokens``. An already-short compacted history
        is not rewritten a second time.
        """
        if not settings.auto_compact:
            return False
        if getattr(self.usage, "context_tokens_estimated", False):
            return False
        if self.context_fraction() < settings.compact_threshold:
            return False
        if self._already_compacted_short():
            return False
        return True

    async def _maybe_auto_compact(self) -> Optional[str]:
        """Auto path: window-pressure Layer 1, then Layer 3.

        Layer 1 blanks old tool results in place. That is a middle rewrite
        of a prefix that is already cacheable (DeepSeek Example 2), so it
        only runs when the context window itself is the constraint. Fat
        results are offloaded at append time (tool_vault) instead. Layer 3
        still waits for a real (not guessed) over-threshold gauge.
        """
        if not settings.auto_compact:
            return None

        tool_heavy = self._tool_result_share() > TOOL_SHARE_MICRO_THRESHOLD
        reclaimed = 0
        window_pressure = self._should_auto_compact()
        if not self._already_compacted_short() and should_micro_cached_prefix(
            window_pressure=window_pressure
        ):
            reclaimed = self.micro_compact()
        micro_report = None
        if reclaimed > 0:
            micro_report = (
                f"Micro-compacted stale tool results "
                f"(~{reclaimed // 4:,} tokens, no API cost)."
            )

        if not window_pressure:
            return micro_report

        # Tool-result bloat was the problem: wait for a real input_tokens
        # rather than rewriting history on the pre-blank gauge.
        if tool_heavy and reclaimed > 0:
            return micro_report

        return await self.compact()

    def _item_plain_text(self, item: dict) -> str:
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                (p.get("text") or "") for p in content if isinstance(p, dict)
            )
        return item.get("output") or ""

    def _is_user_item(self, item: dict) -> bool:
        if item.get("role") != "user":
            return False
        return item.get("type") not in (
            "function_call",
            "function_call_output",
            "web_search_call",
            "reasoning",
        )

    def _is_compact_summary_item(self, item: dict) -> bool:
        return self._is_user_item(item) and self._item_plain_text(item).startswith(
            COMPACT_SUMMARY_LEADIN.rstrip("\n")
        )

    def _is_working_set_item(self, item: dict) -> bool:
        return is_working_set_item(item)

    def _sticky_goal_item(self) -> Optional[dict]:
        """The original user goal — kept as the same dict across recomputes."""
        for item in self.state.items:
            if (
                self._is_user_item(item)
                and not self._is_compact_summary_item(item)
                and not self._is_working_set_item(item)
            ):
                return item
        return None

    def _compact_tail_items(self, head: Optional[dict]) -> List[dict]:
        """Last K original items, pairing-repaired, not duplicating the head.

        Returns the original dicts, never rebuilt copies, so unchanged tail
        objects keep identity for prefix cache and history-integrity tests.
        """
        items = self.state.items
        if not items:
            return []
        start = max(0, len(items) - COMPACT_KEEP_TAIL)
        keep: set[int] = set(range(start, len(items)))
        for i in list(keep):
            item = items[i]
            if head is not None and item is head:
                keep.discard(i)
                continue
            if self._is_compact_summary_item(item):
                keep.discard(i)
                continue
            if self._is_working_set_item(item):
                keep.discard(i)

        by_cid: Dict[str, List[int]] = {}
        for i, item in enumerate(items):
            if item.get("type") in ("function_call", "function_call_output"):
                cid = item.get("call_id")
                if cid:
                    by_cid.setdefault(cid, []).append(i)

        changed = True
        while changed:
            changed = False
            for i in list(keep):
                item = items[i]
                if item.get("type") not in ("function_call", "function_call_output"):
                    continue
                for j in by_cid.get(item.get("call_id") or "", ()):
                    if j in keep:
                        continue
                    other = items[j]
                    if head is not None and other is head:
                        continue
                    if self._is_compact_summary_item(other):
                        continue
                    if self._is_working_set_item(other):
                        continue
                    keep.add(j)
                    changed = True
        return [items[i] for i in sorted(keep)]

    def _working_set_item(self) -> Optional[dict]:
        for item in self.state.items:
            if self._is_working_set_item(item):
                return item
        return None

    def _build_compacted_items(self, summary: str) -> List[dict]:
        head = self._sticky_goal_item()
        ws_item = self._working_set_item()
        from .working_set import working_set_item_content

        current = working_set_item_content(load_working_set())
        if ws_item is None:
            ws_item = {"role": "user", "content": current}
        elif ws_item.get("content") != current:
            # Compact is a new prefix; refresh the snapshot from session_facts.
            ws_item["content"] = current
        summary_item = {
            "role": "user",
            "content": COMPACT_SUMMARY_LEADIN + summary,
        }
        tail = self._compact_tail_items(head)
        out: List[dict] = []
        if head is not None:
            out.append(head)
        out.append(ws_item)
        out.append(summary_item)
        out.extend(x for x in tail if x is not ws_item)
        return out

    def _build_post_turn_items(self, summary: str) -> List[dict]:
        """Next request ``input``: compact summary, not the prior transcript.

        Working-set item is kept (remaining work lives there). Original goal,
        tool pairs, and tail are dropped so the next turn starts on the
        standing instructions plus this summary.
        """
        ws_item = self._working_set_item()
        current = working_set_item_content(load_working_set())
        if ws_item is None:
            ws_item = {"role": "user", "content": current}
        elif ws_item.get("content") != current:
            ws_item["content"] = current
        summary_item = {
            "role": "user",
            "content": COMPACT_SUMMARY_LEADIN + summary,
        }
        return [ws_item, summary_item]

    def _render_transcript(self, max_chars: int = COMPACT_TRANSCRIPT_MAX_CHARS) -> str:
        """Flatten the item list into something a summarizer can read.

        When the transcript is longer than ``max_chars``, keep the *start*
        (goals, constraints) and the *recent tail*, and drop the middle.
        """
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
            content = self._item_plain_text(item)
            if content:
                parts.append(f"[{role}] {content[:4000]}")
        text = "\n\n".join(parts)
        if len(text) <= max_chars:
            return text
        marker = f"{Glyph.ELLIPSIS}{TRANSCRIPT_ELISION_MARKER}{Glyph.ELLIPSIS}"
        head_budget = max(1, max_chars // 2)
        tail_budget = max(1, max_chars - head_budget)
        if head_budget + tail_budget >= len(text):
            return text
        return f"{text[:head_budget]}\n\n{marker}\n\n{text[-tail_budget:]}"

    async def _summarize_history(self, instructions: str = "") -> tuple[Optional[str], Optional[str], float]:
        """Run the compact summarizer. Returns (summary, error_message, cost).

        Sends the live item list plus a short user prompt, with the same
        standing ``instructions`` / ``tools`` / ``reasoning.effort`` as the
        orchestrator loop. A live probe showed that shape hits the prefix;
        flattening into ``COMPACT_SYSTEM`` + effort ``none`` misses from
        token 0. The call is still metered as a subagent so it does not
        overwrite the orchestrator context gauge.
        """
        prompt = COMPACT_USER_PROMPT
        if instructions:
            prompt = f"Focus the summary on: {instructions}\n\n{prompt}"
        items = list(self.state.items) + [{"role": "user", "content": prompt}]
        body = self._request_body(items)
        try:
            with console.status("[accent]Compacting conversation…[/accent]", spinner="dots"):
                resp = await self.client.create(**body)
        except Exception as e:
            return None, f"Compaction failed: {e}", 0.0

        from .api import output_text

        delta = self.usage.record(
            settings.orchestrator_model, resp.get("usage"), scope="subagent"
        )
        summary_cost = delta.cost(settings.orchestrator_model)
        if any(it.get("type") == "function_call" for it in (resp.get("output") or [])):
            return None, "Compaction called a tool — history left untouched.", summary_cost
        summary = output_text(resp)
        if not summary.strip():
            return None, "Compaction produced an empty summary — history left untouched.", 0.0
        visible, handoff, _ = split_reply_meta(summary)
        summary = (handoff or visible or summary).strip()
        if len(summary) > COMPACT_SUMMARY_STORE_CHARS:
            summary = summary[:COMPACT_SUMMARY_STORE_CHARS].rstrip()
        return summary, None, summary_cost

    def _apply_compacted_items(self, items: List[dict], before_chars: int) -> None:
        self.state.items = items
        self._repair_pairing()
        self.state.compactions += 1
        self.state.compacted_len = len(self.state.items)
        try:
            self._session_log_call("log_compact", self.state.items, load_working_set())
        except Exception:
            pass
        after_chars = len(self._render_transcript(max_chars=10**9))
        saved = before_chars - after_chars
        # Guess for the status bar only. Auto-compact must not fire on this
        # figure — wait for the next real orchestrator input_tokens.
        self.usage.context_tokens = max(self.usage.context_tokens - max(saved, 0) // 4, 0)
        self.usage.context_tokens_estimated = True

    async def compact(self, instructions: str = "") -> str:
        """
        Layer 3: summarize the conversation on the standing prefix (same
        instructions, tools, and pinned effort as the loop) and replace the
        middle of history with that summary, keeping a byte-stable original-goal
        head and a pairing-intact recent tail.
        """
        if not self.state.items:
            return "Nothing to compact — the conversation is empty."

        before_items = len(self.state.items)
        before_chars = len(self._render_transcript(max_chars=10**9))
        inline = self._take_inline_handoff()
        used_inline = False
        if inline and not (instructions or "").strip():
            summary = inline
            summary_cost = 0.0
            used_inline = True
        else:
            summary, error, summary_cost = await self._summarize_history(instructions)
            if error:
                return error
            assert summary is not None

        self._apply_compacted_items(self._build_compacted_items(summary), before_chars)
        self._layer3_this_turn = True
        after_items = len(self.state.items)
        after_chars = len(self._render_transcript(max_chars=10**9))
        saved = before_chars - after_chars
        change = (
            f"~{saved // 4:,} tokens reclaimed"
            if saved > 0
            else f"no reduction — the summary is ~{-saved // 4:,} tokens larger"
        )
        cost_bit = (
            "same-response handoff, no API cost"
            if used_inline
            else f"summary cost {format_cost(summary_cost)}"
        )
        return (
            f"Compacted {before_items} items {Glyph.ARROW} {after_items} "
            f"({change} {Glyph.DOT} {cost_bit})."
        )

    def _post_turn_compact_extra_chars(self) -> int:
        """Characters that are not already a compact summary or working-set item."""
        n = 0
        for item in self.state.items:
            if self._is_compact_summary_item(item) or self._is_working_set_item(item):
                continue
            kind = item.get("type")
            if kind == "function_call_output":
                n += len(item.get("output") or "")
            elif kind == "function_call":
                n += len(item.get("arguments") or "") + len(item.get("name") or "")
            elif kind == "web_search_call":
                n += len(json.dumps(item.get("action") or {}))
            elif kind == "reasoning":
                # Scratch work, already billed as output. Counting it as leftover
                # bulk would compact a short think-heavy chat and bust the prefix
                # cache for no transcript to drop.
                continue
            else:
                n += len(self._item_plain_text(item))
        return n

    def _objective_completed(self) -> bool:
        """Local working-set judge. No model call."""
        try:
            return bool(judge_goal_completion().get("completed"))
        except Exception:
            return False

    def _existing_compact_summary_text(self) -> Optional[str]:
        lead = COMPACT_SUMMARY_LEADIN.rstrip("\n")
        for item in self.state.items:
            if not self._is_compact_summary_item(item):
                continue
            text = self._item_plain_text(item)
            if text.startswith(COMPACT_SUMMARY_LEADIN):
                return text[len(COMPACT_SUMMARY_LEADIN) :].lstrip("\n")
            if text.startswith(lead):
                return text[len(lead) :].lstrip("\n")
            return text
        return None

    def _conversation_excerpt(self, items: List[dict]) -> str:
        parts: List[str] = []
        for item in items:
            kind = item.get("type")
            if kind in POST_TURN_COMPACT_TOOL_TYPES or kind == "reasoning":
                continue
            if self._is_compact_summary_item(item) or self._is_working_set_item(item):
                continue
            text = self._item_plain_text(item)
            if not text.strip():
                continue
            role = item.get("role") or "assistant"
            parts.append(f"[{role}] {text[:4000]}")
        return "\n\n".join(parts)

    def _local_handoff_summary(self) -> str:
        """Build a handoff summary without a model call.

        Reuses a compact-summary item when one exists and appends this turn's
        user/assistant text. Tool rows are omitted — they are what the handoff
        exists to drop.
        """
        existing = (self._existing_compact_summary_text() or "").strip()
        start = getattr(self, "_items_at_turn_start", 0) or 0
        this_turn = self._conversation_excerpt(self.state.items[start:])
        if existing:
            if this_turn:
                return f"{existing}\n\n## Latest turn\n{this_turn}"
            return existing
        whole = self._conversation_excerpt(self.state.items).strip()
        return whole or "(empty session)"

    def _this_turn_added_tools(self) -> bool:
        new_items = self.state.items[self._items_at_turn_start :]
        return any(it.get("type") in POST_TURN_COMPACT_TOOL_TYPES for it in new_items)

    def _should_post_turn_compact(self) -> bool:
        """Whether a finished turn should drop history to system + summary.

        This is the handoff into the next orchestration, not Layer 3 (which
        keeps the sticky goal and a tail while the same objective is still
        open). Cache-aware: a hot prefix is cheaper to keep than to bust.
        See ``cost_policy.should_post_turn_compact``.
        """
        extra = self._post_turn_compact_extra_chars()
        window_pressure = False
        if not getattr(self.usage, "context_tokens_estimated", False):
            window_pressure = self.context_fraction() >= settings.compact_threshold
        return should_post_turn_compact(
            has_items=bool(self.state.items),
            objective_completed=self._objective_completed(),
            leftover_chars=extra,
            leftover_min_chars=POST_TURN_COMPACT_MIN_CHARS,
            window_pressure=window_pressure,
            last_hit_tokens=int(getattr(self.usage, "context_cache_hit_tokens", 0) or 0),
            last_miss_tokens=int(getattr(self.usage, "context_cache_miss_tokens", 0) or 0),
            layer3_this_turn=bool(self._layer3_this_turn),
            this_turn_added_tools=self._this_turn_added_tools(),
        )

    def _inline_handoff(self) -> str:
        """Compact summary attached to the last final answer, if any."""
        return (getattr(self, "_last_handoff", None) or "").strip()

    def _take_inline_handoff(self) -> str:
        """Return and consume the last final-answer handoff, if any."""
        text = self._inline_handoff()
        if text:
            self._last_handoff = None
        return text

    def _needs_post_turn_summarizer(self) -> bool:
        """Flash is only needed when there is no summary to reuse or fold."""
        if self._inline_handoff():
            return False
        if self._existing_compact_summary_text():
            return False
        if self._layer3_this_turn:
            return False
        return self._post_turn_compact_extra_chars() >= POST_TURN_COMPACT_MIN_CHARS

    def _persist_objective_log(self, summary: str) -> Optional[Path]:
        goal = ""
        try:
            ws = load_working_set()
        except Exception:
            ws = None
        if isinstance(ws, dict):
            goal = str(ws.get("goal") or "")
        if not goal:
            head = self._sticky_goal_item()
            if head is not None:
                goal = self._item_plain_text(head)[:240]
        completed: Optional[bool]
        try:
            completed = bool(judge_goal_completion(ws).get("completed")) if ws is not None else self._objective_completed()
        except Exception:
            completed = None
        try:
            path = write_objective_log(
                summary,
                working_set=ws,
                goal=goal,
                completed=completed,
            )
        except Exception:
            return None
        self._last_objective_log = path
        return path

    async def _post_turn_compact(self) -> Optional[str]:
        """Replace conversation input with a compact summary after a finished turn.

        The next request's ``input`` is the summary (plus a working-set item if
        one was already in history), not the prior tool-call transcript.
        Instructions and tools stay byte-stable. Skipped when
        ``_should_post_turn_compact`` is false.

        A summarizer call runs only when leftover history is large *and* no
        same-response ``[handoff]`` or compact-summary item exists to reuse.
        Layer 3 just having run, or a small tool turn, is a local fold or a
        no-op — not a second Flash pass. On a successful replace, the summary
        is written to ``.g023/logs/{dstamp_tstamp}.log`` for ``--continue`` /
        ``/resume log``.
        """
        if not self._should_post_turn_compact():
            return None
        before_items = len(self.state.items)
        before_chars = len(self._render_transcript(max_chars=10**9))
        summary_cost = 0.0
        used_summarizer = False
        used_inline = False
        inline = self._take_inline_handoff()
        if inline:
            summary = inline
            used_inline = True
        elif self._needs_post_turn_summarizer():
            summary, error, summary_cost = await self._summarize_history()
            if error or not summary:
                summary = self._local_handoff_summary()
            else:
                used_summarizer = True
        else:
            summary = self._local_handoff_summary()
        if not summary or not str(summary).strip():
            return None
        self._apply_compacted_items(self._build_post_turn_items(summary), before_chars)
        # Input list was replaced, so the next request is a new prefix. Apply
        # a stored [next_effort] now — doing it on a hot continuation misses
        # standing instructions+tools from token 0, and output is ~200× a hit.
        self.release_prefix_effort()
        after_items = len(self.state.items)
        log_path = self._persist_objective_log(summary)
        if used_summarizer:
            cost_bit = f"summary cost {format_cost(summary_cost)}"
        elif used_inline:
            cost_bit = "same-response handoff, no API cost"
        else:
            cost_bit = "local fold, no API cost"
        report = (
            f"Post-turn compact {before_items} items {Glyph.ARROW} {after_items} "
            f"({cost_bit})."
        )
        if log_path is not None:
            try:
                rel = log_path.relative_to(get_project_root())
                shown = rel.as_posix()
            except ValueError:
                shown = str(log_path)
            report += f" Handoff {shown}."
        return report

"""
Orchestrator — the high-level reasoning loop.

Runs on DeepSeek V4.1 Flash over DeepSeek's Anthropic-format endpoint
(``/anthropic/v1/messages``), through ``api.py`` + ``anthropic_codec``.
Web access is two client tools, ``WebSearch`` and ``FetchUrl``, served by DeepSeek's Search and Fetch
APIs through the machine-wide cache (``web.py``). ``web_search_call`` items from older sessions,
recorded when search ran server-side, still replay verbatim.

The conversation is stored as *items* (the codec's internal representation)
rather than chat messages —
``function_call`` / ``function_call_output`` pairs, plus the model's own
``reasoning``, ``message`` and ``web_search_call`` items echoed back verbatim.
Echoing them unchanged is both the highest-fidelity way to carry history and the
most prefix-cache friendly, since the prefix stays byte-identical between turns.

Keeps context clean with local helpers for heavy reads (model-backed subagents
only when ``settings.subagents_enabled``), compacts at ``compact_at_tokens``
(200k) — also mid-turn — and narrates
what it is doing while it does it. Every tool call is timed, summarised, and
folded into a :class:`~g023_code.ui.TurnTrace` so the CLI can close each turn
with an honest account of where the time and the money went.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from rich.live import Live
from rich.markdown import Markdown
from rich.text import Text

from . import finish, hops, multi_edit, orient, ownership, project_instructions, rename as rename_mod
from . import preread as preread_mod
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
from .attribution import Preimages, attribute, shadow_run, strip_view_filters
from . import views as views_mod
from . import callees as callees_mod
from . import testoracle
from . import obligations as obligations_mod
from . import claims as claims_mod
from rich.markup import escape
from . import local_llm, salience, scout as scout_mod
from .cache import get_cache, is_vault_pointer
from .tool_codec import encode_tool_result
from .economics import CompactionMeter
from .cost_policy import (
    VAULT_TOOLS,
    should_micro_cached_prefix,
    should_post_turn_compact,
    should_vault_tool_output,
)
from .config import (
    ITERATION_NOTICE_EVERY,
    MODEL,
    MAX_OUTPUT_CONTINUES,
    UNLIMITED_OUTPUT_TOKENS,
    get_project_root,
    settings,
)
from .checkpoint import fold_summary, is_rehydrate_item, reconcile, rehydrate_note
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
from .ledger import Ledger
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

Core philosophy: Context is Currency. Spend it on what the task needs.

You NEVER dump raw file contents or large search results into your own context.
Instead you always use the provided tools:
- ReadFile → returns a compact structural summary + metadata (use this instead of asking for full files);
  long text files and big sources get a folded outline (sections / symbols with line spans).
  pass focus="<symbol | exact phrase | section path | topic>" to get that part located locally,
  with its section path, start_line/end_line for a range, or raw=true for the FULL file verbatim
- SearchContent → returns grep-style hits ranked definitions-first (total count plus a sample)
- Glob → find files by glob/brace pattern (e.g. *.{ts,js})
- TodoWrite → update the working-set leaves (remaining work lives there)
- WebSearch → web search (DeepSeek Search API). Use it whenever the answer depends on
  current facts, or on a library or API you are not certain about. One precise query
  beats several vague ones; results are cached machine-wide, so a repeat is free.
- FetchUrl → read one page as Markdown. Long pages return a window plus an outline; pass
  focus="<topic>" for just the matching sections, or offset=N to continue. Hits marked
  "page cached" in WebSearch results cost nothing to fetch.
- Bash / WriteFile / ListDir for direct actions

You work alone: there are no subagents unless the user turns them on with
/subagents on. Do the reading, searching and editing yourself.

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
browser, MCP, a local model server, or other machine-local setup. LearnGet loads one guide body. Do not
treat a guide as permission to run a blocked tool.

Project facts from earlier sessions live in .g023/memory/ (MEMORY.md and topics/).
They are not in this prompt. SearchContent can read that directory. Do not copy those
notes into standing instructions.

The project bulletin board is a forum at .g023/bbs/ for this session and future
sessions. Topic slugs are subdirectories; conversations are .txt files with slug
filenames. You (and any subagents, when enabled) write there to pass insights, discussions,
and durable findings both ways. Helper tools live in .g023/bbs/bbs_tools/; keep
.g023/bbs/tools_info.md in sync with those tools, and keep .g023/bbs/GUIDE.md
accurate so later sessions can use the board efficiently. Posts are not in this
prompt — SearchContent can read .g023/bbs/, or ListDir/ReadFile that path. Do not
copy posts into standing instructions.

Surface defects as you go. Do not treat an objective as complete while a surfaced
issue is still open. An issue here is a defect you have evidence for: a failing
command, a broken path, or a contradiction with the objective. A polish idea, or
a file the objective did not ask for, is not one. A failure a check's attribution
field marks pre_existing was not caused by this turn: name it in the answer
rather than investigating it, unless the request covers it.

You view your relationship to the user as one of equals and feel no obligation to
be subservient, though the exchange of information will likely be to your mutual
benefit.

SessionSearch greps this project's .g023/sessions/*.jsonl, including text dropped from
the current view by compact. Omitted write bodies come back as hashes, not contents.

Every model call replays the whole context, so when several tool calls do not depend on each
other's results, issue them together in one response rather than one per response.

Same-response trailers (after the user-visible answer, nothing after them):
When the user message ends with the line [handoff due], or a TodoWrite result in
this turn carries "[handoff due]", emit
[handoff]
## Goals
## Files
## Decisions
## Done
[/handoff]
with specific paths and names. It replaces the whole transcript at compaction, so make it cumulative: cover the whole session so far, not just this turn. Keep it terse. Omit remaining-work guesses (the working-set item holds those). Otherwise omit the block: it is only read when a compaction is about to run, and the orchestrator marks those turns.
Then end with exactly one last line:
[next_effort: N]
N is an integer 0-75 for how hard the NEXT user request looks — not this turn's remaining tool steps. 75 is the thinking-on default and the ceiling (values above 75 are capped to 75); use 25-50 for routine follow-up; 0 to disable thinking. Pick the number that matches the next request; do not default to 75. Do not mention these trailers in the answer. If you omit next_effort, the current effort stays as-is.
"""


# Lean prompt (setting ``lean_prompt``): the contradiction with the batching
# rule goes, and final answers report only what the trace does not show.
CONTRADICTION_LINE = "Prefer multiple precise tool calls over one giant request.\n"
LEAN_PROMPT_ADDENDUM = """
Final answers: the user already sees every edit, file and command result in the tool
trace. Report the outcome and only what the trace does not show: decisions, surprises,
failures left unfixed. Do not restate diffs, list edited lines, or quote passing output.
"""
CLOSURE_ADDENDUM = """
An Edit result's refs_left counts whole-name occurrences left in the project of names and
numbers the edit removed (0 = none left, else the sites); trust it instead of re-searching.
"""


def lean_system(text: str) -> str:
    """The standing prompt with the lean edits applied (idempotent)."""
    if text.endswith(LEAN_PROMPT_ADDENDUM):
        return text
    return text.replace(CONTRADICTION_LINE, "") + LEAN_PROMPT_ADDENDUM


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

SUBAGENT_SYSTEM_ADDENDUM = """
Subagents are ON for this session (/subagents off to disable):
- Agent → spawn research, planning, or todos subagents (explore/plan still work).
  research relays findings; planning relays a plan; todos writes a list,
  orchestrates task workers (each receives issues from the previous task),
  and relays a final summary. Pass prior/issues so workers communicate both ways.
  Delegate only work that is large and separable; a subagent costs a fresh context.
"""

VISION_SYSTEM_ADDENDUM = """
- AnalyzeImage → vision model ({model}) for screenshots, diagrams, and photos of code.
  You cannot see images yourself; always use AnalyzeImage and ask a specific question.
"""

LOCAL_MODEL_ADDENDUM = """
Local helper model is ON (/local off to disable):
- LocalAsk → this machine's small local model reads files for free (no paid tokens)
  and returns a short lead plus verbatim cited lines. Use it for bulk reading you would
  otherwise page through (long logs, large files, .g023/vault/ outputs), or with no paths
  for "where is X handled" in the whole project; verify the lead by its cited lines.
- A [scout: ...] block on a request is the local model's pre-read: verbatim lines that
  probably locate the request. Start from them instead of searching; check before editing.
- Do NOT use LocalAsk when the task involves GPU work (CUDA, training or running ML
  models, GPU benchmarks, llama.cpp/ollama/vllm): it competes for the same GPU. The
  harness also releases the GPU on its own when a command looks like GPU work.
"""

VISION_DISABLED_ADDENDUM = """
You have no vision capability in this session. If the user references an image,
tell them to run /vision on to enable image analysis.
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

# Identical tool+args in one turn. The third call is not executed (the earlier
# result is still in history). The fourth stops the turn before another model
# call — a retry is sometimes real, a loop of the same call is not.
REPEAT_TOOL_WARN = 3
REPEAT_TOOL_STOP = 4


def _tool_call_fingerprint(name: str, args: dict) -> str:
    try:
        blob = json.dumps(args, sort_keys=True, default=str, separators=(",", ":"))
    except TypeError:
        blob = str(args)
    return f"{name}\0{blob}"


# Epoch-aware repeat guard. "Same call" must mean same call against the same
# world: `pytest` -> Edit -> `pytest` -> Edit -> `pytest` is the verify loop,
# not a loop to stop. A successful write (Edit/WriteFile, or a Bash command
# that plausibly mutates files) advances the write epoch, and read/run calls
# are keyed on (epoch, call). Write calls themselves stay epoch-free: the same
# Edit three times is a loop whatever the epoch.
_BASH_MUTATES = re.compile(
    r"\bsed\s+(?:-[a-zA-Z]*\s+)*-[a-zA-Z]*i|\bperl\s+-[a-zA-Z]*i|"
    r"\b(?:tee|mv|cp|rm|touch|patch|mkdir|ln|chmod|install)\s|"
    r"\bgit\s+(?:apply|checkout|restore|stash|reset|mv|rm)\b|"
    r"\bwrite_(?:text|bytes)\(|\bopen\([^)]*,\s*['\"][wax]"
)
# A shell redirect to a file. Checked on the command with quoted text blanked,
# so `print('a -> b')` inside `python -c "..."` is not a redirect.
_BASH_REDIRECT = re.compile(r"(?<![0-9&>])>>?\s*(?!&|/dev/null)[^\s|&;]")
_QUOTED = re.compile(r"'[^']*'|\"(?:\\.|[^\"\\])*\"")


def bash_may_write(command: str) -> bool:
    command = command or ""
    if _BASH_MUTATES.search(command):
        return True
    unquoted = _QUOTED.sub(lambda m: m.group(0)[0] * 2, command)
    return bool(_BASH_REDIRECT.search(unquoted))


def _repeat_key(name: str, args: dict, epoch: int) -> str:
    fp = _tool_call_fingerprint(name, args)
    return fp if name in WRITE_TOOLS else f"{epoch}\0{fp}"


# Verify replay (anticipatory receipt). After a batch whose write succeeded,
# the next observation the model needs is the result of the check that was
# failing; it is predictable and locally free, so it is attached to the
# write's own result instead of costing a hop (which re-buys ~200 reasoning
# tokens and re-reads the prefix). Only check-class commands (tests, type
# checkers, linters) that already ran this turn, failed, finished quickly
# and that the user lets run without confirmation.
VERIFY_REPLAY_MAX_MS = 30_000
VERIFY_REPLAY_TAIL_CHARS = 900


def environment_line() -> str:
    """Where commands run, frozen with the standing prompt (prefix-stable).

    Measured: without it the model guessed `cd /app` / `/home/user` and typed
    `python` on python3-only machines, each costing a hop. Last in the
    instructions, so the shared base prefix is unchanged.
    """
    import platform
    import shutil as _shutil

    shell = "bash" if _shutil.which("bash") else "sh"
    py = "python3" if _shutil.which("python3") else ("python" if _shutil.which("python") else "none")
    if py == "python3":
        py += " (`python` works too)"  # registry shim when python is absent
    # No path here: a per-project path in the instructions forks the prompt
    # cache before the tool schemas (measured: call-1 hit 1.4k / miss 3.9k
    # tokens in every new project). "Relative paths" is the actionable part.
    return (
        "\n\n## Environment\nThe working directory is the project root: relative paths resolve "
        "there and no `cd` is needed.\n"
        f"OS: {platform.system()}; shell: {shell} -o pipefail; python: {py}.\n"
    )


class _ReplayCall:
    name = "Bash"


def _json_dict(text: str) -> dict:
    try:
        data = json.loads(text) if text and text.lstrip().startswith("{") else {}
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}

# Session-compact policy. The summarizer sees the start of the session (goals)
# and the recent tail, not only a truncated tail of tool noise. After a compact
# the original user-goal item stays byte-stable (prefix cache) and recent
# function_call / function_call_output pairs stay as the original dicts.
COMPACT_TRANSCRIPT_MAX_CHARS = 60_000
# Dense 5-heading summary. The compact call sends no output cap (adding
# max_output_tokens would bust the standing prefix); the stored summary is
# clipped instead, so a rambling reply cannot bloat the next turn.
COMPACT_SUMMARY_STORE_CHARS = 8_000
COMPACT_KEEP_TAIL = 12
MICRO_KEEP_RECENT = 6
TOOL_SHARE_MICRO_THRESHOLD = 0.4
COMPACT_SUMMARY_LEADIN = (
    "Summary of the conversation so far (the full history was compacted "
    "to save context):\n\n"
)
TRANSCRIPT_ELISION_MARKER = "[middle transcript elided]"
# Appended (once) to every compact summary item: where the verbatim history
# lives and how to get it back. Compaction is lossy; the session JSONL is not.
RECALL_MARKER = "\n\n[recall] "


def with_recall_pointer(summary: str, transcript: Optional[str] = None) -> str:
    """``summary`` plus one recall line naming the on-disk transcript."""
    base = (summary or "").split(RECALL_MARKER, 1)[0].rstrip()
    where = transcript or ".g023/sessions/ (newest *.jsonl)"
    return (
        f"{base}{RECALL_MARKER}The full pre-compaction transcript is on disk at "
        f"{where}. When an exact detail matters (a path, an error, a command's "
        "output, a decision's reason) and it is not above, call SessionSearch "
        "with a distinctive keyword instead of guessing."
    )
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
# The in-flight request, restated when a mid-turn compaction dropped its item.
MID_TURN_RESTATE_LEADIN = "[continuing after mid-turn compaction] The request in progress is:\n"
MID_TURN_RESTATE_TAIL = "\nContinue from the summary above; do not restart finished work."
# Just-in-time handoff: the [handoff] trailer is output (the dearest token)
# and only a compaction reads it, so it is asked for only on turns after which
# one is forecast (1.5.0: 5-8% of a tool turn's cost, never read).
HANDOFF_DUE_MARKER = "[handoff due]"
# Forecast horizon in turns of observed rent/growth, and the window margin.
HANDOFF_FORECAST_TURNS = 2.0
HANDOFF_WINDOW_MARGIN = 0.9
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


def _write_paths(args: dict) -> list:
    """Files an Edit/WriteFile call writes: its path, plus any ``more_edits`` item paths."""
    if args.get(multi_edit.FIELD):
        return multi_edit.paths(args)
    path = str(args.get("path") or "")
    return [path] if path else []
# Tools after which the model knows a file's current text (views.py).
VIEW_TOOLS = WRITE_TOOLS | {"ReadFile"}

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


class StreamCutOff(Exception):
    """The SSE stream ended before a terminal event: retry the call unstreamed."""


@dataclass
class Reply:
    """A parsed model response (item-shaped, via the codec)."""

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
        self.ledger = Ledger()
        self.usage = get_usage()
        self.printer = printer or ActivityPrinter(root=str(get_project_root()))
        self.trace = TurnTrace()
        self._scout: Optional[dict] = None
        self._scout_touched: list = []
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
        self.last_rehydrate: Optional[dict] = None
        self._last_handoff: Optional[str] = None
        self._handoff_requested = False
        # Per-turn rent (USD) and context growth (tokens), EMA. Feed the
        # just-in-time handoff forecast.
        self._turn_rent_ema = 0.0
        self._turn_growth_ema = 0.0
        self._turn_gauge: Optional[tuple[float, int, int]] = None
        self._next_effort_applied = False
        # Wire ``reasoning.effort`` is pinned to the first request of this
        # prefix. A live probe (2026-09-14) found high→low on a continuation
        # misses from token 0, including standing instructions and tools.
        # ``[next_effort]`` still updates settings; the wire follows when the
        # prefix is already new: /thinking, /clear, or a post-turn compact
        # that replaced the input list. /goal releases only before its own
        # turn so that turn can send the ceiling, then leaves the pin so the
        # warmed transcript still hits. Output is ~200× a cache hit, so
        # retuning on a prefix that is already new is the remaining bill.
        self._prefix_effort_wire: Optional[str] = None
        # Ski-rental compaction: rent paid re-reading history since the last
        # compaction vs. the price of compacting (economics.py).
        self.compaction_meter = CompactionMeter()
        # Temperature is also a cache-key field. Pin it with the
        # standing prefix so a /settings mutation cannot miss from token 0.
        self._prefix_temperature: Optional[float] = None
        self._tool_repeat_counts: dict[str, int] = {}
        self._write_epoch = 0
        self._failing_check: Optional[tuple[str, float]] = None
        self._preimages = Preimages()
        self._turn_written: set = set()  # files this turn wrote (claimed-write guard)
        # Test oracle bookkeeping (testoracle.py): this turn's test runs, the
        # vacuity verdicts already computed.
        self._turn_checks: list[dict] = []
        self._vacuity_seen: dict = {}
        self._oracle_summaries: dict[str, str] = {}
        self._write_seq = 0  # successful writes this turn, in program order (claims.py)
        # Request obligations / write guards (obligations.py), per turn.
        self._obligations = None
        self._obligation_nudged = False
        self._guard_refused: set[str] = set()
        # Last text the model saw per file, for Bash view deltas (views.py).
        self._views = views_mod.ViewStore()
        self._stop_repeated_tools = False
        self._run_demanded = False
        self._budget = hops.BudgetState()
        self.turn_failed = False
        self._turn_user_item: Optional[dict] = None
        self.committed_answer: Optional[str] = None
        # Predicated finish (finish.py): this batch's decision, whether the
        # current turn ended on one, and session counts for /cost.
        self._finish_decision = finish.Decision()
        self._finished_by_tool = False
        self.finish_stats = {"delivered": 0, "discarded": 0}
        self.hop_stats = {"skipped": 0, "checks": 0}
        try:
            from . import bbs as bbs_mod

            bbs_mod.ensure_layout()
        except OSError:
            pass

    def reset(self):
        self.state = OrchestratorState()
        self.ledger.clear()
        self.usage.reset()
        self.finish_stats = {"delivered": 0, "discarded": 0}
        self.hop_stats = {"skipped": 0, "checks": 0}
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
        self.last_rehydrate = None
        self._last_handoff = None
        self._handoff_requested = False
        self._turn_rent_ema = 0.0
        self._turn_growth_ema = 0.0
        self._turn_gauge = None
        self._next_effort_applied = False
        self._prefix_effort_wire = None
        self._prefix_temperature = None
        self._tool_repeat_counts = {}
        self._write_epoch = 0
        self._failing_check = None
        self._preimages.reset(get_project_root())
        self._views = views_mod.ViewStore()  # /clear: the model has seen nothing
        self._stop_repeated_tools = False
        self._budget = hops.BudgetState()
        self.turn_failed = False
        self._turn_user_item: Optional[dict] = None
        self.committed_answer: Optional[str] = None
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
        # The session log is this machine's own record, so only drift is checked (no signature).
        # Earlier notes stay (rewriting history mid-prefix busts the cache); an identical repeat is skipped.
        note = self._rehydrate(ws, None)
        last = next((i for i in reversed(self.state.items) if is_rehydrate_item(i)), None)
        if note and (last is None or last.get("content") != note):
            self.state.add_user(note)
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
        note = self._rehydrate(ws, rec.get("provenance"))
        if note:
            items.append({"role": "user", "content": note})
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

    def _rehydrate(self, ws: Optional[dict], provenance: Optional[str]) -> Optional[str]:
        """Check a saved state against the disk; keep the verdict for the CLI and return the model's note."""
        try:
            drift = reconcile(ws)
        except Exception:
            drift = {"changed": [], "missing": [], "verify_stale": False, "checked": 0}
        note = rehydrate_note(drift, provenance)
        self.last_rehydrate = {"provenance": provenance, "drift": drift, "note": note}
        return note

    def _items_from_handoff(self, summary: str, ws: Optional[dict] = None) -> List[dict]:
        out: List[dict] = []
        if ws is not None:
            out.append({"role": "user", "content": working_set_item_content(ws)})
        if summary:
            body = summary if summary.startswith(COMPACT_SUMMARY_LEADIN.rstrip("\n")) else (
                COMPACT_SUMMARY_LEADIN + with_recall_pointer(summary, self._transcript_path())
            )
            out.append({"role": "user", "content": body})
        return out

    def handle_interrupt(self) -> None:
        """Pairing-safe rollback used when the user interrupts a turn.

        A committed turn (final answer recorded) is not rolled back: only the
        post-turn maintenance after it was interrupted.
        """
        if getattr(self, "committed_answer", None) is None:
            self._rollback_turn("interrupted by the user")
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
            base = ORCHESTRATOR_SYSTEM + VISION_SYSTEM_ADDENDUM.format(model=MODEL)
        else:
            base = ORCHESTRATOR_SYSTEM + VISION_DISABLED_ADDENDUM
        if getattr(settings, "subagents_enabled", False):
            base += SUBAGENT_SYSTEM_ADDENDUM
        if getattr(settings, "local_enabled", False):
            base += LOCAL_MODEL_ADDENDUM
        base += ownership.system_addendum()
        base += finish.system_addendum()
        if getattr(settings, "lean_prompt", False):
            base = lean_system(base)
        if getattr(settings, "closure_receipts", False):
            base += CLOSURE_ADDENDUM
        base = hops.system_text(base)
        frozen = getattr(self, "_frozen_addendum", None)
        if frozen is None:
            frozen = project_instructions.system_addendum() + environment_line()
            self._frozen_addendum = frozen
        return base + frozen

    def _instructions_key(self) -> tuple:
        return (
            bool(settings.vision_enabled),
            bool(getattr(settings, "subagents_enabled", False)),
            ownership.mode(),
            finish.enabled(),
            bool(getattr(settings, "lean_prompt", False)),
            bool(getattr(settings, "closure_receipts", False)),
            bool(getattr(settings, "local_enabled", False)),
            hops.prompt_key(),
        )

    def _tools_key(self) -> tuple:
        return (
            bool(settings.vision_enabled),
            bool(getattr(settings, "subagents_enabled", False)),
            finish.enabled(),
            bool(getattr(settings, "local_enabled", False)),
            hops.check_enabled(),
            multi_edit.enabled(),
            rename_mod.enabled(),
        )

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
            schemas = self.registry.get_schemas()
            if finish.enabled():
                schemas = finish.add_to_schemas(schemas)
            if hops.check_enabled():
                schemas = hops.add_check_to_schemas(schemas)
            if multi_edit.enabled():
                schemas = multi_edit.add_to_schemas(schemas)
            if rename_mod.enabled():
                schemas = rename_mod.add_to_schemas(schemas)
            self._frozen_tool_schemas = schemas
            self._frozen_tool_key = key
        return list(self._frozen_tool_schemas)

    # ------------------------------------------------------------------
    # Model calls
    # ------------------------------------------------------------------

    def release_prefix_effort(self) -> None:
        """Drop the pinned wire effort so the next request re-reads settings.

        Used when the next request is already a new prefix: /thinking,
        /clear, the start of /goal (so that turn is not stuck on a lower
        alias), and a post-turn compact that has replaced history. Not used
        when /goal returns — the transcript it just warmed is still the
        prefix. Automatic ``[next_effort]`` must not call this on a hot
        continuation — retuning the alias mid-prefix misses from token 0.
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
            "model": MODEL,
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
        try:
            with console.status(f"[accent]{label}[/accent]", spinner="dots") as status:
                # Wake the moment the response lands; the timeout only paces the
                # seconds counter. (A sleep-then-check loop added up to 250 ms to
                # every blocking call.)
                while not task.done():
                    await asyncio.wait({task}, timeout=0.25)
                    if task.done():
                        break
                    status.update(
                        f"[accent]{label}[/accent] "
                        f"[muted]{Glyph.DOT} {time.perf_counter() - started:.1f}s[/muted]"
                    )
        finally:
            # Lifetime: the request belongs to this call. asyncio.wait does not
            # cancel what it waits on, so a cancelled caller (Ctrl-C) used to
            # leave the request running — billed, and its reply discarded.
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
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

        # A cut-off stream is a transport failure, not an answer: the partial
        # text must not become the turn's final reply. _call_model retries it
        # once unstreamed (the request had no side effects).
        if final is None:
            raise StreamCutOff("the stream ended before the response completed")
        if (final.get("incomplete_details") or {}).get("reason") == "stream_closed":
            raise StreamCutOff("the stream closed before the response completed")
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
        next user request misses from token 0. ``/thinking``, ``/clear``, and
        a post-turn compact that replaced history release the pin. ``/goal``
        releases only before its own turn. Sets ``_next_effort_applied`` so
        ``/goal`` can keep the trailer instead of restoring the pre-goal dial.
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
        self._apply_final_meta(content_handoff or comment_handoff or item_handoff, parsed)

    def _apply_final_meta(self, handoff: Optional[str], parsed: Optional[int]) -> None:
        """Store a final answer's handoff and apply its next-effort hint (see above)."""
        self._last_handoff = handoff
        if parsed is None:
            return
        self._next_effort_applied = True
        # Transient: the hint drives the next request, never config.json.
        if parsed <= 0:
            settings.set_transient("thinking_enabled", False)
            settings.set_transient("reasoning_effort", 0)
            return
        settings.set_transient("thinking_enabled", True)
        settings.set_transient("reasoning_effort", parsed)

    def _deliver_finish(self, decision: "finish.Decision") -> str:
        """End the turn on a pre-written answer whose step succeeded.

        Trailers are parsed exactly as on a normal final reply. The answer is
        not streamed, so the CLI prints it.
        """
        answer, handoff, effort = split_reply_meta(decision.answer or "")
        decision.answer = answer.strip() or "Done."
        self._apply_final_meta(handoff, effort)
        self._finished_by_tool = True
        self._streamed_answer = False
        self.finish_stats["delivered"] += 1
        return decision.text()

    # ------------------------------------------------------------------
    # The turn
    # ------------------------------------------------------------------

    async def run_turn(self, user_input: str) -> str:
        self.usage.start_turn()
        self.trace = TurnTrace()
        self._finished_by_tool = False
        self._finish_decision = finish.Decision()
        self._step_intent = ""
        local_llm.new_turn()
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
        self._tool_repeat_counts = {}
        self._write_epoch = 0
        self._failing_check = None
        self._preimages.reset(get_project_root())
        self._turn_checks = []
        self._vacuity_seen = {}
        self._oracle_summaries = {}
        self._write_seq = 0
        self._obligation_nudged = False
        self._run_demanded = finish.enabled() and finish.run_demanded(user_input)
        self._request_text = user_input
        self._escrow = None
        self._turn_written: set = set()
        self._guard_refused = set()
        self._obligations = None
        if settings.obligations or settings.write_guards:
            try:
                self._obligations = obligations_mod.extract(user_input)
            except Exception:
                self._obligations = None
        self._stop_repeated_tools = False
        self._budget = hops.BudgetState()
        self.turn_failed = False
        # Turn transaction state. ``_turn_user_item`` anchors rollback;
        # ``committed_answer`` is set once the final answer is in history (the
        # commit point): after it, an interrupt must not undo the turn.
        self._turn_user_item = None
        self.committed_answer = None
        report = await self._maybe_auto_compact()
        self._items_at_turn_start = len(self.state.items)
        if report:
            # Layer-1 blanking of a cached prefix only runs at window
            # pressure (see cost_policy). Don't banner a micro as
            # "auto-compacting" unless we are at threshold or just rewrote.
            over = self.over_compact_trigger()
            if over or report.startswith("Compacted"):
                console.print(
                    f"[warn]Context at {self.usage.context_tokens:,} tokens "
                    f"{Glyph.ARROW} auto-compacting.[/warn]"
                )
            console.print(f"[muted]{report}[/muted]")

        self._handoff_requested = self._handoff_due_forecast()
        self._turn_gauge = (
            self.compaction_meter.rent,
            int(self.usage.context_tokens or 0),
            self.state.compactions,
        )
        where = ""
        if orient.enabled():
            try:
                found = await asyncio.to_thread(orient.card, user_input, get_project_root())
            except Exception:  # orientation is a hint; never fail a turn on it
                found = None
            if found:
                where = "\n\n" + found
        scouted = await self._scout_card(user_input)
        if scouted:
            where += "\n\n" + scouted
        drift = ""
        if views_mod.enabled():
            try:
                found = await asyncio.to_thread(self._views.drift)
            except Exception:
                found = None
            if found:
                drift = "\n\n[files changed since you last saw them, not by your tool calls]\n" + found
        pre_mode, pre_plan = await self._preread_plan(user_input)
        prose = ""
        if pre_mode == "prose" and pre_plan:
            prose = await self._preread_prose(pre_plan)
        self.state.add_user(
            user_input + where + drift + prose
            + ("\n\n" + HANDOFF_DUE_MARKER if self._handoff_requested else "")
        )
        self._turn_user_item = self.state.items[-1]
        self._session_log_call("log_user", self.state.items[-1])
        self.state.turn += 1
        if pre_mode == "ghost" and pre_plan:
            await self._preread_ghost(pre_plan)

        # Read per turn, so /steps takes effect on the next thing you ask.
        # 0 is "no limit": count up forever and let the model, or Ctrl-C, end it.
        configured = max(0, int(settings.max_iterations))
        steps = itertools.count(1) if not configured else iter(range(1, configured + 1))

        try:
            return await self._run_turn_loop(
                user_input, steps, configured, turn_started
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Ctrl-C arrives as CancelledError under asyncio (3.11+); a
            # synchronous KeyboardInterrupt can still land in sync code.
            self.handle_interrupt()
            raise
        finally:
            # Invariant: replaying the session log yields this history.
            self._session_log_call("checkpoint", self.state.items)

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
            if self._stop_repeated_tools:
                final_content = (
                    "Stopped: the same tool call with the same arguments was "
                    "repeated and was not making progress."
                )
                self._streamed_answer = False
                break
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
                # Close the turn so the next one starts on legal history.
                self._rollback_turn(f"stopped by an API error: {e}")
                self._session_log_call("log_interrupt", self.state.items)
                self.turn_failed = True
                return f"API error: {e}"
            self.trace.thinking_ms += (time.perf_counter() - think_started) * 1000

            self._apply_reply_meta(reply)
            self.state.add_output(reply.output)
            self._session_log_call("log_output", reply.output)
            delta = self.usage.record(MODEL, reply.usage, scope="orchestrator")
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
                    format_cost(delta.cost(MODEL)),
                )

            if hops.budget_enabled():
                self._budget.observe(iteration, reply.reasoning or "", reply.commentary or "", reply.content or "")

            if reply.tool_calls:
                # What this step said it is looking for: the free query that
                # picks the lines a vaulted result keeps (salience.py).
                self._step_intent = salience.step_intent(
                    reply.reasoning or "", reply.commentary or "", user_input
                )
                await self._run_tool_calls(reply.tool_calls)
                if self._finish_decision.deliver:
                    final_content = self._deliver_finish(self._finish_decision)
                    break
                await self._maybe_mid_turn_compact(user_input)
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

            note = await self._obligation_probe()
            if note is not None:
                console.print("[muted]Harness check: the request names a callable the code does not "
                              "have in that shape. Continuing once.[/muted]")
                self.state.add_user(note)
                self._session_log_call("log_user", self.state.items[-1])
                continue

            # The model can finish a turn having only narrated. That narration is
            # all it said, so present it rather than an empty panel.
            final_content = reply.content or reply.commentary
            self._streamed_answer = reply.streamed
            escrow = getattr(self, "_escrow", None)
            released = finish.release(final_content or "", escrow and split_reply_meta(escrow)[0].strip())
            if released is not None:
                # Answer escrow: the model released the answer it wrote earlier
                # (this reply's own trailers were applied above; the kept
                # answer's apply only when the reply carried none).
                _answer, handoff, effort = split_reply_meta(escrow)
                if not self._next_effort_applied and (handoff or effort is not None):
                    self._apply_final_meta(handoff, effort)
                final_content = released.strip()
                self._streamed_answer = False
                self.finish_stats["released"] = self.finish_stats.get("released", 0) + 1
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

        self._claim_note(final_content)
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
        self._observe_turn_growth()
        # Commit point: the answer is in history. Post-turn compaction below is
        # maintenance; Ctrl-C during it must keep the finished turn (it swaps
        # history only after a summary succeeds, so history stays whole).
        self.committed_answer = final_content
        self._scout_feedback(final_content)
        report = await self._post_turn_compact()
        if report:
            console.print(f"[muted]{report}[/muted]")
        return final_content

    async def _scout_card(self, user_input: str) -> str:
        """Turn-zero scout card (scout.py), or "" (off, muted, nothing found, any error)."""
        self._scout = None
        self._scout_touched = []
        mode = str(getattr(settings, "scout_card", "local") or "off")
        if mode == "off" or user_input.lstrip().startswith("/"):
            return ""
        local_ok = local_llm.available() and not local_llm.request_wants_gpu(user_input)
        if not local_ok and mode != "always":
            return ""
        kind = "local" if local_ok else "units"
        if not scout_mod.trusted(kind):
            return ""
        try:
            res = await asyncio.wait_for(
                scout_mod.scout(user_input, get_project_root(), use_local=local_ok),
                scout_mod.LOCAL_TIMEOUT_S * 2,
            )
        except Exception:  # a hint; never fail a turn on it
            res = None
        if not res or (res["mode"] == "units" and mode != "always"):
            return ""  # "local" never falls back to a keyword card (e.g. the server is still loading)
        self._scout = res
        if settings.verbose != "low":
            console.print(
                f"[muted]scout ({res['mode']}, {res['secs']:.1f}s): {', '.join(res['files'][:3])}[/muted]"
            )
        return scout_mod.card_text(res)

    async def _preread_plan(self, user_input: str) -> tuple[str, list]:
        """Pre-emptive reads (preread.py): (mode, ReadFile arg dicts), or ("off", [])."""
        self._ghost_paths = []
        pre_mode = preread_mod.mode()
        if pre_mode == "off" or user_input.lstrip().startswith("/"):
            return "off", []
        try:
            planned = await asyncio.to_thread(preread_mod.plan, user_input, get_project_root())
        except Exception:  # a prediction; never fail a turn on it
            planned = []
        return pre_mode, planned

    async def _preread_prose(self, planned: list) -> str:
        """Run the planned reads and return them as text for the request (prose form)."""
        pairs = []
        for i, args in enumerate(planned):
            name = preread_mod.tool_of(args)
            tc = ToolCall(id=preread_mod.ghost_id(self.state.turn, i), name=name, arguments=json.dumps(args))
            try:
                result = await self._invoke_tool(tc, dict(args))
            except Exception:
                continue
            if name == "ReadFile":
                self._note_view(name, args, result)
            pairs.append((args, encode_tool_result(name, result)))
            if name == "ReadFile":
                self._ghost_paths.append(str(args.get("path")))
        self.hop_stats["ghost_reads"] = self.hop_stats.get("ghost_reads", 0) + len(pairs)
        return ("\n\n" + preread_mod.prose_block(pairs)) if pairs else ""

    async def _preread_ghost(self, planned: list) -> None:
        """Run the planned reads as the model's own tool exchange before call 1 (ghost form)."""
        calls = [
            ToolCall(id=preread_mod.ghost_id(self.state.turn, i), name=preread_mod.tool_of(args),
                     arguments=json.dumps(args))
            for i, args in enumerate(planned)
        ]
        items = [
            {"type": "function_call", "call_id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in calls
        ]
        self.state.add_output(items)
        self._session_log_call("log_output", items)
        self._ghost_running = True
        try:
            await self._run_tool_calls(calls)
        finally:
            self._ghost_running = False
        # DeepSeek's thinking mode rejects an assistant tool_use turn without a
        # thinking block (HTTP 400) unless a user text block follows the
        # results: this note closes the exchange (verified live) and says
        # where the reads came from, without putting thinking in the model's mouth.
        self.state.add_user(preread_mod.GHOST_NOTE)
        self._session_log_call("log_user", self.state.items[-1])
        self._ghost_paths = [str(a.get("path")) for a in planned if preread_mod.tool_of(a) == "ReadFile"]
        self.hop_stats["ghost_reads"] = self.hop_stats.get("ghost_reads", 0) + len(calls)

    def _scout_feedback(self, answer: str) -> None:
        """Use-confirmed precision: the turn's tool arguments or answer named a scouted file or symbol."""
        res = getattr(self, "_scout", None)
        if not res:
            return
        seen = "\n".join(getattr(self, "_scout_touched", [])) + "\n" + (answer or "")
        try:
            scout_mod.record_use(res["files"], scout_mod.used_files(res, seen), res["mode"])
        except Exception:
            pass
        self._scout = None

    def _handoff_due_forecast(self) -> bool:
        """Will a compaction that reads [handoff] run before or after the next turn?

        Window: the context plus the forecast growth reaches the trigger or the
        post-turn window threshold. Economic: the rent plus the forecast rent
        reaches the price of a compaction *with* a handoff (the one this
        request makes possible). A gauge estimated after a compaction is not
        a forecast basis; that context is small.
        """
        if getattr(self.usage, "context_tokens_estimated", False):
            return False
        ctx = int(self.usage.context_tokens or 0)
        if ctx <= 0:
            return False
        nxt = ctx + HANDOFF_FORECAST_TURNS * max(self._turn_growth_ema, 0.0)
        trigger = self.compact_trigger_tokens()
        if trigger and nxt >= trigger * HANDOFF_WINDOW_MARGIN:
            return True
        window = int(settings.max_context_tokens or 0)
        if window and nxt / window >= float(settings.compact_threshold) * HANDOFF_WINDOW_MARGIN:
            return True
        if getattr(settings, "compact_policy", "economic") != "economic":
            return False
        meter = self._sync_compaction_meter()
        if nxt < meter.floor_tokens:
            return False
        rent = meter.rent + HANDOFF_FORECAST_TURNS * max(self._turn_rent_ema, 0.0)
        return rent >= meter.price(int(nxt), inline_handoff=True)

    def _observe_turn_growth(self) -> None:
        """Fold this turn's rent and context growth into the forecast EMAs."""
        start = self._turn_gauge
        self._turn_gauge = None
        if start is None or getattr(self.usage, "context_tokens_estimated", False):
            return
        rent0, ctx0, compactions0 = start
        if self.state.compactions != compactions0:
            return  # a compaction reset the meter mid-turn
        meter = self._sync_compaction_meter()
        rent = max(meter.rent - rent0, 0.0)
        growth = max(int(self.usage.context_tokens or 0) - ctx0, 0)
        if ctx0 <= 0:
            # First turn: no prior gauge, the whole context is "growth".
            growth = 0
        a = 0.5
        self._turn_rent_ema = rent if not self._turn_rent_ema else a * rent + (1 - a) * self._turn_rent_ema
        self._turn_growth_ema = (
            growth if not self._turn_growth_ema else a * growth + (1 - a) * self._turn_growth_ema
        )

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
                    MODEL,
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
            result = await self.router.delegate(tc.name, args)
        else:
            result = await self.registry.execute(tc.name, args, tc.id)
        return ownership.mark_result(tc.name, args, result)

    def _maybe_parallel(self, name: str) -> bool:
        return name in READ_ONLY_PARALLEL and self.registry.policy.get(name) == ALLOW

    async def _local_salience_picks(self, stored: str, intent: str) -> list:
        """Local-model rerank of a bulky result's lines (salience.py). [] when off/slow/busy.

        Never blocks on a model load: the first use starts the server in the
        background and this result keeps the deterministic picks.
        """
        if not (settings.salience_excerpt and local_llm.available()):
            return []
        try:
            from .cache import _json_header

            first = stored.partition("\n")[0]
            rel = _json_header(first).get("full_output")
            text = (salience.read_vault_file(str(rel)) if rel else None) or stored
            lines = text.splitlines()
            pool = salience.rerank_pool(lines, intent)
            return await asyncio.wait_for(
                local_llm.pick_lines(lines, pool, intent), local_llm.RERANK_TIMEOUT_S + 1
            )
        except Exception:
            return []

    async def _run_tool_calls(self, calls: List[ToolCall]) -> None:
        prepared: list[tuple[str, ToolCall, dict, Optional[str]]] = []
        # Predicated finish: the answer is taken out of the arguments before
        # display, permission, repeat keys or execution see them. History keeps
        # it in the tool_use input (that is where the model wrote it).
        self._finish_decision = finish.Decision()
        finish_texts: dict[str, str] = {}
        # Write checks (hops.py): taken off like final_answer; run after the batch.
        offered_checks: list[tuple[str, str]] = []
        for tc in calls:
            args = tc.parsed()
            if finish.enabled() and tc.name in finish.FINISH_TOOLS:
                offered = finish.take(args)
                if offered and getattr(self, "_escrow", None):
                    # Answer escrow: "[deliver]" re-offers the kept answer.
                    offered = finish.release(offered, split_reply_meta(self._escrow)[0].strip()) or offered
                if offered:
                    finish_texts[tc.id] = offered
            if hops.check_enabled() and tc.name in hops.CHECK_TOOLS:
                command = hops.take_check(args)
                if command:
                    offered_checks.append((tc.id, command))
            problem = tc.argument_error()
            if problem is not None:
                console.print(f"  [bad]{Glyph.CROSS} {tc.name}: {problem}[/bad]")
                prepared.append(("error", tc, args, problem))
                continue
            guard = self._write_guard(tc, args)
            if guard is not None:
                console.print(f"  [warn]{Glyph.CROSS} {tc.name}: held by the request's constraint \"{guard}\"[/warn]")
                prepared.append(("guard", tc, args, guard))
                continue
            self.printer.tool_start(tc.name, args)
            allowed = await self.registry.check_permission(tc.name, args)
            if not allowed:
                self.printer.tool_denied(tc.name)
                prepared.append(("denied", tc, args, None))
                continue
            prepared.append(("run", tc, args, None))

        skipped_repeat = False
        for index, entry in enumerate(prepared):
            status, tc, args, _problem = entry
            if status != "run":
                continue
            fingerprint = _repeat_key(tc.name, args, self._write_epoch)
            seen = self._tool_repeat_counts.get(fingerprint, 0) + 1
            self._tool_repeat_counts[fingerprint] = seen
            if seen >= REPEAT_TOOL_WARN:
                prepared[index] = ("repeat", tc, args, None)
                skipped_repeat = True
            if seen >= REPEAT_TOOL_STOP:
                self._stop_repeated_tools = True
        if skipped_repeat:
            console.print(
                "[warn]Skipped a repeated tool call with the same arguments. "
                "The earlier result still stands.[/warn]"
            )

        serial_lock = asyncio.Lock()
        bash_wrote: set[str] = set()
        # Ordered batches (hops.py): writes of this response that failed, as
        # (tool, normalised path). Later Bash / same-file writes are skipped.
        ordered = hops.ordered_enabled()
        failed_writes: list[tuple[str, str]] = []
        hop = int(getattr(self.trace, "iterations", 0) or 0)

        def norm_path(args: dict) -> str:
            raw = str(args.get("path") or "")
            return os.path.normpath(raw) if raw else ""
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
            if ordered and tc.name in WRITE_TOOLS and status in ("denied", "guard", "error"):
                failed_writes.append((tc.name, norm_path(args)))
            if status == "error":
                return tc, args, json.dumps({"error": problem}), 0.0, False, False
            if status == "guard":
                return (
                    tc,
                    args,
                    json.dumps({
                        "error": "write_guard",
                        "constraint": problem,
                        "hint": (
                            "The user's request says this file must not change; nothing was "
                            "written. Fix the cause elsewhere. If this exact write is still "
                            "required, repeat the identical call and it will run."
                        ),
                    }),
                    0.0,
                    False,
                    False,
                )
            if status == "denied":
                return (
                    tc,
                    args,
                    json.dumps({"error": "User denied permission"}),
                    0.0,
                    False,
                    True,
                )
            if status == "repeat":
                return (
                    tc,
                    args,
                    json.dumps(
                        {
                            "error": "repeated_tool",
                            "tool": tc.name,
                            "hint": (
                                "This exact call already ran this turn and no file "
                                "has changed since. Use the earlier result and take "
                                "the next step. Do not call it again."
                            ),
                        }
                    ),
                    0.0,
                    False,
                    False,
                )
            if ordered and hops.should_skip(tc.name, norm_path(args), failed_writes, WRITE_TOOLS):
                # Planned on top of a write that failed: do not run it, and do
                # not count it toward the repeat guard (it never ran).
                key = _repeat_key(tc.name, args, self._write_epoch)
                if self._tool_repeat_counts.get(key):
                    self._tool_repeat_counts[key] -= 1
                self.hop_stats["skipped"] += 1
                return tc, args, hops.skipped_result(tc.name, failed_writes), 0.0, False, False
            if tc.name in WRITE_TOOLS:
                for wpath in _write_paths(args):
                    self._preimages.capture(wpath)
            elif tc.name == rename_mod.NAME:
                # A multi-file write: capture each file it will change, as an
                # Edit captures its one path (failure attribution).
                for rel in await asyncio.to_thread(
                    rename_mod.planned_files, str(args.get("old") or ""), str(args.get("new") or ""),
                    str(args.get("path") or "."), str(args.get("include") or ""),
                ):
                    self._preimages.capture(rel)
            # Every Bash runs between two stat fingerprints: files it changes
            # taint attribution and count as working-set files (TodoWrite
            # evidence), as Edit/WriteFile writes do.
            watch = tc.name == "Bash"
            before = await asyncio.to_thread(self._preimages.snapshot) if watch else None
            started = time.perf_counter()
            try:
                result = await self._invoke_tool(tc, args)
            except Exception as e:
                result = json.dumps({"error": str(e), "tool": tc.name})
            elapsed = (time.perf_counter() - started) * 1000
            if ordered and tc.name in WRITE_TOOLS and hops.write_failed(result):
                failed_writes.append((tc.name, norm_path(args)))
            if watch:
                oracle = self.registry.last_oracle
                self.registry.last_oracle = None
                if oracle is not None and testoracle.enabled():
                    result = await self._attach_oracle(result, oracle, elapsed, tc.id)
                after = await asyncio.to_thread(self._preimages.snapshot)
                wrote = self._preimages.note_bash(before, after)
                if wrote:
                    # Observed, not guessed: `python gen.py` / `make` changed
                    # files, so the next identical call runs in a new world.
                    bash_wrote.add(tc.id)
                for rel in wrote[:8]:
                    self._note_bash_write(rel)
                if views_mod.enabled():
                    result = self._attach_view_deltas(result, before, after)
            elif tc.name in VIEW_TOOLS and views_mod.enabled():
                self._note_view(tc.name, args, result)
            elif tc.name == rename_mod.NAME:
                self._note_rename(result)
            if tc.name == "ReadFile" and callees_mod.enabled():
                result = await asyncio.to_thread(
                    callees_mod.attach, result, str(args.get("path") or ""), get_project_root()
                )
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

        results, interrupted = await self._execute_in_program_order(prepared, guarded)
        if offered_checks and not interrupted:
            results = await self._run_write_checks(results, offered_checks, execute_prepared)
        if not interrupted:
            results = await self._after_batch(results, bash_wrote)
            if finish_texts:
                decision = finish.decide(
                    ((tc.id, tc.name, args, result, ran, finish_texts.get(tc.id))
                     for tc, args, result, _elapsed, ran, _denied in results),
                    require_run=getattr(self, "_run_demanded", False),
                    request=getattr(self, "_request_text", ""),
                )
                if decision.deliver and finish.claimed_write_enabled():
                    claims = finish.unwritten_claims(getattr(self, "_request_text", ""), decision.answer,
                                                     getattr(self, "_turn_written", set()), get_project_root())
                    if claims:
                        self.finish_stats["claim_held"] = self.finish_stats.get("claim_held", 0) + 1
                        decision = finish.Decision(discarded=True, carrier=decision.carrier,
                                                   held_note=finish.claimed_write_note(claims))
                self._finish_decision = decision
                if decision.caveats and decision.deliver:
                    self.finish_stats["licensed"] = self.finish_stats.get("licensed", 0) + 1
                if (decision.discarded and decision.offered and not decision.held_for_run
                        and finish.escrow_enabled()):
                    self._escrow = decision.offered
                if decision.discarded:
                    self.finish_stats["discarded"] += 1
                    console.print(
                        "[muted]Prepared final answer held back: "
                        + ("the request asks for a test run and none ran. "
                           if decision.held_for_run else
                           "it names a change no call made. " if decision.held_note else
                           "a step in that batch did not succeed. ")
                        + "Continuing.[/muted]"
                    )

        # Record every function_call_output first, then mirror the working
        # set. Appending a user-role working-set item after the first write
        # of a parallel batch splits the remaining outputs from their calls
        # and DeepSeek 400s with "No tool output found for tool call …".
        pending_ws: list[tuple[ToolCall, dict, str]] = []
        tracker = ""
        if hops.budget_enabled() and results and not getattr(self, "_ghost_running", False):
            tracker = "\n" + self._budget.tracker(
                hop, format_cost(self.usage.turn_cost)
            )
        last_index = len(results) - 1
        for index, (tc, args, result, elapsed, ran, denied) in enumerate(results):
            if not ran:
                if denied:
                    summary = "denied"
                elif '"error": "interrupted"' in (result or ""):
                    summary = "interrupted"
                elif "repeated_tool" in (result or ""):
                    summary = "repeated call skipped"
                elif '"write_guard"' in (result or ""):
                    summary = "held by a request constraint"
                elif '"error": "skipped"' in (result or ""):
                    summary = "skipped: an earlier write in this response failed"
                else:
                    summary = "bad arguments"
                self.trace.events.append(
                    ToolEvent(
                        name=tc.name,
                        args=args,
                        ok=False,
                        denied=denied,
                        summary=summary,
                    )
                )
                if index == last_index and tracker:
                    result = result + tracker
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
            # Densest faithful form for history (tool_codec). UI and working
            # set keep the raw payload; the vault sees the encoded one.
            stored = encode_tool_result(tc.name, result)
            if should_vault_tool_output(tc.name, stored, raw_read=full_read):
                try:
                    intent = salience.call_intent(getattr(self, "_step_intent", ""), args)
                    extra = await self._local_salience_picks(stored, intent)
                    stored = get_cache().offload_tool_output(
                        stored, intent=intent, extra=extra, salient=settings.salience_excerpt
                    )
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
            if self._finish_decision.deliver and tc.id == self._finish_decision.carrier:
                stored = stored + "\n" + finish.DELIVERED_NOTE
            elif self._finish_decision.held_for_run and tc.id == self._finish_decision.carrier:
                stored = stored + "\n" + finish.RUN_HELD_NOTE
            elif self._finish_decision.held_note and tc.id == self._finish_decision.carrier:
                stored = stored + "\n" + self._finish_decision.held_note
            elif (self._finish_decision.discarded and tc.id == self._finish_decision.carrier
                  and getattr(self, "_escrow", None) == self._finish_decision.offered
                  and self._finish_decision.offered):
                stored = stored + "\n" + finish.escrow_note(self._finish_decision.reason)
            if index == last_index and tracker:
                stored = stored + tracker
            self.state.add_tool_result(tc.id, stored)
            self._session_log_call("log_tool_result", tc.id, stored)
            pending_ws.append((tc, args, result))
        for tc, args, result in pending_ws:
            self._apply_working_set_from_tool(tc, args, result)
            if self._scout and not getattr(self, "_ghost_running", False):
                self._scout_touched.append(json.dumps(args, ensure_ascii=False)[:2000])
            if settings.exploration_ledger:
                self.ledger.note(tc.name, args, result, get_project_root())
        if interrupted:
            raise asyncio.CancelledError()

    def _batch_segments(self, prepared: list) -> list[list[int]]:
        """Split a batch into program-order segments.

        Invariant: a call observes every earlier call of the same batch. Runs
        of parallel-safe reads (and instant non-run entries) share a segment
        and overlap; any other call (a write, Bash, an ASK tool) is a barrier
        that runs alone after everything before it and before everything
        after it. An all-read batch is still one fully parallel segment.
        """
        segments: list[list[int]] = []
        current: list[int] = []
        for i, (status, tc, _args, _problem) in enumerate(prepared):
            if status != "run" or self._maybe_parallel(tc.name):
                current.append(i)
                continue
            if current:
                segments.append(current)
                current = []
            segments.append([i])
        if current:
            segments.append(current)
        return segments

    async def _execute_in_program_order(self, prepared: list, run_one) -> tuple[list, bool]:
        """Run ``prepared`` by segment; on cancellation keep what finished.

        Returns (results in call order, interrupted). When the batch is
        cancelled (Ctrl-C), calls that completed keep their real results and
        every other call gets an ``interrupted`` error, so each call still
        has exactly one output and history matches what actually ran. The
        caller records the outputs and then re-raises the cancellation.
        """
        results: list = [None] * len(prepared)
        interrupted = False
        for segment in self._batch_segments(prepared):
            tasks = {i: asyncio.ensure_future(run_one(prepared[i])) for i in segment}
            try:
                # wait, not gather: one failing call must not abandon its
                # siblings mid-flight; each failure becomes that call's output.
                await asyncio.wait(tasks.values())
            except asyncio.CancelledError:
                interrupted = True
            for i, task in tasks.items():
                if not task.done():
                    task.cancel()
            if interrupted:
                pending = [t for t in tasks.values() if not t.done()]
                if pending:
                    try:
                        await asyncio.wait(pending, timeout=5)
                    except asyncio.CancelledError:
                        pass
            for i, task in tasks.items():
                if task.done() and not task.cancelled() and task.exception() is None:
                    results[i] = task.result()
                elif task.done() and not task.cancelled():
                    status, tc, args, _problem = prepared[i]
                    results[i] = (
                        tc, args,
                        json.dumps({"error": str(task.exception()), "tool": tc.name}),
                        0.0, False, False,
                    )
            if interrupted:
                break
        for i, entry in enumerate(prepared):
            if results[i] is None:
                _status, tc, args, _problem = entry
                results[i] = (
                    tc,
                    args,
                    json.dumps(
                        {
                            "error": "interrupted",
                            "tool": tc.name,
                            "hint": "The user interrupted the turn before this call "
                            "finished; it may have partly run.",
                        }
                    ),
                    0.0,
                    False,
                    False,
                )
        return results, interrupted

    async def _run_write_checks(self, results: list, offered: list, execute) -> list:
        """Run the checks Edit/WriteFile carried (hops.py), ``&&`` after all of the batch's writes.

        A check runs through the normal Bash path (permission, oracle, view
        deltas) and its result is merged into the carrying
        write's result, so the history keeps exactly one output per call.
        """
        failed = [
            f"{tc.name} {args.get('path') or ''}".strip()
            for tc, args, result, _e, ran, _d in results
            if tc.name in WRITE_TOOLS and (not ran or hops.write_failed(result))
        ]
        index = {tc.id: i for i, (tc, *_rest) in enumerate(results)}
        for carrier, command in hops.plan_checks(offered):
            i = index.get(carrier)
            if i is None:
                continue
            tc, args, result, elapsed, ran, denied = results[i]
            check_result: Optional[str] = None
            reason = ""
            if failed:
                reason = f"not run: {failed[0]} failed"
            else:
                bash_args = {"command": command}
                synthetic = ToolCall(id=f"{tc.id}#check", name="Bash", arguments=json.dumps(bash_args))
                self.printer.tool_start("Bash", bash_args)
                if not await self.registry.check_permission("Bash", bash_args):
                    self.printer.tool_denied("Bash")
                    reason = "User denied permission"
                else:
                    _tc, _a, check_result, check_ms, _ran, _den = await execute(("run", synthetic, bash_args, None))
                    elapsed += check_ms
                    self.hop_stats["checks"] = self.hop_stats.get("checks", 0) + 1
                    summary, ok = ui.describe_result("Bash", check_result)
                    event = ToolEvent(name="Bash", args=bash_args, elapsed_ms=check_ms,
                                      result_chars=len(check_result), ok=ok, summary=summary)
                    self.trace.events.append(event)
                    if settings.show_answer_detail:
                        self.printer.tool_end(event, show_timing=settings.show_tool_timing)
            results[i] = (tc, args, hops.merge_check(result, command, check_result, reason), elapsed, ran, denied)
        return results

    async def _after_batch(self, results: list, bash_wrote: Optional[set] = None) -> list:
        """Advance the write epoch, track the failing check, and replay it after a fix."""
        last_write = None
        ran_check = False

        async def bash_done(call_id: str, command: str, data: dict, elapsed: float) -> bool:
            """Epoch/check bookkeeping for one Bash run; True if ``data`` gained an attribution."""
            nonlocal ran_check
            if (bash_may_write(command) and data.get("exit_code") == 0) or call_id in (bash_wrote or ()):
                self._write_epoch += 1
            if call_id in (bash_wrote or ()):
                self._write_seq += 1
            if not looks_like_test_command(command):
                return False
            ran_check = True
            code = data.get("exit_code")
            self._turn_checks.append({
                "command": command, "exit": code,
                "summary": self._oracle_summaries.pop(call_id, ""), "seq": self._write_seq,
            })
            if code not in (0, None):
                self._failing_check = (command, elapsed)
                note = await self._attribute_failure(command, data, elapsed)
                if note is not None:
                    data["attribution"] = note
                    return True
            elif code == 0 and self._failing_check and self._failing_check[0] == command:
                self._failing_check = None
            return False

        for i, (tc, args, result, elapsed, ran, _denied) in enumerate(results):
            if not ran:
                continue
            data = _json_dict(result)
            if tc.name in WRITE_TOOLS or tc.name == rename_mod.NAME:
                if data.get("ok") and not data.get("unchanged"):
                    last_write = i
                    self._write_seq += 1
                    if tc.name in WRITE_TOOLS:
                        self._note_turn_write(str(data.get("path") or args.get("path") or ""))
                        for rel in (data.get("files") or {}) if isinstance(data.get("files"), dict) else ():
                            self._note_bash_write(rel)  # cross-file Edit: working set + written
                check = data.get("check")
                if isinstance(check, dict) and "exit_code" in check:
                    # A write-carried check (hops.py) is a Bash run like any other.
                    if await bash_done(f"{tc.id}#check", str(check.get("command") or ""), check, elapsed):
                        results[i] = (tc, args, json.dumps(data, ensure_ascii=False), elapsed, ran, _denied)
                continue
            if tc.name != "Bash":
                continue
            command = str(args.get("command") or "")
            if await bash_done(tc.id, command, data, elapsed):
                results[i] = (tc, args, json.dumps(data, ensure_ascii=False), elapsed, ran, _denied)
        if last_write is None:
            return results
        self._write_epoch += 1
        replay = None if ran_check else await self._verify_replay()
        if replay is None:
            return results
        tc, args, result, elapsed, ran, denied = results[last_write]
        data = _json_dict(result)
        data["verify"] = replay
        results[last_write] = (tc, args, json.dumps(data, ensure_ascii=False), elapsed, ran, denied)
        return results

    def _claim_note(self, answer: str) -> None:
        """Claim check (claims.py): a visible note when "tests pass" is not what the run said."""
        if not claims_mod.enabled() or not answer:
            return
        try:
            note = claims_mod.check(answer, self._turn_checks, self._write_seq)
        except Exception:
            return
        if note is None:
            return
        self.trace.events.append(ToolEvent(name="claim_check", args={}, ok=False, summary=note[:120]))
        console.print(f"[warn]Note:[/warn] [muted]{escape(note)}[/muted]")

    async def _obligation_probe(self) -> Optional[str]:
        """Commit probe (obligations.py): one corrective note when a named callable is missing."""
        obs = self._obligations
        if not settings.obligations or self._obligation_nudged or not obs or not obs.callables:
            return None
        if not self._preimages.files and not self._write_epoch:
            return None  # nothing was written: an answer-only turn owes no code
        try:
            problems = await asyncio.to_thread(obligations_mod.unmet, obs, get_project_root())
        except Exception:
            return None
        self._obligation_nudged = True
        if not problems:
            return None
        self.trace.events.append(
            ToolEvent(name="obligations", args={}, ok=False, summary="unmet: " + problems[0][:80])
        )
        return obligations_mod.corrective_note(problems)

    def _write_guard(self, tc: ToolCall, args: dict) -> Optional[str]:
        """Quote of the request constraint that protects this write, the first time only."""
        obs = self._obligations
        if not settings.write_guards or not obs or not obs.guards or tc.name not in WRITE_TOOLS:
            return None
        root = Path(get_project_root()).resolve()
        raw = Path(str(args.get("path") or ""))
        p = raw if raw.is_absolute() else root / raw
        try:
            p = p.resolve()
            rel = p.relative_to(root).as_posix()
        except (OSError, ValueError):
            return None
        guard = obligations_mod.guarded_by(obs.guards, rel, p.is_file())
        if guard is None:
            return None
        key = _tool_call_fingerprint(tc.name, args)
        if key in self._guard_refused:
            return None  # repeated on purpose: the model decided
        self._guard_refused.add(key)
        return guard.quote

    async def _attach_oracle(self, result: str, oracle: dict, elapsed: float, call_id: str = "") -> str:
        """Add ``tests`` rows (testoracle.py) for what the command's own view hid."""
        report = oracle.get("report") or {}
        command = str(oracle.get("command") or "")
        data = _json_dict(result)
        if not data or data.get("error") and "exit_code" not in data:
            return result
        root = get_project_root()
        visible = str(data.get("stdout") or "") + "\n" + str(data.get("stderr") or "")
        filtered = strip_view_filters(command) != command
        try:
            novel = testoracle.novel_tests(self._preimages.files, root)
        except Exception:
            novel = set()
        new = testoracle.novel_ids(report, novel, root)
        vacuous = await self._vacuity(command, report, new, elapsed)
        self._oracle_summaries[call_id] = testoracle.summary_line(report)
        rows = testoracle.render(report, visible, filtered, new=new, vacuous=vacuous)
        if not rows:
            return result
        data["tests"] = rows
        return json.dumps(data, ensure_ascii=False)

    async def _vacuity(self, command: str, report: dict, new: dict, elapsed: float) -> list:
        """New passing tests that also pass with the turn's source edits reverted."""
        if not settings.vacuity_check or not new:
            return []
        passing = sorted(i for i, o in new.items() if o == "passed")
        if not passing or elapsed > VERIFY_REPLAY_MAX_MS or self.registry.policy.get("Bash") != ALLOW:
            return []
        if self._preimages.tainted:
            return []
        root = get_project_root()
        try:
            if Path(str(report.get("rootdir") or "")).resolve() != Path(root).resolve():
                return []
        except OSError:
            return []
        source = testoracle.source_preimages(self._preimages.changed())
        if not source:
            return []  # tests-only turn: nothing to be vacuous against
        key = (tuple(passing), tuple(sorted(source)))
        if key in self._vacuity_seen:
            return self._vacuity_seen[key]
        cmd = testoracle.vacuity_command(command, passing)
        verdict: list = []
        if cmd is not None:
            probe = testoracle.Probe()
            try:
                run = await shadow_run(cmd, root, source, max(5, int(elapsed / 1000 * 3) + 5), probe=probe)
                shadow = probe.collect() if run is not None else None
            finally:
                probe.close()
            if shadow is not None:
                outcomes = shadow.get("tests") or {}
                verdict = [i for i in passing if outcomes.get(i) == "passed"]
                self.trace.events.append(
                    ToolEvent(
                        name="Bash",
                        args={"command": cmd, "shadow": True},
                        elapsed_ms=run[2] if run else 0.0,
                        result_chars=len(run[1]) if run else 0,
                        ok=True,
                        summary=f"vacuity: {len(verdict)}/{len(passing)} pass without the edits",
                    )
                )
        self._vacuity_seen[key] = verdict
        return verdict

    def _note_view(self, name: str, args: dict, result: str) -> None:
        """A successful ReadFile/Edit/WriteFile: the model now knows this file's text."""
        data = _json_dict(result)
        if data.get("error") or (name in WRITE_TOOLS and not data.get("ok")):
            return
        try:
            for path in (_write_paths(args) if name in WRITE_TOOLS else [str(args.get("path") or "")]):
                self._views.see(path)
        except Exception:
            pass

    def _attach_view_deltas(self, result: str, before: Optional[dict], after: Optional[dict]) -> str:
        """Add ``changed``: diffs of files the command changed, against the model's view."""
        changed, removed = views_mod.changed_and_removed(before, after, self._views.root_dir())
        if not changed and not removed:
            return result
        data = _json_dict(result)
        if not data or data.get("error"):
            return result
        try:
            text = self._views.deltas(changed, removed)
        except Exception:
            return result
        if not text:
            return result
        data["changed"] = text
        return json.dumps(data, ensure_ascii=False)

    def _note_rename(self, result: str) -> None:
        """A Rename that landed: each file it changed is a working-set write."""
        data = _json_dict(result)
        if not data.get("ok"):
            return
        for rel in list(data.get("files") or {})[:50]:
            self._note_bash_write(rel)
            if views_mod.enabled():
                try:
                    if self._views.has(rel):  # its view now includes the rename
                        self._views.see(rel)
                except Exception:
                    pass

    def _note_turn_write(self, path: str) -> None:
        """Remember a file this turn wrote (claimed-write guard), project-relative."""
        if not path:
            return
        try:
            from .paths import rel_to_root

            if not hasattr(self, "_turn_written"):
                self._turn_written = set()
            self._turn_written.add(rel_to_root(path))
        except Exception:
            pass

    def _note_bash_write(self, rel: str) -> None:
        """Record a file a shell command wrote, like an Edit/WriteFile write."""
        if not hasattr(self, "_turn_written"):
            self._turn_written = set()
        self._turn_written.add(rel)
        try:
            p = self._preimages.root / rel
            note_file(str(p), hashlib.sha256(p.read_bytes()).hexdigest())
        except Exception:
            pass

    async def _attribute_failure(self, command: str, data: dict, elapsed: float) -> Optional[dict]:
        """Label a failing check's failures new / pre-existing (g023_code/attribution.py).

        Same gate as verify replay: setting on, Bash allowed without asking,
        the check took < 30 s. Needs at least one file this turn changed, and
        no Bash command this turn that may have written files.
        """
        if not settings.failure_attribution or not self._preimages.usable():
            return None
        if elapsed > VERIFY_REPLAY_MAX_MS or self.registry.policy.get("Bash") != ALLOW:
            return None
        timeout = max(5, int(elapsed / 1000 * 3) + 5)
        root = get_project_root()
        full = strip_view_filters(command)
        if full != command:
            # The model's view (| tail) can hide different ids on each side:
            # compare full outputs, the current tree also run in a shadow.
            now, run = await asyncio.gather(
                shadow_run(full, root, {}, timeout),
                shadow_run(full, root, self._preimages.changed(), timeout),
            )
            if now is None or run is None:
                return None
            after, after_code = now[1], now[0]
        else:
            run = await shadow_run(command, root, self._preimages.changed(), timeout)
            if run is None:
                return None
            after = str(data.get("stdout") or "") + "\n" + str(data.get("stderr") or "")
            after_code = data.get("exit_code")
        code, text, ms = run
        note = attribute(after, after_code, text, code)
        self.trace.events.append(
            ToolEvent(
                name="Bash",
                args={"command": command, "shadow": True},
                elapsed_ms=ms,
                result_chars=len(text),
                ok=True,
                summary="attribution: " + str(note.get("verdict", ""))[:80],
            )
        )
        return note

    async def _verify_replay(self) -> Optional[dict]:
        if not self._failing_check or not settings.verify_replay:
            return None
        command, elapsed = self._failing_check
        if elapsed > VERIFY_REPLAY_MAX_MS or self.registry.policy.get("Bash") != ALLOW:
            return None
        timeout = max(5, int(elapsed / 1000 * 3) + 5)
        started = time.perf_counter()
        try:
            raw = await self.registry._exec_bash(command, timeout=timeout)
        except Exception:
            return None
        ms = (time.perf_counter() - started) * 1000
        oracle = self.registry.last_oracle
        self.registry.last_oracle = None
        data = _json_dict(raw)
        code = data.get("exit_code")
        tail = (str(data.get("stdout") or "") + str(data.get("stderr") or "")).strip()
        if len(tail) > VERIFY_REPLAY_TAIL_CHARS:
            tail = Glyph.ELLIPSIS + tail[-VERIFY_REPLAY_TAIL_CHARS:]
        if oracle is not None and testoracle.enabled():
            report = oracle.get("report") or {}
            self._turn_checks.append({
                "command": command, "exit": code,
                "summary": testoracle.summary_line(report), "seq": self._write_seq,
            })
        if code == 0:
            self._failing_check = None
        else:
            self._failing_check = (command, ms)
        self.trace.events.append(
            ToolEvent(
                name="Bash",
                args={"command": command, "replay": True},
                elapsed_ms=ms,
                result_chars=len(raw),
                ok=code == 0,
                summary=f"verify replay exit {code}",
            )
        )
        # Seed the working set exactly as a model-run check would.
        self._apply_working_set_from_tool(_ReplayCall(), {"command": command}, raw)
        return {
            "command": command,
            "exit_code": code,
            "output_tail": tail,
            "note": "Re-ran automatically after this write; no need to run it again.",
        }

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

    def _rollback_turn(self, reason: str) -> None:
        """Close an unfinished turn as a transaction.

        Invariants: (1) every function_call has exactly one output; (2)
        history never forgets a tool call that completed — its side effects
        (writes, Bash) are already in the world, so dropping its record would
        let the next turn act on a world it cannot see.

        The turn is located by identity (``_turn_user_item``), not by "the
        last user-role item", which may be a working-set mirror or a nudge.
        No completed call: the whole turn is removed (re-sending is a clean
        retry). Otherwise the completed pairs stay, dangling halves go, and a
        short note says the request was not finished.
        """
        items = self.state.items
        anchor = getattr(self, "_turn_user_item", None)
        idx = None
        if anchor is not None:
            for k in range(len(items) - 1, -1, -1):
                if items[k] is anchor:
                    idx = k
                    break
        if idx is None:
            self._rollback_to_last_complete()
            return
        tail = items[idx + 1:]
        called = {i.get("call_id") for i in tail if i.get("type") == "function_call"}
        answered = {
            i.get("call_id") for i in tail if i.get("type") == "function_call_output"
        }
        completed = called & answered
        if not completed:
            self.state.items = items[:idx]
            self._repair_pairing()
            return
        kept = [
            i
            for i in tail
            if i.get("type") not in ("function_call", "function_call_output")
            or i.get("call_id") in completed
        ]
        self.state.items = items[: idx + 1] + kept
        self._repair_pairing()
        self._reseat_items_splitting_tool_pairs()
        self.state.add_user(
            f"[turn {reason}] The request above was not finished. The tool "
            "results above are real (files may already have changed). Do not "
            "resume it unless asked."
        )

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

    def compact_trigger_tokens(self) -> int:
        """Token count at which auto-compaction fires.

        ``compact_at_tokens`` (default 200k) is the normal trigger; the
        window fraction still caps it when a smaller window is configured.
        """
        window = int(settings.max_context_tokens or 0)
        by_fraction = int(window * float(settings.compact_threshold)) if window else 0
        absolute = int(getattr(settings, "compact_at_tokens", 0) or 0)
        candidates = [n for n in (absolute, by_fraction) if n > 0]
        return min(candidates) if candidates else 0

    def over_compact_trigger(self) -> bool:
        trigger = self.compact_trigger_tokens()
        return bool(trigger) and self.usage.context_tokens >= trigger

    def _orchestrator_calls(self) -> int:
        return sum(u.calls for u in self.usage.orchestrator.values())

    def _sync_compaction_meter(self) -> CompactionMeter:
        meter = self.compaction_meter
        if not meter.standing_tokens:
            try:
                standing = len(self._frozen_instructions()) + len(
                    json.dumps(self._frozen_tools())
                )
                meter.standing_tokens = standing // 4
            except Exception:
                pass
        meter.floor_tokens = int(getattr(settings, "compact_floor_tokens", 0) or 0)
        if not getattr(self.usage, "context_tokens_estimated", False):
            meter.observe(self._orchestrator_calls(), int(self.usage.context_tokens or 0))
        return meter

    def economic_compact_due(self) -> bool:
        """Re-reading the history has cost more than compacting it would."""
        if getattr(settings, "compact_policy", "economic") != "economic":
            return False
        if getattr(self.usage, "context_tokens_estimated", False):
            return False
        meter = self._sync_compaction_meter()
        return meter.due(
            int(self.usage.context_tokens or 0), inline_handoff=bool(self._inline_handoff())
        )

    def _transcript_path(self) -> Optional[str]:
        log = self.session_log
        if log is None:
            return None
        try:
            return str(log.path.relative_to(get_project_root()))
        except Exception:
            return str(log.path)

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
            model=MODEL,
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
        if total.web_searches or total.fetches:
            lines.append(
                f"Tools     {total.web_searches} web searches {Glyph.DOT} "
                f"{total.fetches} fetches  {format_cost(total.tool_cost)} "
                "($0.75 per 1,000 successful requests each)"
            )
        all_tokens = total.input_tokens + total.output_tokens
        if total.peak_tokens and all_tokens:
            lines.append(
                f"Peak      {total.peak_tokens / all_tokens:.0%} of tokens billed in a "
                "DeepSeek peak window (2x rate) — priced as such below"
            )
        lines.append(
            f"Est. cost {format_cost(u.cost())}  "
            f"(orchestrator {format_cost(u.scope_cost('orchestrator'))} {Glyph.DOT} "
            f"subagents {format_cost(u.scope_cost('subagent'))})"
        )
        if (
            getattr(settings, "compact_policy", "economic") == "economic"
            and u.context_tokens
            and not getattr(u, "context_tokens_estimated", False)
        ):
            meter = self._sync_compaction_meter()
            lines.append(
                "Compact   "
                + meter.describe(u.context_tokens, inline_handoff=bool(self._inline_handoff()))
                + f" {Glyph.DOT} ceiling {self.compact_trigger_tokens():,} tok"
            )
        return lines

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
        if not (self.over_compact_trigger() or self.economic_compact_due()):
            return False
        if self._already_compacted_short():
            return False
        return True

    async def _maybe_mid_turn_compact(self, user_input: str) -> Optional[str]:
        """Compact between steps of one long agentic turn.

        Turn-start compaction alone lets a 100-step turn grow toward the 1M
        ceiling, paying full price for every re-read token. This runs after a
        tool batch (so every tool_use already has its tool_result), on the
        same real-gauge trigger. The in-flight request is pinned: the
        summarizer is told to keep it, and if the request item fell out of the
        kept tail it is restated after the summary so the model keeps going.
        """
        if not self._should_auto_compact():
            return None
        console.print(
            f"[warn]Context at {self.usage.context_tokens:,} tokens mid-turn "
            f"{Glyph.ARROW} compacting before the next step.[/warn]"
        )
        report = await self.compact(
            instructions=(
                "this turn is still in progress. Keep the user's current request "
                "verbatim, what has been done for it so far, and the next step."
            )
        )
        console.print(f"[muted]{report}[/muted]")
        if not report.startswith("Compacted"):
            return report
        current = getattr(self, "_turn_user_item", None)
        if current is not None and not any(it is current for it in self.state.items):
            self.state.add_user(
                MID_TURN_RESTATE_LEADIN + user_input + MID_TURN_RESTATE_TAIL
            )
            # The restated request is this turn's anchor from now on.
            self._turn_user_item = self.state.items[-1]
            self._session_log_call("log_user", self.state.items[-1])
        return report

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
        due = self._should_auto_compact()
        # Only the ceiling is window pressure; an economic trigger compacts
        # (Layer 3) without blanking cached tool results first.
        window_pressure = due and self.over_compact_trigger()
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

        if not due:
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
                and not is_rehydrate_item(item)
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
            "content": COMPACT_SUMMARY_LEADIN + with_recall_pointer(summary, self._transcript_path()),
        }
        tail = self._compact_tail_items(head)
        out: List[dict] = []
        if head is not None:
            out.append(head)
        out.append(ws_item)
        out.append(summary_item)
        ledger_item = self._ledger_item()
        if ledger_item is not None:
            out.append(ledger_item)
        out.extend(x for x in tail if x is not ws_item)
        return out

    def _ledger_item(self) -> Optional[dict]:
        """Exploration notes for a compacted prefix (None when off or empty)."""
        if not settings.exploration_ledger:
            return None
        try:
            return self.ledger.item()
        except Exception:
            return None

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
            "content": COMPACT_SUMMARY_LEADIN + with_recall_pointer(summary, self._transcript_path()),
        }
        ledger_item = self._ledger_item()
        return [ws_item, summary_item] + ([ledger_item] if ledger_item else [])

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
            MODEL, resp.get("usage"), scope="subagent"
        )
        summary_cost = delta.cost(MODEL)
        if any(it.get("type") == "function_call" for it in (resp.get("output") or [])):
            return None, "Compaction called a tool — history left untouched.", summary_cost
        summary = output_text(resp)
        if not summary.strip():
            return None, "Compaction produced an empty summary — history left untouched.", summary_cost
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
        self.compaction_meter.reset(self._orchestrator_calls())
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

    def _exchanges(self, items: List[dict]) -> List[tuple[str, str]]:
        """``(user request, last assistant text)`` per user turn in ``items``; tool rows are dropped."""
        out: List[tuple[str, str]] = []
        for item in items:
            kind = item.get("type")
            if kind in POST_TURN_COMPACT_TOOL_TYPES or kind == "reasoning":
                continue
            if (
                self._is_compact_summary_item(item)
                or self._is_working_set_item(item)
                or is_rehydrate_item(item)
            ):
                continue
            text = self._item_plain_text(item)
            if not text.strip():
                continue
            if self._is_user_item(item):
                if text.startswith(MID_TURN_RESTATE_LEADIN):
                    request = text[len(MID_TURN_RESTATE_LEADIN):].split(MID_TURN_RESTATE_TAIL, 1)[0]
                    if out:  # the same request, restated after a mid-turn compaction
                        continue
                    text = request
                out.append((text, ""))
            elif (item.get("role") or "assistant") == "assistant":
                if out:
                    out[-1] = (out[-1][0], text)
                else:
                    out.append(("(continued)", text))
        return out

    def _local_handoff_summary(self) -> str:
        """Fold this turn into the carried state without a model call (checkpoint.fold_summary).

        The existing summary's narrative is kept byte-for-byte (prefix cache); this turn's exchange enters a
        bounded ring of recent turns and the oldest leaves as a one-line digest. Before the first summary
        exists, every exchange so far is folded, so the result is bounded either way. Tool rows are omitted —
        they are what the handoff exists to drop.
        """
        existing = (self._existing_compact_summary_text() or "").split(RECALL_MARKER, 1)[0].strip()
        start = getattr(self, "_items_at_turn_start", 0) or 0
        source = self.state.items[start:] if existing else self.state.items
        folded = fold_summary(existing, self._exchanges(source)).strip()
        return folded or "(empty session)"

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

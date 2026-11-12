"""
The slash-command catalogue.

This module is *data*: every command's name, aliases, arguments, and help text
live here, and the CLI supplies the behaviour by defining a method with the
matching ``handler`` name. Keeping the catalogue separate is what lets three
different features share one source of truth — ``/help``, tab-completion, and
the "did you mean" suggestion all read from this list, so a command can never
be completable but undocumented, or documented but unroutable.

``check_handlers`` enforces the other half of that contract at startup.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Optional

from .config import (
    ITERATION_NOTICE_EVERY,
    MAX_CONTEXT_TOKENS,
    MIN_CONTEXT_TOKENS,
    MIN_ITERATIONS,
    MIN_OUTPUT_TOKENS,
    Settings,
)

# The help text quotes the default rather than restating it, so the two cannot
# drift apart.
DEFAULT_MAX_ITERATIONS = Settings.max_iterations
DEFAULT_CONTEXT_WINDOW = Settings.max_context_tokens


@dataclass(frozen=True)
class Argument:
    """One positional argument, used for help text and for completion."""

    name: str
    choices: tuple[str, ...] = ()
    description: str = ""
    required: bool = False

    def render(self) -> str:
        body = "|".join(self.choices) if self.choices else self.name
        return f"<{body}>" if self.required else f"[{body}]"


@dataclass(frozen=True)
class Command:
    name: str  # canonical, with the leading slash
    summary: str  # one line, shown in /help
    handler: str  # CLI method name
    group: str = "Session"
    aliases: tuple[str, ...] = ()
    args: tuple[Argument, ...] = ()
    detail: str = ""  # long help, shown by /help <command>
    examples: tuple[str, ...] = ()
    interactive: bool = False  # opens a picker when called with no arguments

    @property
    def usage(self) -> str:
        parts = [self.name] + [a.render() for a in self.args]
        return " ".join(parts)

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name,) + self.aliases

    def first_arg_choices(self) -> tuple[str, ...]:
        return self.args[0].choices if self.args else ()


LEVEL_CHOICES = ("low", "mid", "high")
EFFORT_CHOICES = ("low", "high", "max", "on", "off")
PERMISSION_CHOICES = ("allow", "ask", "block")
VISION_CHOICES = ("on", "off", "status")
LOCAL_CHOICES = ("status", "auto", "on", "off", "tune", "unload", "models", "model", "vision", "scout")
SURGE_SUB_CHOICES = ("status", "on", "off", "buffer", "notify", "now")
LEARN_SUB_CHOICES = ("search", "get", "save", "forget", "outcome")
BBS_SUB_CHOICES = ("list", "search", "read", "post", "index")


COMMANDS: tuple[Command, ...] = (
    # -- Session ----------------------------------------------------------
    Command(
        name="/help",
        aliases=("/?", "/h", "/commands"),
        summary="List commands, or explain one in detail",
        handler="cmd_help",
        group="Session",
        args=(Argument("command", description="A command name, with or without the slash"),),
        detail=(
            "With no argument, prints every command grouped by topic.\n"
            "With one, prints that command's usage, options, and examples."
        ),
        examples=("/help", "/help vision", "/help compact"),
    ),
    Command(
        name="/status",
        aliases=("/dash",),
        summary="One-screen dashboard: model, context, cost, vision, cache",
        handler="cmd_status",
        group="Session",
        detail=(
            "The quick look. Everything that changes during a session on a single\n"
            "screen — use /settings for the exhaustive list of knobs."
        ),
    ),
    Command(
        name="/clear",
        aliases=("/reset",),
        summary="Reset the conversation and usage counters",
        handler="cmd_clear",
        group="Session",
        detail="Drops the message history and zeroes the session's token/cost counters. Settings are untouched.",
    ),
    Command(
        name="/resume",
        aliases=("/continue",),
        summary="Load the latest JSONL session view, or /resume log for a handoff",
        handler="cmd_resume",
        group="Session",
        args=(
            Argument(
                "source",
                ("log",),
                "log = latest .g023/logs/ handoff; omit to load the JSONL session",
            ),
        ),
        detail=(
            "Bare /resume reads .g023/sessions/ (the CURRENT pointer, else the newest\n"
            "jsonl) and replaces the in-memory item list with the latest view. Pairing\n"
            "is repaired. The working-set item is restored from the log / session_facts.\n"
            "Large write bodies were stored as hashes, not 200k-character contents.\n"
            "/resume log (also --continue at launch) seeds a new orchestration from\n"
            "the latest .g023/logs/{dstamp_tstamp}.log: standing system prompt plus the\n"
            "compact summary written when the last objective finished."
        ),
        examples=("/resume", "/resume log"),
    ),
    Command(
        name="/learn",
        summary="Search, save, or forget reusable procedures across sessions",
        handler="cmd_learn",
        group="Session",
        args=(
            Argument(
                "action",
                LEARN_SUB_CHOICES,
                "search, get, save, forget, outcome; bare prints status",
            ),
        ),
        detail=(
            "The learn store holds procedures that travel across projects\n"
            "(~/.g023/learn for env/user, <project>/.g023/learn for this repo).\n"
            "Guides are not in the system prompt. /learn with no argument prints\n"
            "index counts and remaining write budget.\n\n"
            "/learn search <query>  routing lines only\n"
            "/learn get <id>        one guide body\n"
            "/learn save <id> <scope> <when> | <title>\n"
            "                       user-explicit write; bypasses the session cap\n"
            "/learn forget <id>     tombstone; the id cannot be reused\n"
            "/learn outcome <id> helped|harmed"
        ),
        examples=(
            "/learn",
            "/learn search browser snapshot",
            "/learn get chrome-devtools-snapshot",
            "/learn save fetchurl-cache env FetchUrl,cache | Prefer cache vs live",
            "/learn forget fetchurl-cache",
            "/learn outcome fetchurl-cache helped",
        ),
    ),
    Command(
        name="/bbs",
        summary="Project bulletin board at .g023/bbs/ (not in the system prompt)",
        handler="cmd_bbs",
        group="Session",
        args=(
            Argument(
                "action",
                BBS_SUB_CHOICES,
                "list, search, read, post, index; bare prints status",
            ),
        ),
        detail=(
            "The bulletin board is a forum for this session and later ones.\n"
            "Topic slugs are subdirectories of .g023/bbs/; conversations are\n"
            ".txt files. Posts are searched on demand, never prepended to the\n"
            "system prompt. /bbs with no argument prints topic and post counts.\n\n"
            "/bbs list [topic]          topics, or posts in one topic\n"
            "/bbs search <query>        SearchContent over .g023/bbs/\n"
            "/bbs read <topic/slug>     one thread body\n"
            "/bbs post <topic> <slug>   remaining text is the body (appends)\n"
            "/bbs index                 rebuild INDEX.md from the tree"
        ),
        examples=(
            "/bbs",
            "/bbs list cost-efficiency",
            "/bbs search prefix cache",
            "/bbs read cost-efficiency/cache-vs-history",
            "/bbs post open-issues compact-leak Keep Layer-1 off a hot prefix.",
        ),
    ),
    Command(
        name="/undo",
        aliases=("/restore",),
        summary="Restore a file from its newest .g023/backups/ copy",
        handler="cmd_undo",
        group="Session",
        args=(Argument("path", description="File to restore; defaults to the last written path"),),
        detail=(
            "Restores the newest timestamped backup under .g023/backups/ onto the\n"
            "file (atomic, mode kept). The backup is consumed, so /undo again steps\n"
            "one version further back; the replaced version is kept as *.undone.\n"
            "WriteFile and Edit take those backups before overwriting."
        ),
        examples=("/undo", "/undo src/app.py"),
    ),
    Command(
        name="/exit",
        aliases=("/quit", "/q"),
        summary="Quit g023",
        handler="cmd_exit",
        group="Session",
    ),
    # -- Model ------------------------------------------------------------
    Command(
        name="/model",
        summary="Show the model, its limits and prices",
        handler="cmd_model",
        group="Model",
        detail=(
            "g023 Code runs one model: DeepSeek V4.1 Flash (API id deepseek-flash)\n"
            "on DeepSeek's Anthropic-format endpoint; web search and fetch use\n"
            "DeepSeek's Search and Fetch APIs. There is nothing to switch — this prints what is in use, its\n"
            "context and output limits, and what it costs."
        ),
    ),
    Command(
        name="/thinking",
        aliases=("/effort",),
        summary="Set reasoning effort, or turn thinking off",
        handler="cmd_thinking",
        group="Model",
        interactive=True,
        args=(Argument("effort", EFFORT_CHOICES, "1-100 or low/high/max/on/off; 75 is default and ceiling"),),
        detail=(
            "Reasoning tokens are billed as output, so effort is a cost dial as much as\n"
            "a quality one. The scale is 1-100 but 75 is both the default sweetspot and\n"
            "the ceiling — 76-100, max, and xhigh all store and send as 75 (wire high).\n"
            "low/high/max map to 50/75/75. The API accepts named levels, not integers,\n"
            "so 0 is sent as thinking off, 1-50 as low and 51-75 as high (never max).\n"
            "/goal temporarily forces the ceiling (75) regardless of this setting, and\n"
            "also runs its turn with no step cap. The model may also set the next *user\n"
            "request's* effort by attaching [next_effort: N] at the end of a final answer\n"
            "(after an optional [handoff]...[/handoff] compact summary on that same reply).\n"
            "A trailer on a tool-call step is stripped, not applied — it is not this turn's\n"
            "remaining steps. N should match the next request (25-50 routine, 75 when the\n"
            "remaining work is still hard), not copy 75 by habit. Values above 75 cap."
        ),
        examples=("/thinking", "/thinking 75", "/thinking max", "/thinking off", "/thinking on"),
    ),
    Command(
        name="/verbose",
        aliases=("/v",),
        summary="How much detail to print while working",
        handler="cmd_verbose",
        group="Model",
        interactive=True,
        args=(Argument("level", LEVEL_CHOICES, "low | mid | high"),),
        detail=(
            "low  — the answer, plus a one-line trace of every tool call.\n"
            "mid  — adds each tool's outcome, duration, and the per-turn token line.\n"
            "high — adds the model's reasoning excerpts and per-iteration token counts.\n\n"
            "Saved to config.json."
        ),
        examples=("/verbose", "/verbose high"),
    ),
    Command(
        name="/steps",
        aliases=("/iterations", "/maxsteps"),
        summary="Cap how many tool steps one turn may take",
        handler="cmd_steps",
        group="Model",
        args=(
            Argument("count", (), f"{MIN_ITERATIONS} or more, 'off', or 'default'"),
        ),
        detail=(
            "One step is one call to the orchestrator model. A step that comes back\n"
            "with tool calls runs them and takes another step; the first step that\n"
            "answers instead ends the turn. This is the backstop for a turn that keeps\n"
            "tooling without ever answering — when it trips, the turn ends with\n"
            "'Stopped after N tool iterations' instead of an answer.\n\n"
            "There is no upper bound: any count is accepted, and 'off' removes the\n"
            "backstop so a turn runs until the model answers. An uncapped turn is\n"
            f"still visible — every {ITERATION_NOTICE_EVERY} steps it prints where it\n"
            "is, how long it has run and what it has cost, at any verbosity, and\n"
            "Ctrl-C ends it.\n\n"
            f"Default {DEFAULT_MAX_ITERATIONS}. Raising it raises what a single runaway\n"
            "turn can cost, since every step is a billed round trip. With no argument,\n"
            "prints the current cap. Saved to config.json.\n\n"
            "/goal ignores this cap for its own turn and restores it afterwards."
        ),
        examples=("/steps", "/steps 400", "/steps off", "/steps default"),
    ),
    Command(
        name="/max-tokens",
        aliases=("/maxtokens", "/output"),
        summary="Cap how long one reply may be",
        handler="cmd_max_tokens",
        group="Model",
        args=(
            Argument("count", (), f"{MIN_OUTPUT_TOKENS} or more, 'off', or 'default'"),
        ),
        detail=(
            "The output budget for a single orchestrator reply, in tokens. Reasoning\n"
            "is billed out of the same budget as the answer, so a small cap at high\n"
            "effort can end a reply before any of it is written.\n\n"
            "'off' — the default — sends no budget at all, leaving the model's own\n"
            "maximum as the only ceiling. That is not the same as infinite output:\n"
            "the model still has a maximum, and a reply can still be cut off by it.\n"
            "When that happens the turn says so instead of presenting a sentence\n"
            "that stops mid-word.\n\n"
            "There is no upper bound on what this accepts: the number is passed to\n"
            "the API as given, and what the API does with a budget larger than the\n"
            "model can honour — clamp it or reject the request — is its call, not\n"
            "this client's. With no argument, prints the current budget. Saved to\n"
            "config.json.\n\n"
            "This is the orchestrator's reply only. Subagents keep their own small\n"
            "budgets — a file summary or a router plan is meant to be short, and\n"
            "returning it to the orchestrator costs context."
        ),
        examples=(
            "/max-tokens",
            "/max-tokens 32000",
            "/max-tokens off",
            "/max-tokens default",
        ),
    ),
    # -- Context ----------------------------------------------------------
    Command(
        name="/compact",
        summary="Summarise the history to reclaim context",
        handler="cmd_compact",
        group="Context",
        args=(Argument("focus|micro|auto", (), "free-text focus, 'micro', or 'auto on|off'"),),
        detail=(
            "/compact              summarise everything with the cheap model\n"
            "/compact <focus>      summarise, keeping the named topic in most detail\n"
            "/compact micro        free local pass — blanks stale tool results, no API call\n"
            "/compact auto on|off  toggle automatic compaction at the threshold\n\n"
            "With auto on, stale tool-result text is blanked in place each turn\n"
            "(no API call, prefix behind the moving boundary stays still). A full\n"
            "summary replace still waits until the context passes compact_threshold —\n"
            "set that trigger with /compact-threshold, the window with /max-ctx.\n"
            "If the last final answer already attached a [handoff] block, /compact\n"
            "reuses it (no second model call). A focused /compact <topic> still asks\n"
            "the cheap model so the summary can follow that topic."
        ),
        examples=("/compact", "/compact micro", "/compact the auth refactor", "/compact auto off"),
    ),
    Command(
        name="/g023-compact",
        aliases=("/g023compact",),
        summary="Compact g023.md into g023_compact.md (leaves the original)",
        handler="cmd_g023_compact",
        group="Context",
        detail=(
            "g023.md is this project's standing instructions for the assistant —\n"
            "the same role AGENTS.md plays in other agentic CLIs. When present it\n"
            "is appended to the system prompt.\n\n"
            "If g023_compact.md also exists, that file is used instead (the original\n"
            "g023.md is left in place). /g023-compact rewrites the compact file from\n"
            "the current g023.md using deepseek-flash with reasoning off.\n\n"
            "Delete g023_compact.md to fall back to the full file."
        ),
        examples=("/g023-compact",),
    ),
    Command(
        name="/max-ctx",
        aliases=("/max-context", "/ctx"),
        summary="Set the context window ceiling",
        handler="cmd_max_ctx",
        group="Context",
        args=(Argument("tokens", (), f"{MIN_CONTEXT_TOKENS:,}–{MAX_CONTEXT_TOKENS:,}, or 'default'"),),
        detail=(
            "The ceiling the conversation may grow into, in tokens. The status bar,\n"
            "/context and /cost all read against it, and auto-compact fires at\n"
            "compact_threshold of it — so this is what actually decides how long a\n"
            "session runs before the cheap model summarises it.\n\n"
            f"DeepSeek's real ceiling is 1M tokens; the shipped default is "
            f"{DEFAULT_CONTEXT_WINDOW // 1000}k.\n"
            "With no argument, prints the current window. Saved to config.json."
        ),
        examples=("/max-ctx", "/max-ctx 1000000", "/max-ctx default"),
    ),
    Command(
        name="/compact-threshold",
        aliases=("/compact-at",),
        summary="Set when auto-compact fires: a token count (200k) or a window fraction",
        handler="cmd_compact_threshold",
        group="Context",
        args=(Argument("tokens|fraction|policy", ("economic", "fixed", "floor"), "e.g. 200k, 0 (fraction only), 0.85, 90%, 'default', economic|fixed, floor 64k"),),
        detail=(
            "Auto-compact summarises the history once the live context reaches\n"
            "compact_at_tokens (default 200k), or compact_threshold of the window if\n"
            "that is lower. It is checked at turn start AND between every tool step,\n"
            "so one long agentic turn cannot run away toward the 1M ceiling.\n\n"
            "A token count (200k, 1m, 150000) sets compact_at_tokens; 0 means use\n"
            "the fraction only. A fraction (0.01–1.0, or 90%) sets the ceiling.\n"
            "Every summary ends with a pointer to the on-disk transcript\n"
            "(.g023/sessions/*.jsonl) so details can be recalled with SessionSearch.\n\n"
            "Policy 'economic' (default) also compacts earlier, once the cache-hit\n"
            "rent of re-reading the history since the last compaction exceeds the\n"
            "price of compacting (x3 for summary fidelity), never below the floor\n"
            "(default 64k). 'fixed' uses the token trigger only. /cost shows the meter.\n"
            "Saved to config.json."
        ),
        examples=(
            "/compact-at",
            "/compact-at 200k",
            "/compact-threshold 0.9",
            "/compact-threshold default",
            "/compact-at fixed",
            "/compact-at floor 96k",
        ),
    ),
    Command(
        name="/subagents",
        aliases=("/subagent",),
        summary="Allow or forbid subagents (Agent tool, LLM ReadFile focus) — off by default",
        handler="cmd_subagents",
        group="Context",
        args=(Argument("on|off", ("on", "off"), "with no argument, prints the state"),),
        detail=(
            "Off (the default): the orchestrator works alone. The Agent tool is not\n"
            "offered, and ReadFile's focus is answered by local ranking, not a\n"
            "model call. On: Agent (research/plan/todos) is offered and ReadFile\n"
            "focus may ask the cheap model. Saved to config.json; takes effect on\n"
            "the next request (the tool list is part of the cached prefix)."
        ),
        examples=("/subagents", "/subagents on", "/subagents off"),
    ),
    Command(
        name="/ownership",
        summary="Whose code the model may tidy while it works: auto | user | agent",
        handler="cmd_ownership",
        group="Context",
        args=(
            Argument(
                "mode", ("auto", "user", "agent", "forget"),
                "with no argument, prints the state; forget <path> hands a file back to you",
            ),
        ),
        detail=(
            "auto (default): in files g023 created itself (WriteFile on a new path,\n"
            "listed in .g023/provenance.json) the model merges repeated logic it\n"
            "touches and fixes defects it notices, naming each change. Your files\n"
            "stay minimal-change. user: minimal-change everywhere (the prompt is\n"
            "exactly the pre-ownership one). agent: the tidy-up rule covers the whole\n"
            "project. Measured on deepseek-flash: the rule merged a duplicated loop\n"
            "6/6 vs 0/6 without it, about +28% cost on that task. Saved to config.json."
        ),
        examples=("/ownership", "/ownership user", "/ownership agent", "/ownership forget src/x.py"),
    ),
    Command(
        name="/context",
        summary="Break down what is occupying the context window",
        handler="cmd_context",
        group="Context",
        detail=(
            "Shows the message history by role and by size, so it is obvious whether\n"
            "the window is full of tool results (compact micro) or of real conversation\n"
            "(compact properly)."
        ),
    ),
    Command(
        name="/cost",
        aliases=("/usage",),
        summary="Token usage and estimated spend, split by cache hit/miss",
        handler="cmd_cost",
        group="Context",
        detail=(
            "A cache hit costs roughly a fiftieth of a miss, so the hit rate is the\n"
            "number that actually determines the bill. Estimates only — DeepSeek bills."
        ),
    ),
    Command(
        name="/signals",
        aliases=("/drift",),
        summary="What the loop noticed but nobody raised an error about",
        handler="cmd_signals",
        group="Context",
        detail=(
            "The client does not validate the API's JSON, so that a field DeepSeek adds\n"
            "tomorrow still reaches the model. The cost of that is that a *renamed* field\n"
            "fails quietly — the model just looks silent. This is the view from outside:\n\n"
            "  prefix-cache hit rate  today against the days before it, per project\n"
            "  unknown item types     response items this client does not read\n"
            "  empty responses        output items, no text, and no stated reason\n\n"
            "None of it separates a schema change from a model change from drift in our\n"
            "own prompts — those look identical from here. It only says something moved."
        ),
    ),
    Command(
        name="/settings",
        summary="Show every setting; set, save or reset the persisted ones",
        handler="cmd_settings",
        group="Context",
        args=(Argument("save|reset|set", ("save", "reset", "set"), "persist, restore defaults, or set one key"),),
        detail=(
            "/settings                 show everything, marking which keys persist\n"
            "/settings set KEY VALUE   change one persisted key (typed + validated) and save\n"
            "/settings save            write the persisted keys to config.json\n"
            "/settings reset           restore the built-in defaults and save"
        ),
    ),
    # -- Tools ------------------------------------------------------------
    Command(
        name="/tools",
        aliases=("/permissions", "/perm"),
        summary="List tools and change what they may do without asking",
        handler="cmd_tools",
        group="Tools",
        args=(
            Argument("tool", (), "tool name, e.g. Bash"),
            Argument("level", PERMISSION_CHOICES, "allow | ask | block"),
        ),
        detail=(
            "With no arguments, lists every tool, its permission level, and how many\n"
            "times it has run this session.\n\n"
            "allow — runs silently\n"
            "ask   — prompts every time (the default for anything that writes or\n"
            "        leaves the machine). At the prompt, 'a' promotes the tool to\n"
            "        allow for the rest of the session.\n"
            "block — refused, and the model is told so\n\n"
            "WebSearch is allowed by default (a query to DeepSeek, cached machine-wide).\n"
            "FetchUrl follows permission_default; when it asks, the prompt is also where\n"
            "you choose the cached copy or the network."
        ),
        examples=("/tools", "/tools Bash allow", "/tools WriteFile block"),
    ),
    Command(
        name="/fetch",
        summary="Fetch a URL yourself (DeepSeek Fetch API, machine-wide cache)",
        handler="cmd_fetch",
        group="Tools",
        args=(Argument("url", (), "the page to read"),),
        detail=(
            "/fetch <url>     fetch a page and display it as Markdown\n\n"
            "Pages come from DeepSeek's Fetch API ($0.75 per 1,000 successful\n"
            "fetches; refusals are free) and are kept in the machine-wide cache\n"
            "(<install>/global_cache/web.db) that every project and running instance\n"
            "shares. Pages of WebSearch hits are usually there already: each search\n"
            "files its hits' content at no extra cost."
        ),
        examples=("/fetch https://example.com",),
    ),
    Command(
        name="/search",
        summary="Search the web yourself (DeepSeek Search API, machine-wide cache)",
        handler="cmd_search",
        group="Tools",
        args=(Argument("query", (), "1-128 characters; site:, \"phrase\", -term work"),),
        detail=(
            "/search <query>        show the hits the model would see\n"
            "/search fresh <query>  bypass the cache\n\n"
            "Same cache and rules as the WebSearch tool: term order and case do not\n"
            "matter; time-sensitive queries stay fresh 6 h, others 7 days."
        ),
        examples=("/search httpx latest release", "/search site:docs.python.org asyncio TaskGroup"),
    ),
    Command(
        name="/cache",
        summary="Inspect and purge the caches (project + machine-wide web)",
        handler="cmd_cache",
        group="Tools",
        args=(Argument("stats|clear|web|web clear|web prune", ("stats", "clear", "web"), "default: stats"),),
        detail=(
            "/cache             what is cached, the web savings ledger, and space used\n"
            "/cache web         list cached web searches and pages (machine-wide)\n"
            "/cache web clear   purge the machine-wide web cache (all projects)\n"
            "/cache web prune   drop web entries unused for 90 days\n"
            "/cache clear       purge this project's caches (file summaries, vision\n"
            "                   answers, tool vault); the web cache is left alone"
        ),
    ),
    # -- Vision -----------------------------------------------------------
    Command(
        name="/vision",
        aliases=("/config_vision",),
        summary="Turn image analysis on or off",
        handler="cmd_vision",
        group="Vision",
        interactive=True,
        args=(Argument("on|off|status", VISION_CHOICES, "bare shows status and offers a toggle"),),
        detail=(
            "AnalyzeImage sends one isolated request to deepseek-flash: the image\n"
            "(capped at 800 px on its longest side; jpeg, png, gif or webp) plus\n"
            "the question, with thinking off. Only the short text answer comes\n"
            "back into the conversation, and answers are cached per image and\n"
            "question. Local images must be inside the project folder.\n\n"
            "/vision on       offer AnalyzeImage to the model (default)\n"
            "/vision off      hide it; images are never sent\n"
            "/vision status   show the current setting"
        ),
        examples=("/vision", "/vision on", "/vision off"),
    ),
    Command(
        name="/local",
        summary="Local llama.cpp helper model — status, auto/on/off, tune, unload, model, vision",
        handler="cmd_local",
        group="Model",
        args=(Argument("status|auto|on|off|tune|unload|models|model NAME|vision NAME|scout MODE", LOCAL_CHOICES, "bare shows status"),),
        detail=(
            "A small model served by llama.cpp's llama-server on this machine does free\n"
            "side jobs: it reranks which lines of a long command output DeepSeek sees,\n"
            "answers LocalAsk reading requests (files, or the whole project; verbatim cited\n"
            "lines), pre-reads each request into a short [scout] card of the lines that\n"
            "locate it, and, with a vision model set, answers AnalyzeImage first.\n"
            "DeepSeek still makes every decision. One server is shared by all sessions; it\n"
            "sleeps when idle and is released when another process needs the GPU or a\n"
            "command looks like GPU work.\n\n"
            "/local status         mode, model, server state (default)\n"
            "/local auto           default: on when llama-server, a model and a GPU it\n"
            "                      fits are found; picks the best measured model (saved)\n"
            "/local on | off       force on / never (saved)\n"
            "/local tune           re-run the machine probe and show what auto picks\n"
            "/local unload         stop the server now (frees VRAM)\n"
            "/local models         .gguf files in local_models_dir\n"
            "/local model NAME     pin the model file (local_model_file); 'auto' unpins\n"
            "/local vision NAME    pin a vision model (mmproj found next to it); 'off' =\n"
            "                      DeepSeek vision only; 'auto' = vision when the model has one\n"
            "/local scout MODE     scout card: local (default; only with the local model),\n"
            "                      always (keyword-matched code units without it), off\n\n"
            "Paths: /settings set local_llama_dir PATH, local_models_dir PATH."
        ),
        examples=("/local", "/local tune", "/local auto", "/local model LFM3-1.7B-Q8_0.gguf", "/local vision off"),
    ),
    Command(
        name="/surge",
        summary="DeepSeek surge pricing — status, notify, auto-pause around peak",
        handler="cmd_surge",
        group="Model",
        args=(
            Argument("status|on|off|buffer|notify|now", SURGE_SUB_CHOICES, "see /help surge"),
            Argument("value", (), "minutes for 'buffer', on|off for 'notify'"),
        ),
        detail=(
            "DeepSeek bills peak/off-peak: peak hours are 09:00-12:00 and\n"
            "14:00-18:00 Beijing time (UTC+8) — converted to your local clock\n"
            "here (AM/PM and 24-hour) — and off-peak prices are half of peak.\n"
            "Status shows the countdown to the next surge and how long it lasts.\n\n"
            "/surge                 status: state, local windows, countdown +\n"
            "                       duration, reg vs surge prices\n"
            "/surge now             the same panel, labelled as the current instant\n"
            "/surge notify on|off   show a banner when surge pricing starts/ends\n"
            "/surge on              pause usage from 5 min before peak until 5 min\n"
            "                       after it ends, then resume automatically\n"
            "/surge buffer <min>    widen the pause by N minutes each side\n\n"
            "While paused the CLI waits out the window instead of reading input,"
            "\nso no requests are sent at double price. Ctrl-C always works."
        ),
        examples=("/surge", "/surge now", "/surge on", "/surge buffer 10", "/surge notify off"),
    ),
    Command(
        name="/config",
        aliases=("/cfg",),
        summary="Per-project vs global config, and set current settings as global default",
        handler="cmd_config",
        group="Session",
        interactive=True,
        args=(
            Argument("status|save|global|project|clear", ("status", "save", "global", "project", "clear"), "subcommand"),
        ),
        detail=(
            "Settings live in two files: the machine-level config.json next to\n"
            "K.dat (applies to every project) and, if present, the project's\n"
            "own .g023/config.json (overrides the machine file for this project).\n\n"
            "/config              show both files and which one is active\n"
            "/config save         write current settings to the active scope\n"
            "/config save global  write them to the machine-level config.json\n"
            "/config global       set current settings as the global default\n"
            "                     (same as 'save global' — new projects inherit)\n"
            "/config save project pin them to this project only\n"
            "/config clear        drop this project's override; global applies\n\n"
            "So you can tune settings inside a project, then promote the result\n"
            "to the global default for every future project."
        ),
        examples=("/config", "/config global", "/config save project", "/config clear"),
    ),
    # -- Work -------------------------------------------------------------
    Command(
        name="/goal",
        summary="State a high-level objective; ceiling effort, no step limit",
        handler="cmd_goal",
        group="Work",
        args=(Argument("objective", (), "what you want achieved", required=True),),
        detail=(
            "Runs with reasoning effort forced to the harness ceiling (75 / high —\n"
            "not the API's max, which burns the output budget on thinking), the\n"
            "per-turn step cap lifted, and a prompt that asks the model to break the\n"
            "objective into steps and use tools freely. A goal runs until the model\n"
            "answers rather than stopping at /steps; progress prints as it goes and\n"
            "Ctrl-C ends it. If the working-set judge still says not completed, up\n"
            "to two short continuation turns name the open leaves. A continuation\n"
            "that does not change the working set stops, so the same unfinished\n"
            "answer is not bought again. Identical tool calls in one turn are\n"
            "skipped after the second. A [next_effort: N] on the final answer is\n"
            "remembered. It is sent once this transcript is replaced, or you\n"
            "/thinking or /clear. Until then the wire stays at the effort the\n"
            "goal already warmed, so that prefix still hits. If the model omits\n"
            "the trailer, the previous /thinking dial is restored the same way.\n"
            "/steps is always restored."
        ),
        examples=("/goal add retry-with-backoff to every outbound HTTP call",),
    ),
)


COMMANDS_BY_NAME: dict[str, Command] = {}
for _cmd in COMMANDS:
    for _n in _cmd.names:
        COMMANDS_BY_NAME[_n] = _cmd

GROUP_ORDER = ("Session", "Model", "Context", "Tools", "Vision", "Work")


def lookup(token: str) -> Optional[Command]:
    """Resolve a typed token to a command, tolerating a missing leading slash."""
    token = token.strip().lower()
    if not token:
        return None
    if not token.startswith("/"):
        token = "/" + token
    return COMMANDS_BY_NAME.get(token)


def suggest(token: str, limit: int = 3) -> list[str]:
    """Close matches for an unknown command, for the 'did you mean' line."""
    token = token.strip().lower()
    if not token.startswith("/"):
        token = "/" + token
    names = list(COMMANDS_BY_NAME)
    # Someone who typed a prefix is mid-word, not misspelling: if anything starts
    # with what they typed, edit-distance guesses would only add noise.
    ordered = sorted(n for n in names if n.startswith(token))
    if not ordered:
        ordered = difflib.get_close_matches(token, names, n=limit * 2, cutoff=0.6)
    # Collapse aliases onto their canonical command so we never suggest both.
    seen: set[str] = set()
    out: list[str] = []
    for n in ordered:
        canonical = COMMANDS_BY_NAME[n].name
        if canonical in seen:
            continue
        seen.add(canonical)
        out.append(n)
    return out[:limit]


def completions(text: str) -> list[str]:
    """
    Completion candidates for the current input line.

    Completes the command itself, then that command's first-argument choices —
    so ``/th`` offers ``/thinking`` and ``/thinking `` offers low/high/max/on/off.
    """
    text = text.lstrip()
    if not text.startswith("/"):
        return []

    parts = text.split()
    typing_new_word = text.endswith((" ", "\t"))

    if len(parts) == 1 and not typing_new_word:
        return sorted(n for n in COMMANDS_BY_NAME if n.startswith(parts[0].lower()))

    cmd = lookup(parts[0])
    if cmd is None:
        return []

    choices = cmd.first_arg_choices()
    if not choices:
        return []
    if typing_new_word and len(parts) == 1:
        return list(choices)
    if len(parts) == 2 and not typing_new_word:
        return [c for c in choices if c.startswith(parts[1].lower())]
    return []


def by_group() -> list[tuple[str, list[Command]]]:
    """Commands bucketed for /help, in a deliberate reading order."""
    groups: dict[str, list[Command]] = {}
    for cmd in COMMANDS:
        groups.setdefault(cmd.group, []).append(cmd)
    ordered = [(g, groups[g]) for g in GROUP_ORDER if g in groups]
    ordered += [(g, c) for g, c in sorted(groups.items()) if g not in GROUP_ORDER]
    return ordered


def check_handlers(obj: Any) -> list[str]:
    """Names of commands whose handler method is missing — empty list means sane."""
    return [c.name for c in COMMANDS if not callable(getattr(obj, c.handler, None))]

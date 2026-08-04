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
EFFORT_CHOICES = ("low", "high", "max", "off")
MODEL_CHOICES = ("flash", "pro")
PERMISSION_CHOICES = ("allow", "ask", "block")
BACKEND_CHOICES = ("none", "ollama", "glm", "openai", "local")


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
        examples=("/help", "/help ollama", "/help compact"),
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
        name="/exit",
        aliases=("/quit", "/q"),
        summary="Quit g023",
        handler="cmd_exit",
        group="Session",
    ),
    # -- Model ------------------------------------------------------------
    Command(
        name="/model",
        summary="Choose the orchestrator model",
        handler="cmd_model",
        group="Model",
        interactive=True,
        args=(Argument("model", MODEL_CHOICES, "flash is the default; pro costs ~3x"),),
        detail=(
            "flash — deepseek-v4-flash. The default, and the right answer most of the time.\n"
            "pro   — deepseek-v4-pro. Roughly three times the price; reach for it when\n"
            "        a task is genuinely hard, not merely long.\n\n"
            "Called with no argument, this opens a picker showing both prices."
        ),
        examples=("/model", "/model pro"),
    ),
    Command(
        name="/thinking",
        aliases=("/effort",),
        summary="Set reasoning effort, or turn thinking off",
        handler="cmd_thinking",
        group="Model",
        interactive=True,
        args=(Argument("effort", EFFORT_CHOICES, "higher effort spends more output tokens"),),
        detail=(
            "Reasoning tokens are billed as output, so effort is a cost dial as much as\n"
            "a quality one. /goal temporarily forces max regardless of this setting."
        ),
        examples=("/thinking", "/thinking max", "/thinking off"),
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
            "Automatic compaction fires once the context passes compact_threshold."
        ),
        examples=("/compact", "/compact micro", "/compact the auth refactor", "/compact auto off"),
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
        name="/settings",
        aliases=("/config",),
        summary="Show every setting; save or reset the persisted ones",
        handler="cmd_settings",
        group="Context",
        args=(Argument("save|reset", ("save", "reset"), "persist or restore defaults"),),
        detail=(
            "/settings        show everything, marking which keys persist\n"
            "/settings save   write the persisted keys to config.json\n"
            "/settings reset  restore the built-in defaults and save"
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
            "FetchUrl always asks, whatever the level says: a fetch touches a third\n"
            "party's server, and the prompt is also where you choose cache vs network."
        ),
        examples=("/tools", "/tools Bash allow", "/tools WriteFile block"),
    ),
    Command(
        name="/fetch",
        summary="Fetch a URL yourself, with the cache/fresh prompt",
        handler="cmd_fetch",
        group="Tools",
        args=(Argument("url|status|cookies clear", (), "a URL, or a subcommand"),),
        detail=(
            "/fetch <url>           fetch and display a page as readable text\n"
            "/fetch status          report how closely requests imitate a real browser\n"
            "/fetch cookies clear   forget stored cookies\n\n"
            "Install curl_cffi to match Chrome's TLS fingerprint; /fetch status says\n"
            "which engine you are actually on."
        ),
        examples=("/fetch https://example.com", "/fetch status"),
    ),
    Command(
        name="/cache",
        summary="Inspect and purge the local SQLite caches",
        handler="cmd_cache",
        group="Tools",
        args=(Argument("stats|clear|web|web clear", ("stats", "clear", "web"), "default: stats"),),
        detail=(
            "/cache             what is cached, and how much space it uses\n"
            "/cache web         list cached URL fetches\n"
            "/cache web clear   purge only the URL cache\n"
            "/cache clear       purge everything (file summaries, vision answers, URLs)"
        ),
    ),
    # -- Vision -----------------------------------------------------------
    Command(
        name="/ollama",
        summary="Point vision at any Ollama daemon — local or remote",
        handler="cmd_ollama",
        group="Vision",
        args=(
            Argument("status|host|models|test|ps", ("status", "host", "models", "test", "ps")),
            Argument("value", (), "for 'host': an address, or 'default'"),
        ),
        detail=(
            "The daemon does not have to run on this machine.\n\n"
            "/ollama                    status: host, version, latency, models\n"
            "/ollama host <addr>        point at another machine and test it\n"
            "/ollama host default       fall back to OLLAMA_HOST, then localhost\n"
            "/ollama models             list installed models and their capabilities\n"
            "/ollama test [model]       run a real inference against the daemon\n"
            "/ollama ps                 what the daemon currently holds in VRAM\n\n"
            "Addresses are forgiving: a bare host or IP gets :11434 appended, and a\n"
            "missing scheme becomes http://. The host is saved to config.json.\n\n"
            "For a remote daemon to accept connections it must be bound to the network\n"
            "on that machine — OLLAMA_HOST=0.0.0.0:11434 ollama serve — and the port\n"
            "must be open. Ollama has no authentication, so keep it on a trusted\n"
            "network or in front of an SSH tunnel or authenticating proxy."
        ),
        examples=(
            "/ollama",
            "/ollama host 192.168.1.50",
            "/ollama host gpu-box:11434",
            "/ollama host default",
            "/ollama test",
        ),
    ),
    Command(
        name="/vision",
        aliases=("/config_vision",),
        summary="Enable, disable, or configure image analysis",
        handler="cmd_vision",
        group="Vision",
        interactive=True,
        args=(
            Argument("model|off|status|backend", (), "a model name, or a subcommand"),
            Argument("value", BACKEND_CHOICES, "for 'backend'"),
        ),
        detail=(
            "DeepSeek V4 is text-only, so images go to a local vision model over Ollama.\n"
            "Vision is off by default, and while it is off the AnalyzeImage tool is not\n"
            "even offered to the orchestrator.\n\n"
            "/vision                  interactive picker of installed vision models\n"
            "/vision <model>          enable a specific model directly\n"
            "/vision off              disable\n"
            "/vision status           show the current setting\n"
            "/vision backend <name>   switch backend (only 'ollama' is implemented)\n\n"
            "Use /ollama host to choose which daemon serves it."
        ),
        examples=("/vision", "/vision qwen2.5vl:7b", "/vision off"),
    ),
    # -- Work -------------------------------------------------------------
    Command(
        name="/goal",
        summary="State a high-level objective; runs at max reasoning effort",
        handler="cmd_goal",
        group="Work",
        args=(Argument("objective", (), "what you want achieved", required=True),),
        detail=(
            "Runs one turn with reasoning effort forced to max and a prompt that asks\n"
            "the model to break the objective into steps and use tools freely. Your\n"
            "usual /thinking setting is restored afterwards."
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
    so ``/th`` offers ``/thinking`` and ``/thinking `` offers low/high/max/off.
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

"""
Interactive CLI for g023 Code.

Every slash command is declared in :mod:`g023_code.commands` and implemented
here as a ``cmd_*`` method. Commands that take a fixed set of options open a
picker when called bare, so nothing has to be memorised: ``/model`` is as
usable as ``/model pro``.
"""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Optional

from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import __version__
from . import commands as registry
from . import ollama_client
from . import openrouter_client as or_client
from . import or_subagents
from . import project_instructions
from . import surge
from . import ui
from .commands import Command
from .effort import MAX_EFFORT, parse_effort_value
from .config import (
    AVAILABLE_MODELS,
    ITERATION_NOTICE_EVERY,
    MODEL_ALIASES,
    MAX_COMPACT_THRESHOLD,
    MAX_CONTEXT_TOKENS,
    MIN_COMPACT_THRESHOLD,
    MIN_CONTEXT_TOKENS,
    MIN_ITERATIONS,
    MIN_OUTPUT_TOKENS,
    PERSISTED_KEYS,
    Settings,
    UNAVAILABLE_MODELS,
    UNLIMITED_ITERATIONS,
    UNLIMITED_OUTPUT_TOKENS,
    describe_compact_threshold,
    describe_context_window,
    describe_max_tokens,
    describe_steps,
    get_config_file,
    get_home,
    get_project_config_file,
    get_project_root,
    get_scratch_dir,
    load_config,
    save_config,
    settings,
    load_api_key,
    load_openrouter_key,
)
from .orchestrator import Orchestrator
from .cache import get_cache
from .working_set import (
    judge_goal_completion,
    load as load_working_set,
    mirror_working_set,
    set_goal,
)
from .signals import get_signals, hit_rate_verdict
from .prompt import Reader, choose, confirm, install_hint
from .tools.registry import ALLOW, ASK, BLOCK, get_registry, restore_newest_backup, _describe_age
from .ui import Glyph, console, gauge, human_bytes, human_ms, kv_table, panel
from .usage import PRICING, format_cost, get_usage


class CLI:
    def __init__(self):
        self.orch = Orchestrator()
        self.running = True
        self.reader = Reader(
            history_file=get_scratch_dir() / "history",
            status=self.toolbar,
        )
        missing = registry.check_handlers(self)
        if missing:  # a wiring mistake, not a user error — fail loudly at startup
            raise RuntimeError(f"Commands without handlers: {', '.join(missing)}")
        self.last_goal_judgment: Optional[dict] = None

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------

    def toolbar(self) -> str:
        """Bottom status bar (prompt_toolkit only) — plain text with HTML tags."""
        usage = get_usage()
        frac = self.orch.context_fraction()
        vision = settings.effective_vision_model if settings.vision_enabled else "off"
        surge_badge = " · SURGE 2×" if surge.is_surge() else ""
        or_sub_badge = " · or-sub" if settings.or_sub_enabled else ""
        return (
            f" {settings.orchestrator_model} · think {settings.reasoning_effort} "
            f"· ctx {frac:.0%} of {settings.max_context_tokens // 1000}k "
            f"· {format_cost(usage.cost())} · vision {vision}{surge_badge}{or_sub_badge} · /help "
        )

    def print_banner(self):
        probe = ollama_client.probe(count_models=True, timeout=1.5)
        ollama_line = (
            f"[ok]{probe.host}[/ok] [muted]{Glyph.DOT} {probe.summary}[/muted]"
            if probe.ok
            else f"[muted]{probe.host} {Glyph.DOT} {probe.error}[/muted]"
        )
        console.print()
        console.print(
            ui.banner(
                version=__version__,
                project=str(get_project_root()),
                home=str(get_home()),
                model=settings.orchestrator_model,
                effort=str(settings.reasoning_effort) if settings.thinking_enabled else "off",
                verbose=settings.verbose,
                vision=self.vision_status(),
                ollama=ollama_line,
            )
        )
        hint = install_hint()
        if hint:
            console.print(hint)

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    async def handle_slash(self, line: str) -> None:
        parts = line.strip().split(maxsplit=1)
        token = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""

        command = registry.lookup(token)
        if command is None:
            console.print(f"[warn]Unknown command:[/warn] {token}")
            suggestions = registry.suggest(token)
            if suggestions:
                console.print(f"[muted]Did you mean {', '.join(suggestions)}?[/muted]")
            else:
                console.print("[muted]/help lists everything.[/muted]")
            return

        handler = getattr(self, command.handler)
        result = handler(arg)
        if asyncio.iscoroutine(result):
            await result

    # ------------------------------------------------------------------
    # Session commands
    # ------------------------------------------------------------------

    def cmd_help(self, arg: str = "") -> None:
        if arg:
            command = registry.lookup(arg.split()[0])
            if command is None:
                console.print(f"[warn]No such command:[/warn] {arg}")
                suggestions = registry.suggest(arg)
                if suggestions:
                    console.print(f"[muted]Did you mean {', '.join(suggestions)}?[/muted]")
                return
            self._print_command_help(command)
            return

        console.print()
        for group, commands in registry.by_group():
            table = Table(show_header=False, box=None, padding=(0, 2, 0, 1))
            table.add_column(style="brand", no_wrap=True)
            table.add_column(overflow="fold")
            for c in commands:
                table.add_row(escape(self._short_usage(c)), c.summary)
            console.print(panel(table, group))

        console.print(
            f"[muted]{Glyph.BULLET} [bold]/help <command>[/bold] explains one in detail "
            f"{Glyph.DOT} commands with options open a picker when typed bare "
            f"{Glyph.DOT} anything not starting with / is a prompt[/muted]"
        )
        if self.reader.enhanced:
            console.print(
                f"[muted]{Glyph.BULLET} Tab completes {Glyph.DOT} ↑/↓ history "
                f"{Glyph.DOT} Esc-Enter for a new line[/muted]"
            )

    @staticmethod
    def _short_usage(command: Command, limit: int = 30) -> str:
        """
        The listing shows the first argument only, and only when it stays short.

        A full usage string for something like /vision is wider than the summary
        column, and a wrapped signature is harder to read than no signature at
        all — /help <command> is one keystroke away.
        """
        if not command.args:
            return command.name
        candidate = f"{command.name} {command.args[0].render()}"
        if len(candidate) <= limit:
            return candidate + (f" {Glyph.ELLIPSIS}" if len(command.args) > 1 else "")
        return f"{command.name} {Glyph.ELLIPSIS}"

    def _print_command_help(self, command: Command) -> None:
        body: list = [Text(command.summary, style="bold")]
        body.append(Text(f"\nUsage: {command.usage}", style="dim"))
        if command.aliases:
            body.append(Text(f"Aliases: {', '.join(command.aliases)}", style="dim"))
        if command.args:
            # escape(): an argument renders as [a|b|c], which rich would eat as markup.
            rows = [(escape(a.render()), a.description or "") for a in command.args]
            body.append(Text())
            body.append(kv_table(rows))
        if command.detail:
            body.append(Text())
            body.append(Text(command.detail))
        if command.examples:
            body.append(Text("\nExamples", style="bold"))
            for e in command.examples:
                body.append(Text(f"  {e}", style="accent"))
        console.print()
        console.print(panel(ui.Group(*body), command.name))

    def cmd_status(self, arg: str = "") -> None:
        usage = get_usage()
        total = usage.totals()
        frac = self.orch.context_fraction()

        info = kv_table(
            [
                (
                    "model",
                    f"{settings.orchestrator_model} [muted]{Glyph.DOT} thinking "
                    f"{settings.reasoning_effort if settings.thinking_enabled else 'off'} "
                    f"{Glyph.DOT} verbose {settings.verbose}[/muted]",
                ),
                (
                    "session",
                    f"{self.orch.state.turn} turn(s) {Glyph.DOT} "
                    f"{len(self.orch.state.items)} item(s) {Glyph.DOT} "
                    f"{self.orch.state.compactions} compaction(s) {Glyph.DOT} "
                    f"auto-compact {'on' if settings.auto_compact else 'off'}",
                ),
                ("vision", self.vision_status()),
                (
                    "or-sub",
                    f"{'on' if settings.or_sub_enabled else 'off'} {Glyph.DOT} "
                    f"primary {or_subagents.or_sub_primary() or 'none'} {Glyph.DOT} "
                    f"/orsub to manage",
                ),
                (
                    "ollama",
                    f"{ollama_client.resolve_host()} [muted]({ollama_client.host_source()})[/muted]",
                ),
                ("project", str(get_project_root())),
                ("instructions", self._g023_status_line()),
                ("cache", self._cache_size_line()),
            ]
        )
        console.print()
        console.print(panel(info, "Status"))

        ctx = Table(show_header=False, box=None, padding=(0, 2, 0, 0))
        ctx.add_column(style="key", no_wrap=True)
        ctx.add_column()
        bar = gauge(frac, width=28)
        ctx.add_row(
            "context",
            Text.assemble(
                bar, f" {usage.context_tokens:,} / {settings.max_context_tokens:,} ({frac:.0%})"
            ),
        )
        ctx.add_row(
            "tokens",
            f"{total.input_tokens:,} in ({total.hit_rate():.0%} cached) {Glyph.DOT} "
            f"{total.output_tokens:,} out {Glyph.DOT} {total.reasoning_tokens:,} reasoning",
        )
        ctx.add_row("cost", f"[cost]{format_cost(usage.cost())}[/cost] this session")
        if self.orch.state.history:
            costs = [h.cost for h in self.orch.state.history]
            ctx.add_row(
                "per turn",
                f"[accent]{ui.sparkline(costs)}[/accent] [muted]cost across "
                f"{len(costs)} turn(s)[/muted]",
            )
        console.print(panel(ctx, "Context & Spend"))

    def cmd_clear(self, arg: str = "") -> None:
        self.orch.reset()
        console.print(f"[ok]{Glyph.CHECK} Conversation and counters cleared.[/ok]")

    def cmd_resume(self, arg: str = "") -> None:
        token = (arg or "").strip().lower()
        if token in ("log", "logs", "handoff"):
            self._resume_objective_log()
            return
        items, ws, path = self.orch.resume_from_latest()
        if path is None:
            # Same-session JSONL missing: fall back to the last objective handoff.
            if self._resume_objective_log(quiet_if_missing=True):
                return
            console.print("[muted]No session log under .g023/sessions/.[/muted]")
            return
        n = len(items)
        console.print(
            f"[ok]{Glyph.CHECK} Resumed {n} item(s) from [muted]{path}[/muted][/ok]"
        )
        mismatch = getattr(self.orch, "prefix_mismatch", None)
        if mismatch:
            console.print(f"[warn]{mismatch}[/warn]")

    def _resume_objective_log(self, quiet_if_missing: bool = False) -> bool:
        items, ws, path = self.orch.seed_from_objective_log()
        if path is None:
            if not quiet_if_missing:
                console.print("[muted]No objective handoff under .g023/logs/.[/muted]")
            return False
        n = len(items)
        console.print(
            f"[ok]{Glyph.CHECK} New orchestration from handoff "
            f"[muted]{path}[/muted] ({n} item(s))[/ok]"
        )
        return True

    def cmd_learn(self, arg: str = "") -> None:
        from . import learn

        raw = (arg or "").strip()
        if not raw:
            st = learn.status()
            console.print(
                f"learn [muted]home {st['home']} ({st['home_index']} index) "
                f"{Glyph.DOT} project {st['project']} ({st['project_index']} index) "
                f"{Glyph.DOT} puts left {st['session_puts_remaining']}[/muted]"
            )
            return
        parts = raw.split(None, 1)
        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if action == "search":
            if not rest:
                console.print("[muted]Usage: /learn search <query>[/muted]")
                return
            hits = learn.search(rest)
            if not hits:
                console.print("[muted]No catalog hits.[/muted]")
                return
            for hit in hits:
                console.print(hit.get("line") or hit.get("id"))
            return
        if action == "get":
            gid = rest.split()[0] if rest else ""
            if not gid:
                console.print("[muted]Usage: /learn get <id>[/muted]")
                return
            result = learn.get(gid, count_turn=False)
            if not result.get("ok"):
                console.print(f"[warn]{result.get('error')}[/warn]")
                return
            console.print(result.get("body") or "")
            return
        if action == "save":
            # /learn save <id> <scope> <when> | <title>
            if "|" in rest:
                left, _, title = rest.partition("|")
            else:
                left, title = rest, ""
            bits = left.split()
            if len(bits) < 2:
                console.print(
                    "[muted]Usage: /learn save <id> <scope> <when> | <title>[/muted]"
                )
                return
            gid, scope = bits[0], bits[1]
            when = ",".join(bits[2:]) if len(bits) > 2 else gid
            title = title.strip() or gid
            body = (
                f"# When\n\n{title}\n\n# Do\n\n(see title)\n\n"
                f"# Do not\n\n-\n\n# Why (observed)\n\nSaved by the user on "
                f"{learn._today()}.\n"
            )
            result = learn.put(
                {
                    "id": gid,
                    "kind": "procedure",
                    "scope": scope,
                    "title": title,
                    "when": when,
                    "created": learn._today(),
                    "evidence": "user:explicit",
                },
                body,
                evidence="user:explicit",
                source="user:explicit",
            )
            if result.get("ok"):
                console.print(f"[ok]{Glyph.CHECK} saved {result.get('id')} ({result.get('scope')})[/ok]")
            else:
                console.print(f"[warn]{result.get('error')}[/warn]")
            return
        if action == "forget":
            gid = rest.split()[0] if rest else ""
            if not gid:
                console.print("[muted]Usage: /learn forget <id>[/muted]")
                return
            result = learn.forget(gid)
            if result.get("ok"):
                console.print(f"[ok]{Glyph.CHECK} forgot {gid}[/ok]")
            else:
                console.print(f"[warn]{result.get('error')}[/warn]")
            return
        if action == "outcome":
            bits = rest.split()
            if len(bits) < 2:
                console.print("[muted]Usage: /learn outcome <id> helped|harmed[/muted]")
                return
            result = learn.outcome(bits[0], bits[1])
            if result.get("ok"):
                console.print(
                    f"[ok]{Glyph.CHECK} {bits[0]} {bits[1]} "
                    f"(helpful {result.get('helpful')} harmful {result.get('harmful')})[/ok]"
                )
            else:
                console.print(f"[warn]{result.get('error')}[/warn]")
            return
        console.print("[muted]Usage: /learn [search|get|save|forget|outcome][/muted]")

    def cmd_undo(self, arg: str = "") -> None:
        path = arg.strip()
        if not path:
            files = load_working_set().get("files") or []
            if files:
                path = str(files[-1].get("path") or "")
        if not path:
            console.print("[muted]Usage: /undo <path>[/muted]")
            return
        result = restore_newest_backup(path)
        if result.get("error"):
            console.print(f"[bad]{result['error']}[/bad]")
            return
        console.print(
            f"[ok]{Glyph.CHECK} Restored [key]{result['path']}[/key] "
            f"from [muted]{result['restored_from']}[/muted][/ok]"
        )

    def cmd_exit(self, arg: str = "") -> None:
        self.running = False

    # ------------------------------------------------------------------
    # Model commands
    # ------------------------------------------------------------------

    def cmd_model(self, arg: str = "") -> None:
        aliases = {
            "flash": "deepseek-flash",
            "pro": "deepseek-v4-pro",
            **MODEL_ALIASES,
        }
        raw = arg.strip().lower()
        target = aliases.get(raw, raw) if raw else None

        # Asking for a model the Responses API won't serve gets a reason, not a
        # silent "unknown model" — the name is right, the endpoint just isn't
        # ready for it, and that distinction is worth saying out loud.
        if target in UNAVAILABLE_MODELS:
            console.print(f"[warn]{target} is not available yet.[/warn]")
            console.print(f"[muted]{UNAVAILABLE_MODELS[target]}[/muted]")
            return

        if target is not None and target not in AVAILABLE_MODELS:
            choices = " | ".join(AVAILABLE_MODELS)
            console.print(f"[warn]Unknown model:[/warn] {arg}  [muted]({choices})[/muted]")
            return

        if target is None:
            options = []
            for model in AVAILABLE_MODELS:
                p = PRICING[model]
                current = f"  [ok]{Glyph.CHECK} current[/ok]" if settings.orchestrator_model == model else ""
                options.append(
                    (
                        f"{model}{current}",
                        f"${p.cache_miss}/1M in {Glyph.DOT} ${p.output}/1M out",
                    )
                )
            if len(options) == 1:
                console.print(
                    f"[muted]{AVAILABLE_MODELS[0]} is the only model the Responses API serves "
                    f"today, so there is nothing to switch to.[/muted]"
                )
                for model, why in UNAVAILABLE_MODELS.items():
                    console.print(f"[muted]{model}: {why}[/muted]")
                return
            try:
                default = AVAILABLE_MODELS.index(settings.orchestrator_model) + 1
            except ValueError:
                default = 1
            picked = choose("Orchestrator model", options, default=default)
            if picked is None:
                return
            target = AVAILABLE_MODELS[picked - 1]

        settings.orchestrator_model = target  # type: ignore[assignment]
        save_config()
        console.print(f"[ok]{Glyph.CHECK} Orchestrator {Glyph.ARROW} {target}[/ok] [muted]·saved[/muted]")

    def cmd_thinking(self, arg: str = "") -> None:
        value = arg.strip().lower()
        aliases = {"none": "off", "disabled": "off", "mid": "medium"}
        value = aliases.get(value, value)

        if not value:
            options = [
                ("low", "light reasoning (effort 50)"),
                ("high", "sweetspot (effort 75) — default and ceiling"),
                ("off", "no thinking block at all"),
            ]
            if not settings.thinking_enabled:
                current = "off"
            else:
                n = parse_effort_value(settings.reasoning_effort)
                if n is None or n <= 0:
                    current = "off"
                elif n >= 75:
                    current = "high"
                else:
                    current = "low"
            labels = [o[0] for o in options]
            default = labels.index(current) + 1 if current in labels else 2
            picked = choose("Reasoning effort", options, default=default)
            if picked is None:
                return
            value = options[picked - 1][0]

        if value == "off":
            settings.thinking_enabled = False
            save_config()
            self.orch.release_prefix_effort()
            console.print(f"[warn]Thinking disabled.[/warn] [muted]·saved[/muted]")
            return
        named = {"low": 50, "high": 75, "max": MAX_EFFORT}
        if value in named:
            n = named[value]
        else:
            n = parse_effort_value(value)
            if n is None:
                console.print(
                    f"[warn]Unknown effort:[/warn] {arg}  "
                    f"[muted](1-100 | low | high | max | off; 75 is the ceiling)[/muted]"
                )
                return
            if n == 0:
                settings.thinking_enabled = False
                save_config()
                self.orch.release_prefix_effort()
                console.print(f"[warn]Thinking disabled.[/warn] [muted]·saved[/muted]")
                return

        settings.reasoning_effort = n
        settings.thinking_enabled = True
        save_config()
        self.orch.release_prefix_effort()
        asked_above = value in ("max", "xhigh")
        if not asked_above:
            try:
                asked_above = int(value) > MAX_EFFORT
            except ValueError:
                asked_above = False
        cap_note = f" (capped; {MAX_EFFORT} is the ceiling)" if asked_above else ""
        console.print(
            f"[ok]{Glyph.CHECK} Reasoning effort {Glyph.ARROW} {n}{cap_note}[/ok] "
            f"[muted]·saved[/muted]"
        )

    VERBOSE_HELP = {
        "low": "the answer, plus one line per tool call",
        "mid": "+ tool outcomes, timings, per-turn tokens and cost",
        "high": "+ reasoning excerpts and per-step token counts",
    }

    def cmd_verbose(self, arg: str = "") -> None:
        aliases = {"off": "low", "on": "high", "medium": "mid", "max": "high", "quiet": "low"}
        level = aliases.get(arg.strip().lower(), arg.strip().lower())

        if not level:
            options = [(name, self.VERBOSE_HELP[name]) for name in ("low", "mid", "high")]
            default = ["low", "mid", "high"].index(settings.verbose) + 1
            picked = choose("Output detail", options, default=default)
            if picked is None:
                return
            level = options[picked - 1][0]

        if level not in ("low", "mid", "high"):
            console.print(f"[warn]Unknown level:[/warn] {arg}  [muted](low | mid | high)[/muted]")
            return

        settings.verbose = level  # type: ignore[assignment]
        save_config()
        console.print(
            f"[ok]{Glyph.CHECK} Verbose {Glyph.ARROW} {level}[/ok] "
            f"[muted]{Glyph.DOT} {self.VERBOSE_HELP[level]} ·saved[/muted]"
        )

    def cmd_steps(self, arg: str = "") -> None:
        value = arg.strip().lower()

        if not value:
            current = describe_steps(settings.max_iterations)
            console.print(
                f"[key]Steps per turn[/key] {Glyph.ARROW} {current}  "
                f"[muted]{Glyph.DOT} one model call each "
                f"(/steps <n> to change, /steps off to remove the cap, "
                f"/steps default for {Settings.max_iterations})[/muted]"
            )
            return

        if value in ("default", "reset"):
            count = Settings.max_iterations
        elif value in ("off", "none", "unlimited", "no", "infinite"):
            count = UNLIMITED_ITERATIONS
        else:
            try:
                count = int(value)
            except ValueError:
                console.print(
                    f"[warn]Not a number:[/warn] {escape(arg.strip())}  "
                    f"[muted](a step count, 'off', or 'default')[/muted]"
                )
                return

        # No upper bound — a long job is allowed to be a long job. Only a
        # negative count is meaningless, and 0 already means "no cap".
        if count < UNLIMITED_ITERATIONS:
            console.print(
                f"[warn]Not a step count:[/warn] {count}  "
                f"[muted](use {MIN_ITERATIONS} or more, or 'off' for no cap)[/muted]"
            )
            return

        settings.max_iterations = count
        save_config()
        if count == UNLIMITED_ITERATIONS:
            console.print(
                f"[ok]{Glyph.CHECK} Steps per turn {Glyph.ARROW} unlimited[/ok] "
                f"[muted]{Glyph.DOT} a turn now runs until the model answers; "
                f"progress prints every {ITERATION_NOTICE_EVERY} steps and "
                f"Ctrl-C stops it ·saved[/muted]"
            )
            return
        console.print(
            f"[ok]{Glyph.CHECK} Steps per turn {Glyph.ARROW} {count}[/ok] "
            f"[muted]{Glyph.DOT} every step is a billed model call ·saved[/muted]"
        )

    def cmd_max_tokens(self, arg: str = "") -> None:
        value = arg.strip().lower()

        if not value:
            console.print(
                f"[key]Output budget[/key] {Glyph.ARROW} "
                f"{describe_max_tokens(settings.max_tokens)}  "
                f"[muted]{Glyph.DOT} per reply "
                f"(/max-tokens <n> to cap it, /max-tokens off for the model "
                f"maximum, /max-tokens default to restore the shipped setting)"
                f"[/muted]"
            )
            return

        if value in ("default", "reset"):
            count = Settings.max_tokens
        elif value in ("off", "none", "unlimited", "no", "infinite", "max"):
            count = UNLIMITED_OUTPUT_TOKENS
        else:
            try:
                count = int(value.replace(",", "").replace("_", ""))
            except ValueError:
                console.print(
                    f"[warn]Not a number:[/warn] {escape(arg.strip())}  "
                    f"[muted](a token budget, 'off', or 'default')[/muted]"
                )
                return

        # No upper bound here — a budget larger than the model can honour is the
        # server's to clamp or refuse, and either way it says so. Only a negative
        # budget is meaningless, and 0 already means "no cap".
        if count < UNLIMITED_OUTPUT_TOKENS:
            console.print(
                f"[warn]Not a token budget:[/warn] {count}  "
                f"[muted](use {MIN_OUTPUT_TOKENS} or more, or 'off' for no cap)"
                f"[/muted]"
            )
            return

        settings.max_tokens = count
        save_config()
        if count == UNLIMITED_OUTPUT_TOKENS:
            console.print(
                f"[ok]{Glyph.CHECK} Output budget {Glyph.ARROW} unlimited[/ok] "
                f"[muted]{Glyph.DOT} requests no longer send max_output_tokens, so "
                f"a reply runs to the model's own maximum ·saved[/muted]"
            )
            return
        console.print(
            f"[ok]{Glyph.CHECK} Output budget {Glyph.ARROW} {count:,}[/ok] "
            f"[muted]{Glyph.DOT} replies longer than this are cut off ·saved[/muted]"
        )

    def cmd_max_ctx(self, arg: str = "") -> None:
        value = arg.strip().lower()

        if not value:
            console.print(
                f"[key]Context window[/key] {Glyph.ARROW} "
                f"{describe_context_window(settings.max_context_tokens)}  "
                f"[muted]{Glyph.DOT} auto-compact fires at "
                f"{describe_compact_threshold(settings.compact_threshold)} "
                f"(/max-ctx <n> to change, /max-ctx default to restore the "
                f"shipped setting)[/muted]"
            )
            return

        if value in ("default", "reset"):
            count = Settings.max_context_tokens
        else:
            try:
                count = int(value.replace(",", "").replace("_", ""))
            except ValueError:
                console.print(
                    f"[warn]Not a number:[/warn] {escape(arg.strip())}  "
                    f"[muted](a token count, or 'default')[/muted]"
                )
                return

        if not MIN_CONTEXT_TOKENS <= count <= MAX_CONTEXT_TOKENS:
            console.print(
                f"[warn]Out of range:[/warn] {count:,}  "
                f"[muted]({MIN_CONTEXT_TOKENS:,}–{MAX_CONTEXT_TOKENS:,}; "
                f"DeepSeek's real ceiling is 1M, the shipped default is "
                f"{Settings.max_context_tokens // 1000}k)[/muted]"
            )
            return

        settings.max_context_tokens = count
        save_config()
        console.print(
            f"[ok]{Glyph.CHECK} Context window {Glyph.ARROW} "
            f"{describe_context_window(count)}[/ok] "
            f"[muted]{Glyph.DOT} auto-compact now fires at "
            f"{describe_compact_threshold(settings.compact_threshold)} ·saved[/muted]"
        )

    def cmd_compact_threshold(self, arg: str = "") -> None:
        value = arg.strip().lower()

        if not value:
            console.print(
                f"[key]Auto-compact trigger[/key] {Glyph.ARROW} "
                f"{describe_compact_threshold(settings.compact_threshold)} "
                f"[muted]{Glyph.DOT} = "
                f"{settings.compact_threshold * settings.max_context_tokens:,.0f} tokens "
                f"(/compact-threshold <0.01–1.0> to change, "
                f"/compact-threshold default to restore the shipped setting)[/muted]"
            )
            return

        if value in ("default", "reset"):
            fraction = Settings.compact_threshold
        else:
            try:
                fraction = float(value.rstrip("%"))
                if value.endswith("%"):
                    fraction /= 100.0
            except ValueError:
                console.print(
                    f"[warn]Not a fraction:[/warn] {escape(arg.strip())}  "
                    f"[muted](0.01–1.0, e.g. 0.9 or 90%, or 'default')[/muted]"
                )
                return

        if not MIN_COMPACT_THRESHOLD <= fraction <= MAX_COMPACT_THRESHOLD:
            console.print(
                f"[warn]Out of range:[/warn] {escape(arg.strip())}  "
                f"[muted]({MIN_COMPACT_THRESHOLD}–{MAX_COMPACT_THRESHOLD}; "
                f"0 would compact every turn, >1 never fires)[/muted]"
            )
            return

        settings.compact_threshold = fraction
        save_config()
        console.print(
            f"[ok]{Glyph.CHECK} Auto-compact trigger {Glyph.ARROW} "
            f"{describe_compact_threshold(fraction)} "
            f"[muted]{Glyph.DOT} = {fraction * settings.max_context_tokens:,.0f} tokens "
            f"· compacts once the window passes it ·saved[/muted]"
        )

    # ------------------------------------------------------------------
    # Context commands
    # ------------------------------------------------------------------

    async def cmd_compact(self, arg: str = "") -> None:
        low = arg.lower().strip()

        if low.startswith("auto"):
            rest = low[len("auto"):].strip()
            if rest in ("on", "true", "yes"):
                settings.auto_compact = True
            elif rest in ("off", "false", "no"):
                settings.auto_compact = False
            elif rest:
                console.print("[muted]Usage: /compact auto on|off[/muted]")
                return
            else:
                settings.auto_compact = not settings.auto_compact
            save_config()
            console.print(
                f"[ok]{Glyph.CHECK} Auto-compact {'on' if settings.auto_compact else 'off'}[/ok] "
                f"[muted]{Glyph.DOT} triggers at "
                f"{describe_compact_threshold(settings.compact_threshold)} "
                f"({settings.max_context_tokens:,} tokens) "
                f"· /max-ctx /compact-threshold to move it ·saved[/muted]"
            )
            return

        if low in ("micro", "local", "free"):
            reclaimed = self.orch.micro_compact()
            if reclaimed:
                console.print(
                    f"[ok]{Glyph.CHECK} Micro-compact:[/ok] cleared old tool results "
                    f"[muted](~{reclaimed // 4:,} tokens, no API cost)[/muted]"
                )
            else:
                console.print(
                    "[muted]Nothing to micro-compact — no stale tool results yet. "
                    "Use /compact for a full summary.[/muted]"
                )
            return

        report = await self.orch.compact(instructions=arg)
        console.print(f"[ok]{report}[/ok]")

    async def cmd_g023_compact(self, arg: str = "") -> None:
        if arg.strip():
            console.print("[muted]Usage: /g023-compact[/muted]")
            return

        source = project_instructions.full_path()
        dest = project_instructions.compact_path()
        if not source.is_file():
            console.print(
                f"[warn]No {project_instructions.FULL_NAME} in the project root.[/warn] "
                f"[muted]Create it (same role as AGENTS.md) then run /g023-compact.[/muted]"
            )
            return
        if dest.is_file() and not confirm(
            f"Overwrite existing {project_instructions.COMPACT_NAME} "
            f"from {project_instructions.FULL_NAME}?",
            default=True,
        ):
            return

        report = await project_instructions.compact(self.orch.client, overwrite=True)
        style = "ok" if report.ok else "warn"
        console.print(f"[{style}]{report.message}[/{style}]")

    @staticmethod
    def _g023_status_line() -> str:
        from . import memory_notes

        active = project_instructions.active_file()
        mem = memory_notes.status_line()
        if active is None:
            return (
                f"none [muted]{Glyph.DOT} add {project_instructions.FULL_NAME} "
                f"for standing project instructions {Glyph.DOT} memory {mem}[/muted]"
            )
        freeze = "frozen for this process; file edits apply next process"
        if active.name == project_instructions.COMPACT_NAME:
            return (
                f"{active.name} [muted]{Glyph.DOT} {freeze} "
                f"{Glyph.DOT} memory {mem} "
                f"{Glyph.DOT} /g023-compact to refresh from "
                f"{project_instructions.FULL_NAME}[/muted]"
            )
        return (
            f"{active.name} [muted]{Glyph.DOT} {freeze} "
            f"{Glyph.DOT} memory {mem} "
            f"{Glyph.DOT} /g023-compact to write "
            f"{project_instructions.COMPACT_NAME}[/muted]"
        )

    def cmd_context(self, arg: str = "") -> None:
        breakdown = self.orch.state.role_breakdown()
        if not breakdown:
            console.print("[muted]The conversation is empty.[/muted]")
            return

        total_chars = sum(chars for _, chars in breakdown.values()) or 1
        table = Table(show_header=True, header_style="heading", box=None, padding=(0, 2, 0, 0))
        table.add_column("Kind", style="key")
        table.add_column("Items", justify="right")
        table.add_column("Characters", justify="right")
        table.add_column("Share", justify="right")
        table.add_column("")
        labels = {"tool": "tool results", "assistant": "assistant", "user": "user"}
        for role, (count, chars) in sorted(breakdown.items(), key=lambda kv: -kv[1][1]):
            share = chars / total_chars
            table.add_row(
                labels.get(role, role),
                f"{count:,}",
                f"{chars:,}",
                f"{share:.0%}",
                gauge(share, width=14, warn=0.5, danger=0.75),
            )

        console.print()
        console.print(panel(table, "Context breakdown", subtitle=f"~{total_chars // 4:,} tokens of text"))

        tool_share = breakdown.get("tool", (0, 0))[1] / total_chars
        if tool_share > 0.4:
            console.print(
                f"[muted]{Glyph.BULLET} Tool results are {tool_share:.0%} of the history — "
                "[bold]/compact micro[/bold] clears the stale ones for free.[/muted]"
            )
        elif total_chars > 40_000:
            console.print(
                f"[muted]{Glyph.BULLET} Mostly real conversation — [bold]/compact[/bold] "
                "summarises it with the cheap model.[/muted]"
            )

    def cmd_cost(self, arg: str = "") -> None:
        console.print()
        console.print(panel(self.usage_table(), "Usage & Estimated Cost"))
        for line in self.orch.cost_lines():
            console.print(f"[muted]{line}[/muted]")
        if self.orch.state.history:
            costs = [h.cost for h in self.orch.state.history]
            console.print(
                f"[muted]Per turn  [accent]{ui.sparkline(costs)}[/accent]  "
                f"min {format_cost(min(costs))} {Glyph.DOT} max {format_cost(max(costs))} "
                f"{Glyph.DOT} mean {format_cost(sum(costs) / len(costs))}[/muted]"
            )
        console.print(self.pricing_note())

    def cmd_signals(self, arg: str = "") -> None:
        signals = get_signals()
        console.print()

        try:
            history = get_cache().prefix_history(days=14)
        except Exception as e:
            history = []
            console.print(f"[muted]Prefix history unavailable: {e}[/muted]")

        verdict, latest, baseline = hit_rate_verdict(history)
        table = Table(show_header=True, header_style="heading", box=None, padding=(0, 2, 0, 0))
        table.add_column("Day", style="key")
        table.add_column("Calls", justify="right")
        table.add_column("Hit tokens", justify="right")
        table.add_column("Miss tokens", justify="right")
        table.add_column("Hit rate", justify="right")
        table.add_column("")
        for row in history:
            table.add_row(
                row["day"],
                f"{row['calls']:,}",
                f"{row['hit_tokens']:,}",
                f"{row['miss_tokens']:,}",
                f"{row['hit_rate']:.0%}",
                gauge(row["hit_rate"], width=14),
            )
        if history:
            console.print(panel(table, "Prefix-cache hit rate", subtitle="this project, by day"))
        else:
            console.print("[muted]No prefix-cache history recorded for this project yet.[/muted]")

        line = f"[muted]Hit rate  {verdict}.[/muted]"
        if latest is not None and baseline is not None:
            line = (
                f"[muted]Hit rate  today {latest:.0%} {Glyph.DOT} baseline {baseline:.0%} "
                f"{Glyph.DOT} {verdict}.[/muted]"
            )
        console.print(line)
        console.print(
            "[muted]It moves before answers get visibly worse, but it does not say why: "
            "a changed prompt here and a server-side change look the same from outside.[/muted]"
        )

        console.print()
        if signals.unknown_types:
            for name, count in sorted(signals.unknown_types.items()):
                console.print(
                    f"[warn]{Glyph.BULLET} unknown item type[/warn] [key]{name}[/key] "
                    f"[muted]seen in {count} response(s) this session — echoed back "
                    f"verbatim, absent from the trace.[/muted]"
                )
        else:
            console.print(
                f"[muted]{Glyph.BULLET} No unrecognised response item types this session.[/muted]"
            )

        if signals.empty_responses:
            console.print(
                f"[warn]{Glyph.BULLET} {signals.empty_responses} response(s)[/warn] "
                "[muted]had output items but no readable text and no stated reason for "
                "stopping. That is what a renamed content field looks like — and also "
                "what a model with nothing to say looks like.[/muted]"
            )
        else:
            console.print(
                f"[muted]{Glyph.BULLET} No unexplained empty responses this session.[/muted]"
            )

        recent = signals.recent(limit=8)
        if recent:
            console.print()
            for obs in recent:
                console.print(f"[muted]  turn {obs.turn}  {obs.kind}: {obs.detail}[/muted]")

    def cmd_settings(self, arg: str = "") -> None:
        sub = arg.strip().lower()
        if sub == "save":
            path = save_config()
            console.print(f"[ok]{Glyph.CHECK} Settings saved {Glyph.ARROW}[/ok] [muted]{path}[/muted]")
            return
        if sub == "reset":
            if not confirm("Restore the built-in defaults?", default=False):
                return
            defaults = Settings()
            for key in PERSISTED_KEYS:
                setattr(settings, key, getattr(defaults, key))
            path = save_config()
            console.print(f"[ok]{Glyph.CHECK} Defaults restored[/ok] [muted]{path}[/muted]")
            return
        if sub:
            console.print("[muted]Usage: /settings [save|reset][/muted]")
            return
        self.show_settings()

    def show_settings(self):
        table = Table(show_header=True, header_style="heading", box=None, padding=(0, 2, 0, 0))
        table.add_column("Setting", style="key", no_wrap=True)
        table.add_column("Value", overflow="fold")
        table.add_column("", style="muted", overflow="fold")

        def row(name: str, value, note: str = ""):
            saved = " [muted]·saved[/muted]" if name in PERSISTED_KEYS else ""
            table.add_row(name, f"{value}{saved}", note)

        row("orchestrator_model", settings.orchestrator_model, "/model")
        row("subagent_model", settings.subagent_model)
        row("reasoning_effort", settings.reasoning_effort, "/thinking")
        row("thinking_enabled", settings.thinking_enabled, "/thinking off")
        row("verbose", settings.verbose, self.VERBOSE_HELP[settings.verbose])
        row("stream", settings.stream, "answers render as they are written")
        row("show_tool_timing", settings.show_tool_timing)
        row("show_context_bar", settings.show_context_bar)
        row(
            "max_tokens",
            describe_max_tokens(settings.max_tokens),
            "output per response · /max-tokens",
        )
        row("temperature", settings.temperature)
        row(
            "max_context_tokens",
            describe_context_window(settings.max_context_tokens),
            "/max-ctx",
        )
        row(
            "compact_threshold",
            describe_compact_threshold(settings.compact_threshold),
            "auto-compact trigger · /compact-threshold",
        )
        row("auto_compact", settings.auto_compact, "/compact auto on|off")
        row(
            "max_iterations",
            describe_steps(settings.max_iterations),
            "tool steps per turn · /steps",
        )
        row("max_search_matches", settings.max_search_matches)
        row("file_summary_max_tokens", settings.file_summary_max_tokens)
        row("permission_default", settings.permission_default, "/tools")
        row("vision_backend", settings.vision_backend, "/vision")
        row("vision_model", settings.vision_model or "—")
        row("vision_host", settings.vision_host or f"(auto: {ollama_client.resolve_host()})", "/ollama host")
        row("vision_max_image_dim", settings.vision_max_image_dim, "legacy; vision send is 800px")
        row("vision_num_ctx", f"{settings.vision_num_ctx:,}")
        row("vision_timeout", f"{settings.vision_timeout}s")
        row("vision_keep_alive", settings.vision_keep_alive, "how long the model stays in VRAM")
        row("vision_endpoint", settings.vision_endpoint, "/vision endpoint (deepseek backend)")
        row("deepseek_vision_model", settings.deepseek_vision_model, "deepseek backend default")
        row("or_default_model", settings.or_default_model, "openrouter backend default")
        row("or_credits", settings.or_credits, "$10+ lifetime credits → 1000/24h · /or tier")
        row("surge_notify", settings.surge_notify, "/surge notify on|off")
        row("surge_pause", settings.surge_pause, "/surge on|off")
        row(
            "surge_pause_buffer_minutes",
            f"{settings.surge_pause_buffer_minutes} min",
            "pause starts/ends around peak · /surge buffer",
        )
        row("base_url", settings.base_url)
        row("project_root", settings.project_root)
        row("home", settings.home)
        project_cfg = get_project_config_file()
        cfg_line = str(get_config_file())
        if project_cfg.exists():
            cfg_line += f" + project override {project_cfg}"
        row("config_file", cfg_line)

        console.print()
        console.print(panel(table, "Settings", subtitle="·saved keys persist to config.json"))
        console.print(
            "[muted]/settings save to persist now · /settings reset for defaults · "
            "/config to manage global vs project scope[/muted]"
        )

    def usage_table(self) -> Table:
        usage = get_usage()
        table = Table(show_header=True, header_style="heading", box=None, padding=(0, 1, 0, 0))
        table.add_column("Model", no_wrap=True)
        table.add_column("Calls", justify="right")
        table.add_column("Cache hit", justify="right")
        table.add_column("Cache miss", justify="right")
        table.add_column("Output", justify="right")
        table.add_column("Cost", justify="right")

        per_model = usage.per_model()
        for model, mu in sorted(per_model.items()):
            table.add_row(
                model,
                str(mu.calls),
                f"{mu.cache_hit_tokens:,}",
                f"{mu.cache_miss_tokens:,}",
                f"{mu.output_tokens:,}",
                format_cost(mu.cost(model)),
            )

        total = usage.totals()
        if per_model:
            table.add_section()
        table.add_row(
            "[heading]total[/heading]",
            f"[heading]{total.calls}[/heading]",
            f"[heading]{total.cache_hit_tokens:,}[/heading]",
            f"[heading]{total.cache_miss_tokens:,}[/heading]",
            f"[heading]{total.output_tokens:,}[/heading]",
            f"[heading]{format_cost(usage.cost())}[/heading]",
        )
        table.add_row(
            "[muted]hit rate[/muted]",
            "",
            f"[muted]{total.hit_rate():.0%}[/muted]",
            "",
            f"[muted]reasoning {total.reasoning_tokens:,}[/muted]",
            f"[muted]orch {format_cost(usage.scope_cost('orchestrator'))} {Glyph.DOT} "
            f"sub {format_cost(usage.scope_cost('subagent'))}[/muted]",
        )
        return table

    def pricing_note(self) -> str:
        # Only price what the Responses API will actually serve. Listing a model
        # /model refuses to select reads as a menu, not a footnote.
        parts = [
            f"{model}: ${p.cache_hit}/1M hit {Glyph.DOT} ${p.cache_miss}/1M miss "
            f"{Glyph.DOT} ${p.output}/1M out (surge {p.surge_output}/1M, 2×)"
            for model, p in PRICING.items()
            if model in AVAILABLE_MODELS
        ]
        return (
            "[muted]"
            + "\n".join(parts)
            + "\nPrices are off-peak (regular); peak is 2× during surge hours. "
            + "Estimates only — actual billing is DeepSeek's.[/muted]"
        )

    # ------------------------------------------------------------------
    # Tool permissions
    # ------------------------------------------------------------------

    def cmd_tools(self, arg: str = "") -> None:
        reg = get_registry()
        parts = arg.split()

        if len(parts) >= 2:
            name, level = parts[0], parts[1].lower()
            match = next((t for t in reg.policy if t.lower() == name.lower()), None)
            if match is None:
                console.print(f"[warn]Unknown tool:[/warn] {name}  [muted](/tools lists them)[/muted]")
                return
            if level not in (ALLOW, ASK, BLOCK):
                console.print(f"[warn]Unknown level:[/warn] {level}  [muted](allow | ask | block)[/muted]")
                return
            reg.set_permission(match, level)
            console.print(f"[ok]{Glyph.CHECK} {match} {Glyph.ARROW} {level}[/ok]")
            if match == "FetchUrl" and level == ALLOW:
                console.print(
                    "[muted]FetchUrl still prompts — the prompt is also where you pick "
                    "cache vs network.[/muted]"
                )
            return

        if len(parts) == 1:
            console.print("[muted]Usage: /tools <tool> allow|ask|block[/muted]")
            return

        counts = {}
        for event in self.orch.trace.events:
            counts[event.name] = counts.get(event.name, 0) + 1

        table = Table(show_header=True, header_style="heading", box=None, padding=(0, 2, 0, 0))
        table.add_column("", no_wrap=True)
        table.add_column("Tool", style="key", no_wrap=True)
        table.add_column("Permission")
        table.add_column("Runs this turn", justify="right")
        table.add_column("", style="muted")

        colors = {ALLOW: "ok", ASK: "warn", BLOCK: "bad"}
        for name in sorted(reg.policy):
            style = ui.tool_style(name)
            level = reg.policy[name]
            note = "subagent-isolated" if style.subagent else ""
            if name == "FetchUrl":
                note = "always prompts (cache vs network)"
            table.add_row(
                f"[{style.color}]{style.icon}[/{style.color}]",
                name,
                f"[{colors[level]}]{level}[/{colors[level]}]",
                str(counts.get(name, 0)) if counts.get(name) else "[muted]—[/muted]",
                note,
            )

        console.print()
        console.print(panel(table, "Tools & Permissions"))
        console.print("[muted]/tools <tool> allow|ask|block to change one[/muted]")

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _cache_size_line(self) -> str:
        db = get_cache().db_path
        try:
            return f"{human_bytes(db.stat().st_size)} [muted]{db}[/muted]"
        except OSError:
            return "[muted]empty[/muted]"

    def cmd_cache(self, arg: str = "") -> None:
        sub = arg.strip().lower()

        if sub in ("", "stats", "status"):
            cache = get_cache()
            stats = cache.stats()
            table = kv_table(
                [
                    ("file summaries", f"{stats['files']:,}"),
                    ("vision answers", f"{stats['vision']:,}"),
                    ("cached URLs", f"{stats['web']:,}"),
                    ("tool vault", f"{stats.get('vault', 0):,}"),
                    ("database", self._cache_size_line()),
                ]
            )
            console.print()
            console.print(panel(table, "Cache"))
            console.print("[muted]/cache web to list URLs · /cache clear to purge everything[/muted]")
            return

        if sub == "clear":
            get_cache().clear_all()
            console.print(f"[ok]{Glyph.CHECK} All caches purged.[/ok]")
            return
        if sub == "web":
            self.show_web_cache()
            return
        if sub in ("web clear", "clear web"):
            n = get_cache().clear_web()
            console.print(f"[ok]{Glyph.CHECK} URL cache purged ({n} entries).[/ok]")
            return
        console.print("[muted]Usage: /cache [stats|clear|web|web clear][/muted]")

    def show_web_cache(self):
        entries = get_cache().list_web()
        if not entries:
            console.print("[muted]No URLs cached yet.[/muted]")
            return
        table = Table(show_header=True, header_style="heading", box=None, padding=(0, 2, 0, 0))
        table.add_column("Fetched", style="muted")
        table.add_column("St", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Hits", justify="right")
        table.add_column("URL", overflow="fold")
        for e in entries:
            table.add_row(
                _describe_age(time.time() - e["fetched_at"]),
                str(e["status"]),
                human_bytes(e["size"]),
                str(e["hit_count"]),
                e["url"][:90],
            )
        console.print()
        console.print(panel(table, "Cached pages"))
        console.print("[muted]/cache web clear purges these.[/muted]")

    # ------------------------------------------------------------------
    # /fetch
    # ------------------------------------------------------------------

    def cmd_fetch(self, arg: str = "") -> None:
        from . import web_fetch

        low = arg.lower().strip()

        if low in ("status", "info", ""):
            report = web_fetch.fidelity_report()
            table = kv_table(
                [
                    ("engine", report["engine"]),
                    ("TLS fingerprint", report["tls_fingerprint"]),
                    ("HTTP/2", "yes" if report["http2"] else "no"),
                    ("header order", report["header_order"]),
                    ("cookies", report["cookies"]),
                    ("brotli / zstd", f"{report['brotli']} / {report['zstd']}"),
                    ("JavaScript", "no (static fetch only)"),
                ]
            )
            console.print()
            console.print(panel(table, "FetchUrl"))
            console.print(f"[muted]{report['notes']}[/muted]")
            if not arg:
                console.print("[muted]Usage: /fetch <url>[/muted]")
            return

        if low in ("cookies clear", "clear cookies"):
            web_fetch.CookieStore().clear()
            console.print(f"[ok]{Glyph.CHECK} Stored cookies cleared.[/ok]")
            return

        url = arg.split()[0]
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        args = {"url": url, "cache_mode": "auto"}
        if not get_registry().confirm_fetch(args):
            console.print("[warn]Cancelled.[/warn]")
            return

        cache = get_cache()
        if args["cache_mode"] == "cache":
            cached = cache.get_web(url)
            if not cached:
                console.print("[warn]Nothing cached for that URL.[/warn]")
                return
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
        else:
            try:
                with console.status(f"[accent]Fetching[/accent] {url}", spinner="dots"):
                    result = web_fetch.fetch(url)
            except Exception as e:
                console.print(f"[bad]Fetch failed:[/bad] {e}")
                return
            cache.put_web(
                url,
                body=result.body,
                status=result.status,
                headers=result.headers,
                final_url=result.final_url,
                engine=result.engine,
                profile=result.profile,
            )

        data = web_fetch.extract(result, "text")
        source = "cache" if result.from_cache else f"network {Glyph.DOT} {result.elapsed_ms} ms"
        header = (
            f"HTTP {result.status} {Glyph.DOT} {source} {Glyph.DOT} {result.engine} "
            f"{Glyph.DOT} {human_bytes(len(result.body))}"
        )
        console.print()
        console.print(
            panel(
                f"[heading]{data.get('title') or result.final_url}[/heading]\n"
                f"[muted]{header}[/muted]\n\n" + (data.get("content") or "")[:3000],
                result.final_url[:70],
            )
        )

    # ------------------------------------------------------------------
    # /ollama
    # ------------------------------------------------------------------

    async def cmd_ollama(self, arg: str = "") -> None:
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("status", "", "info"):
            self.ollama_status()
        elif sub == "host":
            self.ollama_set_host(rest)
        elif sub in ("models", "list", "ls"):
            self.ollama_models()
        elif sub == "test":
            await self.ollama_test(rest)
        elif sub in ("ps", "running"):
            self.ollama_ps()
        else:
            console.print(f"[warn]Unknown subcommand:[/warn] {sub}")
            console.print("[muted]/ollama [status|host|models|test|ps]  ·  /help ollama[/muted]")

    def ollama_status(self) -> None:
        host = ollama_client.resolve_host()
        with console.status(f"[accent]Contacting {host}…[/accent]", spinner="dots"):
            probe = ollama_client.probe(host)

        rows = [
            ("host", f"[ok]{probe.host}[/ok]" if probe.ok else f"[bad]{probe.host}[/bad]"),
            ("source", ollama_client.host_source()),
            ("location", "local machine" if ollama_client.is_local(host) else "remote"),
        ]
        if probe.ok:
            rows += [
                ("status", f"[ok]{Glyph.CHECK} reachable[/ok]"),
                ("version", probe.version or "unknown"),
                ("latency", human_ms(probe.latency_ms)),
                ("models", f"{probe.model_count} installed" if probe.model_count is not None else "—"),
            ]
        else:
            rows.append(("status", f"[bad]{Glyph.CROSS} {probe.error}[/bad]"))
        rows.append(("vision", self.vision_status()))

        console.print()
        console.print(panel(kv_table(rows), "Ollama"))
        if not probe.ok:
            console.print(f"[muted]{ollama_client.unreachable_hint(host)}[/muted]")
        elif not settings.vision_enabled:
            console.print("[muted]Vision is off — run [bold]/vision[/bold] to pick a model.[/muted]")

    def ollama_set_host(self, value: str) -> None:
        if not value:
            console.print(f"[muted]Current host:[/muted] {ollama_client.resolve_host()}")
            console.print(
                "[muted]Usage: /ollama host <address|default>   e.g. 192.168.1.50, "
                "gpu-box:11434, https://ollama.example.com[/muted]"
            )
            return

        if value.lower() in ("default", "auto", "reset", "none", "local", "localhost"):
            settings.vision_host = None
            save_config()
            host = ollama_client.resolve_host()
            console.print(
                f"[ok]{Glyph.CHECK} Host override cleared[/ok] [muted]{Glyph.ARROW} {host} "
                f"({ollama_client.host_source()}) ·saved[/muted]"
            )
            self._report_probe(host)
            return

        host = ollama_client.normalize_host(value)
        with console.status(f"[accent]Testing {host}…[/accent]", spinner="dots"):
            probe = ollama_client.probe(host)

        if not probe.ok:
            console.print(f"[bad]{Glyph.CROSS} {host} {Glyph.DOT} {probe.error}[/bad]")
            console.print(f"[muted]{ollama_client.unreachable_hint(host)}[/muted]")
            if not confirm("Save it anyway?", default=False):
                console.print("[muted]Host unchanged.[/muted]")
                return

        settings.vision_host = host
        save_config()
        console.print(f"[ok]{Glyph.CHECK} Ollama host {Glyph.ARROW} {host}[/ok] [muted]·saved[/muted]")
        if probe.ok:
            console.print(f"[muted]{probe.summary}[/muted]")
            if settings.vision_enabled:
                self._warn_if_model_absent(host)
            else:
                console.print("[muted]Run [bold]/vision[/bold] to pick a model on this host.[/muted]")

    def _report_probe(self, host: str) -> None:
        probe = ollama_client.probe(host)
        if probe.ok:
            console.print(f"[muted]{Glyph.CHECK} {probe.summary}[/muted]")
        else:
            console.print(f"[warn]{Glyph.CROSS} {probe.error}[/warn]")

    def _warn_if_model_absent(self, host: str) -> None:
        """A saved model name means nothing on a machine that doesn't have it."""
        try:
            models = ollama_client.list_models(host, probe_capabilities=False)
        except Exception:
            return
        if settings.vision_model and ollama_client.find_model(models, settings.vision_model) is None:
            console.print(
                f"[warn]Note:[/warn] this host has no '{settings.vision_model}'. "
                "Run [bold]/vision[/bold] to pick one of its models."
            )

    def ollama_models(self) -> None:
        host = ollama_client.resolve_host()
        try:
            with console.status(f"[accent]Listing models on {host}…[/accent]", spinner="dots"):
                models = ollama_client.list_models(host)
        except Exception as e:
            console.print(f"[bad]Could not list models:[/bad] {e}")
            console.print(f"[muted]{ollama_client.unreachable_hint(host)}[/muted]")
            return

        if not models:
            console.print(f"[warn]{host} is running but has no models installed.[/warn]")
            console.print("[muted]Pull one there, e.g. [bold]ollama pull qwen2.5vl:7b[/bold][/muted]")
            return

        table = Table(show_header=True, header_style="heading", box=None, padding=(0, 2, 0, 0))
        table.add_column("Model", style="key")
        table.add_column("Size", justify="right")
        table.add_column("Params", justify="right", style="muted")
        table.add_column("Quant", style="muted")
        table.add_column("Vision", justify="center")
        for m in models:
            mark = {
                "reported": f"[ok]{Glyph.CHECK}[/ok]",
                "guessed": "[warn]likely[/warn]",
                "no": "[muted]—[/muted]",
            }[m.vision_certainty]
            current = " [ok](in use)[/ok]" if m.name == settings.vision_model else ""
            table.add_row(
                f"{m.name}{current}",
                f"{m.size_gb:.1f} GB" if m.size_bytes else "—",
                m.parameter_size or "—",
                m.quantization or "—",
                mark,
            )
        console.print()
        console.print(panel(table, f"Models on {host}"))
        console.print("[muted]/vision to enable one · 'likely' means we inferred it from the name[/muted]")

    async def ollama_test(self, model: str = "") -> None:
        """Round-trip a real request so a host is proven, not just pingable."""
        host = ollama_client.resolve_host()
        target = model.strip() or settings.vision_model
        probe = ollama_client.probe(host)
        if not probe.ok:
            console.print(f"[bad]{Glyph.CROSS} {host} {Glyph.DOT} {probe.error}[/bad]")
            console.print(f"[muted]{ollama_client.unreachable_hint(host)}[/muted]")
            return

        console.print(f"[ok]{Glyph.CHECK}[/ok] {host} [muted]{Glyph.DOT} {probe.summary}[/muted]")
        if not target:
            console.print(
                "[muted]No model to test — pass one (/ollama test <model>) or enable "
                "vision with /vision.[/muted]"
            )
            return

        import base64
        import io

        try:
            from PIL import Image
        except ImportError:
            console.print(
                "[warn]Pillow is not installed, so no test image can be generated.[/warn]\n"
                "[muted]pip install pillow — or just point /vision at an image.[/muted]"
            )
            return

        # A 64x64 tile with one unmistakable feature: if the model describes it,
        # the whole path works — image encoding, transport, and inference.
        image = Image.new("RGB", (64, 64), "white")
        for x in range(16, 48):
            for y in range(28, 36):
                image.putpixel((x, y), (255, 0, 0))
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        payload = base64.b64encode(buf.getvalue()).decode("ascii")

        try:
            with console.status(
                f"[accent]Running {target} on {host}…[/accent] [muted](a cold load can take a while)[/muted]",
                spinner="dots",
            ):
                result = await ollama_client.vision_chat_detailed(
                    model=target,
                    prompt="What colour is the shape in this image? Answer in one word.",
                    image_b64=payload,
                    host=host,
                    timeout=min(settings.vision_timeout, 120),
                    num_ctx=settings.vision_num_ctx,
                )
        except Exception as e:
            console.print(f"[bad]{Glyph.CROSS} Inference failed:[/bad] {e}")
            console.print("[muted]/ollama models shows what this host actually has.[/muted]")
            return

        rows = [
            ("model", target),
            ("answer", result.answer[:200] or "[muted](empty)[/muted]"),
            ("round trip", human_ms(result.elapsed_ms)),
            ("model load", human_ms(result.load_ms) if result.load_ms else "already warm"),
            ("tokens", f"{result.eval_count} out at {result.tokens_per_second:.1f}/s"),
        ]
        console.print()
        console.print(panel(kv_table(rows), "Vision round-trip"))
        if "red" in result.answer.lower():
            console.print(f"[ok]{Glyph.CHECK} The model saw the image correctly.[/ok]")
        else:
            console.print(
                "[warn]The model answered, but not with the expected colour (red) — "
                "it may not actually be image-capable.[/warn]"
            )

    def ollama_ps(self) -> None:
        host = ollama_client.resolve_host()
        running = ollama_client.running_models(host)
        if not running:
            console.print(f"[muted]Nothing loaded on {host} right now.[/muted]")
            return
        table = Table(show_header=True, header_style="heading", box=None, padding=(0, 2, 0, 0))
        table.add_column("Model", style="key")
        table.add_column("VRAM", justify="right")
        table.add_column("Expires", style="muted")
        for m in running:
            table.add_row(
                str(m.get("name") or m.get("model") or "?"),
                human_bytes(int(m.get("size_vram") or m.get("size") or 0)),
                str(m.get("expires_at") or "—")[:19],
            )
        console.print()
        console.print(panel(table, f"Loaded on {host}"))

    # ------------------------------------------------------------------
    # /vision
    # ------------------------------------------------------------------

    def vision_status(self) -> str:
        if not settings.vision_enabled:
            return "[muted]disabled[/muted]"
        model = settings.effective_vision_model or "(backend default)"
        label = f"[ok]on[/ok] {Glyph.DOT} {settings.vision_backend} {Glyph.DOT} [heading]{model}[/heading]"
        if settings.vision_backend == "deepseek":
            label += f" [muted]({settings.vision_endpoint})[/muted]"
        return label

    def _disable_vision(self):
        settings.vision_backend = "none"
        settings.vision_model = None
        save_config()
        console.print(f"[warn]Vision disabled.[/warn] [muted]·saved[/muted]")

    def _enable_vision(self, model: str, warn_no_vision: bool = False, backend: Optional[str] = None):
        """Turn vision on for a backend+model and persist it."""
        if backend is not None:
            settings.vision_backend = backend
        elif settings.vision_backend in ("", "none"):
            settings.vision_backend = "deepseek"  # the sensible default backend
        settings.vision_model = model or None
        path = save_config()
        if warn_no_vision:
            console.print(
                f"[warn]Note:[/warn] {model} does not advertise image support — "
                "AnalyzeImage may fail at runtime."
            )
        console.print(
            f"[ok]{Glyph.CHECK} Vision {Glyph.ARROW} {settings.vision_backend} "
            f"{Glyph.DOT} [heading]{settings.effective_vision_model}[/heading][/ok] "
            f"[muted]·saved to {path}[/muted]"
        )

    def cmd_vision(self, arg: str = "") -> None:
        low = arg.lower().strip()

        if low in ("status", "show"):
            console.print(f"Vision: {self.vision_status()}")
            console.print(f"[muted]endpoint: {settings.vision_endpoint} · config: {get_config_file()}[/muted]")
            return
        if low in ("off", "none", "disable", "disabled", "0", "false"):
            self._disable_vision()
            return
        if low in ("default", "reset"):
            settings.vision_model = None
            save_config()
            console.print(
                f"[ok]{Glyph.CHECK} Vision model reset to backend default "
                f"([heading]{settings.effective_vision_model}[/heading])[/ok] [muted]·saved[/muted]"
            )
            return
        if low.startswith("backend"):
            backend = low[len("backend"):].strip()
            if not backend:
                console.print(f"[muted]Current backend:[/muted] {settings.vision_backend}")
                console.print("[muted]Usage: /vision backend none|ollama|deepseek|openrouter[/muted]")
                return
            if backend not in registry.BACKEND_CHOICES:
                console.print(
                    f"[warn]Unknown backend:[/warn] {backend}  "
                    f"[muted]({' | '.join(registry.BACKEND_CHOICES)})[/muted]"
                )
                return
            settings.vision_backend = backend
            save_config()
            console.print(f"[ok]{Glyph.CHECK} Vision backend {Glyph.ARROW} {backend}[/ok] [muted]·saved[/muted]")
            if backend == "ollama" and not settings.vision_model:
                console.print("[muted]No model selected — run [bold]/vision[/bold] to pick one.[/muted]")
            elif backend == "deepseek":
                console.print(
                    f"[muted]Model [heading]{settings.effective_vision_model}[/heading] · "
                    f"endpoint {settings.vision_endpoint} · /vision endpoint to change[/muted]"
                )
            elif backend == "openrouter":
                console.print(
                    f"[muted]Model [heading]{settings.effective_vision_model}[/heading] · "
                    f"list {or_client.load_or_models()} · /or for key/tier/usage[/muted]"
                )
            return
        if low.startswith("endpoint"):
            endpoint = low[len("endpoint"):].strip()
            if not endpoint:
                console.print(f"[muted]Current endpoint:[/muted] {settings.vision_endpoint}")
                console.print("[muted]Usage: /vision endpoint responses|chat_completions[/muted]")
                return
            if endpoint not in registry.ENDPOINT_CHOICES:
                console.print(
                    f"[warn]Unknown endpoint:[/warn] {endpoint}  "
                    f"[muted]({' | '.join(registry.ENDPOINT_CHOICES)})[/muted]"
                )
                return
            settings.vision_endpoint = endpoint
            save_config()
            console.print(f"[ok]{Glyph.CHECK} Vision endpoint {Glyph.ARROW} {endpoint}[/ok] [muted]·saved[/muted]")
            if settings.vision_backend == "openrouter":
                console.print(
                    "[muted]OpenRouter always speaks chat_completions; the endpoint "
                    "setting applies to the deepseek backend.[/muted]"
                )
            return

        # --- backend-specific model selection ---------------------------------
        # With no backend chosen yet, a bare model name belongs to the default
        # backend (deepseek) rather than requiring an Ollama daemon to exist.
        if settings.vision_backend in ("", "none"):
            settings.vision_backend = "deepseek"
            if not arg:
                save_config()
        if settings.vision_backend == "deepseek":
            if arg:
                self._enable_vision(arg)
                return
            console.print()
            console.print(
                panel(
                    kv_table(
                        [
                            ("backend", "[ok]deepseek[/ok] (K.dat key)"),
                            ("model", settings.effective_vision_model),
                            ("endpoint", settings.vision_endpoint),
                        ]
                    ),
                    "Configure Vision",
                )
            )
            console.print(
                "[muted]/vision <model> to override · /vision default to reset · "
                "/vision endpoint chat_completions for the OpenAI wire format[/muted]"
            )
            return

        if settings.vision_backend == "openrouter":
            models = or_client.load_or_models()
            if arg:
                self._enable_vision(arg)
                return
            options = [(m, "OR_VISION.json") for m in models]
            options.append(("[muted]Disable vision[/muted]", "the default"))
            default = 1
            for i, m in enumerate(models, start=1):
                if m == settings.effective_vision_model:
                    default = i
            picked = choose("OpenRouter vision model", options, default=default)
            if picked is None:
                console.print("[muted]Vision setting unchanged.[/muted]")
                return
            if picked == len(options):
                self._disable_vision()
                return
            self._enable_vision(models[picked - 1])
            return

        # --- ollama (local/remote daemon) -------------------------------------
        host = ollama_client.resolve_host()
        with console.status(f"[accent]Contacting {host}…[/accent]", spinner="dots"):
            probe = ollama_client.probe(host, count_models=False)
        if not probe.ok:
            console.print(f"[bad]{Glyph.CROSS} No Ollama daemon at {host} {Glyph.DOT} {probe.error}[/bad]")
            console.print(f"[muted]{ollama_client.unreachable_hint(host)}[/muted]")
            return

        try:
            with console.status(f"[accent]Reading models from {host}…[/accent]", spinner="dots"):
                models = ollama_client.list_models(host)
        except Exception as e:
            console.print(f"[bad]Could not list Ollama models:[/bad] {e}")
            return

        if not models:
            console.print(f"[warn]{host} is running but has no models installed.[/warn]")
            console.print("[muted]Try: [bold]ollama pull qwen2.5vl:7b[/bold][/muted]")
            return

        # One-shot: /vision <model name>
        if arg:
            match = ollama_client.find_model(models, arg)
            if match is None:
                console.print(f"[bad]Model not found on {host}:[/bad] {arg}")
                console.print("[muted]/ollama models lists them · /vision alone opens a picker.[/muted]")
                return
            self._enable_vision(match.name, warn_no_vision=not match.supports_vision, backend="ollama")
            return

        vision_models = [m for m in models if m.supports_vision]
        show_all = not vision_models
        listed = models if show_all else vision_models

        console.print()
        console.print(
            panel(
                kv_table(
                    [
                        ("host", f"[ok]{host}[/ok] [muted]({ollama_client.host_source()})[/muted]"),
                        ("current", self.vision_status()),
                    ]
                ),
                "Configure Vision",
            )
        )
        if show_all:
            console.print("[warn]No model reported image support — listing everything.[/warn]")

        options = []
        for m in listed:
            current = f"  [ok]{Glyph.CHECK} in use[/ok]" if m.name == settings.vision_model else ""
            size = f"{m.size_gb:.1f} GB" if m.size_bytes else "size unknown"
            certainty = {
                "reported": "vision: reported by Ollama",
                "guessed": "vision: inferred from the name",
                "no": "no image support reported",
            }[m.vision_certainty]
            options.append((f"{m.name}{current}", f"{size} {Glyph.DOT} {certainty}"))
        options.append(("[muted]Disable vision[/muted]", "the default"))

        default = 1
        for i, m in enumerate(listed, start=1):
            if m.name == settings.vision_model:
                default = i
        picked = choose("Vision model", options, default=default)
        if picked is None:
            console.print("[muted]Vision setting unchanged.[/muted]")
            return
        if picked == len(options):
            self._disable_vision()
            return
        chosen = listed[picked - 1]
        self._enable_vision(chosen.name, warn_no_vision=not chosen.supports_vision, backend="ollama")

    # ------------------------------------------------------------------
    # /surge
    # ------------------------------------------------------------------

    def cmd_surge(self, arg: str = "") -> None:
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("status", ""):
            self._surge_status()
        elif sub == "on":
            settings.surge_pause = True
            save_config()
            console.print(
                f"[ok]{Glyph.CHECK} Surge pause ON[/ok] [muted]— usage pauses "
                f"{settings.surge_pause_buffer_minutes} min before peak and resumes "
                f"{settings.surge_pause_buffer_minutes} min after it ends ·saved[/muted]"
            )
        elif sub == "off":
            settings.surge_pause = False
            save_config()
            console.print("[warn]Surge pause OFF.[/warn] [muted]Notifications still fire if /surge notify is on ·saved[/muted]")
        elif sub == "buffer":
            try:
                minutes = int(rest)
            except ValueError:
                minutes = -1
            if not (0 <= minutes <= 120):
                console.print("[warn]Buffer must be a whole number of minutes, 0-120.[/warn]")
                return
            settings.surge_pause_buffer_minutes = minutes
            save_config()
            console.print(f"[ok]{Glyph.CHECK} Surge pause buffer {Glyph.ARROW} {minutes} min[/ok] [muted]·saved[/muted]")
        elif sub == "notify":
            on = rest.lower() in ("on", "1", "yes", "true")
            off = rest.lower() in ("off", "0", "no", "false")
            if not rest or (not on and not off):
                console.print(
                    f"[muted]Surge notifications are {'on' if settings.surge_notify else 'off'} — "
                    "usage: /surge notify on|off[/muted]"
                )
                return
            settings.surge_notify = on
            save_config()
            console.print(
                f"[ok]{Glyph.CHECK} Surge notifications {'ON' if on else 'OFF'}[/ok] [muted]·saved[/muted]"
            )
        elif sub == "now":
            active = surge.is_surge()
            console.print(
                f"[heading]now[/heading] "
                f"{'[bad]SURGE ACTIVE — prices 2× off-peak[/bad]' if active else '[ok]off-peak (half of peak)[/ok]'}"
            )
            self._surge_status()
        else:
            console.print(f"[warn]Unknown subcommand:[/warn] {sub}")
            console.print("[muted]/surge [status|on|off|buffer <min>|notify on|off|now] · /help surge[/muted]")

    def _surge_status(self) -> None:
        from datetime import datetime

        now = datetime.now().astimezone()
        active = surge.is_surge()
        next_start, next_end = surge.next_peak_window(now)
        until = surge.format_duration(next_start - now)
        lasts = surge.format_duration(next_end - next_start)
        kind, when = surge.next_transition(now)
        local_peak = " · ".join(
            surge.clock_range(s, e) for s, e in surge.peak_windows_local(now)[:2]
        )
        pause_windows = " · ".join(
            surge.clock_range(s, e) for s, e in surge.pause_ranges(now)[:2]
        )
        rows = [
            (
                "state",
                f"[{'bad' if active else 'ok'}]{'SURGE ACTIVE — prices 2× off-peak' if active else 'off-peak (half of peak)'}[/]",
            ),
            ("peak hours", "09:00–12:00 & 14:00–18:00 Beijing (UTC+8)"),
            ("peak (local)", local_peak),
            (
                "next surge",
                f"in {until} · lasts {lasts} ({surge.clock_range(next_start, next_end)})",
            ),
            (
                "pause window",
                f"{pause_windows} (buffer {settings.surge_pause_buffer_minutes} min)",
            ),
            ("pause", "on" if settings.surge_pause else "off"),
            ("notify", "on" if settings.surge_notify else "off"),
            ("next event", f"{kind.replace('_', ' ')} {surge.clock(when)}"),
        ]
        console.print()
        console.print(panel(kv_table(rows), "DeepSeek surge pricing"))
        console.print(
            "[muted]Times shown AM/PM and 24-hour, in your local clock · "
            "/surge on to auto-pause around peak · /surge buffer <min> · "
            "/surge notify on|off[/muted]"
        )

    # ------------------------------------------------------------------
    # /or — OpenRouter vision
    # ------------------------------------------------------------------

    def cmd_or(self, arg: str = "") -> None:
        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""

        if sub in ("status", ""):
            self._or_status()
        elif sub == "model":
            if not rest:
                console.print(f"[muted]Current OpenRouter model:[/muted] {settings.effective_vision_model}")
                console.print("[muted]Usage: /or model <model-id> | default[/muted]")
                return
            if rest.lower() in ("default", "reset"):
                settings.vision_model = None
            else:
                settings.vision_model = rest
            settings.vision_backend = "openrouter"
            save_config()
            console.print(
                f"[ok]{Glyph.CHECK} OpenRouter vision model {Glyph.ARROW} "
                f"[heading]{settings.effective_vision_model}[/heading][/ok] [muted]·saved[/muted]"
            )
        elif sub in ("models", "list", "reload"):
            models = or_client.load_or_models()
            settings.or_vision_models = models
            save_config()
            console.print(f"[ok]{Glyph.CHECK} OpenRouter model list ({len(models)}):[/ok]")
            for i, m in enumerate(models, 1):
                mark = "  [ok]← in use[/ok]" if m == settings.effective_vision_model else ""
                console.print(f"  {i}. {m}{mark}")
            console.print(f"[muted]source: {or_client.or_models_file()}[/muted]")
        elif sub == "tier":
            if not rest:
                console.print(
                    f"[muted]Current tier:[/muted] {'credits ($10+ bought)' if settings.or_credits else 'free'} "
                    f"[muted]· usage: /or tier free|credits[/muted]"
                )
                return
            if rest not in ("free", "credits"):
                console.print("[warn]Tier must be 'free' or 'credits'.[/warn]")
                return
            settings.or_credits = rest == "credits"
            save_config()
            console.print(
                f"[ok]{Glyph.CHECK} OpenRouter tier {Glyph.ARROW} {rest}[/ok] "
                f"[muted]· ceiling {or_client.active_daily_limit()}/24h ·saved[/muted]"
            )
        elif sub in ("usage", "quota"):
            usage = or_client.get_or_usage()
            limit = or_client.active_daily_limit()
            console.print(
                f"[muted]Today ({usage.date}):[/muted] {usage.requests}/{limit} requests "
                f"{Glyph.DOT} {usage.remaining()} left {Glyph.DOT} {usage.rate_limits} rate-limit hits"
            )
        else:
            console.print(f"[warn]Unknown subcommand:[/warn] {sub}")
            console.print("[muted]/or [status|model <id>|models|tier free|credits|usage] · /help or[/muted]")

    def _or_status(self) -> None:
        key_ok = (get_home() / "OK.dat").exists()
        models = or_client.load_or_models()
        usage = or_client.get_or_usage()
        limit = or_client.active_daily_limit()
        free, credits = or_client.rate_limit_tier()
        rows = [
            ("key", f"[ok]{Glyph.CHECK} OK.dat present[/ok]" if key_ok else "[bad]OK.dat missing[/bad]"),
            ("tier", f"{'credits' if settings.or_credits else 'free'} [muted]({usage.requests}/{limit} used today)[/muted]"),
            ("ceiling", f"free accounts: {free}/24h · with $10+ credits: {credits}/24h"),
            (
                "model",
                f"[heading]{settings.vision_model or settings.or_default_model}[/heading]",
            ),
            (
                "models",
                f"{len(models)} in OR_VISION.json: " + ", ".join(models[:3]) + (" …" if len(models) > 3 else ""),
            ),
            ("rate limits today", str(usage.rate_limits)),
            ("vision backend", settings.vision_backend),
        ]
        console.print()
        console.print(panel(kv_table(rows), "OpenRouter vision"))
        console.print(
            "[muted]/or model <id> · /or models (reload OR_VISION.json) · "
            "/or tier credits if you bought $10+ · /or usage[/muted]"
        )

    # ------------------------------------------------------------------
    # /orsub — OpenRouter subagent offload
    # ------------------------------------------------------------------

    async def cmd_orsub(self, arg: str = "") -> None:
        parts = arg.split(maxsplit=2)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""
        extra = parts[2].strip() if len(parts) > 2 else ""

        if sub in ("status", ""):
            self._orsub_status()
        elif sub in ("on", "enable"):
            settings.or_sub_enabled = True
            save_config()
            console.print(
                f"[ok]{Glyph.CHECK} OpenRouter subagent offload {Glyph.ARROW} on[/ok] "
                f"[muted]·saved[/muted]"
            )
            primary = or_subagents.or_sub_primary()
            if primary:
                console.print(f"[muted]Agent turns will start on [heading]{primary}[/heading].[/muted]")
            else:
                console.print("[muted]No models yet — /orsub add <model> to configure one.[/muted]")
        elif sub in ("off", "disable"):
            settings.or_sub_enabled = False
            save_config()
            console.print(
                f"[ok]{Glyph.CHECK} OpenRouter subagent offload {Glyph.ARROW} off[/ok] "
                f"[muted]·saved · Agent turns return to {settings.subagent_model}[/muted]"
            )
        elif sub == "primary":
            if not rest:
                console.print(f"[muted]Current primary:[/muted] {or_subagents.or_sub_primary() or 'none'}")
                console.print("[muted]Usage: /orsub primary <model-id>[/muted]")
                return
            models = or_subagents.load_or_sub_config()["models"]
            ids = {m["id"] for m in models}
            if rest not in ids:
                console.print(f"[warn]Unknown model:[/warn] {rest}")
                console.print(f"[muted]Known: {', '.join(sorted(ids)) or 'none — add one with /orsub add'}[/muted]")
                return
            or_subagents.save_or_sub_config({"primary": rest, "models": models})
            console.print(
                f"[ok]{Glyph.CHECK} OpenRouter subagent primary {Glyph.ARROW} "
                f"[heading]{rest}[/heading][/ok] [muted]· both OR_SUB_MODELS.json files[/muted]"
            )
        elif sub == "add":
            if not rest:
                console.print("[muted]Usage: /orsub add <model-id> (e.g. openai/gpt-5.2)[/muted]")
                return
            models = or_subagents.load_or_sub_config()["models"]
            if any(m["id"] == rest for m in models):
                console.print(f"[warn]{rest} is already in the list.[/warn]")
                return
            console.print(f"[muted]Looking up {rest} in the OpenRouter catalogue…[/muted]")
            info = or_subagents.fetch_model_info(rest)
            if info is None:
                console.print(
                    f"[warn]Could not verify {rest} against the OpenRouter catalogue.[/warn]"
                )
                console.print(
                    "[muted]The id may be wrong, or the catalogue is unreachable. "
                    "Adding with default limits anyway.[/muted]"
                )
                info = {
                    "context_window": 200_000,
                    "max_output_tokens": 32_768,
                    "reasoning_levels": ["none", "low", "medium", "high"],
                    "default_reasoning": "high",
                }
            entry = {"id": rest, **info, "notes": ""}
            models.append(entry)
            primary = or_subagents.load_or_sub_config().get("primary")
            or_subagents.save_or_sub_config({"primary": primary, "models": models})
            console.print(
                f"[ok]{Glyph.CHECK} Added [heading]{rest}[/heading][/ok] "
                f"[muted]· ctx {entry['context_window']:,} · max out {entry['max_output_tokens']:,} · "
                f"reasoning {'/'.join(entry['reasoning_levels'])}[/muted]"
            )
            console.print(
                "[muted]Written to this project's OR_SUB_MODELS.json and the main copy "
                "next to OK.dat — every project can use it now.[/muted]"
            )
            if not settings.or_sub_enabled:
                console.print("[muted]Enable the offload with [bold]/orsub on[/bold].[/muted]")
        elif sub == "remove":
            if not rest:
                console.print("[muted]Usage: /orsub remove <model-id>[/muted]")
                return
            config = or_subagents.load_or_sub_config()
            models = [m for m in config["models"] if m["id"] != rest]
            if len(models) == len(config["models"]):
                console.print(f"[warn]{rest} is not in the list.[/warn]")
                return
            primary = config["primary"]
            if primary == rest:
                primary = models[0]["id"] if models else None
            or_subagents.save_or_sub_config({"primary": primary, "models": models})
            console.print(
                f"[ok]{Glyph.CHECK} Removed {rest}[/ok] [muted]· from both OR_SUB_MODELS.json files[/muted]"
            )
            if primary and primary != rest:
                console.print(f"[muted]Primary is now [heading]{primary}[/heading].[/muted]")
        elif sub in ("models", "list"):
            self._orsub_status(detail=True)
        elif sub == "priority":
            if not rest or not extra:
                console.print("[muted]Usage: /orsub priority <model-id> <rank>[/muted]")
                return
            try:
                rank = int(extra)
            except ValueError:
                console.print(f"[warn]Priority must be a number, got:[/warn] {extra}")
                return
            config = or_subagents.load_or_sub_config()
            models = list(config["models"])
            ids = [m["id"] for m in models]
            if rest not in ids:
                console.print(f"[warn]Unknown model:[/warn] {rest}")
                return
            if rank < 1 or rank > len(models):
                console.print(f"[warn]Priority must be 1–{len(models)}.[/warn]")
                return
            entry = next(m for m in models if m["id"] == rest)
            models.remove(entry)
            models.insert(rank - 1, entry)
            or_subagents.save_or_sub_config({"primary": config["primary"], "models": models})
            console.print(
                f"[ok]{Glyph.CHECK} [heading]{rest}[/heading] priority {Glyph.ARROW} {rank}[/ok] "
                f"[muted]· both OR_SUB_MODELS.json files[/muted]"
            )
        elif sub == "reasoning":
            model_entry = or_subagents.get_model(rest)
            if model_entry is None:
                if not rest:
                    console.print("[muted]Usage: /orsub reasoning <model-id> <level>[/muted]")
                else:
                    console.print(f"[warn]Unknown model:[/warn] {rest}")
                return
            if not extra:
                console.print(
                    f"[muted]{rest}: default reasoning[/muted] "
                    f"[heading]{model_entry['default_reasoning']}[/heading] "
                    f"[muted]· accepts {'/'.join(model_entry['reasoning_levels'])}[/muted]"
                )
                return
            levels = model_entry["reasoning_levels"]
            if extra not in levels:
                console.print(
                    f"[warn]{extra} is not a level {rest} accepts.[/warn] "
                    f"[muted]({'/'.join(levels)})[/muted]"
                )
                return
            config = or_subagents.load_or_sub_config()
            models = list(config["models"])
            for m in models:
                if m["id"] == rest:
                    m["default_reasoning"] = extra
            or_subagents.save_or_sub_config({"primary": config["primary"], "models": models})
            console.print(
                f"[ok]{Glyph.CHECK} {rest} default reasoning {Glyph.ARROW} "
                f"[heading]{extra}[/heading][/ok] [muted]· both OR_SUB_MODELS.json files[/muted]"
            )
        elif sub == "test":
            await self._orsub_test(rest or None)
        else:
            console.print(f"[warn]Unknown subcommand:[/warn] {sub}")
            console.print(
                "[muted]/orsub [status|on|off|primary <id>|add <id>|remove <id>|models|"
                "priority <id> <n>|reasoning <id> <level>|test] · /help orsub[/muted]"
            )

    def _orsub_status(self, detail: bool = False) -> None:
        key_ok = (get_home() / "OK.dat").exists()
        config = or_subagents.load_or_sub_config()
        models = config["models"]
        primary = config["primary"]
        usage = or_subagents.get_or_sub_usage()
        rows = [
            ("backend", f"{'[ok]on[/ok]' if settings.or_sub_enabled else '[muted]off[/muted]'} · /orsub on"),
            ("key", f"[ok]{Glyph.CHECK} OK.dat present[/ok]" if key_ok else "[bad]OK.dat missing[/bad]"),
            ("primary", f"[heading]{primary}[/heading]" if primary else "[muted]none[/muted]"),
            ("models", f"{len(models)} in OR_SUB_MODELS.json"),
            ("requests today", str(usage.requests)),
            ("rate limits today", str(usage.rate_limits)),
            ("hops today", str(usage.hops)),
        ]
        console.print()
        console.print(panel(kv_table(rows), "OpenRouter subagents"))
        table = Table(show_header=True, box=None, padding=(0, 2, 0, 0))
        table.add_column("", width=2, no_wrap=True)
        table.add_column("model", style="heading", overflow="fold")
        table.add_column("prio", justify="right", no_wrap=True)
        table.add_column("ctx", justify="right", no_wrap=True)
        table.add_column("max out", justify="right", no_wrap=True)
        table.add_column("reasoning", overflow="fold")
        for i, m in enumerate(models, start=1):
            mark = "[accent]★[/accent]" if m["id"] == primary else ""
            table.add_row(
                mark,
                m["id"],
                str(m.get("priority", i)),
                f"{int(m.get('context_window', 0)):,}",
                f"{int(m.get('max_output_tokens', 0)):,}",
                f"{'/'.join(m.get('reasoning_levels') or [])} · default {m.get('default_reasoning')}",
            )
        console.print(table)
        console.print(
            "[muted]/orsub on · primary <id> · add <id> · remove <id> · priority <id> <n> · "
            "reasoning <id> <level> · test[/muted]"
        )

    async def _orsub_test(self, model_id: Optional[str] = None) -> None:
        """Run one real offload, on a specific model or through the failover."""
        if model_id:
            console.print(f"[muted]Testing {model_id} directly…[/muted]")
            try:
                result = await or_subagents.or_sub_chat(
                    model_id=model_id,
                    system="You are a connectivity test. Reply with exactly: OK",
                    prompt="Reply with exactly: OK",
                    reasoning=None,
                )
                console.print(
                    f"[ok]{Glyph.CHECK} {model_id} answered in {human_ms(result.elapsed_ms)}:[/ok] "
                    f"{result.answer[:200]}"
                )
            except Exception as e:
                console.print(f"[bad]✗ {model_id} failed:[/bad] {e}")
            return
        if not or_subagents.load_or_sub_models():
            console.print("[warn]No models configured — /orsub add <model> first.[/warn]")
            return
        console.print(f"[muted]Testing offload (primary first, failover on rate limits)…[/muted]")
        try:
            result, attempts = await or_subagents.or_sub_with_failover(
                system="You are a connectivity test. Reply with exactly: OK",
                prompt="Reply with exactly: OK",
                reasoning=None,
            )
            hop = f" after {len(attempts) - 1} hop(s)" if len(attempts) > 1 else ""
            console.print(
                f"[ok]{Glyph.CHECK} [heading]{result.model}[/heading] answered{hop} in "
                f"{human_ms(result.elapsed_ms)}: {result.answer[:200]}[/ok]"
            )
            console.print(f"[muted]tried: {', '.join(attempts)}[/muted]")
        except Exception as e:
            console.print(f"[bad]✗ Offload failed:[/bad] {e}")

    # ------------------------------------------------------------------
    # /config — global vs per-project
    # ------------------------------------------------------------------

    def cmd_config(self, arg: str = "") -> None:
        sub = arg.strip().lower()

        if sub in ("global", "save-global", "save global"):
            path = save_config(scope="global")
            console.print(
                f"[ok]{Glyph.CHECK} Current settings saved as the global default[/ok] "
                f"[muted]{Glyph.ARROW} {path}[/muted]"
            )
            console.print("[muted]New projects inherit these settings unless they carry their own .g023/config.json.[/muted]")
            return
        if sub in ("project", "save-project", "save project"):
            path = save_config(scope="project")
            console.print(
                f"[ok]{Glyph.CHECK} Settings pinned to this project[/ok] [muted]{Glyph.ARROW} {path}[/muted]"
            )
            return
        if sub == "save":
            path = save_config()
            scope = "project" if path == get_project_config_file() else "global"
            console.print(
                f"[ok]{Glyph.CHECK} Settings saved ({scope})[/ok] [muted]{Glyph.ARROW} {path}[/muted]"
            )
            return
        if sub == "clear":
            project = get_project_config_file()
            if not project.exists():
                console.print("[muted]No project config to clear — the global config applies.[/muted]")
                return
            if not confirm("Drop this project's .g023/config.json? Global defaults will apply.", default=False):
                return
            project.unlink(missing_ok=True)
            defaults = Settings()
            for key in PERSISTED_KEYS:
                setattr(settings, key, getattr(defaults, key))
            load_config(settings)
            console.print(
                f"[ok]{Glyph.CHECK} Project override removed[/ok] [muted]{Glyph.ARROW} "
                f"{get_config_file()} now applies[/muted]"
            )
            return
        if sub in ("", "status", "show"):
            self._config_status()
            return
        console.print("[muted]Usage: /config [status|save|save global|save project|global|project|clear][/muted]")

    def _config_status(self) -> None:
        global_path = get_config_file()
        project_path = get_project_config_file()
        active = "project" if project_path.exists() else "global"
        rows = [
            ("active scope", f"[heading]{active}[/heading]"),
            (
                "global",
                str(global_path) + (" [ok]· exists[/ok]" if global_path.exists() else " [muted]· missing[/muted]"),
            ),
            (
                "project",
                str(project_path)
                + (" [ok]· overrides global[/ok]" if project_path.exists() else " [muted]· none — global applies[/muted]"),
            ),
            ("orchestrator", settings.orchestrator_model),
            ("vision", f"{settings.vision_backend} {Glyph.DOT} {settings.effective_vision_model}"),
            ("surge pause", f"{'on' if settings.surge_pause else 'off'} (buffer {settings.surge_pause_buffer_minutes} min)"),
        ]
        console.print()
        console.print(panel(kv_table(rows), "Config scopes"))
        console.print(
            "[muted]/config global — promote current settings to global default · "
            "/config save project · /config clear[/muted]"
        )

    # ------------------------------------------------------------------
    # /goal
    # ------------------------------------------------------------------

    async def cmd_goal(self, arg: str = "") -> None:
        if not arg:
            console.print("[muted]Usage: /goal <objective>[/muted]")
            console.print("[muted]e.g. /goal add retry-with-backoff to every outbound HTTP call[/muted]")
            return

        previous = settings.reasoning_effort
        previous_enabled = settings.thinking_enabled
        # A goal is the one command whose whole point is "however many steps it
        # takes". The step cap is the backstop against a turn that tools forever
        # without answering; here it is the thing that would cut the job short,
        # so the turn runs uncapped and the loop's progress line plus Ctrl-C are
        # what keep it watched. The saved /steps setting is untouched — this is
        # for the duration of this turn only, and is not written to config.
        # Effort is the harness ceiling (75 / high), not the API's max: max
        # burns the output budget on thinking. A same-response [next_effort]
        # on the final answer is kept for the next user request; only an
        # omitted trailer restores the pre-goal dial.
        previous_iterations = settings.max_iterations
        settings.reasoning_effort = MAX_EFFORT
        settings.thinking_enabled = True
        settings.max_iterations = UNLIMITED_ITERATIONS
        self.orch.release_prefix_effort()
        try:
            set_goal(arg)
            mirror_working_set(
                self.orch.state.items, load_working_set(), self.orch._sticky_goal_item()
            )
        except Exception:
            pass
        console.print(
            f"[accent]Goal[/accent] [muted]{Glyph.DOT} ceiling reasoning effort "
            f"({MAX_EFFORT}), no step limit for this turn[/muted]\n{arg}"
        )
        try:
            await self.run_prompt(
                f"High-level goal (use tools freely, break into steps):\n\n{arg}",
                title=f"g023 {Glyph.DOT} goal",
            )
        finally:
            if not getattr(self.orch, "_next_effort_applied", False):
                settings.reasoning_effort = previous  # type: ignore[assignment]
                settings.thinking_enabled = previous_enabled
            settings.max_iterations = previous_iterations
            self.orch.release_prefix_effort()
            self._emit_goal_judgment()

    def _emit_goal_judgment(self) -> None:
        """One completed/not-completed judgment after a /goal turn returns."""
        try:
            judgment = judge_goal_completion()
        except Exception as e:
            judgment = {
                "completed": False,
                "status": "not completed",
                "reason": f"goal judgment failed: {e}",
                "pending": [],
                "done_without_evidence": [],
            }
        self.last_goal_judgment = judgment
        status = judgment.get("status") or (
            "completed" if judgment.get("completed") else "not completed"
        )
        reason = str(judgment.get("reason") or "")
        style = "good" if judgment.get("completed") else "warn"
        line = f"[{style}]Goal {status}[/{style}]"
        if reason:
            line += f" [muted]{escape(reason)}[/muted]"
        console.print(line)

    # ------------------------------------------------------------------
    # Turn execution
    # ------------------------------------------------------------------

    async def run_prompt(self, text: str, title: str = "g023") -> None:
        try:
            result = await self.orch.run_turn(text)
        except KeyboardInterrupt:
            console.print("\n[warn]Interrupted.[/warn]")
            return
        except Exception as e:
            console.print(f"[bad]Error:[/bad] {e}")
            if settings.show_thinking:
                import traceback

                traceback.print_exc()
            return

        if not self.orch.answer_was_streamed:
            console.print()
            console.print(panel(Markdown(result or "_(no answer)_"), title))

        if settings.show_answer_detail or settings.show_context_bar:
            console.print()
        if settings.show_answer_detail:
            console.print(self.orch.turn_summary())
        if settings.show_context_bar:
            console.print(self.orch.status_line())

    def _surge_tick(self) -> None:
        """Fire the surge notification on state transitions (once per change)."""
        active = surge.is_surge()
        if self._surge_was_active is None:
            self._surge_was_active = active
            if active:
                console.print()
                console.print(
                    panel(
                        "[warn]DeepSeek is at surge pricing (peak hours) — prices are 2x off-peak.[/warn]\n"
                        "[muted]/surge to check the windows · /surge on to auto-pause around peak.[/muted]",
                        "Surge pricing",
                    )
                )
            return
        if active == self._surge_was_active:
            return
        self._surge_was_active = active
        if active:
            _, when = surge.next_transition()
            console.print()
            console.print(
                panel(
                    f"[warn]DeepSeek surge pricing just started — prices doubled.[/warn]\n"
                    f"[muted]Peak until {when:%H:%M} local · /surge on to pause usage around peak.[/muted]",
                    "Surge pricing",
                )
            )
        else:
            console.print()
            console.print(
                panel(
                    "[ok]DeepSeek surge pricing ended — back to off-peak (half price).[/ok]",
                    "Surge pricing",
                )
            )

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def loop(self):
        self.print_banner()
        try:
            load_api_key()
        except Exception as e:
            console.print(f"[bad]{e}[/bad]")
            return

        # Surge monitor state: notify once per transition, pause around peak.
        self._surge_was_active: Optional[bool] = None
        self._surge_pause_announced = False
        self._surge_bypass = False

        while self.running:
            if settings.surge_notify:
                self._surge_tick()

            if settings.surge_pause and not self._surge_bypass:
                remaining = surge.seconds_until_pause_ends()
                if remaining > 0:
                    if not self._surge_pause_announced:
                        _, when = surge.next_transition()
                        console.print()
                        console.print(
                            panel(
                                "[warn]DeepSeek surge pricing — usage paused[/warn]\n"
                                "[muted]Peak hours bill at 2x off-peak. Input is paused "
                                f"for {remaining // 60} min {remaining % 60}s, until the "
                                f"window (+ buffer) ends around {when:%H:%M} local.[/muted]\n"
                                "[muted]Ctrl-C to bypass the pause and work at peak prices.[/muted]",
                                "Surge pause",
                            )
                        )
                        self._surge_pause_announced = True
                    try:
                        await asyncio.sleep(5)
                        continue
                    except KeyboardInterrupt:
                        console.print(
                            "\n[warn]Bypassing surge pause — commands will run at peak prices.[/warn]"
                        )
                        self._surge_pause_announced = False
                        self._surge_bypass = True
            self._surge_bypass = False

            try:
                console.print()
                user = await self.reader.ask_async()
            except (KeyboardInterrupt, EOFError):
                console.print(f"\n[muted]Bye.[/muted]")
                break

            line = user.strip()
            if not line:
                continue

            if line.startswith("/"):
                try:
                    await self.handle_slash(line)
                except KeyboardInterrupt:
                    console.print("\n[warn]Cancelled.[/warn]")
                except Exception as e:
                    console.print(f"[bad]Command failed:[/bad] {e}")
                    if settings.show_thinking:
                        import traceback

                        traceback.print_exc()
                continue

            await self.run_prompt(line)


def parse_cli_argv(argv: Optional[list[str]] = None) -> dict:
    """Split process args into a mode the entry point can act on.

    Launchers (`g023.sh`, `g023.bat`) already forward unused tokens to
    ``python -m g023_code``. Empty argv is the interactive REPL. A leading
    ``--help``/``-h`` or ``--version``/``-V`` is non-interactive. ``--continue``
    seeds from the latest ``.g023/logs/`` handoff (else JSONL). Anything
    else is one prompt, run once, then exit.
    """
    tokens = list(sys.argv[1:] if argv is None else argv)
    continue_flag = False
    if tokens and tokens[0] in ("--continue", "-c"):
        continue_flag = True
        tokens = tokens[1:]
    if tokens and tokens[0] in ("-h", "--help"):
        return {"mode": "help", "prompt": "", "continue": False}
    if tokens and tokens[0] in ("-V", "--version"):
        return {"mode": "version", "prompt": "", "continue": False}
    if tokens:
        return {"mode": "oneshot", "prompt": " ".join(tokens), "continue": continue_flag}
    return {"mode": "repl", "prompt": "", "continue": continue_flag}


def print_flag_help() -> None:
    """Non-interactive help: program name, usage, and the slash-command list."""
    console.print(f"g023 Code {__version__}")
    # Square brackets are rich markup; this line must print as literal usage.
    console.print(
        "Usage: python -m g023_code [--help] [--version] [--continue] [prompt ...]",
        markup=False,
    )
    console.print()
    console.print("With no prompt, starts the interactive REPL.")
    console.print("A prompt runs one turn and exits.")
    console.print(
        "--continue seeds from the latest .g023/logs/ handoff (system + summary)."
    )
    console.print()
    for group, commands in registry.by_group():
        console.print(f"{group}:")
        for c in commands:
            console.print(f"  {c.name}  {c.summary}")
        console.print()
    console.print("Type /help inside the REPL for the same catalogue with detail.")


async def _run_oneshot(cli: "CLI", prompt: str) -> None:
    try:
        load_api_key()
    except Exception as e:
        console.print(f"[bad]{e}[/bad]")
        return
    await cli.run_prompt(prompt)


def main(argv: Optional[list[str]] = None) -> int:
    parsed = parse_cli_argv(argv)
    if parsed["mode"] == "help":
        print_flag_help()
        return 0
    if parsed["mode"] == "version":
        console.print(f"g023 Code {__version__}")
        return 0

    cli = CLI()
    try:
        if parsed.get("continue"):
            seeded = cli._resume_objective_log(quiet_if_missing=True)
            if not seeded:
                cli.cmd_resume()
        if parsed["mode"] == "oneshot":
            asyncio.run(_run_oneshot(cli, parsed["prompt"]))
        else:
            asyncio.run(cli.loop())
    except KeyboardInterrupt:
        console.print(f"\n[muted]Bye.[/muted]")
    return 0

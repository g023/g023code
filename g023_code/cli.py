"""
Interactive CLI for g023 Code.

Every slash command is declared in :mod:`g023_code.commands` and implemented
here as a ``cmd_*`` method. Commands that take a fixed set of options open a
picker when called bare, so nothing has to be memorised: ``/model`` is as
usable as ``/model pro``.
"""

from __future__ import annotations

import asyncio
import time

from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import __version__
from . import commands as registry
from . import ollama_client
from . import ui
from .commands import Command
from .config import (
    AVAILABLE_MODELS,
    PERSISTED_KEYS,
    Settings,
    UNAVAILABLE_MODELS,
    get_config_file,
    get_home,
    get_project_root,
    get_scratch_dir,
    save_config,
    settings,
    load_api_key,
)
from .orchestrator import Orchestrator
from .cache import get_cache
from .signals import get_signals, hit_rate_verdict
from .prompt import Reader, choose, confirm, install_hint
from .tools.registry import ALLOW, ASK, BLOCK, get_registry, _describe_age
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

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------

    def toolbar(self) -> str:
        """Bottom status bar (prompt_toolkit only) — plain text with HTML tags."""
        usage = get_usage()
        frac = self.orch.context_fraction()
        vision = settings.vision_model if settings.vision_enabled else "off"
        return (
            f" {settings.orchestrator_model} · think {settings.reasoning_effort} "
            f"· ctx {frac:.0%} of {settings.max_context_tokens // 1000}k "
            f"· {format_cost(usage.cost())} · vision {vision} · /help "
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
                effort=settings.reasoning_effort if settings.thinking_enabled else "off",
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
                    "ollama",
                    f"{ollama_client.resolve_host()} [muted]({ollama_client.host_source()})[/muted]",
                ),
                ("project", str(get_project_root())),
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

    def cmd_exit(self, arg: str = "") -> None:
        self.running = False

    # ------------------------------------------------------------------
    # Model commands
    # ------------------------------------------------------------------

    def cmd_model(self, arg: str = "") -> None:
        aliases = {"flash": "deepseek-v4-flash", "pro": "deepseek-v4-pro"}
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
        aliases = {"none": "off", "disabled": "off", "medium": "high", "mid": "high"}
        value = aliases.get(value, value)

        if not value:
            options = [
                ("low", "quick answers, fewest reasoning tokens"),
                ("high", "the default — good reasoning without excess"),
                ("max", "hardest problems; noticeably more output tokens"),
                ("off", "no thinking block at all"),
            ]
            current = settings.reasoning_effort if settings.thinking_enabled else "off"
            default = [o[0] for o in options].index(current) + 1
            picked = choose("Reasoning effort", options, default=default)
            if picked is None:
                return
            value = options[picked - 1][0]

        if value == "off":
            settings.thinking_enabled = False
            save_config()
            console.print(f"[warn]Thinking disabled.[/warn] [muted]·saved[/muted]")
            return
        if value not in ("low", "high", "max"):
            console.print(f"[warn]Unknown effort:[/warn] {arg}  [muted](low | high | max | off)[/muted]")
            return

        settings.reasoning_effort = value  # type: ignore[assignment]
        settings.thinking_enabled = True
        save_config()
        console.print(f"[ok]{Glyph.CHECK} Reasoning effort {Glyph.ARROW} {value}[/ok] [muted]·saved[/muted]")

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
                f"[muted]{Glyph.DOT} triggers at {settings.compact_threshold:.0%} of "
                f"{settings.max_context_tokens:,} tokens ·saved[/muted]"
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
        row("max_tokens", f"{settings.max_tokens:,}", "per response")
        row("temperature", settings.temperature)
        row("max_context_tokens", f"{settings.max_context_tokens:,}")
        row("compact_threshold", f"{settings.compact_threshold:.0%}", "auto-compact trigger")
        row("auto_compact", settings.auto_compact, "/compact auto on|off")
        row("max_search_matches", settings.max_search_matches)
        row("file_summary_max_tokens", settings.file_summary_max_tokens)
        row("permission_default", settings.permission_default, "/tools")
        row("vision_backend", settings.vision_backend, "/vision")
        row("vision_model", settings.vision_model or "—")
        row("vision_host", settings.vision_host or f"(auto: {ollama_client.resolve_host()})", "/ollama host")
        row("vision_max_image_dim", settings.vision_max_image_dim, "px on the longest edge")
        row("vision_num_ctx", f"{settings.vision_num_ctx:,}")
        row("vision_timeout", f"{settings.vision_timeout}s")
        row("vision_keep_alive", settings.vision_keep_alive, "how long the model stays in VRAM")
        row("base_url", settings.base_url)
        row("project_root", settings.project_root)
        row("home", settings.home)
        row("config_file", get_config_file())

        console.print()
        console.print(panel(table, "Settings", subtitle="·saved keys persist to config.json"))
        console.print("[muted]/settings save to persist now · /settings reset for defaults[/muted]")

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
            f"{Glyph.DOT} ${p.output}/1M out"
            for model, p in PRICING.items()
            if model in AVAILABLE_MODELS
        ]
        return "[muted]" + "\n".join(parts) + "\nEstimates only — actual billing is DeepSeek's.[/muted]"

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
        model = settings.vision_model or "(backend default)"
        return f"[ok]on[/ok] {Glyph.DOT} {settings.vision_backend} {Glyph.DOT} [heading]{model}[/heading]"

    def _disable_vision(self):
        settings.vision_backend = "none"
        settings.vision_model = None
        save_config()
        console.print(f"[warn]Vision disabled.[/warn] [muted]·saved[/muted]")

    def _enable_vision(self, model: str, warn_no_vision: bool = False):
        settings.vision_backend = "ollama"
        settings.vision_model = model
        path = save_config()
        if warn_no_vision:
            console.print(
                f"[warn]Note:[/warn] {model} does not advertise image support — "
                "AnalyzeImage may fail. [bold]/ollama test[/bold] settles it."
            )
        host = ollama_client.resolve_host()
        console.print(
            f"[ok]{Glyph.CHECK} Vision enabled {Glyph.ARROW}[/ok] [heading]{model}[/heading] "
            f"[muted]{Glyph.DOT} {host}[/muted]"
        )
        console.print(f"[muted]Saved to {path}. AnalyzeImage is now offered to the orchestrator.[/muted]")

    def cmd_vision(self, arg: str = "") -> None:
        low = arg.lower().strip()

        if low in ("status", "show"):
            console.print(f"Vision: {self.vision_status()}")
            console.print(f"[muted]host: {ollama_client.resolve_host()} · config: {get_config_file()}[/muted]")
            return
        if low in ("off", "none", "disable", "disabled", "0", "false"):
            self._disable_vision()
            return
        if low.startswith("backend"):
            backend = low[len("backend"):].strip()
            if not backend:
                console.print(f"[muted]Current backend:[/muted] {settings.vision_backend}")
                console.print("[muted]Usage: /vision backend none|ollama|glm|openai|local[/muted]")
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
            elif backend not in ("none", "ollama"):
                console.print(f"[warn]'{backend}' has no implementation yet; only ollama works today.[/warn]")
            return

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
            self._enable_vision(match.name, warn_no_vision=not match.supports_vision)
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
        self._enable_vision(chosen.name, warn_no_vision=not chosen.supports_vision)

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
        settings.reasoning_effort = "max"  # type: ignore[assignment]
        settings.thinking_enabled = True
        console.print(
            f"[accent]Goal[/accent] [muted]{Glyph.DOT} max reasoning effort for this turn[/muted]\n{arg}"
        )
        try:
            await self.run_prompt(
                f"High-level goal (use tools freely, break into steps):\n\n{arg}",
                title=f"g023 {Glyph.DOT} goal",
            )
        finally:
            settings.reasoning_effort = previous  # type: ignore[assignment]
            settings.thinking_enabled = previous_enabled

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

        while self.running:
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


def main():
    cli = CLI()
    try:
        asyncio.run(cli.loop())
    except KeyboardInterrupt:
        console.print(f"\n[muted]Bye.[/muted]")

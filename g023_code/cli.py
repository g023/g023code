"""
Interactive CLI for g023 Code.

Every slash command is declared in :mod:`g023_code.commands` and implemented
here as a ``cmd_*`` method. Commands that take a fixed set of options open a
picker when called bare, so nothing has to be memorised: ``/model`` is as
usable as ``/vision off``.
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
from pathlib import Path
from typing import Optional

from rich.markdown import Markdown
from rich.markup import escape
from rich.table import Table
from rich.text import Text

from . import __version__
from . import commands as registry
from . import project_instructions
from . import surge
from . import ui
from .commands import Command
from .effort import MAX_EFFORT, parse_effort_value
from .config import (
    ITERATION_NOTICE_EVERY,
    MAX_COMPACT_THRESHOLD,
    MAX_CONTEXT_TOKENS,
    MODEL,
    MODEL_LABEL,
    MIN_COMPACT_THRESHOLD,
    MIN_CONTEXT_TOKENS,
    MIN_ITERATIONS,
    MIN_OUTPUT_TOKENS,
    PERSISTED_KEYS,
    Settings,
    GOAL_CONTINUATION_CAP,
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
    parse_setting,
    save_config,
    settings,
    load_api_key,
)
from .interrupts import UserInterrupt, controller as interrupt_controller
from .orchestrator import Orchestrator
from .cache import get_cache
from .working_set import (
    dumps_working_set,
    judge_goal_completion,
    load as load_working_set,
    mirror_working_set,
    set_goal,
)
from .signals import get_signals, hit_rate_verdict
from .prompt import Reader, choose, confirm, install_hint
from .tools.registry import ALLOW, ASK, BLOCK, get_registry, restore_newest_backup, _describe_age
from .ui import Glyph, console, gauge, human_bytes, kv_table, panel
from .usage import PRICING, format_cost, get_usage


def _parse_token_count(value: str, *, small_is_fraction: bool = True):
    """"200k" / "1m" / "150000" -> int tokens; a fraction or word -> None.

    With ``small_is_fraction`` (the compact-threshold reading) a bare 1..100
    is a percentage and returns None.
    """
    v = value.strip().lower().replace(",", "").replace("_", "")
    if not v:
        return None
    mult = 1
    if v.endswith("k"):
        mult, v = 1_000, v[:-1]
    elif v.endswith("m"):
        mult, v = 1_000_000, v[:-1]
    if not v.isdigit():
        return None
    n = int(v) * mult
    if small_is_fraction and mult == 1 and 0 < n <= 100:
        return None  # "85" reads as a percentage, handled by the fraction path
    return n


def tool_note(name: str, level: str) -> str:
    """The `/tools` note column: what a tool costs or why it is absent, never a guess.

    Agent and AnalyzeImage are not offered to the model while their switch is off;
    ReadFile / SearchContent answer locally (ReadFile pays one model call only for a
    semantic focus under /subagents on); FetchUrl's prompt offers the cached copy.
    """
    if name == "Agent":
        return "isolated worker" if settings.subagents_enabled else "not offered (/subagents on)"
    if name == "AnalyzeImage":
        return "isolated model call" if settings.vision_enabled else "not offered (/vision on)"
    if name == "ReadFile":
        return "local index; semantic focus = 1 call" if settings.subagents_enabled else "local index, no API call"
    if name == "SearchContent":
        return "local, no API call"
    if name == "FetchUrl" and level == ASK:
        return "prompt offers the cached copy"
    return ""


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
        self._goal_interrupted = False

    # ------------------------------------------------------------------
    # Chrome
    # ------------------------------------------------------------------

    def toolbar(self) -> str:
        """Bottom status bar (prompt_toolkit only) — plain text with HTML tags."""
        usage = get_usage()
        frac = self.orch.context_fraction()
        vision = "on" if settings.vision_enabled else "off"
        surge_badge = " · SURGE 2×" if surge.is_surge() else ""
        return (
            f" {MODEL} · think {settings.reasoning_effort} "
            f"· ctx {frac:.0%} of {settings.max_context_tokens // 1000}k "
            f"· {format_cost(usage.cost())} · vision {vision}{surge_badge} · /help "
        )

    def print_banner(self):
        console.print()
        console.print(
            ui.banner(
                version=__version__,
                project=ui.display_path(get_project_root()),
                home=ui.display_path(get_home()),
                model=MODEL,
                effort=str(settings.reasoning_effort) if settings.thinking_enabled else "off",
                verbose=settings.verbose,
                vision=self.vision_status(),
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
            console.print(f"[warn]Unknown command:[/warn] {escape(token)}")
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
                console.print(f"[warn]No such command:[/warn] {escape(arg)}")
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
                    f"{MODEL} [muted]{Glyph.DOT} thinking "
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
                ("project", ui.display_path(get_project_root())),
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
        if token:
            console.print(f"[warn]Unknown argument:[/warn] {escape(arg.strip())}  "
                          f"[muted](/resume | /resume log)[/muted]")
            return
        items, ws, path = self.orch.resume_from_latest()
        if path is None:
            # Same-session JSONL missing: fall back to the last objective handoff.
            if self._resume_objective_log(quiet_if_missing=True):
                return
            console.print("[muted]No session log under .g023/sessions/.[/muted]")
            return
        n = len(items)
        if n == 0:
            console.print("[muted]Nothing to resume: no session under .g023/sessions/ has history yet.[/muted]")
            return
        console.print(
            f"[ok]{Glyph.CHECK} Resumed {n} item(s) from [muted]{ui.display_path(path)}[/muted][/ok]"
        )
        mismatch = getattr(self.orch, "prefix_mismatch", None)
        if mismatch:
            console.print(f"[warn]{mismatch}[/warn]")
        self._print_rehydrate()

    def _resume_objective_log(self, quiet_if_missing: bool = False) -> bool:
        items, ws, path = self.orch.seed_from_objective_log()
        if path is None:
            if not quiet_if_missing:
                console.print("[muted]No objective handoff under .g023/logs/.[/muted]")
            return False
        n = len(items)
        console.print(
            f"[ok]{Glyph.CHECK} New orchestration from handoff "
            f"[muted]{ui.display_path(path)}[/muted] ({n} item(s))[/ok]"
        )
        self._print_rehydrate()
        return True

    def _print_rehydrate(self) -> None:
        """One status line: handoff provenance and what changed on disk since the saved state."""
        info = getattr(self.orch, "last_rehydrate", None) or {}
        drift = info.get("drift") or {}
        bits = []
        prov = info.get("provenance")
        if prov:
            bits.append("signature verified" if prov == "verified" else f"signature: {prov}")
        changed, missing = drift.get("changed") or [], drift.get("missing") or []
        if drift.get("checked"):
            bits.append(
                f"{len(changed)} changed, {len(missing)} missing of {drift['checked']} tracked file(s)"
            )
        if drift.get("verify_stale"):
            bits.append("recorded verify is stale")
        if not bits:
            return
        style = "warn" if info.get("note") else "muted"
        console.print(f"[{style}]rehydrate {Glyph.DOT} {escape(f' {Glyph.DOT} '.join(bits))}[/{style}]")

    def cmd_bbs(self, arg: str = "") -> None:
        from . import bbs as bbs_mod

        raw = (arg or "").strip()
        if not raw:
            bbs_mod.ensure_layout()
            st = bbs_mod.status()
            console.print(
                f"bbs [muted]{st['root']} {Glyph.DOT} {st['topics']} topic(s) "
                f"{Glyph.DOT} {st['posts']} post(s) {Glyph.DOT} "
                f"index {st['index']} {Glyph.DOT} /bbs list|search|read|post[/muted]"
            )
            return
        parts = raw.split(None, 1)
        action = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""
        if action == "list":
            bbs_mod.ensure_layout()
            if rest:
                try:
                    rows = bbs_mod.list_posts(rest)
                except ValueError as exc:
                    console.print(f"[warn]{exc}[/warn]")
                    return
                if not rows:
                    console.print(f"[muted]No posts in topic {rest!r}.[/muted]")
                    return
                for row in rows:
                    console.print(row["path"])
                return
            topics = bbs_mod.list_topics()
            if not topics:
                console.print("[muted]No topics yet. /bbs post <topic> <slug> <body>[/muted]")
                return
            for topic in topics:
                console.print(f"{topic['topic']}/ ({topic['posts']})")
            return
        if action == "search":
            if not rest:
                console.print("[muted]Usage: /bbs search <query>[/muted]")
                return
            result = bbs_mod.search(rest)
            if not result.get("ok"):
                console.print(f"[warn]{result.get('error')}[/warn]")
                return
            samples = result.get("samples") or []
            console.print(
                f"[muted]{result.get('total', 0)} match(es) "
                f"{Glyph.DOT} showing {len(samples)}[/muted]"
            )
            for sample in samples:
                file = sample.get("file") or ""
                line = sample.get("line") or ""
                match = (sample.get("match") or sample.get("text") or "").strip()
                console.print(f"{file}:{line} {match[:120]}")
            return
        if action == "read":
            ref = rest.split()[0] if rest else ""
            if not ref:
                console.print("[muted]Usage: /bbs read <topic/slug>[/muted]")
                return
            result = bbs_mod.read_post(ref)
            if not result.get("ok"):
                console.print(f"[warn]{result.get('error')}[/warn]")
                return
            console.print(result.get("body") or "")
            return
        if action == "post":
            bits = rest.split(None, 2)
            if len(bits) < 3:
                console.print(
                    "[muted]Usage: /bbs post <topic> <slug> <body>[/muted]"
                )
                return
            try:
                result = bbs_mod.post(bits[0], bits[1], bits[2])
            except ValueError as exc:
                console.print(f"[warn]{exc}[/warn]")
                return
            verb = "created" if result.get("created") else "appended"
            console.print(f"[ok]{Glyph.CHECK} {verb} {result.get('path')}[/ok]")
            return
        if action == "index":
            result = bbs_mod.write_index()
            console.print(
                f"[ok]{Glyph.CHECK} {result.get('path')} "
                f"({result.get('topics', 0)} topic(s), "
                f"{result.get('posts', 0)} post(s))[/ok]"
            )
            return
        console.print("[muted]Usage: /bbs [list|search|read|post|index][/muted]")

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
        if result.get("saved_current"):
            console.print(
                f"[muted]The replaced version was kept as {result['saved_current']}; "
                "/undo again steps one version further back.[/muted]"
            )

    def cmd_exit(self, arg: str = "") -> None:
        self.running = False

    # ------------------------------------------------------------------
    # Model commands
    # ------------------------------------------------------------------

    def cmd_model(self, arg: str = "") -> None:
        """Info only: there is exactly one model, so nothing to pick."""
        if arg.strip():
            console.print(
                f"[muted]{MODEL_LABEL} ({MODEL}) is the only supported model; "
                f"there is nothing to switch to.[/muted]"
            )
        p = PRICING[MODEL]
        rows = [
            ("model", f"[heading]{MODEL_LABEL}[/heading] [muted]({MODEL})[/muted]"),
            ("endpoint", f"{settings.base_url.rstrip('/')}/anthropic/v1/messages"),
            ("context", f"{describe_context_window(settings.max_context_tokens)} [muted](model max {MAX_CONTEXT_TOKENS:,})[/muted]"),
            ("output", describe_max_tokens(settings.max_tokens)),
            ("thinking", f"{settings.reasoning_effort if settings.thinking_enabled else 'off'} [muted]· /thinking[/muted]"),
            ("vision", self.vision_status()),
            (
                "price /1M",
                f"${p.cache_hit} hit {Glyph.DOT} ${p.cache_miss} miss {Glyph.DOT} ${p.output} out "
                f"[muted](off-peak; peak 2×)[/muted]",
            ),
        ]
        console.print()
        console.print(panel(kv_table(rows), "Model"))

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

        if value in ("on", "enable", "enabled"):
            # Back on at the saved effort (off keeps it; 0 would mean off again).
            n0 = parse_effort_value(settings.reasoning_effort)
            value = str(n0) if n0 else "high"
        if value == "off":
            settings.thinking_enabled = False
            save_config()
            self.orch.release_prefix_effort()
            console.print("[warn]Thinking disabled.[/warn] [muted]·saved[/muted]")
            return
        named = {"low": 50, "high": 75, "max": MAX_EFFORT}
        if value in named:
            n = named[value]
        else:
            n = parse_effort_value(value)
            if n is None:
                console.print(
                    f"[warn]Unknown effort:[/warn] {escape(arg.strip())}  "
                    f"[muted](1-100 | low | high | max | on | off; 75 is the ceiling)[/muted]"
                )
                return
            if n == 0:
                settings.thinking_enabled = False
                save_config()
                self.orch.release_prefix_effort()
                console.print("[warn]Thinking disabled.[/warn] [muted]·saved[/muted]")
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
        aliases = {"off": "low", "on": "high", "medium": "mid", "max": "high", "quiet": "low",
                   "1": "low", "2": "mid", "3": "high"}  # the picker's numbers
        level = aliases.get(arg.strip().lower(), arg.strip().lower())

        if not level:
            options = [(name, self.VERBOSE_HELP[name]) for name in ("low", "mid", "high")]
            default = ["low", "mid", "high"].index(settings.verbose) + 1
            picked = choose("Output detail", options, default=default)
            if picked is None:
                return
            level = options[picked - 1][0]

        if level not in ("low", "mid", "high"):
            console.print(f"[warn]Unknown level:[/warn] {escape(arg)}  [muted](low | mid | high)[/muted]")
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
                count = _parse_token_count(value, small_is_fraction=False)
                if count is None:  # "-5" still reaches the range message below
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

    def cmd_subagents(self, arg: str = "") -> None:
        value = arg.strip().lower()
        if value in ("on", "true", "yes", "1"):
            settings.subagents_enabled = True
        elif value in ("off", "false", "no", "0"):
            settings.subagents_enabled = False
        elif value:
            console.print("[muted]Usage: /subagents on|off[/muted]")
            return
        else:
            state = "on" if settings.subagents_enabled else "off (single-agent)"
            console.print(f"[key]Subagents[/key] {Glyph.ARROW} {state}")
            return
        save_config()
        console.print(
            f"[ok]{Glyph.CHECK} Subagents {Glyph.ARROW} "
            f"{'on' if settings.subagents_enabled else 'off'}[/ok] [muted]·saved[/muted]"
        )

    def cmd_ownership(self, arg: str = "") -> None:
        from . import ownership

        words = arg.strip().split(maxsplit=1)
        value = words[0].lower() if words else ""
        if value == "forget":
            target = words[1].strip() if len(words) > 1 else ""
            if not target:
                console.print("[muted]Usage: /ownership forget <path>[/muted]")
            elif ownership.forget(target):
                console.print(f"[ok]{Glyph.CHECK} {escape(target)} {Glyph.ARROW} yours again (minimal-change)[/ok]")
            else:
                console.print(f"[muted]{escape(target)} is not on the created-by-g023 list.[/muted]")
            return
        if value in ownership.MODES:
            settings.ownership = value
            save_config()
            console.print(f"[ok]{Glyph.CHECK} Ownership {Glyph.ARROW} {value}[/ok] [muted]·saved[/muted]")
            return
        if value:
            console.print("[muted]Usage: /ownership [auto|user|agent] | /ownership forget <path>[/muted]")
            return
        files = ownership.listing()
        meaning = {
            "auto": "model tidies only files it created",
            "user": "minimal-change everywhere",
            "agent": "model tidies the whole project",
        }[ownership.mode()]
        console.print(f"[key]Ownership[/key] {Glyph.ARROW} {ownership.mode()} [muted]{Glyph.DOT} {meaning}[/muted]")
        if files:
            shown = ", ".join(escape(f) for f in files[:12])
            more = f" (+{len(files) - 12} more)" if len(files) > 12 else ""
            console.print(f"[muted]Created by g023 ({len(files)}): {shown}{more}[/muted]")

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
                count = _parse_token_count(value, small_is_fraction=False)
                if count is None:  # "-5" still reaches the range message below
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

        if value in ("economic", "fixed"):
            settings.compact_policy = value
            save_config()
            note = (
                "also compacts once re-reading history costs more than compacting"
                if value == "economic"
                else "token trigger only"
            )
            console.print(f"[ok]{Glyph.CHECK} Compact policy {Glyph.ARROW} {value}[/ok] [muted]{note} ·saved[/muted]")
            return
        if value.startswith("floor"):
            floor = _parse_token_count(value[5:].strip())
            if floor is None or not 0 <= floor <= MAX_CONTEXT_TOKENS:
                console.print(f"[muted]Usage: /compact-at floor 64k  (0–{MAX_CONTEXT_TOKENS:,} tokens)[/muted]")
                return
            settings.compact_floor_tokens = floor
            save_config()
            console.print(
                f"[ok]{Glyph.CHECK} Economic compaction floor {Glyph.ARROW} {floor:,} tokens[/ok] [muted]·saved[/muted]"
            )
            return

        tokens = _parse_token_count(value)
        if tokens is not None:
            if tokens and not MIN_CONTEXT_TOKENS <= tokens <= MAX_CONTEXT_TOKENS:
                console.print(
                    f"[warn]Out of range:[/warn] {escape(arg.strip())}  "
                    f"[muted]({MIN_CONTEXT_TOKENS:,}–{MAX_CONTEXT_TOKENS:,} tokens, or 0 for "
                    f"fraction-only)[/muted]"
                )
                return
            settings.compact_at_tokens = tokens
            save_config()
            console.print(
                f"[ok]{Glyph.CHECK} Auto-compact at {Glyph.ARROW} "
                + (f"{tokens:,} tokens" if tokens else "fraction of the window only")
                + " [muted]·checked between every step ·saved[/muted]"
            )
            return

        if not value:
            console.print(
                f"[key]Auto-compact at[/key] {Glyph.ARROW} "
                f"{settings.compact_at_tokens:,} tokens "
                f"[muted](/compact-at 200k · 0 = fraction only)[/muted]"
            )
            console.print(
                f"[key]Compact policy[/key] {Glyph.ARROW} {settings.compact_policy} "
                f"[muted]{Glyph.DOT} floor {settings.compact_floor_tokens:,} tokens "
                f"(/compact-at economic|fixed · /compact-at floor 64k)[/muted]"
            )
            console.print(
                f"[key]Auto-compact ceiling[/key] {Glyph.ARROW} "
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
        from . import bbs as bbs_mod
        from . import memory_notes

        active = project_instructions.active_file()
        mem = memory_notes.status_line()
        board = bbs_mod.status_line()
        extra = f"memory {mem} {Glyph.DOT} bbs {board}"
        if active is None:
            return (
                f"none [muted]{Glyph.DOT} add {project_instructions.FULL_NAME} "
                f"for standing project instructions {Glyph.DOT} {extra}[/muted]"
            )
        freeze = "frozen for this process; file edits apply next process"
        if active.name == project_instructions.COMPACT_NAME:
            return (
                f"{active.name} [muted]{Glyph.DOT} {freeze} "
                f"{Glyph.DOT} {extra} "
                f"{Glyph.DOT} /g023-compact to refresh from "
                f"{project_instructions.FULL_NAME}[/muted]"
            )
        return (
            f"{active.name} [muted]{Glyph.DOT} {freeze} "
            f"{Glyph.DOT} {extra} "
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
                    f"[warn]{Glyph.BULLET} unknown item type[/warn] [key]{escape(str(name))}[/key] "
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
            path = save_config(full=True)
            console.print(f"[ok]{Glyph.CHECK} Settings saved {Glyph.ARROW}[/ok] [muted]{ui.display_path(path)}[/muted]")
            return
        if sub == "reset":
            if not confirm("Restore the built-in defaults?", default=False):
                return
            defaults = Settings()
            for key in PERSISTED_KEYS:
                setattr(settings, key, getattr(defaults, key))
            path = save_config()
            console.print(f"[ok]{Glyph.CHECK} Defaults restored[/ok] [muted]{ui.display_path(path)}[/muted]")
            return
        if sub == "set" or sub.startswith("set "):
            parts = arg.strip().split(maxsplit=2)
            if len(parts) < 3:
                console.print("[muted]Usage: /settings set KEY VALUE  (a ·saved key; see /settings)[/muted]")
                return
            key, text = parts[1].strip().lower(), parts[2]
            value, why = parse_setting(key, text)
            if why:
                console.print(f"[warn]Not set:[/warn] {why}")
                return
            setattr(settings, key, value)
            path = save_config()
            console.print(
                f"[ok]{Glyph.CHECK} {key} {Glyph.ARROW} {value}[/ok] [muted]·saved to {ui.display_path(path)}[/muted]"
            )
            return
        if sub:
            console.print("[muted]Usage: /settings [save|reset|set KEY VALUE][/muted]")
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

        row("model", MODEL, "fixed · /model")
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
        row("compact_at_tokens", f"{settings.compact_at_tokens:,}", "auto-compact trigger · /compact-at 200k")
        row("compact_policy", settings.compact_policy, "economic | fixed · /compact-at economic")
        row("compact_floor_tokens", f"{settings.compact_floor_tokens:,}", "economic floor · /compact-at floor 64k")
        row("subagents_enabled", settings.subagents_enabled, "Agent tool + LLM ReadFile focus · /subagents")
        row("ownership", settings.ownership, "who may tidy which files · /ownership")
        row(
            "max_iterations",
            describe_steps(settings.max_iterations),
            "tool steps per turn · /steps",
        )
        row("exploration_ledger", settings.exploration_ledger, "carry files/searches seen across compaction")
        row("write_backup", settings.write_backup, "copy a file before Edit/WriteFile changes it")
        row("write_backup_keep", settings.write_backup_keep, "backups kept per file")
        row("verify_replay", settings.verify_replay, "re-run the check that last failed, after an edit")
        row("failure_attribution", settings.failure_attribution, "label check failures new or pre-existing")
        row("finish_on_success", settings.finish_on_success, "end the turn on a passing final step")
        row("finish_licensed", settings.finish_licensed, "finish despite pre-existing, unrelated test failures (⚠ noted)")
        row("claimed_write_guard", settings.claimed_write_guard, "hold an answer that claims a change to a file no call wrote")
        row("answer_escrow", settings.answer_escrow, "keep a held-back answer; the model replies [deliver] to send it")
        row("closure_receipts", settings.closure_receipts, "Edit lists references left to removed names")
        row("lean_codec", settings.lean_codec, "raw Bash output, no backup paths in history")
        row("lean_prompt", settings.lean_prompt, "answers skip what the trace shows")
        row("orient_card", settings.orient_card, "locate names the request mentions")
        row("scope_count", settings.scope_count, "scoped searches count hits elsewhere")
        row("search_tiers", settings.search_tiers, "search ranks definitions named by the query first")
        row("def_peek", settings.def_peek, "search shows the top definition's body")
        row("view_deltas", settings.view_deltas, "Bash shows diffs of files it changed that were read")
        row("test_oracle", settings.test_oracle, "pytest runs report hidden failures, new tests, failure loci")
        row("vacuity_check", settings.vacuity_check, "flag new tests that pass without the turn's source edits")
        row("obligations", settings.obligations, "check callables the request names before answering")
        row("write_guards", settings.write_guards, "\"don't change X\" in a request guards writes to X")
        row("callee_digest", settings.callee_digest, "ReadFile lists signatures of imported project names")
        row("claim_check", settings.claim_check, "note when an answer's test claim contradicts the last run")
        row("ordered_batches", settings.ordered_batches, "write + its check in one response; failed write skips the rest")
        row("preread", settings.preread, "run the reads the request makes predictable before the first call")
        row("step_budget", settings.step_budget, "model declares [budget MIN-MAX +OVER]; hop tracker on results")
        row("write_check", settings.write_check, "Edit/WriteFile carry a check command run after the response's writes")
        row("multi_edit", settings.multi_edit, "Edit takes more_edits: several hunks, across files, all or nothing")
        row("rename_tool", settings.rename_tool, "Rename tool: an identifier across the project in one call")
        row("max_search_matches", settings.max_search_matches)
        row("file_summary_max_tokens", settings.file_summary_max_tokens)
        row("permission_default", settings.permission_default, "/tools")
        row("vision_enabled", settings.vision_enabled, "AnalyzeImage · /vision on|off")
        row("vision_timeout", f"{settings.vision_timeout}s")
        row("surge_notify", settings.surge_notify, "/surge notify on|off")
        row("surge_pause", settings.surge_pause, "/surge on|off")
        row(
            "surge_pause_buffer_minutes",
            f"{settings.surge_pause_buffer_minutes} min",
            "pause starts/ends around peak · /surge buffer",
        )
        row("base_url", settings.base_url)
        row("project_root", ui.display_path(settings.project_root))
        row("home", ui.display_path(settings.home))
        project_cfg = get_project_config_file()
        cfg_line = ui.display_path(get_config_file())
        if project_cfg.exists():
            cfg_line += f" + project override {ui.display_path(project_cfg)}"
        row("config_file", cfg_line)

        console.print()
        console.print(panel(table, "Settings", subtitle="·saved keys persist to config.json"))
        console.print(
            "[muted]/settings set KEY VALUE to change a ·saved key · /settings save to persist now · "
            "/settings reset for defaults · "
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
        p = PRICING[MODEL]
        parts = [
            f"{MODEL}: ${p.cache_hit}/1M hit {Glyph.DOT} ${p.cache_miss}/1M miss "
            f"{Glyph.DOT} ${p.output}/1M out (surge {p.surge_output}/1M, 2×)"
        ]
        return (
            "[muted]"
            + "\n".join(parts)
            + "\nWebSearch / FetchUrl (DeepSeek Search and Fetch APIs): $0.75 per 1,000 successful requests each "
            + "(failed and empty requests are free)."
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
                console.print(f"[warn]Unknown tool:[/warn] {escape(name)}  [muted](/tools lists them)[/muted]")
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
            note = tool_note(name, level)
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
            return f"{human_bytes(db.stat().st_size)} [muted]{ui.display_path(db)}[/muted]"
        except OSError:
            return "[muted]empty[/muted]"

    def cmd_cache(self, arg: str = "") -> None:
        from . import web
        from .usage import FETCH_REQUEST_USD, SEARCH_REQUEST_USD

        sub = " ".join(arg.strip().lower().split())

        if sub in ("", "stats", "status"):
            stats = get_cache().stats()
            store = web.get_store()
            ws = store.stats()
            led = store.ledger()
            srch = led.get("search", {})
            ftch = led.get("fetch", {})
            served_free = srch.get("cached", 0) + ftch.get("cached", 0) + ftch.get("primed", 0)
            saved = srch.get("cached", 0) * SEARCH_REQUEST_USD + (
                ftch.get("cached", 0) + ftch.get("primed", 0)
            ) * FETCH_REQUEST_USD
            table = kv_table(
                [
                    ("file summaries", f"{stats['files']:,}"),
                    ("vision answers", f"{stats['vision']:,}"),
                    ("tool vault", f"{stats.get('vault', 0):,}"),
                    ("project database", self._cache_size_line()),
                    ("web searches", f"{ws['searches']:,} [muted](machine-wide)[/muted]"),
                    ("web pages", f"{ws['pages']:,} ({human_bytes(ws['page_chars'])} text) · {ws['refusals']} refusals"),
                    (
                        "web requests",
                        f"search {srch.get('network', 0)} paid / {srch.get('cached', 0)} cached · "
                        f"fetch {ftch.get('network', 0)} paid / {ftch.get('cached', 0)} cached / "
                        f"{ftch.get('primed', 0)} from search prefetch",
                    ),
                    ("web fees saved", f"${saved:.4f} [muted]({served_free} requests served free)[/muted]"),
                    ("web database", f"{human_bytes(ws['bytes'])} [muted]{ui.display_path(store.path)}[/muted]"),
                ]
            )
            console.print()
            console.print(panel(table, "Cache"))
            console.print("[muted]/cache web to list · /cache web clear · /cache clear purges this project[/muted]")
            return

        if sub == "clear":
            get_cache().clear_all()
            console.print(f"[ok]{Glyph.CHECK} Project caches purged (the working set and the machine-wide web cache are kept).[/ok]")
            return
        if sub == "web":
            self.show_web_cache()
            return
        if sub in ("web clear", "clear web"):
            n = web.get_store().clear()
            console.print(f"[ok]{Glyph.CHECK} Machine-wide web cache purged ({n} entries).[/ok]")
            return
        if sub == "web prune":
            n = web.get_store().prune()
            console.print(f"[ok]{Glyph.CHECK} Dropped {n} web entries unused for 90 days.[/ok]")
            return
        console.print("[muted]Usage: /cache [stats|clear|web|web clear|web prune][/muted]")

    def show_web_cache(self):
        from . import web

        store = web.get_store()
        searches = store.list_searches(15)
        pages = store.list_pages(25)
        if not searches and not pages:
            console.print("[muted]Nothing in the web cache yet.[/muted]")
            return
        if searches:
            table = Table(show_header=True, header_style="heading", box=None, padding=(0, 2, 0, 0))
            table.add_column("Searched", style="muted")
            table.add_column("Hits", justify="right")
            table.add_column("Results", justify="right")
            table.add_column("Query", overflow="fold")
            for e in searches:
                table.add_row(_describe_age(time.time() - e["fetched_at"]), str(e["hits"]), str(e["n"]), e["query"][:90])
            console.print()
            console.print(panel(table, "Cached searches (machine-wide)"))
        if pages:
            table = Table(show_header=True, header_style="heading", box=None, padding=(0, 2, 0, 0))
            table.add_column("Fetched", style="muted")
            table.add_column("St", justify="right")
            table.add_column("Size", justify="right")
            table.add_column("Hits", justify="right")
            table.add_column("From")
            table.add_column("URL", overflow="fold")
            for e in pages:
                table.add_row(
                    _describe_age(time.time() - e["fetched_at"]),
                    str(e["status"]),
                    human_bytes(e["chars"]),
                    str(e["hits"]),
                    e["source"],
                    e["url"][:90],
                )
            console.print()
            console.print(panel(table, "Cached pages (machine-wide)"))
        console.print("[muted]/cache web clear purges these for every project.[/muted]")

    # ------------------------------------------------------------------
    # /fetch, /search
    # ------------------------------------------------------------------

    def cmd_fetch(self, arg: str = "") -> None:
        from . import web

        url = (arg.split() or [""])[0]
        if not url or url.lower() in ("status", "info"):
            store = web.get_store()
            console.print(
                f"[muted]FetchUrl uses DeepSeek's Fetch API; pages are cached machine-wide in {ui.display_path(store.path)}.[/muted]"
            )
            console.print("[muted]Usage: /fetch <url>[/muted]")
            return
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        args = {"url": url, "cache_mode": "auto"}
        if not get_registry().confirm_fetch(args):
            console.print("[warn]Cancelled.[/warn]")
            return
        try:
            with console.status(f"[accent]Fetching[/accent] {url}", spinner="dots"):
                page = web.fetch(url, mode=args["cache_mode"])
        except web.WebError as e:
            console.print(f"[bad]Fetch failed:[/bad] {e}")
            return
        source = page.source if page.source == "network" else f"{page.source} ({_describe_age(page.age)})"
        if page.cut:
            source += f" {Glyph.DOT} cut at 20 KB by the web service; the rest of the page is missing"
        elif page.note:
            source += f" {Glyph.DOT} {escape(page.note)}"
        console.print()
        console.print(
            panel(
                f"[heading]{page.title or page.url}[/heading]\n"
                f"[muted]{source} {Glyph.DOT} {human_bytes(len(page.body))}[/muted]\n\n" + page.body[:3000],
                page.url[:70],
            )
        )

    def cmd_search(self, arg: str = "") -> None:
        from . import web

        text = arg.strip()
        fresh = False
        if text.lower().startswith("fresh "):
            fresh, text = True, text[6:].strip()
        if not text:
            console.print("[muted]Usage: /search [fresh] <query>[/muted]")
            return
        try:
            with console.status(f"[accent]Searching[/accent] {text}", spinner="dots"):
                out = web.search(text, 10, fresh=fresh)
        except web.WebError as e:
            console.print(f"[bad]Search failed:[/bad] {e}")
            return
        console.print()
        console.print(panel(web.render_search(out, 10), f"search {Glyph.DOT} {out.source}"))

    # ------------------------------------------------------------------
    # /vision
    # ------------------------------------------------------------------

    def vision_status(self) -> str:
        if not settings.vision_enabled:
            return "[muted]off[/muted]"
        return f"[ok]on[/ok] {Glyph.DOT} {MODEL} [muted](800 px cap)[/muted]"

    def _set_vision(self, enabled: bool) -> None:
        settings.vision_enabled = enabled
        path = save_config()
        word = "[ok]on[/ok]" if enabled else "[warn]off[/warn]"
        console.print(f"Vision {Glyph.ARROW} {word} [muted]·saved to {ui.display_path(path)}[/muted]")
        if enabled:
            console.print("[muted]AnalyzeImage is offered from the next request.[/muted]")

    def cmd_vision(self, arg: str = "") -> None:
        low = arg.lower().strip()
        if low in ("on", "enable", "enabled", "1", "true", "yes"):
            self._set_vision(True)
            return
        if low in ("off", "none", "disable", "disabled", "0", "false", "no"):
            self._set_vision(False)
            return
        if low in ("status", "show"):
            console.print(f"Vision: {self.vision_status()}")
            return
        if low:
            console.print(f"[warn]Unknown option:[/warn] {escape(arg)}  [muted](on | off | status)[/muted]")
            return
        console.print(f"Vision: {self.vision_status()}")
        flip = not settings.vision_enabled
        if confirm(f"Turn vision {'on' if flip else 'off'}?", default=False):
            self._set_vision(flip)

    # ------------------------------------------------------------------
    # /local
    # ------------------------------------------------------------------

    def local_status(self) -> str:
        from . import local_llm

        st = local_llm.status()
        auto = st["mode"] == "auto"
        if not st["enabled"]:
            if auto:
                return f"[muted]auto {Glyph.ARROW} off: {escape(st['auto'])}[/muted]"
            return "[muted]off[/muted]"
        label = f"auto {Glyph.ARROW} on" if auto else "on"
        if not st["binary"]:
            return f"[warn]{label}, llama-server not found[/warn] [muted](local_llama_dir)[/muted]"
        if not st["model"]:
            return f"[warn]{label}, model file not found[/warn] [muted](local_models_dir / local_model_file)[/muted]"
        state = f"running :{st['port']} pid {st['pid']}" if st["running"] else "starts on first use"
        name = Path(st["model"]).name + (" + vision" if st["mmproj"] else "")
        return f"[ok]{label}[/ok] {Glyph.DOT} {escape(name)} {Glyph.DOT} {state}"

    def cmd_local(self, arg: str = "") -> None:
        from . import local_llm

        parts = arg.split(maxsplit=1)
        sub = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""
        if sub in ("on", "off", "auto"):
            settings.local_model = sub
            if not settings.local_enabled:
                local_llm.release()
            path = save_config()
            console.print(f"Local model {Glyph.ARROW} {self.local_status()} [muted]·saved to {ui.display_path(path)}[/muted]")
            return
        if sub == "unload":
            stopped = local_llm.release()
            console.print("Local server stopped." if stopped else "[muted]No local server was running.[/muted]")
            return
        if sub == "models":
            names = local_llm.list_models()
            console.print("\n".join(names) if names else f"[muted]no .gguf files in {escape(settings.local_models_dir)}[/muted]")
            return
        if sub in ("model", "vision"):
            if not rest:
                console.print(f"[warn]Usage:[/warn] /local {sub} NAME")
                return
            if sub == "vision" and rest.lower() in ("off", "none"):
                settings.local_vision_file = "off"
            elif rest.lower() == "auto":
                setattr(settings, "local_model_file" if sub == "model" else "local_vision_file", "")
            else:
                if local_llm.resolve_model(rest) is None:
                    console.print(f"[bad]Not found:[/bad] {escape(rest)} [muted](see /local models)[/muted]")
                    return
                setattr(settings, "local_model_file" if sub == "model" else "local_vision_file", rest)
            local_llm.release()  # next use loads the new model
            path = save_config()
            console.print(f"Local {sub} {Glyph.ARROW} {escape(rest)} [muted]·saved to {ui.display_path(path)}[/muted]")
            return
        if sub == "scout":
            if rest.lower() not in ("local", "always", "off"):
                console.print("[warn]Usage:[/warn] /local scout local | always | off")
                return
            settings.scout_card = rest.lower()
            path = save_config()
            console.print(f"Scout card {Glyph.ARROW} {settings.scout_card} [muted]·saved to {ui.display_path(path)}[/muted]")
            return
        if sub == "tune":
            from . import autotune

            prof = autotune.refresh()
            gpu = f"{prof.gpu.name} ({prof.gpu.total_mib:,} MiB)" if prof.gpu.kind else "none found"
            console.print(
                f"Auto-tune: {'[ok]usable[/ok]' if prof.usable else '[warn]not usable[/warn]'}"
                + (f" [muted]{escape(prof.reason)}[/muted]" if prof.reason else "")
                + f"\n[muted]binary {escape(str(prof.binary or '-'))}\nmodels {escape(str(prof.models_dir or '-'))}"
                + f"\nmodel  {escape(str(prof.model or '-'))}"
                + (f" ({prof.picked_by})" if prof.picked_by else "")
                + f"\nvision {escape(str(prof.mmproj)) if prof.mmproj else 'DeepSeek'}\ngpu    {escape(gpu)}"
                + f"\nmode   {settings.local_model} (local_model; /local auto|on|off)[/muted]"
            )
            return
        if sub not in ("status", "show", ""):
            console.print(f"[warn]Unknown option:[/warn] {escape(arg)}  [muted](status | auto | on | off | tune | unload | models | model NAME | vision NAME | scout MODE)[/muted]")
            return
        st = local_llm.status()
        from . import scout

        scout_line = ""
        for kind, row in sorted(scout.ledger().items()):
            recent = row.get("recent") or []
            scout_line += (f"\n  {kind} cards: {row.get('used', 0)}/{row.get('cards', 0)} used"
                           + (f", last {len(recent)}: {sum(recent)}" if recent else "")
                           + ("" if scout.trusted(kind) else " (muted: mostly ignored)"))
        console.print(f"Local model: {self.local_status()}")
        console.print(
            f"[muted]binary {escape(st['binary'] or '-')}\nmodel  {escape(st['model'] or '-')}"
            + (f"\nmmproj {escape(st['mmproj'])}" if st["mmproj"] else "")
            + (f"\nlast error: {escape(st['last_error'])}" if st["last_error"] else "")
            + f"\nsalient vault excerpts: {'on' if settings.salience_excerpt else 'off'}"
            + f"\nscout card: {settings.scout_card}{scout_line}[/muted]"
        )

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
            surge.clock_range(s, e) for s, e in surge.upcoming_peak_windows(now)
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
    # /config — global vs per-project
    # ------------------------------------------------------------------

    def cmd_config(self, arg: str = "") -> None:
        sub = arg.strip().lower()

        if sub in ("global", "save-global", "save global"):
            path = save_config(scope="global")
            console.print(
                f"[ok]{Glyph.CHECK} Current settings saved as the global default[/ok] "
                f"[muted]{Glyph.ARROW} {ui.display_path(path)}[/muted]"
            )
            console.print("[muted]New projects inherit these settings unless they carry their own .g023/config.json.[/muted]")
            return
        if sub in ("project", "save-project", "save project"):
            path = save_config(scope="project")
            console.print(
                f"[ok]{Glyph.CHECK} Settings pinned to this project[/ok] [muted]{Glyph.ARROW} {ui.display_path(path)}[/muted]"
            )
            return
        if sub == "save":
            path = save_config(full=True)
            scope = "project" if path == get_project_config_file() else "global"
            console.print(
                f"[ok]{Glyph.CHECK} Settings saved ({scope})[/ok] [muted]{Glyph.ARROW} {ui.display_path(path)}[/muted]"
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
                f"{ui.display_path(get_config_file())} now applies[/muted]"
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
                ui.display_path(global_path) + (" [ok]· exists[/ok]" if global_path.exists() else " [muted]· missing[/muted]"),
            ),
            (
                "project",
                ui.display_path(project_path)
                + (" [ok]· overrides global[/ok]" if project_path.exists() else " [muted]· none — global applies[/muted]"),
            ),
            ("model", MODEL),
            ("vision", "on" if settings.vision_enabled else "off"),
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

        dial_snapshot = settings.snapshot(
            ("reasoning_effort", "thinking_enabled", "max_iterations")
        )
        # A goal is the one command whose whole point is "however many steps it
        # takes". The step cap is the backstop against a turn that tools forever
        # without answering; here it is the thing that would cut the job short,
        # so the turn runs uncapped and the loop's progress line plus Ctrl-C are
        # what keep it watched. The saved /steps setting is untouched — this is
        # for the duration of this turn only, and is not written to config.
        # Effort is the harness ceiling (75 / high), not the API's max: max
        # burns the output budget on thinking. Release once, before the turn,
        # so a session pinned at a lower alias is not stuck there. The turn
        # re-pins the ceiling and warms this transcript at that alias.
        # A same-response [next_effort] is stored for when the prefix is
        # already new; only an omitted trailer restores the pre-goal dial.
        # Transient overrides: a save_config() during the goal (any slash
        # command) writes the pre-goal values, never these.
        settings.set_transient("reasoning_effort", MAX_EFFORT)
        settings.set_transient("thinking_enabled", True)
        settings.set_transient("max_iterations", UNLIMITED_ITERATIONS)
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
        self._goal_interrupted = False
        try:
            await self.run_prompt(self._goal_prompt(arg), title=f"g023 {Glyph.DOT} goal")
            await self._continue_goal_until_done_or_stalled()
        finally:
            if not getattr(self.orch, "_next_effort_applied", False):
                settings.restore(dial_snapshot, ("reasoning_effort", "thinking_enabled"))
            settings.restore(dial_snapshot, ("max_iterations",))
            # Do not release the pin here. The goal turn re-pinned the ceiling
            # and that transcript is the prefix the next request should hit.
            # high→low on it misses from token 0 (instructions, tools, and
            # the transcript). Post-turn compact releases the pin only after
            # it replaces the input list. /thinking and /clear release it
            # because the user asked for a new prefix. A stored [next_effort]
            # is what those later requests send.
            self._emit_goal_judgment()

    _GAME_GOAL = re.compile(
        r"\b(game|games|canvas|arcade|pong|snake|breakout|platformer)\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _goal_prompt(objective: str) -> str:
        # This text is the user message, the tail of the request. Standing
        # instructions and tools stay the cached prefix. V4.1 Flash decode
        # is the expensive side (output ~200× a cache hit), so the contract
        # is short and forbids extra rounds: no web, learn, or board unless
        # something actually failed.
        text = (
            "High-level goal (use tools freely, break into steps):\n\n"
            f"{objective}\n\n"
            "Finish this objective before you stop. Record the remaining steps "
            "with TodoWrite, do those steps, and do not cancel them to make the "
            "list look empty. Mark a leaf done only with evidence that cites a "
            "file this session wrote or a verify command that already exited 0. "
            "If a test fails, fix it and re-run that same command until it exits 0. "
            "A different command passing does not clear a failed one.\n"
            "Do not add files, docs, or features the objective did not ask for. "
            "A polish idea is not an open issue. An open issue is a failing "
            "command, a broken path, or a contradiction with the objective.\n"
            "Do not search the web, the learn store, or the bulletin board unless "
            "a tool failed or the objective depends on a current external fact."
        )
        if CLI._GAME_GOAL.search(objective or ""):
            text += (
                "\nThis is a playable game. Ship the smallest artifact that plays: "
                "prefer one HTML file with a canvas and an inline script. "
                "Then prove it. Extract each inline script and run node --check "
                "on that script, and run one node smoke command that exits 1 "
                "unless the file contains a canvas and a game loop. Fix until "
                "that same command exits 0. Do not stop at a title screen."
            )
        return text

    @staticmethod
    def _goal_continuation_prompt(judgment: dict, n: int) -> str:
        pending = ", ".join(judgment.get("pending") or []) or "(none)"
        missing = ", ".join(judgment.get("done_without_evidence") or []) or "(none)"
        reason = str(judgment.get("reason") or "not completed")
        return (
            f"Goal continuation {n}/{GOAL_CONTINUATION_CAP}. "
            f"The objective is not completed: {reason}. "
            f"Pending ids: {pending}. Done without evidence: {missing}. "
            "Finish only those leaves. Do not add scope and do not cancel "
            "leaves to look finished. Use tools. If a verify command failed, "
            "fix the code and re-run that same command, then mark the leaf "
            "done with that evidence."
        )

    async def _continue_goal_until_done_or_stalled(self) -> None:
        """Extra turns only while the judge says the goal is still open.

        One stalled continuation (working set unchanged) stops the loop.
        A second turn is allowed only when the first one moved the working
        set and the cap has not been hit. Output tokens are the expensive
        side of the bill; repeating the same unfinished answer is not.
        """
        for n in range(1, GOAL_CONTINUATION_CAP + 1):
            if self._goal_interrupted or self.orch.turn_failed:
                return
            try:
                judgment = judge_goal_completion()
            except Exception:
                return
            if judgment.get("completed"):
                return
            before = dumps_working_set(load_working_set())
            reason = str(judgment.get("reason") or "")
            console.print(
                f"[muted]Goal still open ({escape(reason)}) — "
                f"continuation {n}/{GOAL_CONTINUATION_CAP}.[/muted]"
            )
            # A continuation is not the user's next request. An omitted
            # trailer on it must not throw away a [next_effort] the goal
            # answer already stored — run_turn clears that flag on entry.
            applied_before = bool(self.orch._next_effort_applied)
            # A [next_effort] on the goal answer is stored for when the
            # prefix is new. The continuation is still the goal. If compact
            # already released the pin, sending that lower dial would think
            # less on the unfinished work and would miss the new prefix the
            # continuation is about to warm. Keep the stored dial; force the
            # ceiling only for this call. The call re-pins the ceiling when
            # the pin was clear.
            dial = settings.snapshot(("reasoning_effort", "thinking_enabled"))
            settings.set_transient("reasoning_effort", MAX_EFFORT)
            settings.set_transient("thinking_enabled", True)
            try:
                await self.run_prompt(
                    self._goal_continuation_prompt(judgment, n),
                    title=f"g023 {Glyph.DOT} goal",
                )
            finally:
                # In finally: a cancel of the /goal scope itself (not caught by
                # run_prompt) must not leave the forced ceiling as the live dial.
                if not self.orch._next_effort_applied:
                    settings.restore(dial)
                    if applied_before:
                        self.orch._next_effort_applied = True
            if self._goal_interrupted or self.orch.turn_failed:
                return
            after = dumps_working_set(load_working_set())
            if after == before:
                console.print(
                    "[muted]No working-set change on that continuation — "
                    "stopping so this does not repeat.[/muted]"
                )
                return

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
            # A child task Ctrl-C can cancel: run_turn rolls the turn back in
            # its CancelledError path, then UserInterrupt lands here.
            result = await interrupt_controller.run(self.orch.run_turn(text))
        except (UserInterrupt, KeyboardInterrupt):
            self._goal_interrupted = True
            committed = getattr(self.orch, "committed_answer", None)
            if committed is not None:
                # Ctrl-C landed after the commit point (post-turn compaction):
                # the turn is kept, so its answer is still shown.
                if not self.orch.answer_was_streamed:
                    console.print()
                    console.print(panel(Markdown(committed or "_(no answer)_"), title))
                console.print("\n[warn]Interrupted after the answer; the turn is kept (compaction skipped).[/warn]")
                return
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
        installed = interrupt_controller.install()
        try:
            await self._loop()
        finally:
            if installed:
                interrupt_controller.uninstall()
            await _close_api_client()

    async def _loop(self):
        self.print_banner()
        try:
            load_api_key()
        except Exception as e:
            console.print(f"[bad]{e}[/bad]")
            return

        if settings.local_enabled:
            # Load the local model while the user types, so the first turn's
            # scout card and LocalAsk do not miss it (never blocks; fails open).
            try:
                from . import local_llm

                await local_llm.ensure(wait=False)
            except Exception:
                pass

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
                        await interrupt_controller.run(asyncio.sleep(5))
                        continue
                    except (UserInterrupt, KeyboardInterrupt):
                        console.print(
                            "\n[warn]Bypassing surge pause — commands will run at peak prices.[/warn]"
                        )
                        self._surge_pause_announced = False
                        self._surge_bypass = True
            self._surge_bypass = False

            try:
                console.print()
                user = await interrupt_controller.run(self.reader.ask_async())
            except (UserInterrupt, KeyboardInterrupt, EOFError):
                console.print("\n[muted]Bye.[/muted]")
                break

            line = user.strip()
            if not line:
                continue

            if line.startswith("/"):
                try:
                    await interrupt_controller.run(self.handle_slash(line))
                except (UserInterrupt, KeyboardInterrupt):
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
    console.print("A known slash command, such as /goal, runs that command and exits.")
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


async def _close_api_client() -> None:
    """Close the shared HTTP client on the loop that owns it."""
    try:
        from .api import close_client

        await close_client()
    except Exception:
        pass


async def _run_oneshot(cli: "CLI", prompt: str) -> None:
    installed = interrupt_controller.install()
    try:
        await _run_oneshot_body(cli, prompt)
    finally:
        if installed:
            interrupt_controller.uninstall()
        await _close_api_client()


async def _run_oneshot_body(cli: "CLI", prompt: str) -> None:
    """One prompt, or one known slash command.

    ``python -m g023_code /goal …`` used to send the slash line to the model
    as user text. The step cap stayed on, the working-set judge never ran,
    and an unattended project build could not finish. A token the catalogue
    knows is dispatched. Anything else, including an unknown ``/…``, stays
    a prompt.
    """
    try:
        load_api_key()
    except Exception as e:
        console.print(f"[bad]{e}[/bad]")
        return
    stripped = (prompt or "").strip()
    if stripped.startswith("/"):
        token = stripped.split(maxsplit=1)[0]
        if registry.lookup(token) is not None:
            try:
                await interrupt_controller.run(cli.handle_slash(stripped))
            except UserInterrupt:
                console.print("\n[warn]Cancelled.[/warn]")
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
        console.print("\n[muted]Bye.[/muted]")
    return 0

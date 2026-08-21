# `ui.py` — the terminal UI layer

`g023_code/ui.py` is the single place pixels get decided. Everything
rendered goes through here: a shared rich `console`, glyphs, panels,
tables, gauges, and the activity printer the orchestrator narrates
through. `_unicode_ok()` detects a non-Unicode terminal and `Glyph`
falls back to ASCII.

## Primitives

- `Glyph` — the symbol set (arrows, dots, spinners) with ASCII fallbacks.
- `ToolStyle` / `tool_style(name)` — per-tool colours.
- `human_bytes(n)` / `human_ms(ms)` — sizes and durations for humans.
- `truncate(text, limit)`, `shorten_path(path, root)`, `short_url(url)` —
  the three common shortenings (paths are shown relative to the project
  root, URLs reduced to scheme+host+path tail).
- `gauge(fraction, width, warn, danger)` — the context bar; colour
  shifts at `warn`/`danger` fractions.
- `sparkline(values)` — tiny trend line.
- `kv_table(rows)` / `panel(renderable, title)` — the standard dashboard
  building blocks.

## Tool narration

- `describe_call(name, args, root)` — one human line for a tool call
  (e.g. `ReadFile → docs/foo.md (summary)`); knows each tool's argument
  shapes so it can pick what's worth showing.
- `describe_result(name, result)` — the one-line outcome preview after a
  tool runs (per-verbosity).
- `ToolEvent` / `TurnTrace` — per-turn bookkeeping: events, tool counts
  (`tool_count`, `tools_by_name`, `slowest`), thinking/iteration timing.
- `ActivityPrinter` — the live narration surface the orchestrator drives:
  `tool_start` (spinner + one line), `tool_end` (result + duration),
  `tool_denied`, `step_notice` (persistent progress line every N steps),
  `iteration` (per-iteration token/cost line at high verbosity), `note`.

## Whole-screen renderers

- `trace_summary(trace)` — the end-of-turn trace panel.
- `status_bar()` — the status line (context gauge + cost) shown after
  each turn.
- `banner()` — the startup title/version banner.

## Related

- `docs/orchestrator.md` — the consumer of `ActivityPrinter`/`TurnTrace`.
- `docs/cli.md` — the slash commands that compose `kv_table`/`panel`.
- `docs/prompt.md` — shares the same `console`.

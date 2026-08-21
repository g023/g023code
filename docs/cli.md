# `cli.py` — the CLI: REPL, slash commands, dashboard

`g023_code/cli.py` (~1,950 lines) is the user-facing shell. It owns the
`CLI` class: startup banner, the main REPL loop, every slash-command
handler, and the per-turn orchestration call. All rendering goes through
`ui.py`; all input through `prompt.py`.

## Startup (`CLI.__init__`, `print_banner`, `loop`)

- `__init__` builds the Orchestrator, the tool registry accessor, the
  command catalogue, the reader (from `prompt.py`), and the ActivityPrinter.
- `loop()` (the async main loop):
  1. `print_banner()` — title/version banner (see `ui.banner`).
  2. `load_api_key()` — dies early with a clear message if `K.dat` is
     missing or a placeholder. (OpenRouter key is only loaded on demand.)
  3. Runs a surge monitor (`_surge_tick`): prints a one-time panel when
     peak pricing starts/stops if `surge_notify` is on.
  4. Surge pause: if `surge_pause` is on and the next peak (plus buffer) is
     coming, the loop sleeps in 5 s slices, printing a "usage paused" panel
     once; Ctrl-C bypasses the pause for the rest of the session
     (`_surge_bypass`).
  5. Reads a line. `/`-prefixed lines go to `handle_slash`; everything else
     goes to `run_prompt`. Ctrl-C/EOF prints "Bye." and exits.
- `main()` is the sync wrapper: builds a `CLI` and `asyncio.run`s `loop`.

## Slash dispatch (`handle_slash`)

`handle_slash` splits the line, resolves the command name via
`commands.lookup`, then calls `getattr(self, cmd.handler)` with parsed
args. Unknown commands get a "did you mean" line from `commands.suggest`.
Errors inside a handler print `Command failed: …` (with a traceback when
`show_thinking` is on) and the REPL survives.

## The 23 handlers, by group

**Session** — `cmd_help` (list / one-command detail), `cmd_status`
(one-screen dashboard: model, context, cost, vision, cache, surge),
`cmd_clear` (reset conversation + usage counters), `cmd_exit`.

**Model** — `cmd_model` (choose orchestrator model; only `flash` today),
`cmd_thinking` (reasoning effort low/high/max/off), `cmd_verbose`
(low/mid/high detail level).

**Context** — `cmd_steps` (`/steps N|off|default` — per-turn step cap),
`cmd_max_tokens` (`/max-tokens N|off` — per-reply output budget, 0 = send
none), `cmd_compact` (manual compaction + what was dropped),
`cmd_context` (context gauge, token breakdown), `cmd_cost` (per-model
cost + totals), `cmd_signals` (drift-signal log and hit-rate verdict),
`cmd_settings` / `show_settings` (all knobs in one panel + `usage_table`
+ `pricing_note`), `cmd_tools` (registry policy and `_cache_size_line`),
`cmd_cache` (cache stats + `show_web_cache` entries).

**Tools** — `cmd_fetch` (manual web fetch, shows the cached result and
engine fidelity), `cmd_ollama` (status / `set-host` / `models` / `test` /
`ps`), `cmd_vision` (backend none|ollama|deepseek|openrouter, endpoint,
model, downscale, ctx, keep-alive, timeout; `_enable_vision` /
`_disable_vision`), `cmd_surge` (status / on / off / buffer / notify /
`now`), `cmd_or` (OpenRouter status, `models`, credits tier).

**Config** — `cmd_config` (`save`, `save global|project`, `global`,
`clear`; `_config_status` shows both files and which is active).

**Work** — `cmd_goal` (`/goal <objective>` — one turn with reasoning
forced to `max`, step cap lifted, and a prompt asking the model to break
the objective into tool-using steps; restores the usual settings after).

Each handler's help text, choices, aliases, and examples live in
`commands.py` (see `commands.md`); the handlers here implement the
behaviour. `interactive` commands (model, thinking, verbose) run a
`choose`-style picker when no argument is given.

## Running a prompt (`run_prompt`, `_surge_tick`)

`run_prompt(text)` calls `orch.run_turn(text)`:

- KeyboardInterrupt → "Interrupted."; other exceptions → `Error: …`.
- If the answer was **not** streamed, prints it in a panel.
- `show_answer_detail` → prints `orch.turn_summary()` (tokens/cost line).
- `show_context_bar` → prints `orch.status_line()` (context gauge).

## How to extend

1. Add the command's metadata to `COMMANDS` in `commands.py` (name,
   aliases, group, args, help, `handler="cmd_xxx"`).
2. Add `cmd_xxx` as a method here.
3. `tests/` and this doc both get a refresh (see `READ_1ST_CHANGES.txt`).

## Related

- `docs/commands.md` — the catalogue data this file consumes.
- `docs/orchestrator.md` — `run_turn` and everything it does.
- `docs/ui.md`, `docs/prompt.md`, `docs/surge.md` — the surfaces it drives.

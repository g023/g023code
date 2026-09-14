# `cli.py` — the CLI: REPL, slash commands, dashboard

`g023_code/cli.py` (~2,670 lines) is the user-facing shell. It owns the
`CLI` class: startup banner, the main REPL loop, every slash-command
handler, and the per-turn orchestration call. All rendering goes through
`ui.py`; all input through `prompt.py`.

`CLI.__init__` calls `commands.check_handlers(self)` and raises if a
catalogue entry has no matching `cmd_*` method.

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
- `main(argv=None)` parses argv (`parse_cli_argv`): `--help`/`-h` prints
  `print_flag_help()` (program name + slash-command catalogue) and
  returns 0 without building a Reader; `--version`/`-V` prints
  `g023 Code <version>`; `--continue` (`-c`) seeds a new orchestration from
  the latest `.g023/logs/` handoff (else `/resume` JSONL) then continues;
  remaining tokens are one prompt (`_run_oneshot` → `run_prompt`) then
  exit; no tokens start the REPL (`CLI.loop()`). Launchers already forward
  `"$@"` / `%*` to `python -m g023_code`.

The toolbar shows the live effort integer (`think 75`), context fraction,
session cost, vision, and `SURGE 2×` / `or-sub` badges.

## Slash dispatch (`handle_slash`)

`handle_slash` splits the line, resolves the command name via
`commands.lookup`, then calls `getattr(self, cmd.handler)` with parsed
args. Unknown commands get a "did you mean" line from `commands.suggest`.
Errors inside a handler print `Command failed: …` (with a traceback when
`show_thinking` is on) and the REPL survives.

## The handlers, by group

**Session** — `cmd_help` (list / one-command detail), `cmd_status`
(one-screen dashboard: model, context, cost, vision, or-sub, cache,
surge, which `g023.md`/`g023_compact.md` is loaded, memory-notes line),
`cmd_clear` (reset conversation + usage counters + working set),
`cmd_resume` (latest JSONL view; `/resume log` seeds from `.g023/logs/`;
warns on prefix-fingerprint mismatch), `cmd_learn` (status / search /
get / save / forget / outcome — see `learn.md`), `cmd_undo` (restore
newest `.g023/backups/` copy; bare `/undo` uses the last working-set
file pointer), `cmd_exit`, `cmd_config`.

**Model** — `cmd_model` (choose orchestrator model; only `deepseek-flash`
today; `flash` and `deepseek-v4-flash` aliases; `pro` is refused with
the UNAVAILABLE_MODELS reason), `cmd_thinking` (0–100 or
low/high/max/off; 75 is the ceiling; picker when bare; saved), `cmd_verbose`
(low/mid/high), `cmd_steps`, `cmd_max_tokens`.

**Context** — `cmd_compact` (manual compaction + what was dropped),
`cmd_g023_compact` (compact `g023.md` → `g023_compact.md` with Flash, no
reasoning; leaves the original),
`cmd_max_ctx`, `cmd_compact_threshold`,
`cmd_context` (context gauge, token breakdown), `cmd_cost` (per-model
cost + totals), `cmd_signals` (drift-signal log and hit-rate verdict),
`cmd_settings` / `show_settings` (all knobs in one panel + `usage_table`
+ `pricing_note`).

**Tools** — `cmd_tools` (registry policy and `_cache_size_line`),
`cmd_fetch` (manual web fetch, shows the cached result and
engine fidelity), `cmd_cache` (cache stats + `show_web_cache` entries).

**Vision** — `cmd_ollama` (status / `set-host` / `models` / `test` /
`ps`), `cmd_vision` (backend none|ollama|deepseek|openrouter, endpoint,
model, downscale, ctx, keep-alive, timeout; `_enable_vision` /
`_disable_vision`), `cmd_surge` (status / on / off / buffer / notify / `now` —
current instant, same panel as status), `cmd_or` (OpenRouter status,
`models`, credits tier),
`cmd_orsub` (OpenRouter subagent offload: enable, primary, add/remove
models, priorities, reasoning, live `test`; `_orsub_status` shows the
model table and today's usage).

**Work** — `cmd_goal` (`/goal <objective>` — one turn with reasoning
forced to 75 (the harness ceiling), step cap lifted, and a prompt asking
the model to break the objective into tool-using steps; seeds
`working_set.goal`; keeps a same-response `[next_effort]` for the next
request, otherwise restores the usual settings after).

Each handler's help text, choices, aliases, and examples live in
`commands.py` (see `commands.md`); the handlers here implement the
behaviour. `interactive` commands (model, thinking, verbose, vision, or,
orsub, config) run a `choose`-style picker when no argument is given.

## Running a prompt (`run_prompt`, `_surge_tick`)

`run_prompt(text)` calls `orch.run_turn(text)`:

- KeyboardInterrupt → "Interrupted."; other exceptions → `Error: …`.
- If the answer was **not** streamed, prints it in a panel.
- `show_answer_detail` → prints `orch.turn_summary()` (tokens/cost line).
- `show_context_bar` → prints `orch.status_line()` (context gauge).

The orchestrator, not the CLI, writes `.g023/prompts/{n}.txt`.

## How to extend

1. Add the command's metadata to `COMMANDS` in `commands.py` (name,
   aliases, group, args, help, `handler="cmd_xxx"`).
2. Add `cmd_xxx` as a method here.
3. `tests/` and this doc both get a refresh.

## Related

- `docs/commands.md` — the catalogue data this file consumes.
- `docs/orchestrator.md` — `run_turn` and everything it does.
- `docs/project_instructions.md` — `/g023-compact` and the `/status` line.
- `docs/learn.md` — `/learn`.
- `docs/ui.md`, `docs/prompt.md`, `docs/surge.md` — the surfaces it drives.

# `prompt.py` — user input handling

`g023_code/prompt.py` centralises every way the program
asks the human something: the main input line, yes/no questions, and
option pickers.

## `Reader`

The input line used by the REPL.

- If `prompt_toolkit` is installed **and** stdin is a TTY, builds a
  `PromptSession` with:
  - a live status toolbar (`_toolbar` — context/cost line from `ui.status_bar`),
  - history (`.g023/history`),
  - tab completion via `SlashCompleter` (defined here when prompt_toolkit
    is present): completes `/command` names then first-argument choices,
    using `commands.completions`,
  - key bindings (`make_key_bindings`): Esc-Enter inserts a newline;
    **Ctrl+Up** replaces the line with `prompt_store.last_prompt()`
    (the last `.g023/prompts/{n}.txt`). Up-arrow FileHistory is unchanged.
- Otherwise degrades to rich's `Prompt.ask` — a plain line, no
  completion. `enhanced` reports which mode is active.
- `ask(label)` sync / `ask_async(label)` async — the REPL calls
  `ask_async`. KeyboardInterrupt/EOF propagate so `cli.loop` can exit
  cleanly.

## Helpers

- `confirm(question, default=False)` — yes/no via rich `Confirm.ask`;
  cancellation (Ctrl-C/EOF) is treated as "no" (with a "Cancelled."
  notice), never a crash.
- `choose(title, options, default=1, allow_cancel=True)` — numbered
  picker; returns the 1-based index of the choice, or `None` on cancel
  (0 = cancel option). Used by the interactive slash commands
  (`/model`, `/thinking`, `/verbose`, `/vision`, …).
- `install_hint()` — one-time "install prompt_toolkit for completions"
  suggestion printed when the reader is running in degraded mode.

## Related

- `docs/cli.md` — the REPL loop that owns the `Reader`.
- `docs/commands.md` — `completions()` source of completion candidates.
- `docs/prompt_store.md` — the files Ctrl+Up reads.
- `docs/ui.md` — the shared console and status bar.

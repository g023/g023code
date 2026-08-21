# `commands.py` — the slash-command catalogue

`g023_code/commands.py` is pure data plus lookup helpers. It holds the
**single source of truth** for every slash command: names, aliases,
grouping, arguments, help text, and which CLI method handles it. The CLI
handlers themselves live in `cli.py`; `prompt.py` uses this module for
completion; `check_handlers` keeps the two in sync.

## Data model

- `Argument(name, choices=(), help="")` — frozen dataclass. `render()`
  formats it for help output; `choices` feeds tab completion and the
  interactive pickers.
- `Command(name, aliases=(), summary, handler, group, args=(), detail="",
  examples=(), interactive=False, required_args=...)` — frozen dataclass.
  - `names` property = `(name, *aliases)`.
  - `first_arg_choices()` = choices of the first argument (for completion).

## The catalogue (`COMMANDS`)

23 commands in 6 groups (`GROUP_ORDER = Session, Model, Context, Tools,
Vision, Work`):

| Group | Commands |
|-------|----------|
| Session | `/help` (`/?` `/h` `/commands`), `/status` (`/dash`), `/clear` (`/reset`), `/exit` (`/quit` `/q`) |
| Model | `/model`, `/thinking` (`/effort`), `/verbose` (`/v`) |
| Context | `/steps` (`/iterations` `/maxsteps`), `/max-tokens`, `/compact`, `/context`, `/cost`, `/signals`, `/settings`, `/tools`, `/cache` |
| Tools | `/fetch`, `/ollama`, `/vision`, `/surge`, `/or` |
| Config | `/config` |
| Work | `/goal` |

Argument-choice constants at the top of the file:
`LEVEL_CHOICES` (low/mid/high), `EFFORT_CHOICES` (low/high/max/off),
`MODEL_CHOICES` (flash), `PERMISSION_CHOICES` (allow/ask/block),
`BACKEND_CHOICES` (none/ollama/deepseek/openrouter),
`ENDPOINT_CHOICES` (responses/chat_completions), `OR_TIER_CHOICES`
(free/credits), `SURGE_SUB_CHOICES` (status/on/off/buffer/notify/now).

Each command's `detail` string is the user-facing help — it documents
semantics (e.g. `/model` explains that `deepseek-v4-pro` is withheld
because the Responses API does not serve it yet; `/steps` explains a step
= one orchestrator call; `/config` documents the machine-vs-project scope
split).

## Lookup & helpers

- `lookup(token)` → resolve a typed token to a `Command`, tolerating a
  missing leading slash (case-insensitive).
- `suggest(token, limit=3)` → "did you mean" candidates: prefix matches
  first, then `difflib` close matches; aliases are collapsed onto their
  canonical command.
- `completions(text)` → candidates for the current input line:
  completes the command itself (`/th` → `/thinking`), then the first
  argument's choices (`/thinking ` → low/high/max/off).
- `by_group()` → commands bucketed in reading order for `/help`.
- `check_handlers(obj)` → names of commands whose `handler` method is
  missing from `obj` — a sanity check that the catalogue and the CLI stay
  in sync (used by the test suite).

## Related

- `docs/cli.md` — the handlers that implement these commands.
- `docs/prompt.md` — `SlashCompleter` uses `completions()`.
- `tests/test_config_scopes.py` etc. assert catalogue-driven behaviour.

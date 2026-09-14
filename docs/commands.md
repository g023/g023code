# `commands.py` — the slash-command catalogue

`g023_code/commands.py` is pure data plus lookup helpers. It holds the
**single source of truth** for every slash command: names, aliases,
grouping, arguments, help text, and which CLI method handles it. The CLI
handlers themselves live in `cli.py`; `prompt.py` uses this module for
completion; `check_handlers` keeps the two in sync.

## Data model

- `Argument(name, choices=(), description="", required=False)` — frozen
  dataclass. `render()` formats it for help output; `choices` feeds tab
  completion and the interactive pickers.
- `Command(name, aliases=(), summary, handler, group, args=(), detail="",
  examples=(), interactive=False)` — frozen dataclass.
  - `names` property = `(name, *aliases)`.
  - `first_arg_choices()` = choices of the first argument (for completion).

## The catalogue (`COMMANDS`)

30 commands in 6 groups (`GROUP_ORDER = Session, Model, Context, Tools,
Vision, Work`). `/config` is in Session.

| Group | Commands |
|-------|----------|
| Session | `/help` (`/?` `/h` `/commands`), `/status` (`/dash`), `/clear` (`/reset`), `/resume` (`/continue`; `/resume log` for a `.g023/logs/` handoff), `/learn`, `/undo` (`/restore`), `/exit` (`/quit` `/q`), `/config` (`/cfg`) |
| Model | `/model`, `/thinking` (`/effort`), `/verbose` (`/v`), `/steps` (`/iterations` `/maxsteps`), `/max-tokens` (`/maxtokens` `/output`) |
| Context | `/compact`, `/g023-compact` (`/g023compact`), `/max-ctx` (`/max-context` `/ctx`), `/compact-threshold` (`/compact-at`), `/context`, `/cost` (`/usage`), `/signals` (`/drift`), `/settings` |
| Tools | `/tools` (`/permissions` `/perm`), `/fetch`, `/cache` |
| Vision | `/ollama`, `/vision` (`/config_vision`), `/surge`, `/or`, `/orsub` (`/ors` `/or-sub`) |
| Work | `/goal` |

Argument-choice constants at the top of the file:
`LEVEL_CHOICES` (low/mid/high), `EFFORT_CHOICES` (low/high/max/off),
`MODEL_CHOICES` (flash), `PERMISSION_CHOICES` (allow/ask/block),
`BACKEND_CHOICES` (none/ollama/deepseek/openrouter),
`ENDPOINT_CHOICES` (responses/chat_completions), `OR_TIER_CHOICES`
(free/credits), `OR_SUB_CHOICES` (status/on/off/primary/add/remove/models/
priority/reasoning/test), `SURGE_SUB_CHOICES` (status/on/off/buffer/notify/now),
`LEARN_SUB_CHOICES` (search/get/save/forget/outcome).

`/surge now` is the current-instant panel (same body as status).
`/config status` is the scope dashboard (bare `/config` is the same).
`/vision` first-arg choices: off/status/backend/endpoint.
`/learn` with no argument prints index counts and remaining write budget.

Each command's `detail` string is the user-facing help — it documents
semantics (e.g. `/model` explains that `deepseek-v4-pro` is withheld
because the Responses API does not serve it yet, and that the sent id is
`deepseek-flash`; `/steps` explains a step = one orchestrator call;
`/config` documents the machine-vs-project scope split; `/g023-compact`
documents `g023.md` as this program's AGENTS.md; `/compact` documents
that a same-response `[handoff]` is reused instead of a second summarizer
call (then consumed so a later pass is not stale), while a focused
`/compact <topic>` still asks Flash; `/thinking`
documents the 0–100 scale with a 75 ceiling and `[next_effort: N]` on a
final answer (not a tool-call step; N should match the next request, not
copy 75; values above 75 cap);
`/learn` documents that guides are not in the system prompt).

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
  in sync (used at CLI startup and by the test suite).

## Related

- `docs/cli.md` — the handlers that implement these commands.
- `docs/prompt.md` — `SlashCompleter` uses `completions()`.
- `docs/project_instructions.md` — `/g023-compact` and the `g023.md` load order.
- `docs/learn.md` — `/learn`.
- `tests/test_config_scopes.py`, `tests/test_disconnect.py` — catalogue-driven behaviour.

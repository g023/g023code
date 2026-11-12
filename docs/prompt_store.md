# `prompt_store.py` — incrementing user-prompt files

`g023_code/prompt_store.py` persists each user prompt as
`.g023/prompts/{n}.txt` (1-based, next index = max existing + 1). A
small pure store: next index, write, read last. The REPL binds Ctrl+Up
to `last_prompt()`; it does not own numbering or I/O.

`Orchestrator.run_turn` calls `save_prompt(user_input)` at the start of
a turn. `/clear` does not delete the folder.

With `prompt_toolkit`, Ctrl+Up replaces the input line with the last
stored request. Up-arrow `FileHistory` (`.g023/history`) is unchanged.

## Related

- `docs/prompt.md` — the Ctrl+Up binding.
- `docs/orchestrator.md` — who writes the files.
- `tests/test_goal_improvements.py` — prompt-store identity.

`save_prompt` claims the number with O_EXCL (`fsutil.create_next_numbered`),
so two REPLs in one project never overwrite each other's prompt file.

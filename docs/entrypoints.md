# Entry points & launchers

Four small files decide how the program starts. None of them contain logic;
they resolve the environment and hand control to `cli.main()`.

## `g023_code/__main__.py`

The `python -m g023_code` entry point. Imports `cli.main` and calls it.
Nothing else.

## `g023_code/__init__.py`

Package metadata only:

- `__version__ = "1.3.3"` — the version shown in the banner and by the CLI.
- `__title__ = "g023 Code"`.

It deliberately does **not** reassign `__name__` (a comment in the file
explains that doing so breaks every `from g023_code import <submodule>`).

## `g023.sh` (POSIX / macOS / WSL launcher)

1. Resolves the directory the script lives in.
2. Exports `G023_HOME` (installation dir — where `K.dat`/`config.json`
   live), `G023_PROJECT_ROOT` (the `pwd` at launch — the project the agent
   operates on), and `PYTHONPATH` (so `g023_code` is importable).
3. Picks an interpreter: bundled `.venv/bin/python` if present, else
   `python3`, else `python`.
4. `exec`s `python -m g023_code "$@"`.

## `g023.bat` (Windows launcher)

Same contract, batch-file flavour:

1. Sets `SCRIPT_DIR`, `G023_HOME`, `G023_PROJECT_ROOT`, `PYTHONPATH`.
2. Runs `python -m g023_code %*` with the most reliable interpreter found:
   `.venv\Scripts\python.exe` first, then `python`, then `py -3`.
3. If none exist, prints "Python 3 is required… Run installer.bat first."
   and exits 1.

## Environment variables that matter at startup

Set by the launchers (override at your own risk):

| Variable | Meaning | Read by |
|----------|---------|---------|
| `G023_HOME` | installation dir; holds `K.dat`, `OK.dat`, machine `config.json`, `ds_*.json` | `config.get_home()` |
| `G023_PROJECT_ROOT` | the working project; its `.g023/` holds `cache.db` and the project `config.json` | `config.get_project_root()` |

Read directly from the environment by `config.py` (see `config.md`):
`G023_READFILE_RAW`, `G023_WRITE_BACKUP`, `G023_WRITE_BACKUP_KEEP`, and
(through `ollama_client`) `OLLAMA_HOST`.

## Related

- `docs/installers.md` — how `g023.sh`/`g023.bat` get created and how the
  `.venv` is provisioned.
- `docs/cli.md` — what `cli.main()` actually runs.

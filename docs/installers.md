# Installers & dependency tiers

`installer.sh` (POSIX/macOS/WSL) and `installer.bat` (Windows) take a bare
machine to a working g023 installation. Both are idempotent — re-running
repairs an incomplete setup.

## What an install does

1. **Python check** — needs 3.11+ (shell installer enforces
   `MIN_MAJOR=3, MIN_MINOR=11`; handles Debian's externally-managed
   environment marker).
2. **Virtualenv** — creates `.venv/` next to the program (or offers to
   use the current interpreter).
3. **Dependencies** — installed in three tiers from `requirements.txt`
   (see below), each with a fallback "skip and degrade" behaviour.
4. **API key** — prompts for the DeepSeek key and writes `K.dat`; when
   skipped, leaves the placeholder that `config.load_api_key()` recognises
   and turns into a clear instruction.
5. **Config** — writes a default machine-level `config.json`.
6. **Launcher** — creates a `g023` shim in `~/.local/bin` (POSIX;
   `installer.bat` sets up the `.bat` launcher), so `g023` works from any
   directory. `G023_HOME`/`G023_PROJECT_ROOT` are resolved by the
   launchers at runtime (`entrypoints.md`).
7. Uninstall option, non-interactive mode, and a self-repairing re-run.

## `requirements.txt` — the dependency tiers

```
Required           rich, httpx, tiktoken, pygments   → won't start without
Recommended        prompt_toolkit                    → input degrades to a plain prompt
Optional (vision)  pillow                            → downscale; avoids out-of-VRAM
Optional (FetchUrl) curl_cffi, h2, brotli, zstandard → each makes fetches look more
                                                       like a real browser; without
                                                       them /fetch status reports
                                                       what's missing and httpx fallback
                                                       is used
```

The installer mirrors these tiers (`REQUIRED`/`RECOMMENDED`/`OPTIONAL`
lists), so a user can decline the heavy bits and still get a working
program.

## Related

- `docs/entrypoints.md` — what the launchers created here do at runtime.
- `docs/data_files.md` — `K.dat`, `config.json` written by installers.
- `docs/web_fetch.md` — how the FetchUrl optional deps are used.

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
5. **Config** — writes a default machine-level `config.json` if none
   exists. It is generated, not hand-kept: both installers run
   `config.write_default_config()`, which writes every `PERSISTED_KEYS` entry
   at its built-in `Settings` default (1M window, compaction at 200k with the
   `economic` policy, effort 75, verbose `low`, `permission_default: ask`, …)
   and ignores `G023_*` switches in the installing shell. (Until 1.5.0 the
   template was a literal that had drifted to a 256k window and lacked six
   keys; `tests/test_release.py` now pins the generator.) It names no model
   (the model is a constant) and no provider. An existing valid
   `config.json` is left untouched; keys older versions wrote (model,
   `api_format`, Ollama/OpenRouter vision, `or_*`) are ignored on load and
   dropped on the next save.
6. **Launcher** — creates a `g023` shim in `~/.local/bin` (POSIX;
   `installer.bat` sets up the `.bat` launcher), so `g023` works from any
   directory. `G023_HOME`/`G023_PROJECT_ROOT` are resolved by the
   launchers at runtime (`entrypoints.md`).
7. Uninstall option, non-interactive mode, and a self-repairing re-run.

## `requirements.txt` — the dependency tiers

```
Required           rich, httpx, pygments             → won't start without
Recommended        prompt_toolkit                    → input degrades to a plain prompt
Optional (vision)  pillow                            → downscale; avoids out-of-VRAM
```

The installer mirrors these tiers (`REQUIRED`/`RECOMMENDED`/`OPTIONAL`
lists), so a user can decline the heavy bits and still get a working
program. The context gauge is last API `input_tokens` /
`settings.max_context_tokens`; there is no local tokenizer.

## Related

- `docs/entrypoints.md` — what the launchers created here do at runtime.
- `docs/data_files.md` — `K.dat`, `config.json` written by installers.
- `docs/web.md` — web search/fetch need nothing beyond httpx.

# `tests/` — what the suite pins

pytest suite. Each file's docstring states, in plain English, the
behaviour it exists to protect — read the docstrings first. All tests run
offline: `conftest.py` stubs the API client so nothing makes network
calls.

## `conftest.py`

- `pytest_configure` / `pytest_pyfunc_call` — async-test support.
- `isolated_project(tmp_path, monkeypatch)` — fixture that points
  `G023_HOME`/`G023_PROJECT_ROOT` at a temp dir and resets the
  module-level singletons (cache, usage, signals, settings) between tests.
- `CountingClient` — a stub `ResponsesClient` that counts `create()` calls
  and returns canned responses; powers the call-accounting tests.
- `message(...)` / `response(...)` — builders for Responses-shaped items.

## Per-file

| File | Protects |
|------|----------|
| `test_call_accounting.py` | The published API-call table (SearchContent = 0 calls, small-Python ReadFile = 0, cache hit = 0, range read = 0, first big/focused read = 1) — the checkable core of the delegation argument. |
| `test_config_scopes.py` | Machine `config.json` vs project `.g023/config.json` override order; `/config global` promotion; coercion of bad values. |
| `test_drift_signals.py` | The three drift signals fire on the shapes that matter and **stay quiet** on unusual-but-normal shapes. |
| `test_file_reader.py` | Symbol map, range reads as ground truth, raw reads, caps, and cache behaviour. |
| `test_history_integrity.py` | `function_call` ⇄ `function_call_output` pairing after `_rollback_to_last_complete` and `_repair_pairing` — a dangling pair kills the whole session. |
| `test_openrouter_vision.py` | Model failover on HTTP 429, daily-quota tracking, and the tier-aware final error. |
| `test_output_limit.py` | `max_tokens` semantics: default sends **no** budget, configured budgets pass through unchanged, cut-off notice names the limit that applied, config can't set what the CLI refuses. |
| `test_step_limit.py` | Per-turn step cap: honouring `max_iterations`, the off switch, and the "Stopped after N" path. |
| `test_surge.py` | Beijing-anchored windows, surge on/off arithmetic, pause-buffer widening, always reported in local time. |
| `test_usage_accounting.py` | Price arithmetic: rates × right token counts, hit/miss split derived the same way from either endpoint's field naming, no invented cache hits. |
| `test_write_backup.py` | Every overwrite snapshots `.g023/backups/` (mirrored tree, pruned to the newest few); skipped for `.g023/` paths, new files, unchanged content. |

## Running

```
python -m pytest tests/ -q        # from the project root
```

## Related

- `docs/overview.md` — the design claims these tests make checkable.
- `docs/orchestrator.md`, `docs/surge.md`, `docs/usage.md`, etc. — the
  behaviours each file pins.

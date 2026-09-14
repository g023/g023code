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
| `test_call_accounting.py` | The published API-call table (SearchContent = 0 calls, unfocused ReadFile = 0, cache hit = 0, range read = 0, semantic focused read = 1) — the checkable core of the delegation argument. Exact-symbol focus = 0 lives in `test_goal_cost_wins.py`. |
| `test_config_scopes.py` | Machine `config.json` vs project `.g023/config.json` override order; `/config global` promotion; coercion of bad values. |
| `test_context_window.py` | `/max-ctx` and `/compact-threshold` bounds, persistence, and the auto-compact trigger. |
| `test_drift_signals.py` | The three drift signals fire on the shapes that matter and **stay quiet** on unusual-but-normal shapes. |
| `test_file_reader.py` | Symbol map, range reads as ground truth, raw reads, caps, and cache behaviour. |
| `test_goal_cost_wins.py` | Remaining cost leaks: a local AST ReadFile summary > 6000 chars stays parseable JSON in history (no mid-JSON slice); bulky Agent JSON is not sliced; nested Agent vaults Bash/ListDir bodies; exact/unique-suffix symbol `focus` is 0 Flash calls; `end_line`-only range reads are spared from micro-compact; temperature is pinned with the standing prefix. |
| `test_goal_improvements.py` | Goal-path identity (`deepseek-flash`, installer templates, old-config coerce, prompt store, A_Improve/AGENTS.md, bidirectional Agent relay) and post-turn compact: a same-response `[handoff]` skips Flash; an unclosed handoff still summarises; large leftover history still summarises when no handoff exists; short chat, small tool turns, and think-heavy short chats (reasoning scratch) do not buy a summarizer call; an existing summary folds locally; a completed objective writes `.g023/logs/`. |
| `test_goal_judge.py` | Working-set completed vs not-completed: pending leaves, done-without-evidence, empty list, file/verify evidence, `path truncatedhash` citations, `node test/headless.js` as a verify command. |
| `test_reasoning.py` | Effort 0–100 ↔ wire aliases, capped at 75 / `high` (never `xhigh`/`max`); `[next_effort: N]` and `[handoff]...[/handoff]` on the same reply are stripped from the visible answer and from history; next-effort is stored but the wire stays pinned on a hot prefix; `/thinking` / `/goal` / `/clear` / post-turn compact release the pin so the stored dial is sent; a trailer on a tool-call step does not change this turn's remaining steps; `/goal` uses the 75 ceiling and honours a same-response next-effort instead of restoring the pre-goal dial; the standing prompt uses `[next_effort: N]`, not a copy-me 75, and names 75 as the ceiling. |
| `test_session_compact.py` | Layer-1/3 compact shape (sticky goal, pairing-intact tail); auto Layer 1 does **not** rewrite a cached prefix below window pressure; at threshold, tool-heavy histories still micro first; Layer 3 reuses a same-response handoff (no Flash) unless `/compact <focus>` asks for a topic-specific summary; a used handoff is consumed so a later pass is not stale; summarizer `max_output_tokens` is capped. |
| `test_history_integrity.py` | `function_call` ⇄ `function_call_output` pairing after `_rollback_to_last_complete` and `_repair_pairing` — a dangling pair kills the whole session. `_reseat_items_splitting_tool_pairs` moves a user/working-set row out of an open parallel batch. |
| `test_parallel_tools.py` | ALLOW read-only calls overlap; same-path writes stay serial; the first parallel WriteFile batch keeps outputs contiguous (no working-set user item between them). |
| `test_openrouter_vision.py` | Model failover on HTTP 429, daily-quota tracking, and the tier-aware final error. |
| `test_or_subagents.py` | OR_SUB_MODELS.json management (main vs project, priorities, primary), failover across hop-able statuses, reasoning clamping, output-budget clamping, daily usage. |
| `test_output_limit.py` | `max_tokens` semantics: default sends **no** budget, configured budgets pass through unchanged, cut-off notice names the limit that applied, a no-tool cut-off auto-continues the turn (capped), config can't set what the CLI refuses. |
| `test_project_instructions.py` | `g023.md` / `g023_compact.md` load order, `AGENTS.md` addendum, `/g023-compact` writes the compact file with Flash and reasoning off and leaves the original. |
| `test_step_limit.py` | Per-turn step cap: honouring `max_iterations`, the off switch, and the "Stopped after N" path. |
| `test_surge.py` | Beijing-anchored windows, Monday–Friday peak days, weekend off-peak, pause-buffer widening, always reported in local time. |
| `test_usage_accounting.py` | Price arithmetic: rates × right token counts, hit/miss split derived the same way from either endpoint's field naming, no invented cache hits. |
| `test_cost_efficiency.py` | Cache-aware policy: rewriting a cached prefix does not recoup; post-turn compact keeps a hot prefix and still fires on uncached leftover / completed objectives; tool vault is append-only; Agent without a prefix keeps readonly tools; Agent with a shared prefix reuses parent instructions+tools+effort and still refuses writes. |
| `test_write_backup.py` | Every overwrite snapshots `.g023/backups/` (mirrored tree, pruned to the newest few); skipped for `.g023/` paths, new files, unchanged content. |
| `test_disconnect.py` | Advertised catalogue choices are handler branches; `/surge now` and `/config status` run; FileReader sends `settings.file_summary_max_tokens`; Agent isolated loop executes ListDir, keeps parent `state.items` clean, honours DeepSeek `reasoning` (unset todos=`medium`, else `high`; `medium` is a real wire alias); `main(["--help"])` prints g023 and a slash command and exits 0; `--continue` is parsed. |
| `test_session_log.py` | JSONL `/resume` keeps pairing and the working-set item; objective handoff `.g023/logs/` round-trips and `/resume log` seeds a new orchestration. |
| `test_learn.py` | Learn store curator, tools, prefix pin (put does not change `instructions` or tools), cross-project env visibility, secret/auth reject, root confine. |
| `test_prefix_stable.py` | `/responses` body prefix (instructions + tools) stays byte-identical across turns until vision changes. |
| `test_root_confine.py` | ReadFile, ListDir, and SearchContent refuse paths outside the project root. |
| `test_search.py` | SearchContent total-vs-samples, gitignore, glob/brace find, grep fallback. |
| `test_vision_resize.py` | Constrained 800px resize on every vision send path; `vision_max_image_dim` does not override it. |

## Running

```
python -m pytest tests/ -q        # from the project root
```

421 tests as of 1.3.4. No plugins beyond `pytest` — async tests run
through a small hook in `tests/conftest.py`, and every test points
`G023_PROJECT_ROOT` at a temporary directory, so a run never reads
your real cache and never touches the network.

What these do not cover: anything requiring the real API, end-to-end
session cost, or whether a summary was good enough for the question asked.

## Related

- `docs/overview.md` — the design claims these tests make checkable.
- `docs/orchestrator.md`, `docs/surge.md`, `docs/usage.md`, etc. — the
  behaviours each file pins.

# Changelog

All notable changes to g023 Code. Dates are release dates (UTC).

## 1.5.0 — 2026-11-11

The themes are **cost per solved task** — what a turn re-reads, what a tool
result carries, and how many hops a fix takes — **checks on what the model
claims** (test results it filtered away, a function the request named, "all
tests pass"), and **reliability**: what happens on Ctrl-C, on a crash
mid-write, on a dropped connection, and when two g023 processes share a
project. Techniques, evidence and prior art are summarised in the README's
[For researchers](README.md#for-researchers) section.

### Added

- **Opt-in round-trip experiments** (`hops.py`): `ordered_batches` (prompt
  states that calls run in program order; a failed Edit/WriteFile skips the
  later Bash calls and same-file writes of that response with a typed
  `skipped` result), `write_check` (Edit/WriteFile take an optional `check`
  command, run once after the response's writes only if all succeeded; its
  result rides on the write and counts as the turn's test run). Both
  measured neutral live, so they are off.
- **Optional local model via llama.cpp** (`local_llm.py`, `local_ask.py`,
  docs/local_model.md, `/local`, `G023_LOCAL=1`). `LocalAsk`
  reads files on the local model and returns verbatim, anchored evidence lines;
  AnalyzeImage can answer locally first; local rerank of excerpt lines. One
  shared server per machine, idle sleep, GPU guard, and the local model steps
  aside on GPU-work commands; the prompt tells DeepSeek not to use it for GPU work.
  A model whose vocabulary cannot build llama.cpp's JSON grammar sampler is
  asked again without the grammar (schema in the prompt), remembered per server.
- **Local model auto-tune** (`autotune.py`, `/local tune`, `/local auto`).
  `local_model` is now `auto` | `on` | `off` and defaults to `auto`: the local
  llama.cpp helper turns itself on in every project when `llama-server`, a
  model and a GPU it fits on are found, and picks the model the scout eval
  measured best (LFM3-VL-2B Q8: text cards as good as LFM3-1.7B, ~1.1 s, plus
  local-first vision from the same server; 4B models scored no higher at 3x the
  latency). Empty `local_model_file` / `local_vision_file` mean "pick
  automatically"; `local_vision_file: off` keeps vision on DeepSeek.
  Projectors named `<family>-mmproj-<quant>.gguf` are now found for their model
  and are no longer listed or ranked as chat models.
- **Scout card** (`scout.py`, docs/scout.md, `scout_card` / `G023_SCOUT`,
  `/local scout local|always|off`). With the local model on, each request carries
  a `[scout: ...]` block: lines the local model picked from the best-matching
  code units, copied verbatim from the files and grouped by enclosing def, plus
  the next best units. Cards the paid model keeps ignoring mute themselves
  (use-confirmed ledger). `LocalAsk` without `paths` searches the whole project.
  The local server is warmed at startup when the local model is on.
- **Salient long-output excerpts and a readable vault** (`salience.py`,
  docs/salience.md). A cut Bash/ListDir/Glob result keeps the head, up to 14
  numbered lines that match the step's own reasoning, and the tail (decisive
  line shown on 11 of 14 real long outputs, was 3 of 14). The raw output is kept
  in `.g023/vault/<id>.txt` and the excerpt says how to read it back instead of
  re-running the command. `G023_SALIENCE=0` restores the old excerpt.
- **Signed, evidence-bound handoffs** (`checkpoint.py`, docs/checkpoint.md).
  `.g023/logs/` handoffs are signed with HMAC-SHA256 under a machine-local key
  (`<global cache dir>/handoff.key`). `/resume log` and `--continue` report the
  log's provenance (`verified` / `modified` / `foreign` / `unsigned`) and re-hash
  the working set's files; files changed or missing since, and a passing verify
  that no longer describes the disk, reach the model as one `[rehydrate]` note
  and the user as a `rehydrate ·` line. Bare `/resume` runs the same disk check.

- **Ski-rental compaction timing** (`economics.py`, `compact_policy`
  `economic` by default, `compact_floor_tokens` 64k). Auto-compaction also fires
  once re-reading history since the last compaction has cost `fidelity ×` the
  price of compacting; the 200k token trigger stays the hard ceiling.
  `/compact-at fixed` restores the token trigger alone. `/cost` shows rent vs
  compaction price.
- **Tool-result codec** (`tool_codec.py`). Each tool result is encoded once,
  losslessly, before it enters history: echoed arguments and duplicate views
  dropped, search rendered as grep-style hits with `@ where` headers.
- **Just-in-time handoff.** The same-response `[handoff]` summary is requested
  only when compaction is forecast within about two turns (`[handoff due]`),
  or when a TodoWrite closes the last open todo.
- **Client-side web layer** (`web.py`). `WebSearch` and `FetchUrl` on DeepSeek's
  Search and Fetch APIs through one SQLite cache shared by every instance on
  the machine (`<install>/global_cache/web.db`, `G023_GLOBAL_CACHE_DIR`):
  canonical keys, query-aware TTLs, near-duplicate query reuse, search-primed
  pages, bounded page views with BM25 `focus`, a fee ledger. New `/search`,
  `/fetch`, `/cache web [clear|prune]`. DeepSeek returns some pages cut at
  exactly 20,480 bytes with no flag: the cut is detected, one plain-text twin
  (`<url>.md`, raw GitHub) is fetched and kept only if it is the same
  document, and a page that stays cut says `"complete": false`.
- **Long documents and large codebases** (`docindex.py`). Plain-text heading
  induction, outline addressing, phrase / section / BM25 focus, fold-to-budget
  outlines, C/C++ function and Java/C# method spans, and a section or function
  breadcrumb on every search hit.
- **Edit engine** (`edit_engine.py`). Match ladder exact → normalised →
  affine indentation map, `line=N` disambiguation, CRLF / BOM / file mode
  preserved, atomic writes; a missed Edit returns every occurrence or the
  closest region.
- **Verify replay.** After a successful write, the check that failed this turn
  is re-run (Bash on `allow`, under 30 s) and its result attached to the write
  (`G023_VERIFY_REPLAY=0` disables).
- **Counterfactual failure attribution** (`attribution.py`). A check that fails
  after this turn's writes is re-run in a shadow copy with the pre-edit files
  restored; failures are labelled new / pre-existing / fixed by edits. Any
  project change Bash made that has no pre-image disables it for the turn
  (`G023_ATTRIBUTION=0` disables).
- `LICENSE` (MIT) and `CHANGELOG.md`.
- **Test oracle** (`testoracle.py`, `probes/g023_pytest_probe.py`,
  `docs/testoracle.md`, on). A pytest run through Bash writes a report
  outside the project; the result adds only what the visible output lacks:
  failing ids with `@ path:line`, source line and error, a `(new this turn)`
  tag, and new tests that also pass with the turn's source edits reverted
  (vacuity check, setting `vacuity_check`). Commands with their own
  environment (tox, docker, `env -i`, ...) are left alone; a plugin import
  failure re-runs the command without it.
- **Request obligations and write guards** (`obligations.py`,
  `docs/obligations.md`, on). A function signature the request asks for is
  checked with `ast` before the final answer; a missing or misshapen one gets
  one note. "do not change the tests" / "don't modify X" refuses the first
  write to a protected file; an identical repeat goes through. On the
  method-instead-of-function task: 9/9 solved with, 18/25 without.
- **Claim check** (`claims.py`, `docs/claims.md`, on). A final answer that says
  the tests pass when no run after the last write passed gets a note under
  it. Display only.
- **Callee digests** (`callees.py`, `docs/callees.md`, on). ReadFile of a
  Python file lists the signatures of the project names it imports.
- Test oracle, obligations, claim check and callee digests together, against
  all four off, 7 tasks × 5 runs: 35/35 solved both ways, cost neutral; the
  per-feature gains above are within batch-to-batch noise.
- **View deltas** (`views.py`, `docs/views.md`, on by default). A Bash call
  that changes a file the model has read or edited returns that file's current
  text (a diff for files over 2,400 characters). Files the model has seen
  that change between turns, by anything other than its own tool calls, are
  reported at the start of the next turn. Live, 3 edit tasks × 5 runs:
  formatter-rewrite task −24% $ (n=9/arm, two batches); tasks where it never fires are unchanged.
- **Search tiers and definition peek** (opt-in: `search_tiers`, `def_peek`).
  Tiers rank hits by what the line defines; peek appends the body of the
  definition a search names unambiguously. They measured neutral live.
- **Lean turns** (`closure.py`, `orient.py`, `docs/lean.md`). Four features,
  on by default. Edit results report `refs_left`, whole-token counts of the
  removed names and numbers still in the project. Bash output is stored as a
  JSON header plus raw text, and backup paths are dropped from history. The
  standing prompt loses a line that contradicted the batching rule and asks
  for answers that skip what the trace shows. Requests that name code get a
  `[where: ...]` orientation line. Live, 7 task shapes × 8 runs: $/solved
  −26%, output −32%, 56/56 solved both ways. Opt-in `scope_count` adds
  `elsewhere_in_project` to scoped searches; it measured neutral.
- **Smart reads** (`shapes.py`, `dirmap.py`). ReadFile sniffs before reading.
  Binaries get a structural digest (images, archives, SQLite, executables, PDF,
  unknown data). Text over 64 MB is streamed. Long lines are clipped at 2,000
  chars. CSV/TSV, JSON, JSONL and log files get column profiles, shapes and line
  templates with exact rare-line counts. ListDir returns a templated digest
  instead of the first 200 names, and Glob reports a digest over all hits past
  200. Live bench with Bash blocked: 6/9 solved for $0.1824 became 8/9 for
  $0.0055.
- **Glob literal intent**: a directory or extension named in the pattern lifts
  the ignore list for that call (`included_ignored`).
- **Exploration ledger** (`ledger.py`, `exploration_ledger`, default off): an
  opt-in session wiki carried across compaction. A live A/B found no gain over
  the compaction summary, so it is not on by default.
- **Code ownership** (`ownership.py`, `/ownership auto|user|agent`, default
  `auto`). The model merges repeated logic and fixes defects it notices only in
  files g023 created itself (WriteFile on a new path, recorded in
  `.g023/provenance.json`, results marked `"yours":true`). Your files stay
  minimal-change. Live A/B on deepseek-flash: an identity sentence ("this
  codebase is yours") merged 0/12; the explicit directive merged 6/6 for about
  +28% cost. `user` keeps the previous prompt byte-identical.
- **Near-miss names on a zero-hit search** (`near_miss.py`, on; `G023_NEAR_MISS=0`
  disables). When SearchContent finds nothing, it lists up to 5 names the project
  defines that are spelled like the query (`count_words` -> `word_count`,
  `WordCounter`; typos per name part), time- and byte-bounded. Live A/B on a
  lookup task: $0.000334 -> $0.000247 per solved task, 3.67 -> 2.83 calls, 6/6 both.
- **Predicated finish** (`finish.py`, `finish_on_success`, on by default; see Changed). Bash,
  Edit and WriteFile accept an optional `final_answer`; if the step succeeds (exit
  0 and no test-failure summary; edit applied, no syntax error, re-run passing)
  the turn ends with that answer plus a stamp quoting the real result, without an
  answer-only model call. On failure the text is discarded. Live: calls -22%,
  wall time -10%, cost unchanged alone (the answer is still written); with step
  budgets $ −8%.
- **`/settings set KEY VALUE`**: change any saved key, typed and validated like a
  loaded config. `/settings` now also lists `exploration_ledger`, `write_backup`,
  `write_backup_keep`, `verify_replay`, `failure_attribution` and
  `finish_on_success`.

### Changed

- **Cheaper closing calls, on by default** (docs/finish.md, docs/multi_edit.md,
  docs/rename.md). Measured live on held-out prompts, the five together: cost
  per solved task −20 % [90 % CI −30, −9], calls −14 %, wall time −15 %, every
  run solved.
  - *Licensed finish* (`finish_licensed`): an answer on a check whose failures
    all pre-date the turn (failure attribution) is delivered with a ⚠ line
    naming them, unless the request is about those tests or the answer claims
    a clean run.
  - *Answer escrow* (`answer_escrow`): a held-back answer is kept; the model
    can reply `[deliver]` instead of writing it again.
  - *Claimed-write guard* (`claimed_write_guard`): an answer that says a file
    the request names was changed is held when no call this turn wrote it.
  - *Multi-hunk, multi-file Edit* (`multi_edit`): Edit takes `more_edits`;
    an item may name another file; all files are written or none.
  - *Rename tool* (`rename_tool`): `Rename(old, new, path, include)` renames an
    identifier across the project, whole words only, all files or none; asks
    before it runs.
  Each turns off with `G023_<NAME>=0` or `/settings set <key> off`.
- **Pre-emptive reads are on by default** (`preread`, docs/preread.md).
  Before the first model call the harness runs the reads the request makes
  predictable (named files, a named test's subject, a named source's test
  file, the unique definition of a named function, one search for a rename)
  and records them as the model's own tool exchange. Measured live, every run
  solved: cost per solved task −18 % [90 % CI −22, −12], calls −18 %
  (130 runs per arm, including held-out prompts). `G023_PREREAD=off` or
  `/settings set preread off` turns it off; `prose` appends the files to the
  request instead.
- **A requested test run is honoured by predicated finish**: when the request
  asks for the tests to be run, a `final_answer` on a batch that ran nothing
  (e.g. a bare Edit) is held back and the model is told to run them.
- **Step budgets and predicated finish are on by default** (`step_budget`,
  `finish_on_success`; docs/hops.md). The model declares `[budget MIN-MAX
  +OVER]` model calls per step, each batch's last result ends with a `[hop N ·
  step k of L-H · request $X]` tracker (one note on overrun, never a stop), and
  the step that completes a task may carry the final answer. Measured live on 7
  task shapes (n=35 each, all solved): $ −8 %, calls −16 %, wall time −10 %.
  Turn off with `G023_STEP_BUDGET=0` / `G023_FINISH_ON_SUCCESS=0` or
  `/settings set ... off`. Saved config files now carry a `defaults_rev`
  stamp; a `finish_on_success: false` in a file without it (written wholesale
  by an older build, including the first 1.5.0 build) is read as unset, so the
  new default reaches those installs. An explicit off saved by this build
  sticks.

- **Paths under the home directory print as `~`** in the banner, `/status`,
  `/settings`, `/config` and `/cache`, so screenshots and pasted logs do not
  publish the local account name.
- **The installers generate `config.json` from `Settings`**
  (`config.write_default_config()`). The hand-kept templates had drifted to a
  256k window and lacked six keys.
- `installer.bat` and `g023.bat` use CRLF line endings; LF-only batch files can
  make `cmd.exe` miss `call :label` targets.
- Surge-aware billing: each call's tokens are tagged peak or off-peak and
  priced at that rate.
- Unfocused reads of small files (≤ 4,000 chars) return the file verbatim; the
  loop guard keys reads and runs on the write epoch, so test → edit → test is
  not a loop.
- Bash: process-group kill on timeout with partial output returned, stdin
  closed, a `python` → `python3` shim when only `python3` exists, and exit 127
  names an installed alternative.
- The frozen system prompt ends with an environment line (cwd, OS, shell,
  Python) that carries no per-project path, so the prefix cache is shared
  across projects.
- Test-command detection recognises venv paths, `timeout`, `uv`/`poetry`/…
  `run`, env prefixes and JS runners.
- Blocking model calls wake on completion instead of polling.
- `/tools` notes describe what each tool does now: local tools say "no API
  call", `Agent` / `AnalyzeImage` say when they are not offered, and FetchUrl
  no longer claims to prompt when it is set to allow. The launch banner reads
  "Single-Agent by Default", and the Learn tools' icon is `≡` (the old one
  is missing from common monospace fonts).
- The `/thinking` help names the levels actually sent (off / low / high).
- `/surge` lists the same upcoming windows in its "peak (local)" and "pause
  window" rows; the peak row used to keep a window that had already ended.
- `installer.sh` captures the import check in a private temporary file and
  rewrites the shell rc in place on uninstall (mode and symlink kept).
- **`/cache clear` keeps the working set** (`session_facts`): it is session
  state, not a cache. `/clear` still resets it.
- **Config saves merge per key.** A save triggered by changing one setting
  writes only the keys this process changed, so two instances changing
  different settings both keep their change. `/config save` and
  `/settings save` still write every key.
- Per-reply effort hints (`[next_effort]`) and `/goal`'s temporary settings
  are a transient overlay: they are never written to `config.json`.

### Reliability

- **Ctrl-C mid-turn works and keeps the REPL alive.** Since Python 3.11,
  `asyncio.run` delivers Ctrl-C as `CancelledError`, so the old
  `KeyboardInterrupt` handlers never ran (`interrupts.py` now cancels only the
  running work). At a permission prompt Ctrl-C means "no"; three presses within
  2 s force-quit. A cancelled Bash command kills its whole process group, and a
  cancelled model call is cancelled rather than left running and billed.
- **Turns are transactions.** An interrupted or API-failed turn keeps the tool
  calls that completed (real side effects) and withdraws the rest with a note.
  The final answer is the turn's commit point: Ctrl-C during the compaction that
  follows it no longer erases the turn.
- **`/resume` replays exactly what the model saw.** The session log records
  every history rewrite; compaction summaries and prompts are never elided
  (only re-derivable tool outputs and write payloads are), and a log whose last
  line was torn by a crash is repaired before the next record is appended.
- **Tool batches run in program order**: reads overlap, while writes, Bash and
  permission prompts are barriers, so a read after a write sees it.
- **API retries**: bounded, jittered retries on 408/409/429/5xx/529 and
  transport errors, honouring `Retry-After`; a stream cut off mid-reply falls
  back to one blocking call.
- **Crash- and race-safe stores** (`fsutil.py`): config, session pointers,
  objective logs, prompts, learn guides and counters, BBS and memory indexes use
  atomic replace, `O_EXCL` unique names and a cross-process lock (hidden
  `.<name>.lock` sidecar files).
- **`/undo` restores the right version.** Backup names carry nanoseconds
  (same-second writes could restore the wrong version), restores are atomic and
  keep the file mode, the replaced version is kept as `*.undone`, and repeated
  `/undo` steps further back.
- **No silent drops or duplicate side effects.** TodoWrite rejects todos beyond
  the open-leaf cap with their ids instead of dropping them (a goal could look
  complete); identical parallel web searches and fetches pay once; a re-sent
  identical BBS post is not appended twice.

### Fixed

- **`default_config()` picked up `G023_*` switches from the shell.** Only 5 of
  about 20 switches were set aside while building defaults, so e.g.
  `G023_FINISH_ON_SUCCESS` leaked into a fresh `config.json`. Every `G023_*`
  variable is now ignored there.
- **The post-turn local fold dropped every new turn.** It appended the turn after
  the summary's `[recall]` line, which `with_recall_pointer` cuts off. The fold is
  now bounded and cache-ordered: the narrative is kept byte-for-byte, the last 3
  exchanges sit in a ring, older requests leave as at most 12 one-line digests.
  The first fold (no summary yet) is bounded the same way.

- ReadFile line counts no longer count a phantom line after a trailing newline
  (`total_lines`/`loc` were one too high). The cached index version was bumped
  to 3 so older caches rebuild.
- `docs/overview.md` said `max_iterations` defaults to 0 (no cap); it is 255.
- A saved `write_backup_keep` of 0 or a negative `vision_timeout` /
  `surge_pause_buffer_minutes` was accepted; such values now keep the default.
- The repeat guard blocked the third identical run of a script that wrote
  files (`python gen.py`, `make`) when the command matched no write pattern.
  A file change observed after any Bash call now counts as a write.
- With piped stdin, an invalid answer to a confirm or prompt re-asked and ate
  the following input lines. It is now a decline.
- `/thinking on` was rejected; it restores the saved effort. `/max-ctx 200k`
  and `/max-tokens 8k` were rejected as not numbers. `/verbose 1|2|3` work.
- `/clear` then `/resume` resumed the empty new log; it now resumes the newest
  log with history.
- SearchContent with an empty query matched every line; it is now an error.
  ReadFile on a directory said "File not found"; it now says it is a directory.
  A reversed `start_line`/`end_line` range reads the span between them.
- Unknown arguments typed into `/verbose`, `/tools`, `/help` and similar were
  read as console markup (`[red]x` showed as `x`); they now print as typed.
  `/resume` with an unknown argument says so instead of resuming anyway.
- The usage tracker and the web store could each be built twice when first
  used from two threads at once; both are now created under a lock.

### Removed

- The server-side `web_search` tool and the `web_search_max_uses` setting.
  Sessions recorded with it still replay.
- The local browser fetcher (`web_fetch.py`) and its dependencies (`curl_cffi`,
  `h2`, `brotli`, `zstandard`), and the `fetch_backend` setting.
- `apply_unique_edit` (now `edit_engine.apply_edit`).

### Release

- Archives are reproducible (sorted members, fixed timestamps, no owner
  names or ids) and pass a privacy gate (no home path, account or host name,
  keys, e-mail addresses, private IPs, PNG metadata, and no machine
  fingerprint: CPU model, local timezone, kernel release, machine-id, GPU UUID). The release contains no
  `K.dat`, no personal `config.json` and no caches; `config.json` holds the
  built-in defaults.
- Every shipped document passes a mechanical accuracy audit before the archive
  is built: links and anchors, commands, tools, counts, constants, settings,
  environment variables, file paths and function names are checked against the
  code in the tree being released, and the changelog must be dated for this
  version.

## 1.4.0 — 2026-11-07

One model, one wire, one credential: **DeepSeek V4.1 Flash**
(`deepseek-flash`) on `POST /anthropic/v1/messages`, keyed by `K.dat`.

- **Removed:** OpenRouter (vision failover, subagent offload, `/or`, `/orsub`,
  `OK.dat`), Ollama (`/ollama`, `OLLAMA_HOST`), the legacy `/responses` wire,
  `deepseek-v4-pro` and the `/model` switcher, and the `vision_*` backend
  settings.
- **Changed:** the model is a constant (`config.MODEL`), not a setting; vision
  is one isolated call on the same model (`/vision on|off|status`, real media
  types, 800 px cap); effort sends only `low` / `high` / thinking disabled.
  Anthropic-format wire through a deterministic codec; subagents off by
  default (`/subagents on`); 1M window with auto-compaction at 200k, checked
  after every tool batch.
- **Fixed:** vision answers were missing from `/cost`; every image was labelled
  `image/png`; `AnalyzeImage` accepted paths outside the project.
- **Upgrading from 1.3.x:** nothing to do. Old `config.json` keys are ignored on
  load and dropped on the next save.

## 1.3.4 — 2026-09-14

Recut of 1.3.3: `deepseek-flash` everywhere on the `/responses` wire, thinking
as a 0–100 dial (default and ceiling 75), the 2026-09-10 price sheet with
weekday-only surge, and a prefix-cache-preserving compaction policy.

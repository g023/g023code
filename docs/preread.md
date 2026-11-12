# Pre-emptive reads — `preread.py`

The first model call of a coding turn is nearly always the same hop: ReadFile
of the files the request names, of the module a named test file imports, or
of the file that defines a named function. On recorded live runs that hop came
first in about 95% of runs of every task shape. It is paid in full: the model
writes the calls (output tokens) and then the whole context is replayed for
the next call. The harness can predict it from the request text alone, so it
runs those reads itself before call 1.

## Switch

Setting `preread` (env `G023_PREREAD`): `ghost` (default), `prose`, `off`.
`/settings` shows it. Slash commands never trigger it.

## Predictor (`plan`)

Tiers, highest precision first; caps `MAX_READS` = 4 files and
`TOTAL_MAX_CHARS` = 12,000 characters per turn, plus at most one search.
Entries are tool argument dicts; `tool_of` says which tool (an entry with
`query` is a SearchContent).

- **T1** paths the request names that exist, are UTF-8 text and fit one whole
  read (`WHOLE_MAX_CHARS` = 4,000, the size at which ReadFile returns a file
  whole).
- **T2** project modules a named Python *test* file imports (test -> subject).
  `from pkg import mod` resolves to `pkg/mod.py` when that is a module.
  Standard library and site packages never resolve (outside the root).
- **T2b** the reverse: when the request mentions tests, a named source file's
  unique `test_<name>.py` / `<name>_test.py`.
- **T3** the unique definition of a code-like identifier the request names
  (snake_case, digits, lowerCamel, CONSTANT_CASE — same rule as the orient
  card): the whole file when small, else `ReadFile focus=<name>`. Undefined or
  defined in more than one file: no read.
- **T4** only when the request names no path and nothing above fired: a
  lowercase word whose stem is the stem of exactly one small non-test source
  file, read whole, and only if the request also names that file's folder
  (`counts` ... `in textkit` -> `textkit/count.py`) or uses the word as a code
  noun (`the slug helper`). Generic stems (`main`, `utils`, `config`,
  `models`, ...) never match. At most two.
- **T1b** a capitalised word that is the stem of exactly one top-level file
  (`README` -> `README.md`), only next to another prediction.
- **T5** a rename / every-reference request (`rename`, `all callers`,
  `everywhere`) about a uniquely defined name: one `SearchContent` for it,
  scoped to the top folder of its definition.

What was tried and removed, on held-out prompts: outlines of big files (the
model still asked for a line range: no hop saved), a README on its own (the
model still read the code file), and stem words without a folder or code noun
(the English word "count" matched `count.py`). The common failure is a
*partial* prediction: if the model still needs one more read before it can
act, the hop is not saved and the pre-read files are paid for nothing.

Why so strict: a read the model then ignores is paid once as cache-miss input
($0.15/M). About 1k unused tokens cost more than the hop a correct guess
saves, so only high-precision tiers are used.

## Wire forms

- **ghost** (default): the reads are recorded as the model's own tool
  exchange — `function_call` items with ids `ghost_<turn>_<i>`, then their
  results through the normal tool path (permission, codec, view tracking,
  repeat guard) — followed by one user text block, `GHOST_NOTE`. That note is
  required: DeepSeek's thinking mode answers HTTP 400 "The `content[].thinking`
  in the thinking mode must be passed back to the API" for an assistant
  tool_use turn that has no thinking block, unless a user text block follows
  its results (probed live; a fake thinking block also passes, but it puts
  words in the model's mouth). The budget tracker and the scout's
  use-confirmation ignore ghost calls.
- **prose**: the same results appended to the user message under
  `PROSE_HEAD`, one `--- ReadFile <path>` block each.

`hop_stats["ghost_reads"]` counts reads per session; `used()` reports how many
ghost paths the model then touched (bench instrumentation).

## Interaction with finish

With the edit reachable in call 1, a bugfix can carry `final_answer` on a bare
Edit. When the request asks for a run, `finish.decide(require_run=True)` holds
that answer until something ran (`finish.md`).

## Measured (live, deepseek-flash, every run solved)

Main A/B, 7 task shapes × 5 runs per arm:

| arm | $/solved | calls | secs |
|---|---|---|---|
| off | 0.000532 | 2.94 | 8.1 |
| ghost | 0.000441 | 2.34 | 7.6 |
| prose | 0.000456 | 2.31 | 7.2 |

Task-stratified vs off, 90% bootstrap CI: ghost $ -18.9% [-25.0, -9.4],
calls -19.9%; prose $ -12.3% [-20.0, -2.0]. Ghost beats prose on cost: with
the files inside the user message, the answer that follows is longer
(question: 202 vs 110 output tokens). The model re-read a ghost file 0 times
in 50.

Held-out prompts (never used to tune the rules), ghost vs off: two reworded
sets gave $ -5.7% and -7.5% (not significant); every loss was a partial
prediction, which led to the removals listed above and to T2b. A third fresh
set run on the final rules (7 tasks × 5): $ -33.8% [-41.1, -24.8], calls
-35%, wall time -22%. Pooled over all batches (130 runs per arm): $ -17.9%
[-21.7, -12.2], calls -17.8%, wall time -7%. Read-only questions about named
code take one call instead of two. Where nothing is predicted, nothing is
added, so the cost is the same as off.

Prior art: Claude Code's `@file` mentions inject synthetic Read tool results
into the conversation; IDE agents prefetch open files. The difference here is
that the reads are *predicted* from the request text by bounded,
high-precision rules priced against the cache-miss cost of a wrong guess, and
recorded as the model's own exchange.

Tests: `tests/test_preread.py`.

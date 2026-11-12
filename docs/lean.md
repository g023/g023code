# Lean turns: closure receipts, lean codec, lean prompt, orientation card

Four small features that cut what a turn costs without changing what it does.
They are on by default. Each one has a saved setting and an env override
(`=0` turns it off).

| setting | env | module | what it does |
|---|---|---|---|
| `closure_receipts` | `G023_CLOSURE_RECEIPTS` | `closure.py` | An Edit result gains `refs_left`: whole-token counts, across the project, of the names and numbers the edit removed. `0` means none left; otherwise up to 6 `path:line` sites. |
| `lean_codec` | `G023_LEAN_CODEC` | `tool_codec.py` | History copies of Edit/WriteFile results drop the backup path. Bash results become a JSON header (`exit_code`, notes) followed by raw `[stdout]` / `[stderr]` bodies, not JSON-escaped. In test-runner output only, banner rulers shrink to two characters a side; elsewhere `=====` lines are data and stay byte-exact. |
| `lean_prompt` | `G023_LEAN_PROMPT` | `orchestrator.lean_system` | Removes a standing-prompt line that contradicted the batching rule ("Prefer multiple precise tool calls…"). Asks final answers to report only what the tool trace does not already show. |
| `orient_card` | `G023_ORIENT_CARD` | `orient.py` | When the request names code-like identifiers (snake_case, lowerCamel, digits) or project paths, one `[where: …]` line is appended to the user message: each file's line count, and each name's definition site and use count, or "not found". |

`scope_count` (`G023_SCOPE_COUNT`, **off**) is a fifth, opt-in feature. A
SearchContent scoped to a subdirectory also reports `elsewhere_in_project`,
the number of hits outside that scope. It had no measurable effect, so it
ships off.

`search_tiers` (`G023_SEARCH_TIERS`, **off**) and `def_peek` (`G023_DEF_PEEK`,
**off**) are two more opt-in search features in `subagents/searcher.py`:

- **Tiers.** Without tiers, every line that "defines" something ties: a
  `def` of a surge method, the `SURGE_SUB_CHOICES` tuple and a local
  `surge_badge = ...`. File name order then decides, so when this was measured
  on this package the answer to a "surge" question came 6th of 12 samples. With tiers, rank is a def/class/function
  named by the query, then a module-level name, then an indented local
  assignment, then a plain use. A name that equals the query beats one that
  only contains it. Among equal files, one whose name matches the query
  comes first. The answer moves to 3rd.
- **Peek.** When exactly one shown definition's name equals a query term, or
  only one shown line is a definition at all, its body (decorators included,
  at most 30 lines / 2,400 characters) is appended as
  `## definition path:a-b` with `N|` line numbers. Any other shape is a
  guess, so nothing is added.

Live, over 4 lookup questions × 5 runs per arm, the two together were neutral
to slightly worse ($0.000741 vs $0.000804 per solved, 20/20 both). The
model's queries are usually broad (`surge|peak`, 20 samples), so the peek
rarely fires, and the run-to-run spread comes from which queries it picks. So
both ship off.

## Why each one helps

- **Closure receipts.** After a rename the model greps to confirm nothing is
  left. A plain grep for `load_cfg` also matches the new `safe_load_cfg`, so
  the task can look unfinished after it is done. The receipt counts whole
  tokens only, so the new name never counts as a leftover of the old one.
  The scan skips `.g023/`, ignored directories, and data files (`.jsonl`,
  `.log`, …), with limits of 0.5 s and 16 MB. A word that occurs more than 40
  times is ordinary vocabulary and is not reported, unless it is distinctive
  (snake_case, camelCase, contains digits, or 12+ characters).
- **Lean codec.** Every byte in history is paid as a cache miss once and as a
  cache hit on every later call. The backup path is never read: WriteFile's
  shrink warning still names it. JSON-escaped test output costs `\n` and `\"`
  bytes and is harder to read. The encoding is idempotent: re-encoding its own
  output returns the same bytes.
- **Lean prompt.** Output tokens cost about 200× a cache hit. Final answers
  that repeat the diff and the test output were the largest single block of
  "say" output.
- **Orientation card.** A request that names a function usually spends its
  first call on searching for it. The card costs a few dozen input tokens;
  the hop it replaces costs a model call. It is added only when something
  named resolves.

## Measured (deepseek-flash, live)

Seven task shapes: four on a toy repo (bug fix, feature + test, config +
README, question) and three on a ~25k-line copy of this package (two lookup
questions, one cross-file rename). All Bash/Edit/WriteFile calls were
allowed, with a fresh fixture for every run.

| arm | runs | solved | $/solved | calls | output tok | miss tok | hit tok |
|---|---|---|---|---|---|---|---|
| all off (two batches) | 56 | 56 | 0.000948 | 5.05 | 916 | 2,007 | 32,329 |
| all four on (two batches) | 56 | 56 | 0.000703 | 4.41 | 620 | 1,643 | 28,002 |

That is **−26% cost per solved task**: −32% output, −18% cache-miss input
and −13% calls, with no loss of solved tasks. The edit tasks gained most
(config 0.000906 → 0.000515, feature 0.001581 → 0.000924, rename 0.001480 →
0.001064). The read-only lookups stayed within noise (surge 0.000869 →
0.000918, effort 0.000615 → 0.000481).
Measured one at a time over 21 runs each (7 shapes × 3), the gains were
lean prompt −12%, orientation card −10%, lean codec −5% and closure receipts
−5%. Those per-feature numbers are within run-to-run noise, so only the
combined result is claimed.

The lean prompt was also tested in halves (7 shapes × 4 runs per arm).
Removing the contradictory line alone cut thinking output 13%. The
answer-scope clause alone shortened answers 22% but raised thinking 13%.
Neither half alone changed cost. Together they gave thinking −16%, answers
−36%, and −14% $/solved. So the two halves ship together.

A post-edit window (the edited lines ±2 in every Edit result) was tried on
top of the bundle and cost 8% more per solved task, so it was removed.

Observed behaviour: the model reads `refs_left` ("refs_left shows 0") but
often still runs a confirming grep in the same batch as its import check.
That grep costs tool-argument tokens but not an extra call.

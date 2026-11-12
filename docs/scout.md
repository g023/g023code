# Scout card — `scout.py`

A free pre-read of the project before the paid model's first call. Most turns
open with digging (ListDir, SearchContent, ReadFile): on 408 recorded bench runs
an average of 3.6 of 7.8 model calls came before the first edit, and every hop
replays the context and bills reasoning. The scout does that first hop on this
machine and puts the result in the user message as a lead.

## Pipeline

1. **Index** (`Index`, `index_for`): project files (same walk as the closure
   index) cut into units: one per def/class (`_ANCHOR`), markdown by heading,
   anything longer than `UNIT_MAX` (80) lines in `WINDOW` (60) line windows.
   Cached per file by (mtime, size); data files (`.json`, `.jsonl`, `.csv`,
   logs, locks, minified), binaries and files over 400 KB are skipped. 1407
   units of a 25k-line package: 0.12 s cold, ~10 ms per query.
2. **Rank** (`rank`): BM25 over unit text, path and symbol name count ×1.5.
   Test files ×0.6 unless the request mentions tests; docs (`.md`, `.rst`,
   `.txt`) ×`DOC_DISCOUNT` (0.6) unless the request asks for docs.
3. **Expand** (`expand`, local model): up to 10 identifier-like terms the code
   probably uses for the request's words ("surge factor" → `peak_multiplier`),
   added to the ranking at weight 0.5. Time box: half of `LOCAL_TIMEOUT_S` (8 s).
4. **Read** (`locate`, local model): the top `TOP_UNITS` (8) units are shown
   as numbered lines; the model returns the ids of the lines that locate the
   request. Ids that were never shown are dropped and the text is copied from
   the file (`verify`, cite-or-drop), so every card line is verbatim.
5. **Card** (`render`, `card_text`): picked lines grouped by file and
   enclosing def/heading, then `also matching: <next 3 units>`:

   ```
   [scout: a free local pre-read of this request; lines are verbatim from the files, the choice is a lead to check, not a fact]
   g023_code/usage.py (in: def surge_multiplier(self) -> float:):
     42: def surge_multiplier(self) -> float:
     43: """Peak is exactly twice off-peak on the current tidal sheet."""
     44: return self.surge_output / self.output
     53: surge_output=1.20,
     54: )
   g023_code/surge.py (in: def is_peak_weekday(bj: datetime) -> bool:):
     52: def is_peak_weekday(bj: datetime) -> bool:
   also matching: g023_code/surge.py:1-43; g023_code/cli.py:2068-2108 _surge_tick
   ```

   (Real output for "how is the surge (peak) price multiplier computed?" on the
   a copy of this package, LFM3-1.7B, 1.06 s.)

   Without the local model (`scout_card always`) the card lists the top units
   only, headed `[scout: best-matching code units for this request (keyword
   match, a lead)]`.

No card when: the mode is `off`; the input is a slash command; the request has
fewer than `MIN_TERMS` (2) content words; the local model answered with no
lines; any error or timeout; the GPU is busy or the request itself talks about
GPU work (`local_llm.request_wants_gpu`); or the mode is muted (below).

## Use-confirmed precision (self-muting)

After the turn, `used_files` checks whether the paid model's tool arguments or
its answer named a scouted file (path or basename) or a symbol on the card;
`record_use` appends the result to `<global cache>/local_llm/scout_ledger.json`
(cards, used, last 20). `trusted(mode)` turns a mode off while fewer than 30 %
of its last 10 cards were used. The paid model's own trajectory is the label,
so the scout's quality is measured on real work without any extra call.
`/local status` shows the counts.

## Settings

`scout_card` (`G023_SCOUT`): `local` (default; a card only when the local model
is on and answers), `always` (fall back to the keyword card without the local
model), `off`. `/local scout local|always|off`.

## LocalAsk without paths

`LocalAsk` with no `paths` uses the same `locate` over the whole project, then
drafts the answer from the verbatim evidence (`local_ask._run_project`; result
field `searched`).

## Evidence

20 natural-language questions over the 25k-line package snapshot, gold =
(file, def):

| | top-1 | top-3 | top-8 |
|---|---|---|---|
| BM25 units, no doc discount | 9 | 13 | 18 |
| + doc discount 0.6 | 10–11 | 15–16 | 18–19 |

Card hit (a card line inside the gold def), LFM3-1.7B Q8: 13/20 at 0.35 s
without expansion, 15/20 at ~0.9 s with it, 17/20 counting the `also
matching` line (~1 s). Nanbeige4.2-3B: 14/20 at 5–10 s, so the 1.7B model is
the recommended one.

Live A/B (DeepSeek Flash): 5 tasks × 3 reps per arm (2 package Q&A, 3 ledgerkit edit/trace), 15/15 solved in every arm; per task: no card $0.00125 and 5.4 calls, keyword card $0.00112 / 4.93, local card $0.00098 / 4.73 (−22 % cost). The card mostly replaces searching (SearchContent 1.27 → 0.40 per task; ReadFile unchanged at 2.67); all 30 cards were used by the paid model. Small sample, a direction rather than a measurement.

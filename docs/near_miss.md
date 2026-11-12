# `near_miss.py` — near-miss names on a zero-hit search

The model searches for the name it expects (`count_words`); the project spells
it differently (`word_count`). A bare "0 matches" costs several calls of
ListDir, ReadFile and re-grepping. When SearchContent finds nothing,
`searcher._add_near_misses` asks this module for up to 5 names the project
defines that are spelled like the query.

## Matching

- `query_bags`: identifiers in the query (regex escapes such as `\b` removed,
  search words such as `def`/`class`/`function` dropped), each split by
  `name_parts` on `_`, camelCase and digits, lowercased, with simple endings
  removed (`ings ing ers er ed es s`, keeping ≥ 3 chars). A multi-word query
  also yields one bag of all its words (`word count`).
- Candidates: names the files *define* (one MULTILINE regex: def/class/
  function/fn/func/struct/enum/trait/type/const/let/var, Go methods, `#define`,
  column-0 assignments, C-style function heads, indented methods with an
  optional return type) plus file stems.
- Score: Jaccard over part sets where a part also matches its typo
  (similarity ≥ 0.8, length ≥ 4), or the whole joined name's similarity
  ≥ 0.8. Kept at ≥ 0.5. One row per name (best score, then shortest path), so
  vendored or release copies do not crowd the list.

## Budget

Runs only when `total == 0`. The walk is the searcher's own (`_walk_files`,
`_should_skip`, `.gitignore`). Docs and data files (`.md .txt .json .csv .lock
.svg .html …`), files over 512 KB and binaries are skipped. Stops at 1 s or
24 MB; the result says `complete: false` internally when cut. About 0.6 s on a
repo with four release copies; a few ms on small projects. A failure inside the
hint never breaks the search. `G023_NEAR_MISS=0` disables it.

## Output

The search payload gains `did_you_mean: [{name, path, line}]` and the summary
gains "Similar defined names: …". `tool_codec.encode_search` renders one line:
`did you mean: word_count (textkit/count.py:4), WordCounter (textkit/count.py:12)`.

## Measured (live, deepseek-flash, lookup question × 6 per arm)

| arm | solved | calls | $/solved |
|---|---|---|---|
| off | 6/6 | 3.67 | 0.000334 |
| on  | 6/6 | 2.83 | 0.000247 |

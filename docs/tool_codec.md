# `tool_codec.py` — tool results encoded for history

Each byte of a tool result in history is billed once as a cache miss, then
again as a cache hit on every later step. `encode_tool_result(name, text,
root=None)` runs once, in `Orchestrator._run_tool_calls`, before the vault
decision. The UI (`ui.describe_result`) and the working set still parse the
raw JSON. Only the history copy (and the session log's `tool_result`) holds
the encoded form.

Properties: deterministic, idempotent (encoding its own output returns it
unchanged), non-JSON text passes through, and file text (`content`, search
`match`) is never altered.

## What it drops (derivable or never read)

| From | Dropped |
|---|---|
| all JSON | `null`, `""`, `[]`, `{}`; the project-root prefix of whole-value paths; 64-hex `hash`/`sha256` → 12 hex (a valid evidence prefix, `working_set._evidence_cites_file`) |
| SearchContent | echoed args (`query`, `path`, `file_glob`, `max_matches`), `backend`, `files_scanned`, `files`, `metadata_summary`, the old `matches` duplicate |
| Edit / WriteFile (`lean_codec`) | `backup` (the WriteFile shrink warning still names it) |
| ReadFile | `from_cache`, `summary_source`, the local `summary` (generated from the metadata it keeps), `metadata.symbols` entries the outline covers, the fixed per-call notes (the tool schema states them once) |

## Layout

Bash (`lean_codec`, default on): the JSON header holds `exit_code` and any
notes (`attribution`, `hint`); multi-line or long `stdout`/`stderr` follow
raw under `[stdout]` / `[stderr]`, and pytest banner rulers shrink to two
characters a side (`== 1 failed in 0.1s ==`). Short single-line output stays
in the header.

ReadFile:

```
{"path":"g023_code/economics.py","hash":"cff318a5cce5","language":"python","loc":124}
imports: __future__.annotations, dataclasses.dataclass, ...
class CompactionMeter  L60-115
  observe(self, calls, context_tokens)  L84-97
compaction_cost(context_tokens, ...)  L43-57  # USD of one compaction: ...
-----
<verbatim content, exactly as on disk, when the read returned source>
```

ListDir: a header, then one row per entry (`name/` for
directories, `name  size` for files), or the digest rows when ListDir
returned a digest:

```
{"path":"frames","dirs":"0","files":"4,001","digest":true,"legend":"..."}
frame_{00000..03999}.png  x4000  3.1 MB
README.md  12 B
```

Glob: a header (`total`, `truncated`, `included_ignored`), then the digest rows,
then `first N:` and the file rows.

SearchContent (`grep -C` style; `N:` hit, `N-` context, `--` gap):

```
{"total":10,"shown":"all"}
## g023_code/usage.py
109-
110: def pricing_for(model: str) -> ModelPricing:
111- return PRICING.get(model, DEFAULT_PRICING)
more files (hits): docs/x.md (3)
```

A zero-hit search with near-miss names (`docs/near_miss.md`) ends with one line:
`did you mean: word_count (textkit/count.py:4), WordCounter (textkit/count.py:12)`.

`shown:"all"` means the list is exhaustive. Live bench: without it, the
model re-grepped a complete 10/10 result to confirm it.

## Measured (1.5.0)

| payload | raw chars | encoded |
|---|---|---|
| ReadFile summary (economics.py) | 2 013 | 825 |
| ReadFile focus + verbatim section | 2 922 | 1 656 |
| ReadFile range 30-40 | 641 | 535 |
| README.md summary | 1 137 | 513 |

A development bench (4 fixed read-only tasks, all solved, single runs):
baseline $0.002302 per solved task, 21 calls. With the codec, ranked search
and the JIT handoff: $0.000867 (14 calls), $0.001065 (16 calls), $0.001067
(13 calls); mean ~$0.00100, about 2.3x cheaper per solved task. The baseline
is a single run.

## Related

- `docs/cost_policy.md` — the vault runs after the codec
- `docs/subagents.md` — search sample ranking (`_pick_samples`)
- `tests/test_tool_codec.py`

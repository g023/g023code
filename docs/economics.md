# `economics.py` — when compaction pays (ski-rental rule)

`g023_code/economics.py` decides when the auto-compact path should fire
*before* the hard token trigger. It makes no API calls.

## The trade

- **Rent**: every orchestrator call re-reads the whole history at the
  cache-hit rate. Rent per call = `(P − B) × hit`, where `P` is the real
  context gauge and `B` the size the context would shrink to.
- **Buy**: compacting costs one summarizer pass (`P × hit + ~2k output`,
  zero when a same-response `[handoff]` exists) plus a one-time miss on the
  rebuilt body `(B − standing) × (miss − hit)`. The standing prefix
  (instructions + tools) is byte-stable and stays a hit.

`CompactionMeter` accumulates rent since the last compaction and reports
`due` once `rent ≥ fidelity × buy` and `P ≥ compact_floor_tokens`. That is
the deterministic ski-rental rule (2-competitive in the worst case). Under
linear growth it equals the EOQ optimum `n* = sqrt(2C / (h·g))`.
Every price doubles in a surge window, so the decision is the same on and off peak.

- `fidelity` (3) charges for information lost in a summary. With fidelity 1
  the rule optimises dollars alone.
- `B` is learned: the first real gauge after a compaction is the rebuilt size.
- `/clear` (usage reset) and every compaction restart the rent.

## Settings and commands

| setting | default | command |
|---|---|---|
| `compact_policy` | `economic` (`fixed` = token trigger only) | `/compact-at economic\|fixed` |
| `compact_floor_tokens` | 64,000 | `/compact-at floor 64k` |
| `compact_at_tokens` | 200,000, the hard ceiling (also window pressure) | `/compact-at 200k` |

An economic trigger runs Layer 3 only. Layer-1 micro-blanking stays reserved
for real window pressure (the ceiling). `/cost` prints the meter:
`Compact   rent $… of $… compaction price (…%), floor 64,000 tok · ceiling 200,000 tok`.

## Evidence

A development simulation (not shipped) ran 20 sessions of 600 calls each:

| policy | cost vs fixed-200k |
|---|---|
| fixed 200k (old) | 100% |
| ski-rental, fidelity 1, no floor | 87.2% |
| ski-rental, fidelity 3, floor 64k (default) | 92.6% |
| best fixed threshold in hindsight (oracle, ~77k) | 86.5% |

The online rule comes within 0.7 points of the hindsight oracle.

# `signals.py` — drift signals (quiet failure detection)

`g023_code/signals.py` implements the session's early-warning system.
The DeepSeek client never validates responses, so the failure mode the
program is most exposed to is **silent**: a renamed content field makes
`api.output_text` return `""` and the model simply looks like it went
quiet. Signals record cheap observations that "something moved" — without
raising exceptions — and surface them to the user.

## `Observation` & `SignalLog`

- `Observation(tag, detail, when)` — one recorded event.
- `SignalLog` (singleton via `get_signals()`):
  - `note_unknown_types(names)` — record unknown Responses item types
    (fed by `api.unknown_item_types`).
  - `note_empty_response()` — a reply with no parseable output.
  - `recent()` — the last N observations for `/signals`.
  - `reset()` — clear the session log.

## `hit_rate_verdict(history)`

Compares the **latest day's prefix-cache hit rate** (from
`cache.prefix_history`) against prior days and returns
`(verdict, latest, baseline)` where verdict ∈
`no history | insufficient baseline | down | up | steady`:

- `MIN_BASELINE_DAYS = 2` days of history required before judging.
- `HIT_RATE_ALERT_DROP = 0.15` — a drop of ≥15 points vs baseline = "down".

The idea: a persistent collapse in cache-hit ratio is a sign the API's
prompt-cache behaviour changed — worth knowing even though nothing
crashed.

## Design constraints

- Signals must fire on shapes that matter **and stay quiet on shapes that
  are merely unusual** — a signal that fires on ordinary turns gets
  ignored and stops being a signal.
- None of these detect a schema change *as such*; they detect "something
  moved". `silent_degradation` in `api.py` is the per-response pass;
  this module is the session log + the cross-day verdict.

## Related

- `docs/api.md` — `unknown_item_types`, `silent_degradation`.
- `docs/cache.md` — `cache.prefix_history` feeds the verdict.
- `docs/cli.md` — `/signals` command renders the log and verdict.

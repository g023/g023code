# `surge.py` — DeepSeek surge (peak/off-peak) pricing windows

`g023_code/surge.py` implements DeepSeek's tidal pricing: peak hours bill
at 2× the off-peak rate, and the windows are anchored to **Beijing wall-
clock time**. The module converts those windows to the machine's local
time, decides whether surge is active right now, and computes pause
ranges for the "don't work during peak" feature.

## Source of truth

`config.SURGE_PEAK_WINDOWS_BEIJING` / `config.SURGE_UTC_OFFSET` /
`config.SURGE_PEAK_WEEKDAYS`, loaded from `ds_times.json` (Beijing
09:00–12:00 & 14:00–18:00, UTC+8, Monday–Friday) with a hard-coded
fallback. Weekends are off-peak. Chinese public holidays are off-peak in
full (pricing sheet); DeepSeek does not publish the dates, so
`config.SURGE_HOLIDAYS` comes from the optional `"holidays"` list
(`YYYY-MM-DD`, Beijing dates) in `ds_times.json` — empty by default — and
`is_peak_weekday` returns False on those dates. `surge.py` never hard-codes
the windows itself.

## Functions

- `beijing_now(now=None)` — the current instant as Beijing wall-clock time
  (the only timezone the API's pricing actually cares about).
- `is_surge(now=None)` — inside a peak window right now (and on a peak weekday)?
- `is_peak_weekday(bj)` — Monday–Friday in Beijing wall-clock time.
- `peak_windows_local(now, days)` — upcoming windows expressed in the
  **machine's local timezone** (what the user's clock shows).
- `next_peak_window(now)` — the next one, for "peak until HH:MM" notices.
- `clock(when)` / `clock_range(start, end)` / `format_duration(td)` —
  HH:MM formatting helpers.
- `pause_ranges(now, buffer_minutes)` — widen each peak by the pause
  buffer (default 5 min) and merge overlaps, in local time.
- `seconds_until_pause_ends(now)` — seconds left in the active pause
  window (0 when not paused); the CLI's surge-pause loop sleeps on it.
- `next_transition(now, buffer_minutes)` — next surge on/off boundary in
  local time, used by `_surge_tick` notifications.
- `describe()` — a human summary of today's windows (the `/surge`
  panel).

## Guarantees (per `tests/test_surge.py`)

Three facts come from DeepSeek: the Beijing peak windows, that they apply
Monday–Friday, and that off-peak = half of peak. Everything here is
arithmetic on top — converting the current instant to Beijing time,
deciding on/off, skipping weekends, widening by the buffer — **always
reported back in local time**.

## Related

- `docs/config.md` — where the windows/offset come from.
- `docs/data_files.md` — `ds_times.json`.
- `docs/cli.md` — `_surge_tick`, the surge-pause loop, `/surge`.
- `docs/usage.md` — peak tokens priced at the surge rate.

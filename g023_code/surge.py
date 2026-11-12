"""
DeepSeek surge (peak/off-peak) pricing windows.

DeepSeek moved to tidal pricing on 2026-08-17 (sheet restated 2026-09-10):
peak hours are **09:00–12:00 and 14:00–18:00 Beijing time (UTC+8), Monday
through Friday, excluding Chinese public holidays**. Weekends and holidays
(dates listed under ``"holidays"`` in ds_times.json) are off-peak. Off-peak prices are exactly half of
peak. That anchor is fixed, but the user's clock is not — so every window below
is computed from the current instant's Beijing wall-clock time and *reported in
the machine's local timezone*. ``/surge status`` and the loop notifications
therefore always match the clock the user actually looks at. Times are shown in
both **AM/PM and 24-hour** formats (see :func:`clock` / :func:`clock_range`),
and the status line counts down to the next surge and its duration
(:func:`next_peak_window` / :func:`format_duration`).

Two concepts are kept distinct:

* **surge window** — the raw peak hours. Notifications fire on entering and
  leaving these.
* **pause window** — the surge window widened by ``buffer`` minutes on both
  edges (default 5). When ``settings.surge_pause`` is on, the CLI refuses to
  start new work while inside a pause window, waits it out, then resumes.

The peak windows come from ``ds_times.json`` (loaded by ``config.py``); the
peak/off-peak price rows come from ``ds_pricing.json`` (loaded by ``usage.py``).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from .config import (
    SURGE_HOLIDAYS,
    SURGE_PEAK_WEEKDAYS,
    SURGE_PEAK_WINDOWS_BEIJING,
    SURGE_UTC_OFFSET,
)

# Beijing has no DST; a fixed offset is correct all year round.
BEIJING_TZ = timezone(timedelta(hours=SURGE_UTC_OFFSET))


def beijing_now(now: Optional[datetime] = None) -> datetime:
    """The current instant expressed in Beijing wall-clock time."""
    now = now or datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()  # attach the system's local zone
    return now.astimezone(BEIJING_TZ)


def is_peak_weekday(bj: datetime) -> bool:
    """True when ``bj`` (Beijing wall-clock) falls on a published peak weekday.

    Chinese public holidays listed in ds_times.json ``"holidays"`` are
    off-peak in full, per the pricing sheet.
    """
    days = SURGE_PEAK_WEEKDAYS or (0, 1, 2, 3, 4)
    if bj.date().isoformat() in SURGE_HOLIDAYS:
        return False
    return bj.weekday() in days


def is_surge(now: Optional[datetime] = None) -> bool:
    """True when the DeepSeek API is at surge pricing right now."""
    bj = beijing_now(now)
    if not is_peak_weekday(bj):
        return False
    for start_hour, end_hour in SURGE_PEAK_WINDOWS_BEIJING:
        if start_hour <= bj.hour < end_hour:
            return True
    return False


def peak_windows_local(
    now: Optional[datetime] = None, days: int = 2
) -> list[tuple[datetime, datetime]]:
    """Raw (unbuffered) surge windows for the next ``days`` peak days, in local time.

    Weekend calendar days are skipped (published sheet is Monday–Friday).
    These are the true peak hours — the pause windows in :func:`pause_ranges`
    are these same windows widened by the buffer. Ordered by start time.
    """
    bj = beijing_now(now)
    out: list[tuple[datetime, datetime]] = []
    found_days = 0
    day_offset = 0
    while found_days < days and day_offset < 14:
        day = bj.date() + timedelta(days=day_offset)
        day_offset += 1
        mid = datetime.combine(day, datetime.min.time(), tzinfo=BEIJING_TZ)
        if not is_peak_weekday(mid):
            continue
        found_days += 1
        for start_hour, end_hour in SURGE_PEAK_WINDOWS_BEIJING:
            start = mid.replace(hour=start_hour).astimezone()
            end = mid.replace(hour=end_hour).astimezone()
            out.append((start, end))
    return out


def upcoming_peak_windows(
    now: Optional[datetime] = None, count: int = 2
) -> list[tuple[datetime, datetime]]:
    """The next ``count`` peak windows that have not ended yet, in local time.

    /surge shows these beside :func:`pause_ranges` (which also drops ended
    windows), so both rows describe the same upcoming windows.
    """
    now_local = (now or datetime.now()).astimezone()
    return [w for w in peak_windows_local(now, days=3) if w[1] > now_local][:count]


def next_peak_window(
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime]:
    """The nearest surge window that *starts* in the future, as (local_start, local_end).

    If a surge is running right now this returns the *following* window — the
    countdown to "the next surge" is always something in front of the user, not
    the window they are already inside. Returns the current window only when no
    future start exists (kept for safety — windows repeat daily, so it cannot
    happen).
    """
    now_local = (now or datetime.now()).astimezone()
    for start, end in peak_windows_local(now, days=3):
        if start > now_local:
            return start, end
    return now_local, now_local + timedelta(hours=3)


def clock(when: datetime) -> str:
    """One instant as ``09:00 AM / 09:00`` — AM/PM and 24-hour together."""
    return f"{when:%I:%M %p} / {when:%H:%M}"


def clock_range(start: datetime, end: datetime) -> str:
    """A span as ``09:00 AM–12:00 PM / 09:00–12:00`` — both formats, unambiguously."""
    return f"{start:%I:%M %p}–{end:%I:%M %p} / {start:%H:%M}–{end:%H:%M}"


def format_duration(td: timedelta) -> str:
    """``3h 00m`` / ``2h 45m`` / ``12m`` — human countdown, always minute-exact."""
    total_min = max(0, int(td.total_seconds() // 60))
    hours, minutes = divmod(total_min, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"


def pause_ranges(
    now: Optional[datetime] = None, buffer_minutes: Optional[int] = None
) -> list[tuple[datetime, datetime]]:
    """Surge windows widened by the pause buffer, as local-time (start, end).

    ``buffer_minutes`` defaults to ``settings.surge_pause_buffer_minutes`` and
    0 means "no buffer" — pause exactly during peak hours. Windows are returned
    in the machine's local timezone so the CLI can sleep until the right moment
    on the user's clock.
    """
    from .config import settings  # local import: avoids a cycle at import time

    if buffer_minutes is None:
        buffer_minutes = settings.surge_pause_buffer_minutes
    bj = beijing_now(now)
    buffer = timedelta(minutes=buffer_minutes)
    ranges: list[tuple[datetime, datetime]] = []
    # Look ahead a week so Friday evening still finds Monday's window.
    for day_offset in range(0, 8):
        day = bj.date() + timedelta(days=day_offset)
        mid = datetime.combine(day, datetime.min.time(), tzinfo=BEIJING_TZ)
        if not is_peak_weekday(mid):
            continue
        for start_hour, end_hour in SURGE_PEAK_WINDOWS_BEIJING:
            start_bj = mid.replace(hour=start_hour) - buffer
            end_bj = mid.replace(hour=end_hour) + buffer
            start_local = start_bj.astimezone()
            end_local = end_bj.astimezone()
            if end_local > (now or datetime.now()).astimezone():
                ranges.append((start_local, end_local))
    return ranges


def seconds_until_pause_ends(now: Optional[datetime] = None) -> int:
    """Seconds until the active pause window ends (0 when not paused)."""
    now_local = (now or datetime.now()).astimezone()
    for start, end in pause_ranges(now):
        if start <= now_local < end:
            return max(0, int((end - now_local).total_seconds()))
    return 0


def next_transition(
    now: Optional[datetime] = None, buffer_minutes: Optional[int] = None
) -> tuple[str, datetime]:
    """The next event worth acting on: ("surge_start"|"surge_end"|"pause_start"|"pause_end", local_when).

    If surge pause is enabled the pause edges are what the loop waits for;
    otherwise the raw surge edges drive notifications. Returns
    ("none", local_now) when no future event exists (cannot happen — the
    windows repeat daily, but kept for safety).
    """
    from .config import settings  # local import: avoids a cycle at import time

    if buffer_minutes is None:
        buffer_minutes = settings.surge_pause_buffer_minutes
    now_local = (now or datetime.now()).astimezone()

    candidates: list[tuple[str, datetime]] = []
    bj = beijing_now(now)
    for day_offset in range(0, 8):
        day = bj.date() + timedelta(days=day_offset)
        mid = datetime.combine(day, datetime.min.time(), tzinfo=BEIJING_TZ)
        if not is_peak_weekday(mid):
            continue
        buffer = timedelta(minutes=buffer_minutes)
        for start_hour, end_hour in SURGE_PEAK_WINDOWS_BEIJING:
            start_bj = mid.replace(hour=start_hour)
            end_bj = mid.replace(hour=end_hour)
            if settings.surge_pause:
                candidates.append(("pause_start", (start_bj - buffer).astimezone()))
                candidates.append(("pause_end", (end_bj + buffer).astimezone()))
            else:
                candidates.append(("surge_start", start_bj.astimezone()))
                candidates.append(("surge_end", end_bj.astimezone()))
    future = [(kind, when) for kind, when in candidates if when > now_local]
    if not future:
        return ("none", now_local)
    return min(future, key=lambda item: item[1])


def describe() -> str:
    """One human-readable summary line, in local time, AM/PM + 24-hour."""
    now = datetime.now().astimezone()
    local_tz = now.strftime("%Z") or f"UTC{now.utcoffset() or timedelta(0)}"
    active = is_surge(now)
    ranges = pause_ranges(now)
    if not ranges:
        windows = "—"
    else:
        windows = " · ".join(clock_range(start, end) for start, end in ranges[:2])
    next_start, next_end = next_peak_window(now)
    until = format_duration(next_start - now)
    lasts = format_duration(next_end - next_start)
    kind, when = next_transition(now)
    return (
        f"{'SURGE ACTIVE' if active else 'off-peak'} · peak (Beijing 09-12/14-18 Mon–Fri) "
        f"in your clock: {windows} · next surge in {until}, lasts {lasts} "
        f"({clock_range(next_start, next_end)}) · next: "
        f"{kind.replace('_', ' ')} {clock(when)} ({local_tz})"
    )

"""
Surge pricing windows — Beijing-anchored, reported in local time.

The only hard facts DeepSeek publishes are the Beijing-time peak windows
(09:00-12:00 and 14:00-18:00, UTC+8) and that the off-peak price is half of
peak. Everything here is arithmetic on top of those: converting the current
instant to Beijing wall-clock time, deciding surge on/off, and widening the
windows by the pause buffer — always reported back in the machine's local time.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from g023_code import surge
from g023_code.config import SURGE_PEAK_WINDOWS_BEIJING, Settings, settings


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=timezone.utc)


def test_peak_windows_are_the_published_ones():
    # 09:00-12:00 and 14:00-18:00 Beijing time.
    assert SURGE_PEAK_WINDOWS_BEIJING == ((9, 12), (14, 18))


def test_surge_is_decided_on_beijing_wall_clock():
    # Beijing 10:00 == UTC 02:00 — inside the morning peak.
    assert surge.is_surge(_utc(21, 2, 0)) is True
    # Beijing 16:00 == UTC 08:00 — inside the afternoon peak.
    assert surge.is_surge(_utc(21, 8, 0)) is True
    # Beijing 12:30 == UTC 04:30 — between the two peaks.
    assert surge.is_surge(_utc(21, 4, 30)) is False
    # Beijing 08:00 == UTC 00:00 — before the morning peak.
    assert surge.is_surge(_utc(21, 0, 0)) is False
    # Beijing 18:30 == UTC 10:30 — after the afternoon peak.
    assert surge.is_surge(_utc(21, 10, 30)) is False


def test_pause_window_widens_by_buffer_on_both_edges(monkeypatch):
    old_buffer = settings.surge_pause_buffer_minutes
    settings.surge_pause_buffer_minutes = 5
    try:
        # Beijing 11:58 == UTC 03:58: inside the morning peak but within 5 min
        # of the 12:00 end — must count as paused (the +5 min tail).
        assert surge.in_pause_window(_utc(21, 3, 58)) is True
        # Beijing 08:57 == UTC 00:57: 3 min before the 09:00 start — the
        # -5 min lead-in must count as paused too.
        assert surge.in_pause_window(_utc(21, 0, 57)) is True
        # Beijing 08:50 == UTC 00:50: 10 min before start — outside the window.
        assert surge.in_pause_window(_utc(21, 0, 50)) is False
        # Beijing 12:03 == UTC 04:03: right at the tail edge — still paused.
        assert surge.in_pause_window(_utc(21, 4, 3)) is True
        # Beijing 12:06 == UTC 04:06: just past the tail — resumed.
        assert surge.in_pause_window(_utc(21, 4, 6)) is False
    finally:
        settings.surge_pause_buffer_minutes = old_buffer


def test_pause_window_zero_buffer_matches_raw_peak():
    old_buffer = settings.surge_pause_buffer_minutes
    settings.surge_pause_buffer_minutes = 0
    try:
        assert surge.in_pause_window(_utc(21, 4, 30)) is False  # 12:30 BJ, between peaks
        assert surge.in_pause_window(_utc(21, 2, 0)) is True     # 10:00 BJ
    finally:
        settings.surge_pause_buffer_minutes = old_buffer


def test_next_transition_is_the_nearest_future_event(monkeypatch):
    settings.surge_pause = False
    # Beijing 13:00 == UTC 05:00: between peaks → next is the 14:00 start.
    kind, when = surge.next_transition(_utc(21, 5, 0))
    assert kind == "surge_start"
    # Beijing 14:00 == UTC 06:00 → that's Beijing 14:00 on the 21st.
    assert when.hour == _utc(21, 6, 0).astimezone().hour
    assert when.date() == _utc(21, 6, 0).astimezone().date()


def test_pause_transitions_are_shifted_by_the_buffer(monkeypatch):
    settings.surge_pause = True
    old_buffer = settings.surge_pause_buffer_minutes
    settings.surge_pause_buffer_minutes = 5
    try:
        # Beijing 13:00 == UTC 05:00, 1h before the 14:00 peak. With pause on,
        # the relevant next event is the pause_start (13:55 Beijing == 05:55 UTC).
        kind, when = surge.next_transition(_utc(21, 5, 0))
        assert kind == "pause_start"
        expect = _utc(21, 5, 55).astimezone()
        assert when == expect
    finally:
        settings.surge_pause_buffer_minutes = old_buffer
        settings.surge_pause = False


def test_pause_seconds_until_ends():
    old_buffer = settings.surge_pause_buffer_minutes
    settings.surge_pause_buffer_minutes = 5
    try:
        # Beijing 11:58 == UTC 03:58 → 2 min of peak left + 5 min tail = 420 s.
        assert surge.seconds_until_pause_ends(_utc(21, 3, 58)) == 420
        # Not in a pause window → 0.
        assert surge.seconds_until_pause_ends(_utc(21, 10, 30)) == 0
    finally:
        settings.surge_pause_buffer_minutes = old_buffer


def test_ranges_are_ordered_and_cover_the_next_24h():
    ranges = surge.pause_ranges()
    assert len(ranges) >= 2  # today's remaining window(s) + tomorrow's
    starts = [start for start, _ in ranges]
    assert starts == sorted(starts)
    now = datetime.now().astimezone()
    assert all(end > now for _, end in ranges)


def test_next_peak_window_is_the_first_future_window():
    # Beijing 08:00 == UTC 00:00 — just before the 09:00 morning peak.
    start, end = surge.next_peak_window(_utc(21, 0, 0))
    # Compare in UTC so the machine's local timezone never matters.
    assert start.astimezone(timezone.utc) == _utc(21, 1, 0)  # 09:00 Beijing
    assert end.astimezone(timezone.utc) == _utc(21, 4, 0)  # 12:00 Beijing
    assert surge.format_duration(end - start) == "3h 00m"


def test_next_peak_window_skips_an_active_window():
    # Beijing 10:00 == UTC 02:00 — inside the morning peak; the *next* surge is
    # the afternoon one (14:00-18:00 Beijing).
    start, end = surge.next_peak_window(_utc(21, 2, 0))
    assert start.astimezone(timezone.utc) == _utc(21, 6, 0)  # 14:00 Beijing
    assert end.astimezone(timezone.utc) == _utc(21, 10, 0)  # 18:00 Beijing
    assert surge.format_duration(end - start) == "4h 00m"


def test_clock_pair_has_am_pm_and_24h():
    when = _utc(21, 9, 30).astimezone()  # an instant, rendered in local time
    text = surge.clock(when)
    assert "AM" in text or "PM" in text
    assert text.endswith(f" / {when:%H:%M}")


def test_clock_range_has_both_formats():
    start, end = surge.next_peak_window(_utc(21, 0, 0))
    text = surge.clock_range(start, end)
    assert "AM" in text or "PM" in text
    assert " / " in text
    assert text.endswith(f"/ {start:%H:%M}–{end:%H:%M}")


def test_format_duration():
    assert surge.format_duration(timedelta(hours=3)) == "3h 00m"
    assert surge.format_duration(timedelta(hours=2, minutes=45)) == "2h 45m"
    assert surge.format_duration(timedelta(minutes=12)) == "12m"
    assert surge.format_duration(timedelta()) == "0m"

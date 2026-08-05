"""
The three drift signals, and the limits of each.

The client does not validate responses, so the failure it is most exposed to is
a quiet one: a renamed content field means ``output_text`` returns "" and the
model simply looks silent. These tests check that the three cheap observations
fire on the shapes that matter — and, just as importantly, that they stay quiet
on the shapes that are merely unusual, since a signal that fires on ordinary
turns gets ignored and stops being a signal.

None of these detect a schema change as such. They detect "something moved".
"""

from __future__ import annotations

from conftest import message, response
from g023_code.api import silent_degradation, unknown_item_types
from g023_code.signals import (
    HIT_RATE_ALERT_DROP,
    MIN_BASELINE_DAYS,
    SignalLog,
    hit_rate_verdict,
)

# -- signal 1: item types we do not read ------------------------------------


def test_known_item_types_are_not_flagged():
    payload = response(
        {"type": "reasoning", "summary": []},
        {"type": "function_call", "call_id": "c1", "name": "ReadFile", "arguments": "{}"},
        message("done"),
    )
    assert unknown_item_types(payload) == set()


def test_new_item_type_is_flagged():
    payload = response(message("hi"), {"type": "image_generation_call", "id": "x"})
    assert unknown_item_types(payload) == {"image_generation_call"}


def test_unknown_types_are_counted_but_only_described_once():
    log = SignalLog()
    log.note_unknown_types({"widget_call"}, turn=1)
    log.note_unknown_types({"widget_call"}, turn=2)

    assert log.unknown_types == {"widget_call": 2}
    assert len(log.observations) == 1, "one new type is one thing to say, not one per turn"


# -- signal 2: empty output with no stated reason ----------------------------


def test_normal_reply_is_not_degradation():
    assert silent_degradation(response(message("here you go"))) is None


def test_declared_truncation_is_not_degradation():
    """The model stopping early and saying so is a known state, not a silent one."""
    payload = response(
        message(""),
        status="incomplete",
        incomplete_details={"reason": "max_output_tokens"},
    )
    assert silent_degradation(payload) is None


def test_tool_call_turn_with_no_text_is_not_degradation():
    """A turn that only calls a tool is the normal shape of half this loop."""
    payload = response(
        {"type": "function_call", "call_id": "c1", "name": "SearchContent", "arguments": "{}"}
    )
    assert silent_degradation(payload) is None


def test_empty_message_with_no_reason_is_flagged():
    """What a renamed content field looks like from outside."""
    payload = response(message(""))
    detail = silent_degradation(payload)
    assert detail and "no readable output_text" in detail


def test_renamed_text_field_is_flagged():
    payload = response(
        {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            # 'text' renamed to 'value' — exactly the change nothing else would catch
            "content": [{"type": "output_text", "value": "the real answer"}],
        }
    )
    assert silent_degradation(payload) is not None


def test_empty_output_list_is_not_flagged():
    """No items at all is a transport problem, and shows up as one elsewhere."""
    assert silent_degradation(response()) is None


# -- signal 3: prefix hit rate against its own history -----------------------


def day(name: str, hit: int, miss: int) -> dict:
    return {"day": name, "model": "m", "hit_tokens": hit, "miss_tokens": miss, "calls": 1}


def test_no_history_says_so_rather_than_reporting_zero():
    verdict, latest, baseline = hit_rate_verdict([])
    assert latest is None and baseline is None
    assert "no history" in verdict


def test_one_prior_day_is_not_called_a_baseline():
    verdict, latest, baseline = hit_rate_verdict([day("2026-01-01", 90, 10), day("2026-01-02", 10, 90)])
    assert baseline is None, "one day of history is another session, not a baseline"
    assert latest == 0.1
    assert "not enough" in verdict


def test_steady_hit_rate_reads_as_steady():
    history = [day(f"2026-01-0{i}", 90, 10) for i in range(1, 5)]
    verdict, latest, baseline = hit_rate_verdict(history)
    assert "steady" in verdict
    assert round(latest, 2) == round(baseline, 2) == 0.9


def test_a_collapse_in_hit_rate_is_called_out():
    history = [day(f"2026-01-0{i}", 95, 5) for i in range(1, 4)]
    history.append(day("2026-01-04", 20, 80))

    verdict, latest, baseline = hit_rate_verdict(history)
    assert baseline - latest >= HIT_RATE_ALERT_DROP
    assert "down" in verdict
    # And it must not pretend to know the cause.
    assert "does not say which" in verdict


def test_small_wobble_is_not_an_alert():
    history = [day(f"2026-01-0{i}", 90, 10) for i in range(1, 4)]
    history.append(day("2026-01-04", 82, 18))
    verdict, _, _ = hit_rate_verdict(history)
    assert "steady" in verdict, "day-to-day variation must not read as breakage"


def test_multiple_models_in_a_day_are_pooled():
    history = [
        day("2026-01-01", 50, 50),
        day("2026-01-02", 50, 50),
        {"day": "2026-01-03", "model": "a", "hit_tokens": 90, "miss_tokens": 10, "calls": 1},
        {"day": "2026-01-03", "model": "b", "hit_tokens": 10, "miss_tokens": 90, "calls": 1},
    ]
    _, latest, _ = hit_rate_verdict(history)
    assert latest == 0.5


def test_baseline_threshold_constants_are_documented_values():
    assert MIN_BASELINE_DAYS == 2
    assert 0 < HIT_RATE_ALERT_DROP < 1


# -- persistence -------------------------------------------------------------


def test_hit_rate_history_survives_a_new_cache_handle(isolated_project):
    """Diffing across days only works if the numbers outlive the session."""
    from g023_code import cache as cache_module

    cache_module.get_cache().record_prefix_stats("deepseek-v4-flash", 900, 100)
    cache_module._cache = None  # simulate a later run of the program

    history = cache_module.get_cache().prefix_history()
    assert history and history[0]["hit_tokens"] == 900
    assert history[0]["hit_rate"] == 0.9


def test_repeated_records_accumulate_within_a_day(isolated_project):
    from g023_code import cache as cache_module

    c = cache_module.get_cache()
    c.record_prefix_stats("m", 100, 100)
    c.record_prefix_stats("m", 300, 0)

    row = c.prefix_history()[0]
    assert (row["hit_tokens"], row["miss_tokens"], row["calls"]) == (400, 100, 2)


def test_clearing_the_cache_keeps_drift_history(isolated_project):
    """The stats are a record of what happened, not cached data to be rebuilt."""
    from g023_code import cache as cache_module

    c = cache_module.get_cache()
    c.record_prefix_stats("m", 900, 100)
    c.clear_all()

    assert c.prefix_history(), "clearing cached summaries must not erase the drift baseline"

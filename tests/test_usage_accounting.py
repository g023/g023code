"""
Cost accounting, and the price ratio the documentation quotes.

The 50x figure in the docs is DeepSeek's published price sheet, not a result
this project produced. The only thing this project can be held to is arithmetic:
that it applies those two rates to the right token counts, derives the hit/miss
split the same way whichever endpoint reported it, and does not quietly assume a
cache hit when the provider did not report one.
"""

from __future__ import annotations

from g023_code.usage import PRICING, ModelUsage, UsageTracker, pricing_for
from g023_code.commands import COMMANDS, check_handlers


def test_the_published_ratio_is_the_price_sheet():
    """Where 50x comes from: two numbers DeepSeek publishes, divided."""
    p = PRICING["deepseek-v4-flash"]
    assert round(p.cache_miss / p.cache_hit, 6) == 50.0
    # It is a property of the pricing, not of anything the harness does.


def test_responses_api_spelling_is_understood():
    tracker = UsageTracker()
    delta = tracker.record(
        "deepseek-v4-flash",
        {
            "input_tokens": 10_000,
            "input_tokens_details": {"cached_tokens": 9_500},
            "output_tokens": 200,
        },
    )
    assert (delta.cache_hit_tokens, delta.cache_miss_tokens) == (9_500, 500)
    assert tracker.context_tokens == 10_000


def test_chat_completions_spelling_is_understood():
    tracker = UsageTracker()
    delta = tracker.record(
        "deepseek-v4-flash",
        {
            "prompt_tokens": 10_000,
            "prompt_cache_hit_tokens": 9_500,
            "prompt_cache_miss_tokens": 500,
            "completion_tokens": 200,
        },
    )
    assert (delta.cache_hit_tokens, delta.cache_miss_tokens) == (9_500, 500)


def test_an_unreported_split_is_charged_as_a_full_miss():
    """Assuming a hit nobody reported would understate cost. Assume the worst."""
    tracker = UsageTracker()
    delta = tracker.record("deepseek-v4-flash", {"input_tokens": 10_000, "output_tokens": 10})
    assert delta.cache_hit_tokens == 0
    assert delta.cache_miss_tokens == 10_000


def test_missing_usage_is_zero_not_a_crash():
    assert UsageTracker().record("deepseek-v4-flash", None) == ModelUsage()


def test_cost_is_the_two_rates_applied_to_their_own_token_counts():
    usage = ModelUsage(
        calls=1, cache_hit_tokens=1_000_000, cache_miss_tokens=1_000_000, output_tokens=1_000_000
    )
    p = PRICING["deepseek-v4-flash"]
    assert round(usage.cost("deepseek-v4-flash"), 6) == round(
        p.cache_hit + p.cache_miss + p.output, 6
    )


def test_an_unknown_model_is_priced_as_flash_and_that_is_an_estimate():
    """A cost shown for an unpriced model is a Flash-rate approximation.

    Worth pinning because the number appears in ``/cost`` with no asterisk: if a
    new model is ever added without a price entry, its reported spend is a guess,
    not a bill.
    """
    assert pricing_for("some-other-model") is PRICING["deepseek-v4-flash"]
    assert ModelUsage(calls=1, cache_miss_tokens=1_000_000).cost("some-other-model") == round(
        PRICING["deepseek-v4-flash"].cache_miss, 6
    )


def test_subagent_spend_is_tracked_separately_but_counted_in_the_turn():
    """Delegation moves work to another call; it does not make the work free."""
    tracker = UsageTracker()
    tracker.start_turn()
    tracker.record("deepseek-v4-flash", {"input_tokens": 5_000, "output_tokens": 100})
    tracker.record(
        "deepseek-v4-flash", {"input_tokens": 8_000, "output_tokens": 400}, scope="subagent"
    )

    assert tracker.turn.calls == 2
    assert tracker.turn_cost > 0
    assert tracker.orchestrator["deepseek-v4-flash"].calls == 1
    assert tracker.subagent["deepseek-v4-flash"].calls == 1
    # The subagent's prompt is not this conversation's context.
    assert tracker.context_tokens == 5_000


def test_hit_rate_is_zero_rather_than_undefined_with_no_calls():
    assert ModelUsage().hit_rate() == 0.0


def test_every_slash_command_has_a_handler():
    """A command in the table with no method behind it is a dead menu entry."""

    class Fake:
        pass

    fake = Fake()
    for cmd in COMMANDS:
        setattr(Fake, cmd.handler, lambda self: None)
    assert check_handlers(fake) == []


def test_check_handlers_names_a_missing_one():
    class Empty:
        pass

    missing = check_handlers(Empty())
    assert len(missing) == len(COMMANDS)


def test_command_names_and_aliases_do_not_collide():
    seen: dict[str, str] = {}
    for cmd in COMMANDS:
        for name in [cmd.name, *getattr(cmd, "aliases", [])]:
            assert name not in seen, f"{name} is claimed by both {seen.get(name)} and {cmd.name}"
            seen[name] = cmd.name

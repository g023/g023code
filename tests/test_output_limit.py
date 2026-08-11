"""
The per-reply output budget, which is a setting rather than a literal — and
which is off by default.

``max_tokens`` used to be a fixed 8,192, so a long answer stopped mid-word and
the turn said it had hit "the 8,192-token output limit". That number was this
client's choice, not the model's. Four things are worth pinning: that the
default sends no budget at all (so the model's own maximum is the only ceiling),
that a configured budget is still sent unchanged, that the cut-off notice names
whichever limit actually applied, and that a config file cannot put the budget
somewhere the CLI would refuse to set it.
"""

from __future__ import annotations

import json

import pytest

from g023_code import config as config_module
from g023_code import orchestrator as orchestrator_module
from g023_code.config import (
    MIN_OUTPUT_TOKENS,
    PERSISTED_KEYS,
    Settings,
    UNLIMITED_OUTPUT_TOKENS,
    describe_max_tokens,
    load_config,
    save_config,
)
from g023_code.orchestrator import Orchestrator, OrchestratorState, Reply


def make_orchestrator() -> Orchestrator:
    orc = Orchestrator()  # no client is built until a call is made
    orc.state = OrchestratorState()
    return orc


# ---------------------------------------------------------------------------
# What goes on the wire
# ---------------------------------------------------------------------------


def test_the_default_sends_no_output_budget(monkeypatch):
    """The field is omitted, not sent as 0 — 0 would ask for an empty reply."""
    monkeypatch.setattr(
        config_module.settings, "max_tokens", UNLIMITED_OUTPUT_TOKENS
    )
    assert "max_output_tokens" not in make_orchestrator()._request_body()


@pytest.mark.parametrize("budget", [MIN_OUTPUT_TOKENS, 8192, 64_000, 1_000_000])
def test_a_configured_budget_is_sent_unchanged(monkeypatch, budget):
    monkeypatch.setattr(config_module.settings, "max_tokens", budget)
    assert make_orchestrator()._request_body()["max_output_tokens"] == budget


def test_the_shipped_default_is_unlimited():
    assert Settings().max_tokens == UNLIMITED_OUTPUT_TOKENS


# ---------------------------------------------------------------------------
# What the user is told when a reply is cut off anyway
# ---------------------------------------------------------------------------


async def run_truncated_turn(orc: Orchestrator, monkeypatch) -> str:
    """One turn whose single reply comes back cut off at the output limit."""
    printed: list[str] = []
    monkeypatch.setattr(
        orchestrator_module.console,
        "print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )

    async def fake_call_model(iteration: int, limit: int = 0) -> Reply:
        return Reply(
            content="A sentence that stops mid",
            status="incomplete",
            incomplete_reason="max_output_tokens",
        )

    monkeypatch.setattr(orc, "_call_model", fake_call_model)
    await orc.run_turn("write something long")
    return "\n".join(printed)


@pytest.mark.asyncio
async def test_an_unlimited_run_blames_the_model_not_a_number(monkeypatch):
    """With no budget sent, there is no client-side number to cite or to raise."""
    monkeypatch.setattr(
        config_module.settings, "max_tokens", UNLIMITED_OUTPUT_TOKENS
    )
    out = await run_truncated_turn(make_orchestrator(), monkeypatch)

    assert "cut off" in out
    assert "model's own output limit" in out
    assert "0-token" not in out


@pytest.mark.asyncio
async def test_a_capped_run_names_the_cap_and_how_to_raise_it(monkeypatch):
    monkeypatch.setattr(config_module.settings, "max_tokens", 8192)
    out = await run_truncated_turn(make_orchestrator(), monkeypatch)

    assert "8,192-token output limit" in out
    assert "/max-tokens" in out


@pytest.mark.asyncio
async def test_a_complete_reply_says_nothing_about_limits(monkeypatch):
    """The notice is for truncation only, at either setting."""
    monkeypatch.setattr(
        config_module.settings, "max_tokens", UNLIMITED_OUTPUT_TOKENS
    )
    orc = make_orchestrator()
    printed: list[str] = []
    monkeypatch.setattr(
        orchestrator_module.console,
        "print",
        lambda *args, **kwargs: printed.append(" ".join(str(a) for a in args)),
    )

    async def fake_call_model(iteration: int, limit: int = 0) -> Reply:
        return Reply(content="done")

    monkeypatch.setattr(orc, "_call_model", fake_call_model)
    await orc.run_turn("hello")

    assert "cut off" not in "\n".join(printed)


def test_unlimited_reads_as_words_not_a_zero():
    assert describe_max_tokens(UNLIMITED_OUTPUT_TOKENS) == "unlimited (model maximum)"
    assert describe_max_tokens(64_000) == "64,000"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_the_budget_persists_to_config_json(isolated_project):
    assert "max_tokens" in PERSISTED_KEYS

    target = Settings()
    target.max_tokens = 32_000
    path = save_config(target)
    assert json.loads(path.read_text())["max_tokens"] == 32_000

    reloaded = Settings()
    load_config(reloaded)
    assert reloaded.max_tokens == 32_000


def test_an_unlimited_budget_survives_a_round_trip(isolated_project):
    """0 is a real setting, not a missing one — it must not be coerced away."""
    target = Settings()
    target.max_tokens = 4096
    save_config(target)

    target.max_tokens = UNLIMITED_OUTPUT_TOKENS
    save_config(target)

    reloaded = Settings()
    reloaded.max_tokens = 4096
    load_config(reloaded)
    assert reloaded.max_tokens == UNLIMITED_OUTPUT_TOKENS


@pytest.mark.parametrize("bad", [-1, "lots", True, None])
def test_a_meaningless_config_value_is_ignored(isolated_project, bad):
    """A hand-edited config cannot set a budget the CLI would reject."""
    config_module.get_config_file().write_text(json.dumps({"max_tokens": bad}))

    target = Settings()
    load_config(target)

    assert target.max_tokens == Settings().max_tokens


@pytest.mark.parametrize(
    "good", [UNLIMITED_OUTPUT_TOKENS, MIN_OUTPUT_TOKENS, 8192, 128_000, 1_000_000]
)
def test_any_non_negative_config_value_is_applied(isolated_project, good):
    """There is no upper bound left to reject a large-but-deliberate budget."""
    config_module.get_config_file().write_text(json.dumps({"max_tokens": good}))

    target = Settings()
    target.max_tokens = 4096  # something the file must overwrite either way
    load_config(target)

    assert target.max_tokens == good

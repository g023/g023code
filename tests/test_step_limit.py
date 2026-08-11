"""
The per-turn step cap, which is a setting rather than a literal — and which can
be turned off entirely.

A turn ends when the model answers instead of calling tools. A model that never
stops calling tools would otherwise loop forever, so the loop counts its own
model calls and gives up at ``settings.max_iterations``. Five things are worth
pinning: that a cap really spends that many calls and no more, that
``max_iterations = 0`` means no cap at all, that an uncapped run says where it
is as it goes rather than running silently, that a config file cannot put the
cap somewhere the CLI would refuse to set it, and that /goal — the one command
whose point is "however many steps it takes" — lifts the cap for its own turn
and puts the setting back afterwards.
"""

from __future__ import annotations

import json

import pytest

from g023_code import config as config_module
from g023_code.config import (
    ITERATION_NOTICE_EVERY,
    MIN_ITERATIONS,
    PERSISTED_KEYS,
    Settings,
    UNLIMITED_ITERATIONS,
    describe_steps,
    load_config,
    save_config,
)
from g023_code.orchestrator import Orchestrator, OrchestratorState, Reply, ToolCall


def make_orchestrator() -> Orchestrator:
    orc = Orchestrator()  # no client is built until a call is made
    orc.state = OrchestratorState()
    return orc


def tool_call_reply() -> Reply:
    """A reply that keeps the loop going: one tool call, no answer."""
    return Reply(
        content="",
        tool_calls=[ToolCall(id="c1", name="SearchContent", arguments="{}")],
    )


async def run_capped_turn(orc: Orchestrator, monkeypatch) -> tuple[str, list[int]]:
    """Run one turn where the model only ever asks for tools. Returns (answer, steps)."""
    steps: list[int] = []

    async def fake_call_model(iteration: int, limit: int = 0) -> Reply:
        steps.append(iteration)
        return tool_call_reply()

    async def fake_run_tool_calls(calls) -> None:
        return None

    monkeypatch.setattr(orc, "_call_model", fake_call_model)
    monkeypatch.setattr(orc, "_run_tool_calls", fake_run_tool_calls)
    answer = await orc.run_turn("go")
    return answer, steps


async def run_turn_answering_at(
    orc: Orchestrator, monkeypatch, answer_step: int
) -> tuple[str, list[int]]:
    """A model that tools its way to ``answer_step`` and only then answers."""
    steps: list[int] = []

    async def fake_call_model(iteration: int, limit: int = 0) -> Reply:
        steps.append(iteration)
        if iteration >= answer_step:
            return Reply(content="done")
        return tool_call_reply()

    async def fake_run_tool_calls(calls) -> None:
        return None

    monkeypatch.setattr(orc, "_call_model", fake_call_model)
    monkeypatch.setattr(orc, "_run_tool_calls", fake_run_tool_calls)
    answer = await orc.run_turn("go")
    return answer, steps


@pytest.mark.asyncio
async def test_loop_spends_exactly_max_iterations_model_calls(monkeypatch):
    monkeypatch.setattr(config_module.settings, "max_iterations", 7)
    orc = make_orchestrator()

    answer, steps = await run_capped_turn(orc, monkeypatch)

    assert steps == list(range(1, 8))
    assert "Stopped after 7 tool iterations" in answer


@pytest.mark.asyncio
async def test_changing_the_setting_changes_the_cap(monkeypatch):
    """The literal is gone: the loop reads the setting on every turn."""
    monkeypatch.setattr(config_module.settings, "max_iterations", 3)
    orc = make_orchestrator()
    _, first = await run_capped_turn(orc, monkeypatch)

    monkeypatch.setattr(config_module.settings, "max_iterations", 11)
    _, second = await run_capped_turn(orc, monkeypatch)

    assert len(first) == 3
    assert len(second) == 11


# ---------------------------------------------------------------------------
# No cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_zero_means_the_loop_runs_past_any_cap(monkeypatch):
    """0 is not "one step" or "no steps": it is "as many as the answer takes"."""
    monkeypatch.setattr(
        config_module.settings, "max_iterations", UNLIMITED_ITERATIONS
    )
    orc = make_orchestrator()

    answer, steps = await run_turn_answering_at(orc, monkeypatch, answer_step=140)

    assert steps == list(range(1, 141))
    assert answer == "done"
    assert "Stopped after" not in answer


@pytest.mark.asyncio
async def test_an_uncapped_turn_reports_progress_as_it_goes(monkeypatch):
    """
    The reason there can be no ceiling: the run is watched instead of bounded.

    The notice is printed by the loop itself, not by the verbosity-gated trace,
    so it is asserted here as the loop's own output.
    """
    monkeypatch.setattr(
        config_module.settings, "max_iterations", UNLIMITED_ITERATIONS
    )
    monkeypatch.setattr(config_module.settings, "verbose", "low")
    orc = make_orchestrator()

    notices: list[tuple[int, int]] = []
    monkeypatch.setattr(
        orc.printer,
        "step_notice",
        lambda n, limit, elapsed_ms, cost, tools=0: notices.append((n, limit)),
    )

    await run_turn_answering_at(orc, monkeypatch, answer_step=35)

    counts = [n for n, _ in notices]
    assert counts == list(
        range(ITERATION_NOTICE_EVERY, 35, ITERATION_NOTICE_EVERY)
    )
    # The notice carries the cap so it can say "step 20/100" or just "step 20".
    assert {limit for _, limit in notices} == {UNLIMITED_ITERATIONS}


@pytest.mark.asyncio
async def test_a_capped_turn_reports_progress_too(monkeypatch):
    monkeypatch.setattr(config_module.settings, "max_iterations", 25)
    orc = make_orchestrator()

    notices: list[tuple[int, int]] = []
    monkeypatch.setattr(
        orc.printer,
        "step_notice",
        lambda n, limit, elapsed_ms, cost, tools=0: notices.append((n, limit)),
    )

    await run_capped_turn(orc, monkeypatch)

    assert [n for n, _ in notices] == list(
        range(ITERATION_NOTICE_EVERY, 26, ITERATION_NOTICE_EVERY)
    )
    assert {limit for _, limit in notices} == {25}


def test_unlimited_reads_as_a_word_not_a_zero():
    assert describe_steps(UNLIMITED_ITERATIONS) == "unlimited"
    assert describe_steps(100) == "100"


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_default_is_two_hundred_and_fifty_five():
    assert Settings().max_iterations == 255


def test_the_cap_persists_to_config_json(isolated_project):
    assert "max_iterations" in PERSISTED_KEYS

    target = Settings()
    target.max_iterations = 250
    path = save_config(target)
    assert json.loads(path.read_text())["max_iterations"] == 250

    reloaded = Settings()
    load_config(reloaded)
    assert reloaded.max_iterations == 250


@pytest.mark.parametrize("bad", [-5, "many", True, None])
def test_a_meaningless_config_value_is_ignored(isolated_project, bad):
    """A hand-edited config cannot set a cap the CLI would reject."""
    path = config_module.get_config_file()
    path.write_text(json.dumps({"max_iterations": bad}))

    target = Settings()
    load_config(target)

    assert target.max_iterations == Settings().max_iterations


@pytest.mark.parametrize(
    "good", [UNLIMITED_ITERATIONS, MIN_ITERATIONS, 25, 100, 1000, 25_000]
)
def test_any_non_negative_config_value_is_applied(isolated_project, good):
    """There is no upper bound left to reject a large-but-deliberate count."""
    path = config_module.get_config_file()
    path.write_text(json.dumps({"max_iterations": good}))

    target = Settings()
    load_config(target)

    assert target.max_iterations == good


# ---------------------------------------------------------------------------
# /goal
# ---------------------------------------------------------------------------


def make_cli():
    from g023_code.cli import CLI

    return CLI()


@pytest.mark.asyncio
async def test_goal_runs_its_turn_with_no_step_cap(monkeypatch):
    """A goal is not allowed to end in 'Stopped after N tool iterations'."""
    monkeypatch.setattr(config_module.settings, "max_iterations", 5)
    cli = make_cli()

    seen: list[int] = []

    async def fake_run_prompt(text, title="g023"):
        seen.append(config_module.settings.max_iterations)

    monkeypatch.setattr(cli, "run_prompt", fake_run_prompt)
    await cli.cmd_goal("ship the thing")

    assert seen == [UNLIMITED_ITERATIONS]


@pytest.mark.asyncio
async def test_goal_restores_the_saved_cap_afterwards(monkeypatch):
    """Lifting the cap is for the goal's turn only — /steps is not rewritten."""
    monkeypatch.setattr(config_module.settings, "max_iterations", 5)
    cli = make_cli()

    async def boom(text, title="g023"):
        raise RuntimeError("turn blew up")

    monkeypatch.setattr(cli, "run_prompt", boom)
    with pytest.raises(RuntimeError):
        await cli.cmd_goal("ship the thing")

    # Restored even when the turn raises, since a lost cap would silently
    # outlive the goal and apply to every later turn.
    assert config_module.settings.max_iterations == 5


@pytest.mark.asyncio
async def test_goal_with_no_objective_changes_nothing(monkeypatch):
    monkeypatch.setattr(config_module.settings, "max_iterations", 5)
    cli = make_cli()

    async def unreached(text, title="g023"):
        raise AssertionError("no objective — nothing should run")

    monkeypatch.setattr(cli, "run_prompt", unreached)
    await cli.cmd_goal("")

    assert config_module.settings.max_iterations == 5

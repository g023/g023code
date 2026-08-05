"""
Tool-call pairing, which the API enforces in both directions.

A ``function_call`` left in history with no matching ``function_call_output`` is
a 400, and so is an output whose call was never made. The consequence is not one
bad turn: the broken history is sent again on the next turn, so the session is
dead until the pair is repaired. Anything that edits history has to leave the
pairing intact, and these tests pin that down for the two editors that exist.
"""

from __future__ import annotations

from conftest import message
from g023_code.orchestrator import OrchestratorState, Orchestrator, parse_reply


def call(call_id: str, name: str = "ReadFile") -> dict:
    return {"type": "function_call", "call_id": call_id, "name": name, "arguments": "{}"}


def unpaired(items: list[dict]) -> set:
    called = {i.get("call_id") for i in items if i.get("type") == "function_call"}
    answered = {i.get("call_id") for i in items if i.get("type") == "function_call_output"}
    return called.symmetric_difference(answered)


def make_orchestrator(items: list[dict]) -> Orchestrator:
    orc = Orchestrator.__new__(Orchestrator)  # no client, no key, no network
    orc.state = OrchestratorState(items=list(items))
    return orc


def test_a_call_with_no_result_is_dropped():
    orc = make_orchestrator([{"role": "user", "content": "hi"}, call("c1")])
    orc._repair_pairing()

    assert unpaired(orc.state.items) == set()
    assert orc.state.items == [{"role": "user", "content": "hi"}]


def test_a_result_with_no_call_is_dropped():
    orc = make_orchestrator(
        [
            {"role": "user", "content": "hi"},
            {"type": "function_call_output", "call_id": "ghost", "output": "{}"},
        ]
    )
    orc._repair_pairing()
    assert unpaired(orc.state.items) == set()


def test_complete_pairs_are_left_alone():
    items = [
        {"role": "user", "content": "hi"},
        call("c1"),
        {"type": "function_call_output", "call_id": "c1", "output": "{}"},
        message("done"),
    ]
    orc = make_orchestrator(items)
    orc._repair_pairing()
    assert orc.state.items == items


def test_repair_keeps_the_pairs_around_the_orphan():
    """One broken call must not take the finished work of the turn with it."""
    orc = make_orchestrator(
        [
            {"role": "user", "content": "hi"},
            call("c1"),
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            call("c2"),  # the turn died here
        ]
    )
    orc._repair_pairing()

    assert unpaired(orc.state.items) == set()
    assert any(i.get("call_id") == "c1" for i in orc.state.items)
    assert not any(i.get("call_id") == "c2" for i in orc.state.items)


def test_rollback_after_a_failed_turn_leaves_valid_history():
    """The state after an API failure has to be sendable, not just smaller."""
    orc = make_orchestrator(
        [
            {"role": "user", "content": "first"},
            call("c1"),
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
            message("answered"),
            {"role": "user", "content": "second"},
            call("c2"),
        ]
    )
    orc._rollback_to_last_complete()

    assert unpaired(orc.state.items) == set()
    assert {i.get("content") for i in orc.state.items if i.get("role") == "user"} == {"first"}
    assert any(i.get("type") == "message" for i in orc.state.items)


def test_rollback_on_an_empty_history_is_a_no_op():
    orc = make_orchestrator([])
    orc._rollback_to_last_complete()
    assert orc.state.items == []


def test_parsed_output_is_echoed_back_byte_identical():
    """Prefix caching depends on the earlier items being resent unchanged.

    Reconstructing items from parsed fields would round-trip differently and
    break the prefix on every turn, so history stores the server's own dicts.
    """
    output = [
        {"type": "reasoning", "id": "r1", "content": [{"type": "reasoning_text", "text": "hm"}]},
        call("c1"),
        message("done"),
    ]
    reply = parse_reply({"status": "completed", "output": output})

    state = OrchestratorState()
    state.add_output(reply.output)
    assert state.items == output
    assert all(a is b for a, b in zip(state.items, output)), "items must not be rebuilt"


def test_unknown_items_stay_in_history_even_though_nothing_reads_them():
    """An item type we do not understand is still part of the model's own prefix."""
    output = [{"type": "widget_call", "id": "w1", "payload": {"a": 1}}, message("done")]
    reply = parse_reply({"status": "completed", "output": output})

    state = OrchestratorState()
    state.add_output(reply.output)

    assert output[0] in state.items
    assert reply.unknown_types == {"widget_call"}
    assert reply.content == "done"

"""
FileReader behaviour: the symbol map, range reads, and saying what was covered.

The summary path's known weakness is that nothing forces an escalation when the
summary drops what was needed. There is no detector for that, and these tests do
not pretend there is one. What they do check is that the escalation path exists,
is exact, and that its inputs (the line ranges) are ground truth rather than the
model's guess — which is what makes escalation a single targeted read instead of
a hunt.
"""

from __future__ import annotations

import json

from conftest import CountingClient
from g023_code.subagents.file_reader import (
    LLM_INPUT_MAX_CHARS,
    RANGE_MAX_CHARS,
    _local_python_metadata,
    run_file_reader,
)

DECORATED = '''import functools


@functools.cache
def cached_thing(x):
    """Doc."""
    return x * 2


class Holder:

    @property
    def value(self):
        return 42

    async def fetch(self, url):
        return url
'''


def test_symbol_map_gives_exact_line_ranges():
    meta = _local_python_metadata(DECORATED)
    symbols = meta["symbols"]

    lines = DECORATED.splitlines()
    # The decorator line, not the def line, is where the definition starts.
    assert lines[symbols["cached_thing"][0] - 1].strip() == "@functools.cache"
    assert lines[symbols["cached_thing"][1] - 1].strip() == "return x * 2"

    assert lines[symbols["Holder.value"][0] - 1].strip() == "@property"
    assert lines[symbols["Holder.fetch"][0] - 1].strip() == "async def fetch(self, url):"
    assert symbols["Holder"][0] < symbols["Holder.value"][0]
    assert symbols["Holder"][1] >= symbols["Holder.fetch"][1]


def test_every_symbol_range_is_inside_the_file():
    meta = _local_python_metadata(DECORATED)
    total = DECORATED.count("\n") + 1
    for name, (start, end) in meta["symbols"].items():
        assert 1 <= start <= end <= total, name


def test_unparseable_python_says_so_rather_than_reporting_nothing():
    meta = _local_python_metadata("def broken(:\n    pass\n")
    assert meta["parse_error"], "a syntax error must not read as 'no classes, no functions'"
    assert meta["classes"] == []


async def test_symbol_range_round_trips_to_the_real_source(isolated_project):
    """The point of the map: name → range → exact bytes, with no guessing."""
    path = isolated_project / "mod.py"
    path.write_text(DECORATED)
    client = CountingClient()

    summary = json.loads(await run_file_reader(path=str(path), client=client))
    start, end = summary["metadata"]["symbols"]["Holder.fetch"]

    body = json.loads(
        await run_file_reader(path=str(path), start_line=start, end_line=end, client=client)
    )

    assert client.call_count == 0
    assert body["content"] == "    async def fetch(self, url):\n        return url"


async def test_summary_points_at_the_escalation_path(isolated_project):
    path = isolated_project / "mod.py"
    path.write_text(DECORATED)

    result = json.loads(await run_file_reader(path=str(path), client=CountingClient()))
    assert "start_line" in result["note"] or "symbols" in result["note"]


async def test_range_beyond_the_end_of_file_is_clamped(isolated_project):
    path = isolated_project / "short.txt"
    path.write_text("one\ntwo\nthree\n")

    result = json.loads(
        await run_file_reader(path=str(path), start_line=2, end_line=9999, client=CountingClient())
    )
    assert result["start_line"] == 2
    assert result["end_line"] == 3
    assert result["content"] == "two\nthree"


async def test_oversized_range_reports_the_line_it_actually_reached(isolated_project):
    """A truncated range must not claim the end line it was asked for.

    Reporting the requested end for a body that stopped short is how a caller
    comes to believe it has seen a whole definition when it has seen the top of
    one.
    """
    path = isolated_project / "huge.txt"
    line = "x" * 200
    path.write_text("\n".join(f"{i}:{line}" for i in range(1, 400)))

    result = json.loads(
        await run_file_reader(path=str(path), start_line=1, end_line=399, client=CountingClient())
    )

    assert result["truncated"]
    assert result["end_line"] < 399
    assert len(result["content"]) <= RANGE_MAX_CHARS
    # The reported end line is the last one genuinely present in the content.
    assert result["content"].splitlines()[-1].startswith(f"{result['end_line']}:")


async def test_partial_summary_says_it_is_partial(isolated_project):
    """A summary of the first 18k chars must not read as a summary of the file."""
    path = isolated_project / "enormous.md"
    path.write_text("# Doc\n" + ("word " * (LLM_INPUT_MAX_CHARS // 2)))

    client = CountingClient()
    result = json.loads(await run_file_reader(path=str(path), client=client))

    assert result["summary_covers"]["complete"] is False
    assert result["summary_covers"]["chars_summarised"] == LLM_INPUT_MAX_CHARS
    assert "first" in result["note"]
    # The model is told too, not just the caller.
    assert "TRUNCATED" in client.calls[0]["input"][0]["content"]


async def test_local_ast_facts_survive_a_model_that_contradicts_them(isolated_project):
    """Line numbers the model did not compute are guesses; measured ones win."""
    path = isolated_project / "focused.py"
    path.write_text(DECORATED)

    client = CountingClient(
        response={
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "metadata": {
                                        "language": "python",
                                        "symbols": {"Holder.fetch": [999, 1000]},
                                        "invented": "field",
                                    },
                                    "summary": "Focused summary.",
                                }
                            ),
                        }
                    ],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
    )

    result = json.loads(await run_file_reader(path=str(path), focus="fetch", client=client))
    meta = result["metadata"]

    assert meta["symbols"]["Holder.fetch"] != [999, 1000]
    assert meta["symbols"]["Holder.fetch"][1] <= DECORATED.count("\n") + 1
    # Fields only the model supplied are still carried through.
    assert meta["invented"] == "field"


async def test_truncated_model_output_is_reported_not_presented_as_a_summary(isolated_project):
    path = isolated_project / "app.ts"
    path.write_text("export const x = 1\n")

    client = CountingClient(
        response={
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": "This file def"}],
                }
            ],
            "usage": {"input_tokens": 10, "output_tokens": 800},
        }
    )

    result = json.loads(await run_file_reader(path=str(path), client=client))
    assert "truncated" in result["summary"]
    assert "max_output_tokens" in result["summary"]


async def test_api_failure_falls_back_locally_and_says_so(isolated_project):
    class Failing:
        async def create(self, **body):
            raise RuntimeError("connection reset")

    path = isolated_project / "app.ts"
    path.write_text("export const x = 1\n")

    result = json.loads(await run_file_reader(path=str(path), client=Failing()))
    assert result["summary_source"] == "local_fallback"
    assert "connection reset" in result["summary"]


async def test_missing_file_is_an_error_not_an_empty_summary(isolated_project):
    result = json.loads(await run_file_reader(path=str(isolated_project / "nope.py")))
    assert "error" in result

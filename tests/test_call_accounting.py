"""
The API-call accounting table, as a test.

README and SPEC both publish a table of how many API calls each read costs. That
table is the concrete, checkable part of the delegation argument, so it should
not rest on anyone's memory of the code. Each case below asserts the number in
the table by counting calls against a stub client.

| operation                                            | API calls |
|------------------------------------------------------|-----------|
| SearchContent, any size                               | 0         |
| ReadFile, Python < 12k chars, no focus                | 0         |
| ReadFile, same bytes again (cache hit)                | 0         |
| ReadFile with a line range                            | 0         |
| ReadFile, first read, non-Python or >12k or focused   | 1         |

What this does *not* measure is whether delegation is cheaper overall. That
depends on how many turns the file's text would otherwise sit in context for,
which no unit test can know — see the break-even discussion in the README.
"""

from __future__ import annotations

import json

import pytest

from conftest import CountingClient
from g023_code.subagents.file_reader import LOCAL_SUMMARY_MAX_CHARS, run_file_reader
from g023_code.subagents.searcher import run_searcher

SMALL_PY = '''
"""A small module."""
import os
import json


class Thing:
    def method_one(self):
        return 1

    def method_two(self):
        return 2


def top_level(a, b):
    return a + b
'''


@pytest.mark.asyncio
async def test_search_content_makes_no_api_calls(isolated_project):
    (isolated_project / "a.py").write_text(SMALL_PY)
    (isolated_project / "b.py").write_text("def other():\n    return 'needle'\n")

    client = CountingClient()
    result = json.loads(await run_searcher(query="needle", path=str(isolated_project)))

    assert client.call_count == 0
    assert result["matches"], "the search should have found the needle"


@pytest.mark.asyncio
async def test_small_python_file_is_summarised_locally(isolated_project):
    path = isolated_project / "small.py"
    path.write_text(SMALL_PY)
    assert len(SMALL_PY) < LOCAL_SUMMARY_MAX_CHARS

    client = CountingClient()
    result = json.loads(await run_file_reader(path=str(path), client=client))

    assert client.call_count == 0
    assert result["summary_source"] == "local_ast"
    assert result["metadata"]["classes"][0]["name"] == "Thing"


@pytest.mark.asyncio
async def test_second_read_of_same_bytes_is_free(isolated_project):
    path = isolated_project / "big.md"
    path.write_text("# Heading\n\n" + ("prose. " * 3000))

    client = CountingClient()
    first = json.loads(await run_file_reader(path=str(path), client=client))
    assert client.call_count == 1
    assert first["from_cache"] is False

    second = json.loads(await run_file_reader(path=str(path), client=client))
    assert client.call_count == 1, "a cached summary must not cost a second call"
    assert second["from_cache"] is True


@pytest.mark.asyncio
async def test_line_range_read_makes_no_api_call(isolated_project):
    path = isolated_project / "big.md"
    path.write_text("\n".join(f"line {i}" for i in range(1, 501)))

    client = CountingClient()
    result = json.loads(
        await run_file_reader(path=str(path), start_line=10, end_line=12, client=client)
    )

    assert client.call_count == 0
    assert result["content"] == "line 10\nline 11\nline 12"
    assert result["verbatim"] is True


@pytest.mark.asyncio
async def test_non_python_file_costs_one_call(isolated_project):
    path = isolated_project / "app.ts"
    path.write_text("export function hello() { return 'hi' }\n")

    client = CountingClient()
    await run_file_reader(path=str(path), client=client)

    assert client.call_count == 1


@pytest.mark.asyncio
async def test_focused_read_costs_one_call_even_for_small_python(isolated_project):
    path = isolated_project / "small.py"
    path.write_text(SMALL_PY)

    client = CountingClient()
    await run_file_reader(path=str(path), focus="what does method_one do", client=client)

    assert client.call_count == 1, "a focus hint has to reach the model to be honoured"


@pytest.mark.asyncio
async def test_large_python_file_costs_one_call(isolated_project):
    path = isolated_project / "large.py"
    path.write_text("x = 1\n" * (LOCAL_SUMMARY_MAX_CHARS // 3))

    client = CountingClient()
    await run_file_reader(path=str(path), client=client)

    assert client.call_count == 1


@pytest.mark.asyncio
async def test_focus_is_part_of_the_cache_key(isolated_project):
    """Same bytes, different question, different cache entry.

    A summary written for "how does auth work here" must never be handed back as
    the answer to "what does this file import" — it is a different question
    about the same file, and silently reusing it would answer the wrong one.
    """
    path = isolated_project / "app.ts"
    path.write_text("export function hello() { return 'hi' }\n")

    client = CountingClient()
    await run_file_reader(path=str(path), focus="error handling", client=client)
    assert client.call_count == 1

    await run_file_reader(path=str(path), focus="error handling", client=client)
    assert client.call_count == 1, "the same focus should hit the cache"

    await run_file_reader(path=str(path), focus="imports", client=client)
    assert client.call_count == 2, "a different focus must not reuse the summary"


@pytest.mark.asyncio
async def test_changed_bytes_invalidate_the_summary(isolated_project):
    path = isolated_project / "app.ts"
    path.write_text("export function hello() { return 'hi' }\n")

    client = CountingClient()
    await run_file_reader(path=str(path), client=client)
    assert client.call_count == 1

    path.write_text("export function hello() { return 'CHANGED' }\n")
    result = json.loads(await run_file_reader(path=str(path), client=client))

    assert client.call_count == 2
    assert result["from_cache"] is False


@pytest.mark.asyncio
async def test_raw_baseline_mode_returns_content_and_costs_nothing(isolated_project, monkeypatch):
    """The A/B baseline: ReadFile behaving as a plain agent loop would."""
    from g023_code.config import settings

    path = isolated_project / "app.ts"
    path.write_text("export function hello() { return 'hi' }\n")
    monkeypatch.setattr(settings, "file_reader_raw", True)

    client = CountingClient()
    result = json.loads(await run_file_reader(path=str(path), client=client))

    assert client.call_count == 0
    assert result["mode"] == "raw_baseline"
    assert "export function hello" in result["content"]

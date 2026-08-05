"""
Shared fixtures.

Every test in this suite runs with no API key and no network. Where a test needs
the API, it substitutes a client that counts calls — which is the point: the
claims worth testing here are about *how often we call the API*, and a counting
stub measures that exactly.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# Async support without pytest-asyncio. The suite has to be runnable with
# nothing but `pip install pytest` on top of the project's own requirements —
# a test suite that needs its own install story tends not to get run.
def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run this coroutine test on a fresh event loop")


def pytest_pyfunc_call(pyfuncitem):
    if not inspect.iscoroutinefunction(pyfuncitem.obj):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(pyfuncitem.obj(**kwargs))
    return True


@pytest.fixture(autouse=True)
def isolated_project(tmp_path, monkeypatch):
    """Point the project root, cache DB and config at a throwaway directory.

    Without this the suite would read the developer's own ``.g023/cache.db`` and
    a passing run would depend on what happened to be cached.
    """
    monkeypatch.setenv("G023_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("G023_HOME", str(tmp_path))
    monkeypatch.delenv("G023_READFILE_RAW", raising=False)

    from g023_code import cache as cache_module
    from g023_code import config, signals

    cache_module._cache = None
    signals._log = None
    monkeypatch.setattr(config.settings, "project_root", tmp_path)
    monkeypatch.setattr(config.settings, "file_reader_raw", False)
    yield tmp_path
    cache_module._cache = None
    signals._log = None


class CountingClient:
    """A stand-in for ResponsesClient that records every request it is given."""

    def __init__(self, response=None):
        self.calls: list[dict] = []
        self._response = response or {
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"metadata": {"language": "text"}, '
                            '"summary": "A stub summary.", "key_snippets": []}',
                        }
                    ],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }

    async def create(self, **body):
        self.calls.append(body)
        return self._response

    @property
    def call_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def counting_client():
    return CountingClient


def message(text: str, phase: str = "final_answer", item_id: str = "msg_1") -> dict:
    return {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "phase": phase,
        "content": [{"type": "output_text", "text": text}],
    }


def response(*items, status: str = "completed", **extra) -> dict:
    payload = {"status": status, "output": list(items)}
    payload.update(extra)
    return payload

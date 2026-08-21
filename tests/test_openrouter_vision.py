"""
OpenRouter vision client — model failover on rate limits, daily quota.

The behaviour worth locking down is the failover: when one :free model returns
HTTP 429, the next model on the OR_VISION.json list is tried, and when every
model is rate-limited the caller gets one clear error naming the tier (free =
50 req/24h, $10+ credits = 1000 req/24h) instead of a raw exception.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from g023_code import openrouter_client as orc
from g023_code.config import settings


def _ok_response(answer: str):
    class Headers(dict):
        def get(self, key, default=None):  # dict.get covers header lookups
            return super().get(key.lower(), default)

    class Resp:
        status_code = 200
        headers = Headers({"x-ratelimit-remaining": "49", "x-ratelimit-limit": "50"})
        text = json.dumps({"choices": [{"message": {"content": answer}}], "usage": {}})

        def json(self):
            return json.loads(self.text)

    return Resp()


def _rate_limited_response():
    class Resp:
        status_code = 429
        headers = {"retry-after": "60"}
        text = "Rate limit exceeded"

        def json(self):
            return {}

    return Resp()


@pytest.fixture(autouse=True)
def ok_dat(tmp_path, monkeypatch):
    (tmp_path / "OK.dat").write_text("sk-or-v1-test-key\n", encoding="utf-8")
    monkeypatch.setattr(settings, "or_credits", False)
    monkeypatch.setattr(settings, "or_vision_models", None)
    yield


def test_load_or_models_reads_project_file(tmp_path):
    (tmp_path / "OR_VISION.json").write_text(
        json.dumps(["model-a:free", "model-b:free", "model-c:free"]), encoding="utf-8"
    )
    assert orc.load_or_models() == ["model-a:free", "model-b:free", "model-c:free"]


def test_load_or_models_falls_back_to_default_when_missing(tmp_path):
    assert orc.load_or_models() == [settings.or_default_model]


def test_load_or_models_ignores_broken_file(tmp_path):
    (tmp_path / "OR_VISION.json").write_text("not json", encoding="utf-8")
    assert orc.load_or_models() == [settings.or_default_model]


def test_failover_skips_rate_limited_models(monkeypatch):
    calls: list[str] = []

    async def fake_chat(model, prompt, image_b64, system, timeout, key=None):
        calls.append(model)
        if model == "model-a:free":
            raise orc.OrRateLimitError("429 on a")
        return orc.OrVisionResult(answer="seen it", model=model)

    monkeypatch.setattr(orc, "or_vision_chat", fake_chat)
    monkeypatch.setattr(settings, "or_vision_models", ["model-a:free", "model-b:free"])

    result, attempts = asyncio.run(
        orc.or_vision_with_failover(prompt="q", image_b64="AAA", system="s")
    )
    assert calls == ["model-a:free", "model-b:free"]
    assert attempts == ["model-a:free", "model-b:free"]
    assert result.answer == "seen it"
    assert result.model == "model-b:free"


def test_all_models_rate_limited_raises_with_tier_hint(monkeypatch):
    async def fake_chat(model, prompt, image_b64, system, timeout, key=None):
        raise orc.OrRateLimitError(f"429 on {model}", retry_after=60)

    monkeypatch.setattr(orc, "or_vision_chat", fake_chat)
    monkeypatch.setattr(settings, "or_vision_models", ["a:free", "b:free"])

    with pytest.raises(orc.OrRateLimitError) as exc:
        asyncio.run(orc.or_vision_with_failover(prompt="q", image_b64="AAA", system="s"))
    assert "50/day" in str(exc.value)
    assert "a:free" in str(exc.value) and "b:free" in str(exc.value)


def test_all_rate_limited_credits_tier_says_1000(monkeypatch):
    async def fake_chat(model, prompt, image_b64, system, timeout, key=None):
        raise orc.OrRateLimitError("429", retry_after=60)

    monkeypatch.setattr(orc, "or_vision_chat", fake_chat)
    monkeypatch.setattr(settings, "or_vision_models", ["a:free"])
    monkeypatch.setattr(settings, "or_credits", True)

    with pytest.raises(orc.OrRateLimitError) as exc:
        asyncio.run(orc.or_vision_with_failover(prompt="q", image_b64="AAA", system="s"))
    assert "1000/day" in str(exc.value)


def test_usage_accounting_rolls_over_daily(tmp_path, monkeypatch):
    monkeypatch.setattr(orc, "_or_usage", None)
    monkeypatch.setattr(orc, "get_scratch_dir", lambda: tmp_path / ".g023")
    usage = orc.get_or_usage()
    usage.record_request("a:free")
    usage.record_request("b:free")
    assert usage.requests == 2

    # A fresh load (new process) on the same day sees the same count.
    monkeypatch.setattr(orc, "_or_usage", None)
    usage2 = orc.get_or_usage()
    assert usage2.requests == 2
    assert usage2.remaining() == orc.OPENROUTER_FREE_DAILY_LIMIT - 2


def test_active_daily_limit_reflects_tier(monkeypatch):
    monkeypatch.setattr(settings, "or_credits", False)
    assert orc.active_daily_limit() == orc.OPENROUTER_FREE_DAILY_LIMIT
    monkeypatch.setattr(settings, "or_credits", True)
    assert orc.active_daily_limit() == orc.OPENROUTER_CREDITS_DAILY_LIMIT


def test_or_vision_chat_sends_image_url(monkeypatch):
    """The request body carries the image as a data: URI in image_url parts."""

    async def fake_post(self, url, headers=None, json=None):
        body = json
        assert body["model"] == "m:free"
        content = body["messages"][1]["content"]
        types = [part["type"] for part in content]
        assert types == ["text", "image_url"]
        image = content[1]["image_url"]["url"]
        assert image.startswith("data:image/png;base64,")
        return _ok_response("a red square")

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        post = fake_post

    monkeypatch.setattr(orc.httpx, "AsyncClient", FakeClient)
    result = asyncio.run(
        orc.or_vision_chat(model="m:free", prompt="what?", image_b64="QUJD", system="s")
    )
    assert result.answer == "a red square"
    assert result.ratelimit["x-ratelimit-remaining"] == "49"

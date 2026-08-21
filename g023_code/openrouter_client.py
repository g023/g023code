"""
OpenRouter vision client.

OpenRouter is reached through its OpenAI-compatible ``/chat/completions``
endpoint with the key from ``OK.dat``. Vision models take the image as a
``data:`` URL inside ``image_url`` content parts — the same payload shape the
deepseek ``chat_completions`` backend uses, just against a different host.

Two OpenRouter-specific concerns live here rather than in the vision subagent:

* **Model failover.** ``./OR_VISION.json`` in the project folder lists the
  vision models to try, in priority order. When a call is rate-limited (HTTP
  429), the next model on the list is tried instead, so a free-tier quota
  exhaustion on one :free model doesn't stall image analysis — it hops to the
  next one. Without the file, the single configured default model is used.

* **Daily quota accounting.** OpenRouter caps ``:free`` models at 50
  requests/day for accounts with under $10 lifetime credits, and up to 1000
  requests/day once $10+ credits have been bought. The tracker persists to the
  project's ``.g023/or_usage.json`` so ``/or status`` can show where the
  session stands against the ceiling (and suggest the tier change).
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import httpx

from .config import (
    OPENROUTER_CREDITS_DAILY_LIMIT,
    OPENROUTER_DEFAULT_MODEL,
    OPENROUTER_FREE_DAILY_LIMIT,
    get_project_root,
    get_scratch_dir,
    load_openrouter_key,
    settings,
)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# OpenRouter uses OpenAI's chat/completions shape; that is the only endpoint it
# speaks, so the openrouter backend pins this regardless of vision_endpoint.
OPENROUTER_CHAT_URL = f"{OPENROUTER_BASE_URL}/chat/completions"

OR_VISION_FILE = "OR_VISION.json"  # project-folder model list


class OrRateLimitError(RuntimeError):
    """HTTP 429 from OpenRouter — free-tier quota or per-minute cap hit."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class OrVisionResult:
    answer: str
    model: str
    elapsed_ms: int = 0
    usage: Optional[dict] = None
    ratelimit: Optional[dict] = None  # x-ratelimit-* headers, for /or status
    cached: bool = False


# ---------------------------------------------------------------------------
# Model list (OR_VISION.json)
# ---------------------------------------------------------------------------

def or_models_file() -> Path:
    """The per-project model list, if the user created one."""
    return get_project_root() / OR_VISION_FILE


def load_or_models() -> list[str]:
    """Models to try for openrouter vision, in priority order.

    Precedence: settings.or_vision_models (explicit, e.g. after /or models was
    pointed at a file) → ./OR_VISION.json in the project folder → the single
    configured default model. The file is a JSON array of model id strings.
    """
    # An explicitly configured list wins; it may have been set by the user or
    # by a previous read of OR_VISION.json this session.
    explicit = getattr(settings, "or_vision_models", None)
    if explicit:
        cleaned = [m for m in explicit if isinstance(m, str) and m.strip()]
        if cleaned:
            return cleaned

    path = or_models_file()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                cleaned = [m.strip() for m in data if isinstance(m, str) and m.strip()]
                if cleaned:
                    return cleaned
        except (json.JSONDecodeError, OSError):
            pass

    return [settings.or_default_model or OPENROUTER_DEFAULT_MODEL]


def rate_limit_tier() -> tuple[int, int]:
    """(free ceiling, ceiling with $10+ credits) for :free models, per 24h."""
    return OPENROUTER_FREE_DAILY_LIMIT, OPENROUTER_CREDITS_DAILY_LIMIT


def active_daily_limit() -> int:
    free, credits = rate_limit_tier()
    return credits if settings.or_credits else free


# ---------------------------------------------------------------------------
# Daily usage accounting (.g023/or_usage.json)
# ---------------------------------------------------------------------------

@dataclass
class OrUsage:
    date: str = ""
    requests: int = 0
    rate_limits: int = 0
    last_model: Optional[str] = None
    _path: Optional[Path] = field(default=None, repr=False)

    @classmethod
    def load(cls) -> "OrUsage":
        path = get_scratch_dir() / "or_usage.json"
        obj = cls(_path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            obj.date = data.get("date", "")
            obj.requests = int(data.get("requests", 0))
            obj.rate_limits = int(data.get("rate_limits", 0))
            obj.last_model = data.get("last_model")
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            pass
        today = date.today().isoformat()
        if obj.date != today:  # new day: fresh quota
            obj.date = today
            obj.requests = 0
            obj.rate_limits = 0
            obj.last_model = None
        return obj

    def _save(self) -> None:
        path = self._path or (get_scratch_dir() / "or_usage.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "date": self.date,
                        "requests": self.requests,
                        "rate_limits": self.rate_limits,
                        "last_model": self.last_model,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    def record_request(self, model: str) -> None:
        self.requests += 1
        self.last_model = model
        self._save()

    def record_rate_limit(self) -> None:
        self.rate_limits += 1
        self._save()

    def remaining(self) -> int:
        return max(0, active_daily_limit() - self.requests)


_or_usage: Optional[OrUsage] = None


def get_or_usage() -> OrUsage:
    global _or_usage
    if _or_usage is None:
        _or_usage = OrUsage.load()
    return _or_usage


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------

async def or_vision_chat(
    model: str,
    prompt: str,
    image_b64: str,
    system: str,
    timeout: float = 180.0,
    key: Optional[str] = None,
) -> OrVisionResult:
    """One OpenRouter vision request. Raises OrRateLimitError on HTTP 429."""
    key = key or load_openrouter_key()
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            },
        ],
        "max_tokens": 2048,
        "temperature": 0.2,
    }
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": "g023-code",  # OpenRouter asks apps to identify themselves
    }
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        r = await client.post(OPENROUTER_CHAT_URL, headers=headers, json=body)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if r.status_code == 429:
        retry_after = None
        raw = r.headers.get("Retry-After")
        if raw:
            try:
                retry_after = int(float(raw))
            except ValueError:
                retry_after = None
        get_or_usage().record_rate_limit()
        raise OrRateLimitError(
            f"OpenRouter rate limit on '{model}' (HTTP 429). "
            f"{r.text[:300]}",
            retry_after=retry_after,
        )
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter HTTP {r.status_code}: {r.text[:400]}")

    data = r.json()
    try:
        answer = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        answer = ""
    ratelimit = {}
    for name in (
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
    ):
        if name in r.headers:
            ratelimit[name] = r.headers[name]
    usage = data.get("usage") if isinstance(data, dict) else None
    if not answer:
        answer = json.dumps(
            {"error": "OpenRouter returned an empty answer", "raw": (r.text or "")[:400]}
        )
    return OrVisionResult(
        answer=answer,
        model=model,
        elapsed_ms=elapsed_ms,
        usage=usage,
        ratelimit=ratelimit,
    )


async def or_vision_with_failover(
    prompt: str,
    image_b64: str,
    system: str,
    timeout: float = 180.0,
) -> tuple[OrVisionResult, list[str]]:
    """Try the OR_VISION.json list in order, hopping on 429.

    Returns (result, attempts) where attempts lists every model tried. When the
    whole list is exhausted on rate limits, the last OrRateLimitError is
    re-raised with a hint about the tier.
    """
    models = load_or_models()
    usage = get_or_usage()
    # Prefer the model that last worked, if it's still on the list.
    if usage.last_model in models and len(models) > 1:
        models = [usage.last_model] + [m for m in models if m != usage.last_model]
    attempts: list[str] = []
    last_error: Optional[OrRateLimitError] = None
    for model in models:
        attempts.append(model)
        try:
            result = await or_vision_chat(
                model=model,
                prompt=prompt,
                image_b64=image_b64,
                system=system,
                timeout=timeout,
            )
            usage.record_request(model)
            return result, attempts
        except OrRateLimitError as e:
            last_error = e
            continue  # next model
    # Everyone on the list is rate-limited.
    assert last_error is not None
    tier = "1000/day" if settings.or_credits else "50/day"
    raise OrRateLimitError(
        f"All {len(models)} OpenRouter vision model(s) rate-limited. "
        f"Free :free tier is ~{tier} requests per 24h "
        f"({usage.requests} used today). "
        "Add $10+ credits on openrouter.ai to raise the ceiling to 1000/day, "
        "or wait for the reset. Tried: " + ", ".join(models),
        retry_after=last_error.retry_after,
    )

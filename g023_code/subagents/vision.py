"""
Vision Subagent — isolated image analysis.

The orchestrator never sees image bytes; it receives only a compact JSON answer.
Results are cached by (image content hash, question, backend+model) so repeated
questions about the same screenshot cost nothing. Three backends are supported,
selected with ``settings.vision_backend``:

* ``ollama``     — a local/remote Ollama daemon (``vision_chat_detailed``).
* ``deepseek``   — DeepSeek's ``deepseek-flash`` over the same
                   K.dat key, on either ``/responses`` or ``/chat/completions``
                   (``settings.vision_endpoint``).
* ``openrouter`` — OpenRouter's chat/completions with the OK.dat key, cycling
                   through ``./OR_VISION.json`` when rate-limited.

The daemon doing the work may be local or on another machine — see
``ollama_client.resolve_host``.
"""

from __future__ import annotations

import hashlib
import json

from ..api import chat_answer, get_client
from ..cache import get_cache
from ..config import settings
from ..ollama_client import (
    VISION_SEND_MAX_SIDE,
    is_local,
    load_image_for_vision,
    probe,
    resolve_host,
    vision_chat_detailed,
)
from ..openrouter_client import (
    OrRateLimitError,
    load_or_models,
    or_vision_with_failover,
)

VISION_SYSTEM = (
    "You are a Vision Subagent for a terminal coding assistant. "
    "Answer the question about the image precisely and concisely. "
    "Transcribe any code, error messages, stack traces, file paths, or UI labels "
    "exactly as they appear. If the image is unclear, say so instead of guessing."
)


async def run_vision(path_or_url: str, question: str) -> str:
    """Analyze an image with the configured vision backend. Returns compact JSON."""
    backend = settings.vision_backend

    if not settings.vision_enabled:
        return json.dumps(
            {
                "error": "Vision is disabled.",
                "hint": "Run /vision to enable it and pick a vision backend+model.",
                "path_or_url": path_or_url,
            }
        )

    if backend == "ollama":
        return await _run_ollama(path_or_url, question)
    if backend == "deepseek":
        return await _run_deepseek(path_or_url, question)
    if backend == "openrouter":
        return await _run_openrouter(path_or_url, question)

    return json.dumps(
        {
            "error": f"Vision backend '{backend}' is not implemented.",
            "hint": "Run /vision backend ollama|deepseek|openrouter",
            "path_or_url": path_or_url,
        }
    )


async def _load(path_or_url: str):
    """Load an image once — all three backends share the loader.

    Always constrained-resize to VISION_SEND_MAX_SIDE (800) on each edge.
    ``settings.vision_max_image_dim`` of 1024 or 0 does not change the send size.
    """
    try:
        # load_image_for_vision always uses VISION_SEND_MAX_SIDE (800).
        return await load_image_for_vision(path_or_url)
    except Exception as e:
        raise RuntimeError(f"Could not load image: {e} (send cap {VISION_SEND_MAX_SIDE}px)")


def _cache_key(backend: str, model: str) -> str:
    if backend == "deepseek":
        return f"deepseek:{model}:{settings.vision_endpoint}"
    return f"{backend}:{model}"


def _check_cache(image_hash: str, question: str, cache_key: str):
    return get_cache().get_vision(image_hash, question, cache_key)


def _put_cache(image_hash: str, question: str, cache_key: str, answer: str):
    get_cache().put_vision(image_hash, question, cache_key, answer)


# ---------------------------------------------------------------------------
# ollama backend
# ---------------------------------------------------------------------------

async def _run_ollama(path_or_url: str, question: str) -> str:
    model = settings.vision_model or ""
    if not model:
        return json.dumps(
            {
                "error": "No Ollama vision model selected.",
                "hint": "Run /vision to pick one.",
                "path_or_url": path_or_url,
            }
        )
    host = resolve_host(settings.vision_host)
    image = await _load(path_or_url)
    image_hash = hashlib.sha256(image.raw).hexdigest()
    cache_key = _cache_key("ollama", model)

    cached = _check_cache(image_hash, question, cache_key)
    if cached is not None:
        return json.dumps(
            {
                "backend": "ollama",
                "model": model,
                "host": host,
                "path_or_url": path_or_url,
                "question": question,
                "answer": cached,
                "cached": True,
            },
            ensure_ascii=False,
        )

    try:
        result = await vision_chat_detailed(
            model=model,
            prompt=question,
            image_b64=image.b64,
            system=VISION_SYSTEM,
            host=host,
            timeout=settings.vision_timeout,
            num_ctx=settings.vision_num_ctx,
            keep_alive=settings.vision_keep_alive,
        )
    except Exception as e:
        return json.dumps(
            {
                "error": f"Vision call failed: {e}",
                "backend": "ollama",
                "model": model,
                "host": host,
                "hint": _failure_hint(host, model),
            }
        )

    if not result.answer:
        return json.dumps(
            {"error": "Vision model returned an empty answer", "model": model, "host": host}
        )

    _put_cache(image_hash, question, cache_key, result.answer)
    payload = {
        "backend": "ollama",
        "model": model,
        "host": host,
        "path_or_url": path_or_url,
        "question": question,
        "answer": result.answer,
        "cached": False,
        "elapsed_ms": result.elapsed_ms,
        "image_pixels": f"{image.final_size[0]}x{image.final_size[1]}" if image.final_size[0] else None,
        "image_bytes_sent": image.bytes_sent,
    }
    if image.downscaled:
        payload["downscaled_from"] = f"{image.original_size[0]}x{image.original_size[1]}"
    return json.dumps(payload, ensure_ascii=False)


# ---------------------------------------------------------------------------
# deepseek backend
# ---------------------------------------------------------------------------

async def _run_deepseek(path_or_url: str, question: str) -> str:
    model = settings.effective_vision_model
    image = await _load(path_or_url)
    image_hash = hashlib.sha256(image.raw).hexdigest()
    cache_key = _cache_key("deepseek", model)

    cached = _check_cache(image_hash, question, cache_key)
    if cached is not None:
        return json.dumps(
            {
                "backend": "deepseek",
                "endpoint": settings.vision_endpoint,
                "model": model,
                "path_or_url": path_or_url,
                "question": question,
                "answer": cached,
                "cached": True,
            },
            ensure_ascii=False,
        )

    client = get_client()
    try:
        if settings.vision_endpoint == "chat_completions":
            resp = await client.vision_chat(
                model=model,
                prompt=question,
                image_b64=image.b64,
                system=VISION_SYSTEM,
                timeout=settings.vision_timeout,
            )
            answer = chat_answer(resp)
            elapsed_ms = None
        else:  # responses (default)
            resp = await client.vision_responses(
                model=model,
                prompt=question,
                image_b64=image.b64,
                system=VISION_SYSTEM,
                timeout=settings.vision_timeout,
            )
            from ..api import output_text

            answer = output_text(resp)
            elapsed_ms = None
    except Exception as e:
        return json.dumps(
            {
                "error": f"DeepSeek vision call failed: {e}",
                "backend": "deepseek",
                "endpoint": settings.vision_endpoint,
                "model": model,
                "hint": (
                    f"Model '{model}' on endpoint '{settings.vision_endpoint}'. "
                    "The Responses API is the program's native format; if the "
                    "vision model rejects it, try /vision endpoint chat_completions."
                ),
            }
        )

    if not answer:
        return json.dumps(
            {
                "error": "DeepSeek vision model returned an empty answer",
                "model": model,
                "endpoint": settings.vision_endpoint,
            }
        )

    _put_cache(image_hash, question, cache_key, answer)
    return json.dumps(
        {
            "backend": "deepseek",
            "endpoint": settings.vision_endpoint,
            "model": model,
            "path_or_url": path_or_url,
            "question": question,
            "answer": answer,
            "cached": False,
            "image_pixels": f"{image.final_size[0]}x{image.final_size[1]}" if image.final_size[0] else None,
            "image_bytes_sent": image.bytes_sent,
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# openrouter backend
# ---------------------------------------------------------------------------

async def _run_openrouter(path_or_url: str, question: str) -> str:
    model = settings.effective_vision_model
    image = await _load(path_or_url)
    image_hash = hashlib.sha256(image.raw).hexdigest()
    cache_key = _cache_key("openrouter", model)

    cached = _check_cache(image_hash, question, cache_key)
    if cached is not None:
        return json.dumps(
            {
                "backend": "openrouter",
                "model": model,
                "path_or_url": path_or_url,
                "question": question,
                "answer": cached,
                "cached": True,
            },
            ensure_ascii=False,
        )

    try:
        result, attempts = await or_vision_with_failover(
            prompt=question,
            image_b64=image.b64,
            system=VISION_SYSTEM,
            timeout=settings.vision_timeout,
        )
    except OrRateLimitError as e:
        return json.dumps(
            {
                "error": f"OpenRouter rate limit: {e}",
                "backend": "openrouter",
                "models": load_or_models(),
                "hint": (
                    "Free :free models allow ~50 requests/24h without credits, "
                    "~1000/24h with $10+ lifetime credits (/or tier credits). "
                    "Add more models to OR_VISION.json to fail over."
                ),
            }
        )
    except Exception as e:
        return json.dumps(
            {
                "error": f"OpenRouter vision call failed: {e}",
                "backend": "openrouter",
                "model": model,
                "hint": "Check OK.dat key and /or status.",
            }
        )

    answer = result.answer
    _put_cache(image_hash, question, cache_key, answer)
    return json.dumps(
        {
            "backend": "openrouter",
            "model": result.model,
            "path_or_url": path_or_url,
            "question": question,
            "answer": answer,
            "cached": False,
            "elapsed_ms": result.elapsed_ms,
            "attempts": attempts,
            "image_pixels": f"{image.final_size[0]}x{image.final_size[1]}" if image.final_size[0] else None,
            "image_bytes_sent": image.bytes_sent,
        },
        ensure_ascii=False,
    )


def _failure_hint(host: str, model: str) -> str:
    """Distinguish 'daemon is down' from 'model is wrong' — they need different fixes."""
    reachable = probe(host, timeout=3.0, count_models=False).ok
    if not reachable:
        where = "locally" if is_local(host) else "on that machine"
        return (
            f"No Ollama daemon answered at {host}. Check it is running {where} "
            "(/ollama to test, /ollama host to point somewhere else)."
        )
    return (
        f"The daemon at {host} is up, so '{model}' is likely missing or not "
        "image-capable. Run /ollama models to see what it has, /vision to pick one."
    )

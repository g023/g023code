"""
Vision Subagent — isolated image analysis.

The orchestrator never sees image bytes; it receives only a compact JSON answer.
Results are cached by (image content hash, question, backend+model) so repeated
questions about the same screenshot cost nothing. The daemon doing the work may
be local or on another machine — see ``ollama_client.resolve_host``.
"""

from __future__ import annotations

import hashlib
import json

from ..cache import get_cache
from ..config import settings
from ..ollama_client import (
    is_local,
    load_image,
    probe,
    resolve_host,
    vision_chat_detailed,
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
                "hint": "Run /vision to enable it and pick an Ollama vision model.",
                "path_or_url": path_or_url,
            }
        )

    if backend != "ollama":
        return json.dumps(
            {
                "error": f"Vision backend '{backend}' is not implemented.",
                "hint": "Run /vision backend ollama, then /vision to pick a model.",
                "path_or_url": path_or_url,
            }
        )

    model = settings.vision_model or ""
    host = resolve_host(settings.vision_host)

    try:
        image = await load_image(path_or_url, max_dim=settings.vision_max_image_dim)
    except Exception as e:
        return json.dumps({"error": f"Could not load image: {e}", "path_or_url": path_or_url})

    image_hash = hashlib.sha256(image.raw).hexdigest()
    cache = get_cache()
    cache_key = f"ollama:{model}"

    cached = cache.get_vision(image_hash, question, cache_key)
    if cached is not None:
        return json.dumps(
            {
                "backend": backend,
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
                "backend": backend,
                "model": model,
                "host": host,
                "hint": _failure_hint(host, model),
            }
        )

    if not result.answer:
        return json.dumps(
            {"error": "Vision model returned an empty answer", "model": model, "host": host}
        )

    cache.put_vision(image_hash, question, cache_key, result.answer)
    payload = {
        "backend": backend,
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

"""
Vision Subagent — isolated image analysis on DeepSeek V4.1 Flash.

The orchestrator never sees image bytes; it receives only a compact JSON answer.
One isolated request (image block + question, thinking off) goes to the same
model and wire as everything else (:meth:`DeepSeekClient.vision`). Answers are
cached by (image content hash, question, model) so repeated questions about the
same screenshot cost nothing; tokens are recorded under the ``subagent`` scope.
"""

from __future__ import annotations

import hashlib
import json

from ..api import get_client, output_text
from ..cache import get_cache
from ..config import MODEL, settings
from ..images import VISION_SEND_MAX_SIDE, load_image_for_vision
from ..usage import get_usage

VISION_SYSTEM = (
    "You are a Vision Subagent for a terminal coding assistant. "
    "Answer the question about the image precisely and concisely. "
    "Transcribe any code, error messages, stack traces, file paths, or UI labels "
    "exactly as they appear. If the image is unclear, say so instead of guessing."
)


def _cache_key() -> str:
    return f"deepseek:{MODEL}"


LOCAL_NOTE = (
    "answered by the small local vision model; for a DeepSeek answer call AnalyzeImage "
    "again with the same question"
)


async def _try_local(image, image_hash: str, question: str, base: dict):
    """Local-first answer (local_llm.py) when a local vision model is set.

    Escalation costs no prompt bytes: the same image + question asked a second
    time skips the local model and goes to DeepSeek.
    """
    if not settings.local_enabled:
        return None
    try:
        from .. import local_llm

        name = local_llm.vision_model()
        if not name or not local_llm.available():
            return None
        key = f"local:{name}"
        cache = get_cache()
        if cache.get_vision(image_hash, question, key) is not None:
            return None  # repeat: escalate
        answer = await local_llm.vision(question, image.data_uri, system=VISION_SYSTEM)
        if not answer:
            return None
        cache.put_vision(image_hash, question, key, answer)
    except Exception:
        return None
    return json.dumps(
        {**base, "model": key, "answer": answer, "cached": False, "note": LOCAL_NOTE},
        ensure_ascii=False,
    )


async def run_vision(path_or_url: str, question: str) -> str:
    """Answer ``question`` about one image. Returns compact JSON (never raises)."""
    if not settings.vision_enabled:
        return json.dumps(
            {
                "error": "Vision is disabled.",
                "hint": "Run /vision on to enable it.",
                "path_or_url": path_or_url,
            }
        )

    try:
        image = await load_image_for_vision(path_or_url)
    except Exception as e:
        return json.dumps(
            {
                "error": f"Could not load image: {e}",
                "path_or_url": path_or_url,
                "send_cap_px": VISION_SEND_MAX_SIDE,
            }
        )

    image_hash = hashlib.sha256(image.raw).hexdigest()
    cache = get_cache()
    base = {"model": MODEL, "path_or_url": path_or_url, "question": question}

    cached = cache.get_vision(image_hash, question, _cache_key())
    if cached is not None:
        return json.dumps({**base, "answer": cached, "cached": True}, ensure_ascii=False)

    local = await _try_local(image, image_hash, question, base)
    if local is not None:
        return local

    try:
        resp = await get_client().vision(
            question,
            image.data_uri,
            system=VISION_SYSTEM,
            timeout=float(settings.vision_timeout),
        )
    except Exception as e:
        return json.dumps({**base, "error": f"Vision call failed: {e}"}, ensure_ascii=False)

    get_usage().record(MODEL, resp.get("usage"), scope="subagent")
    answer = output_text(resp).strip()
    if not answer:
        return json.dumps({**base, "error": "The model returned an empty answer."}, ensure_ascii=False)

    cache.put_vision(image_hash, question, _cache_key(), answer)
    payload = {
        **base,
        "answer": answer,
        "cached": False,
        "media_type": image.media_type,
        "image_pixels": f"{image.final_size[0]}x{image.final_size[1]}" if image.final_size[0] else None,
        "image_bytes_sent": image.bytes_sent,
    }
    if image.downscaled:
        payload["downscaled_from"] = f"{image.original_size[0]}x{image.original_size[1]}"
    return json.dumps(payload, ensure_ascii=False)

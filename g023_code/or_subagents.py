"""
OpenRouter subagent offload.

Lets the orchestrator hand an ``Agent`` (Explore/Plan) turn to an OpenRouter
model instead of DeepSeek — a second brain that costs nothing on ``:free``
models and does not touch the K.dat quota. Everything user-facing lives in
``OR_SUB_MODELS.json``:

* the **main** copy sits next to OK.dat (``get_home()``) and is shared by every
  project;
* a project may carry its own ``OR_SUB_MODELS.json`` whose ``models`` and
  ``primary`` override the main file for this project only.

Any change made inside a project (add / remove / re-prioritise / switch
primary / change reasoning) is written to *both* copies, so a model added in
one project becomes available in all of them.

Each model entry records what the OpenRouter catalog says about the endpoint:

* ``context_window`` — the prompt ceiling for the endpoint, so the orchestrator
  knows how much it can stuff into an offload before the request bounces;
* ``max_output_tokens`` — the largest single reply the endpoint will honour;
* ``reasoning_levels`` — the effort values the model accepts (``none`` included
  only where the provider lets reasoning be switched off);
* ``default_reasoning`` — the level used when the orchestrator does not name one.

``priority`` is the fallback order: the primary model is tried first, and on a
rate limit (HTTP 429) or a model that refuses the call (403/402), the next
priority is tried. The daily ``:free`` quota is the same one the vision client
accounts for in ``.g023/or_usage.json``.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import get_home, get_project_root, load_openrouter_key, settings
from .openrouter_client import (
    OPENROUTER_CHAT_URL,
    OrRateLimitError,
    get_or_usage,
)

OR_SUB_MODELS_FILE = "OR_SUB_MODELS.json"

# Reasoning effort values, in the order a picker should offer them.
REASONING_LEVELS: tuple[str, ...] = ("none", "low", "medium", "high")

# HTTP statuses that mean "this model cannot take the call right now" — hop to
# the next priority rather than failing the offload. 429 is the classic free-
# tier rate limit; 402 is an account with no credits reaching a paid model;
# 403 is a model restricted to agentic harnesses or missing BYOK; 400 is an
# unknown model id (bad id in the file) or a reasoning level the model rejects.
_HOP_STATUSES = (400, 402, 403, 429)

DEFAULT_MODEL_ENTRY = {
    "context_window": 200_000,
    "max_output_tokens": 32_768,
    "reasoning_levels": ["none", "low", "medium", "high"],
    "default_reasoning": "high",
    "notes": "",
}


# ---------------------------------------------------------------------------
# File access — main copy (home) + per-project override
# ---------------------------------------------------------------------------


def main_models_file() -> Path:
    """The shared copy next to OK.dat — every project reads and writes it."""
    return get_home() / OR_SUB_MODELS_FILE


def project_models_file() -> Path:
    """This project's own copy, if it created one."""
    return get_project_root() / OR_SUB_MODELS_FILE


def _read_file(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def load_or_sub_config() -> dict:
    """Merged config: project file over the main file, then defaults.

    Returns a dict with ``primary`` (str or None) and ``models`` (list of
    entries, each with id/priority/context_window/max_output_tokens/
    reasoning_levels/default_reasoning/notes), sorted by priority.
    """
    project = _read_file(project_models_file()) or {}
    main = _read_file(main_models_file()) or {}

    primary = project.get("primary") or main.get("primary") or None
    raw_models: dict[str, dict] = {}

    for source in (main, project):
        for entry in source.get("models") or []:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                continue
            model_id = entry["id"].strip()
            if not model_id:
                continue
            merged = dict(DEFAULT_MODEL_ENTRY)
            merged.update({k: v for k, v in entry.items() if k != "id"})
            merged["id"] = model_id
            raw_models[model_id] = merged

    models = sorted(raw_models.values(), key=lambda m: (m.get("priority", 999), m["id"]))
    if primary not in raw_models:
        primary = models[0]["id"] if models else None
    return {"primary": primary, "models": models}


def _normalise_models(models: list[dict], primary: Optional[str]) -> dict:
    """Validate/clean a model list into the shape the file stores, with
    priorities renumbered 1..N in list order."""
    cleaned: list[dict] = []
    for i, entry in enumerate(models, start=1):
        model_id = str(entry.get("id", "")).strip()
        if not model_id:
            continue
        levels = entry.get("reasoning_levels")
        if not isinstance(levels, list):
            levels = list(REASONING_LEVELS)
        levels = [lvl for lvl in levels if lvl in REASONING_LEVELS] or ["high"]
        default = entry.get("default_reasoning", "high")
        if default not in levels:
            default = levels[-1]
        cleaned.append(
            {
                "id": model_id,
                "priority": i,
                "context_window": int(entry.get("context_window", DEFAULT_MODEL_ENTRY["context_window"])),
                "max_output_tokens": int(entry.get("max_output_tokens", DEFAULT_MODEL_ENTRY["max_output_tokens"])),
                "reasoning_levels": levels,
                "default_reasoning": default,
                "notes": str(entry.get("notes", "") or ""),
            }
        )
    if primary not in {m["id"] for m in cleaned}:
        primary = cleaned[0]["id"] if cleaned else None
    return {"primary": primary, "models": cleaned}


def save_or_sub_config(config: dict, *, write_project: bool = True) -> None:
    """Write the config to the main file and (by default) this project's file.

    The main copy is always written so the change is visible to every project;
    ``write_project`` mirrors it into ``./OR_SUB_MODELS.json`` so the project
    the user is in reflects the change immediately.
    """
    data = _normalise_models(config.get("models") or [], config.get("primary"))
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        main_models_file().write_text(text, encoding="utf-8")
    except OSError:
        pass
    if write_project:
        project_path = project_models_file()
        try:
            project_path.write_text(text, encoding="utf-8")
        except OSError:
            pass


def load_or_sub_models() -> list[dict]:
    """Model entries in fallback order (primary first)."""
    config = load_or_sub_config()
    models = list(config["models"])
    primary = config["primary"]
    if primary and primary in {m["id"] for m in models} and len(models) > 1:
        models = [m for m in models if m["id"] == primary] + [m for m in models if m["id"] != primary]
    return models


def or_sub_primary() -> Optional[str]:
    return load_or_sub_config().get("primary")


def get_model(model_id: str) -> Optional[dict]:
    for m in load_or_sub_config()["models"]:
        if m["id"] == model_id:
            return m
    return None


# ---------------------------------------------------------------------------
# Usage accounting (daily, mirrors the vision tracker)
# ---------------------------------------------------------------------------


@dataclass
class OrSubUsage:
    date: str = ""
    requests: int = 0
    rate_limits: int = 0
    hops: int = 0
    last_model: Optional[str] = None
    _path: Optional[Path] = field(default=None, repr=False)

    @classmethod
    def load(cls) -> "OrSubUsage":
        path = get_project_root() / ".g023" / "or_sub_usage.json"
        obj = cls(_path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            obj.date = str(data.get("date", ""))
            obj.requests = int(data.get("requests", 0))
            obj.rate_limits = int(data.get("rate_limits", 0))
            obj.hops = int(data.get("hops", 0))
            obj.last_model = data.get("last_model")
        except (OSError, ValueError, TypeError):
            pass
        from datetime import date as _date

        today = _date.today().isoformat()
        if obj.date != today:
            obj.date = today
            obj.requests = 0
            obj.rate_limits = 0
            obj.hops = 0
            obj.last_model = None
        return obj

    def _save(self) -> None:
        path = self._path or (get_project_root() / ".g023" / "or_sub_usage.json")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "date": self.date,
                        "requests": self.requests,
                        "rate_limits": self.rate_limits,
                        "hops": self.hops,
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

    def record_hop(self) -> None:
        self.hops += 1
        self._save()


_or_sub_usage: Optional[OrSubUsage] = None


def get_or_sub_usage() -> OrSubUsage:
    global _or_sub_usage
    if _or_sub_usage is None:
        _or_sub_usage = OrSubUsage.load()
    return _or_sub_usage


# ---------------------------------------------------------------------------
# The call itself
# ---------------------------------------------------------------------------


@dataclass
class OrSubResult:
    answer: str
    model: str
    reasoning: str = ""
    elapsed_ms: int = 0
    usage: Optional[dict] = None
    truncated: bool = False


def _clamp_output_tokens(model: dict, requested: Optional[int]) -> int:
    """Keep the output budget under the model's ceiling, with headroom."""
    ceiling = int(model.get("max_output_tokens", DEFAULT_MODEL_ENTRY["max_output_tokens"]))
    if requested is None:
        return min(8192, ceiling)
    return max(1, min(int(requested), ceiling))


def _reasoning_block(model: dict, level: Optional[str]) -> Optional[dict]:
    """The ``reasoning`` block for a model, honouring its supported levels.

    Returns ``None`` when the model has no reasoning support at all (the
    parameter is then omitted from the request entirely — some endpoints 400
    on an unknown block). ``none`` maps to ``enabled: false`` and is only sent
    when the model lists ``none`` — ox-alpha's endpoint makes reasoning
    mandatory and rejects it, so there ``none`` clamps to the model default.
    """
    levels = model.get("reasoning_levels") or list(REASONING_LEVELS)
    if not level or level not in levels:
        level = model.get("default_reasoning", "high")
    if level == "none":
        if "none" in levels:
            return {"enabled": False}
        return {"effort": "high" if "high" in levels else levels[-1]}
    return {"effort": level}


async def or_sub_chat(
    model_id: str,
    system: str,
    prompt: str,
    reasoning: Optional[str] = None,
    max_output_tokens: Optional[int] = None,
    timeout: float = 180.0,
    key: Optional[str] = None,
) -> OrSubResult:
    """One OpenRouter subagent request. Raises OrRateLimitError on HTTP 429
    (and RuntimeError for other hop-able statuses, with the status attached)."""
    model = get_model(model_id) or dict(DEFAULT_MODEL_ENTRY, id=model_id)
    key = key or load_openrouter_key()
    reasoning_block = _reasoning_block(model, reasoning)
    body = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": _clamp_output_tokens(model, max_output_tokens),
        "temperature": 0.3,
    }
    if reasoning_block is not None:
        body["reasoning"] = reasoning_block
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "X-Title": "g023-code",
        "X-OpenRouter-Title": "g023-code",
    }
    started = time.perf_counter()
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        r = await client.post(OPENROUTER_CHAT_URL, headers=headers, json=body)
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    if r.status_code in (400, 402, 403, 429):
        msg = ""
        try:
            msg = json.loads(r.text).get("error", {}).get("message", "") or ""
        except (ValueError, AttributeError):
            pass
        if r.status_code == 429:
            retry_after = None
            raw = r.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = int(float(raw))
                except ValueError:
                    retry_after = None
            get_or_usage().record_rate_limit()
            get_or_sub_usage().record_rate_limit()
            raise OrRateLimitError(
                f"OpenRouter rate limit on '{model_id}' (HTTP 429). {msg[:300]}",
                retry_after=retry_after,
            )
        raise RuntimeError(f"OpenRouter HTTP {r.status_code} on '{model_id}': {msg[:300]}")
    if r.status_code != 200:
        raise RuntimeError(f"OpenRouter HTTP {r.status_code}: {r.text[:400]}")

    data = r.json()
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        message = {}
    answer = message.get("content") or ""
    reasoning_text = message.get("reasoning") or ""
    usage = data.get("usage") if isinstance(data, dict) else None
    truncated = bool((usage or {}).get("finish_reason") == "length")
    if not answer:
        answer = json.dumps({"error": "OpenRouter returned an empty answer", "raw": (r.text or "")[:400]})
    return OrSubResult(
        answer=answer,
        model=model_id,
        reasoning=reasoning_text,
        elapsed_ms=elapsed_ms,
        usage=usage,
        truncated=truncated,
    )


async def or_sub_with_failover(
    system: str,
    prompt: str,
    reasoning: Optional[str] = None,
    max_output_tokens: Optional[int] = None,
    timeout: float = 180.0,
) -> tuple[OrSubResult, list[str]]:
    """Try the OR_SUB_MODELS.json list in priority order, hopping on hop-able
    statuses. Returns (result, attempts); raises when every model failed.

    ``attempts`` lists every model id tried, for the trace and for status.
    The primary model goes first (it is the one the user picked), then the
    rest in priority order.
    """
    models = load_or_sub_models()
    if not models:
        raise RuntimeError(
            "No OpenRouter subagent models configured — add some with /orsub add, "
            "or check OR_SUB_MODELS.json."
        )
    usage = get_or_sub_usage()
    attempts: list[str] = []
    last_error: Optional[Exception] = None
    for entry in models:
        model_id = entry["id"]
        attempts.append(model_id)
        try:
            result = await or_sub_chat(
                model_id=model_id,
                system=system,
                prompt=prompt,
                reasoning=reasoning,
                max_output_tokens=max_output_tokens,
                timeout=timeout,
            )
            usage.record_request(model_id)
            return result, attempts
        except (OrRateLimitError, RuntimeError) as e:
            last_error = e
            usage.record_hop()
            continue
    assert last_error is not None
    raise RuntimeError(
        f"All {len(models)} OpenRouter subagent model(s) failed. "
        f"Tried: {', '.join(attempts)}. Last error: {last_error}",
    ) from last_error


# ---------------------------------------------------------------------------
# Model catalogue lookup (for /orsub add, so context limits are auto-filled)
# ---------------------------------------------------------------------------


def fetch_model_info(model_id: str, timeout: float = 30.0) -> Optional[dict]:
    """Look a model up in the OpenRouter catalogue for its endpoint limits.

    Returns a dict with context_window / max_output_tokens / reasoning_levels,
    or None when the id is unknown or the catalogue is unreachable. Sync by
    design — the /orsub add command wants an answer before it prints.
    """
    try:
        import httpx as _httpx

        key = load_openrouter_key()
        r = _httpx.get(
            f"https://openrouter.ai/api/v1/models/{model_id}/endpoints",
            headers={"Authorization": f"Bearer {key}"},
            timeout=timeout,
        )
        if r.status_code != 200:
            return None
        data = r.json().get("data") or {}
        endpoints = data.get("endpoints") or []
        if not endpoints:
            return None
        # Prefer the widest context among the live endpoints for this id.
        best = max(endpoints, key=lambda e: int(e.get("context_length") or 0))
        context = int(best.get("context_length") or 0)
        max_out = int(best.get("max_completion_tokens") or 0)
        params = best.get("supported_parameters") or []
        reasoning: list[str] = []
        if "reasoning" in params or "reasoning_effort" in params:
            # Conservative: assume reasoning is mandatory until proven otherwise
            # — ox-alpha lists "reasoning" yet rejects enabled:false, so "none"
            # is only added when the user verifies it via /orsub reasoning.
            reasoning = ["low", "medium", "high"]
        else:
            # No reasoning support at all: the block is omitted, "none" is the
            # only level (a label for "don't send a reasoning block").
            reasoning = ["none"]
        return {
            "context_window": context or DEFAULT_MODEL_ENTRY["context_window"],
            "max_output_tokens": max_out or DEFAULT_MODEL_ENTRY["max_output_tokens"],
            "reasoning_levels": reasoning,
            "default_reasoning": "high" if "high" in reasoning else reasoning[-1],
        }
    except Exception:
        return None

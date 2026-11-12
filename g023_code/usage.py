"""
Token accounting and cost estimation.

Every API call in the process funnels its ``usage`` block through the global
tracker, so /cost, /settings and the auto-compaction trigger all read from the
same numbers. DeepSeek reports the prefix-cache split directly
(``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``), which matters a
lot here: a cache hit is ~50x cheaper than a miss on the current sheet.

Pricing follows DeepSeek's tidal sheet (effective 2026-09-10): every model has
a **regular** (off-peak) rate and a **surge** (peak) rate that is exactly 2x
the regular one. The rates are loaded from ``ds_pricing.json`` when present
(home dir, then project root) and otherwise fall back to the published USD
table below. The peak windows live in ``ds_times.json`` and are consumed by
``surge.py``; both files come from the same price sheet.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens) — DeepSeek tidal sheet, effective 2026-09-10.
# Regular = off-peak; surge = peak (exactly 2x regular). Mirrors ds_pricing.json.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    # Regular (off-peak) rates
    cache_hit: float
    cache_miss: float
    output: float
    # Surge (peak) rates — 2x regular on the tidal sheet
    surge_cache_hit: float
    surge_cache_miss: float
    surge_output: float


_FLASH_PRICING = ModelPricing(
    cache_hit=0.003,
    cache_miss=0.15,
    output=0.60,
    surge_cache_hit=0.006,
    surge_cache_miss=0.30,
    surge_output=1.20,
)

# One model, one price row. Usage recorded under any other name (older session
# logs, a ds_pricing.json alias) falls through ``pricing_for`` to this row.
_DEFAULT_PRICING: Dict[str, ModelPricing] = {"deepseek-flash": _FLASH_PRICING}


def _load_ds_pricing() -> Dict[str, ModelPricing]:
    """Overlay ds_pricing.json (home, then project root) on the defaults."""
    from .config import get_home, get_project_root  # local import: no cycle

    for path in (
        get_home() / "ds_pricing.json",
        get_project_root() / "ds_pricing.json",
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        models = data.get("models")
        if not isinstance(models, dict):
            continue
        out: Dict[str, ModelPricing] = {}
        for name, row in models.items():
            try:
                reg, sur = row["regular"], row["surge"]
                out[name] = ModelPricing(
                    cache_hit=float(reg["cache_hit"]),
                    cache_miss=float(reg["cache_miss"]),
                    output=float(reg["output"]),
                    surge_cache_hit=float(sur["cache_hit"]),
                    surge_cache_miss=float(sur["cache_miss"]),
                    surge_output=float(sur["output"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
        if out:
            return {**_DEFAULT_PRICING, **out}
    return _DEFAULT_PRICING


PRICING: Dict[str, ModelPricing] = _load_ds_pricing()

# Search / Fetch are billed per *successful* request, not per token, and have no
# peak/off-peak split: $0.75 per 1,000 each (api-docs.deepseek.com/guides/search).
# Server-side web_search inside a model call is billed the same way; DeepSeek
# reports successful searches as ``usage.server_tool_use.tool_search``.
SEARCH_REQUEST_USD = 0.75 / 1000
FETCH_REQUEST_USD = 0.75 / 1000
# Pseudo-model buckets for Search / Fetch API calls (no tokens, only per-request cost).
FETCH_BUCKET = "deepseek-fetch-api"
SEARCH_BUCKET = "deepseek-search-api"

# Anything unknown is priced as Flash so estimates stay in the right ballpark.
DEFAULT_PRICING = PRICING["deepseek-flash"]


def pricing_for(model: str) -> ModelPricing:
    return PRICING.get(model, DEFAULT_PRICING)


# ---------------------------------------------------------------------------
# Per-model accumulator
# ---------------------------------------------------------------------------


@dataclass
class ModelUsage:
    calls: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    # Billable per-request tools (successful only).
    web_searches: int = 0
    fetches: int = 0
    # The part of the token counts above that was billed during a DeepSeek
    # peak window (2x). Subsets, not extra tokens: cost() prices these at the
    # surge rate and the rest at the regular rate.
    peak_cache_hit_tokens: int = 0
    peak_cache_miss_tokens: int = 0
    peak_output_tokens: int = 0

    @property
    def tool_cost(self) -> float:
        return self.web_searches * SEARCH_REQUEST_USD + self.fetches * FETCH_REQUEST_USD

    @property
    def input_tokens(self) -> int:
        return self.cache_hit_tokens + self.cache_miss_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost(self, model: str, surge: bool = False) -> float:
        """USD cost at the rates that were in force when the tokens were billed.

        Every call is tagged at record time: tokens billed inside a DeepSeek
        peak window are priced at the surge rate (2x), the rest at the regular
        rate. ``surge=True`` is the worst case: every token at the surge rate.
        """
        p = pricing_for(model)
        if surge:
            return (
                self.cache_hit_tokens * p.surge_cache_hit
                + self.cache_miss_tokens * p.surge_cache_miss
                + self.output_tokens * p.surge_output
            ) / 1_000_000 + self.tool_cost
        hit_peak = min(self.peak_cache_hit_tokens, self.cache_hit_tokens)
        miss_peak = min(self.peak_cache_miss_tokens, self.cache_miss_tokens)
        out_peak = min(self.peak_output_tokens, self.output_tokens)
        return (
            (self.cache_hit_tokens - hit_peak) * p.cache_hit
            + hit_peak * p.surge_cache_hit
            + (self.cache_miss_tokens - miss_peak) * p.cache_miss
            + miss_peak * p.surge_cache_miss
            + (self.output_tokens - out_peak) * p.output
            + out_peak * p.surge_output
        ) / 1_000_000 + self.tool_cost

    @property
    def peak_tokens(self) -> int:
        return self.peak_cache_hit_tokens + self.peak_cache_miss_tokens + self.peak_output_tokens

    def hit_rate(self) -> float:
        return (self.cache_hit_tokens / self.input_tokens) if self.input_tokens else 0.0

    def merge(self, other: "ModelUsage") -> "ModelUsage":
        return ModelUsage(
            calls=self.calls + other.calls,
            cache_hit_tokens=self.cache_hit_tokens + other.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens + other.cache_miss_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            web_searches=self.web_searches + other.web_searches,
            fetches=self.fetches + other.fetches,
            peak_cache_hit_tokens=self.peak_cache_hit_tokens + other.peak_cache_hit_tokens,
            peak_cache_miss_tokens=self.peak_cache_miss_tokens + other.peak_cache_miss_tokens,
            peak_output_tokens=self.peak_output_tokens + other.peak_output_tokens,
        )


def _details(obj: Any, name: str) -> Any:
    """A nested token-details bag, whether the usage came back as an object or a dict."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _get(obj: Any, name: str) -> Optional[int]:
    """Pull a token field off an SDK usage object, dict, or model_extra bag."""
    val = getattr(obj, name, None)
    if val is None and isinstance(obj, dict):
        val = obj.get(name)
    if val is None:
        extra = getattr(obj, "model_extra", None)
        if isinstance(extra, dict):
            val = extra.get(name)
    return int(val) if isinstance(val, (int, float)) else None


# ---------------------------------------------------------------------------
# Tracker
# ---------------------------------------------------------------------------


# Process-wide override of the peak clock (the test suite pins it off-peak so
# cost assertions do not depend on the hour they run at). None = surge.is_surge.
PEAK_CLOCK: Optional[Callable[[], bool]] = None


@dataclass
class UsageTracker:
    # model -> usage, split by scope so /settings can show orchestrator vs subagents
    orchestrator: Dict[str, ModelUsage] = field(default_factory=dict)
    subagent: Dict[str, ModelUsage] = field(default_factory=dict)

    # Size of the prompt on the most recent orchestrator call — i.e. how full
    # the conversation context currently is.
    context_tokens: int = 0
    context_cache_hit_tokens: int = 0
    context_cache_miss_tokens: int = 0
    # True after compact writes a char/4 guess into ``context_tokens``. Auto-compact
    # ignores that guess and waits for the next real orchestrator ``input_tokens``.
    context_tokens_estimated: bool = False

    # Rolling per-turn deltas (reset at the top of each user turn)
    turn: ModelUsage = field(default_factory=ModelUsage)
    turn_cost: float = 0.0
    # Clock hook: is a DeepSeek peak window in force right now? Tests replace it.
    peak_clock: Optional[Callable[[], bool]] = None

    def _peak_now(self) -> bool:
        clock = self.peak_clock or PEAK_CLOCK
        if clock is None:
            from .surge import is_surge as clock
        try:
            return bool(clock())
        except Exception:
            return False

    def record(self, model: str, usage: Any, scope: str = "orchestrator") -> ModelUsage:
        """Fold one API response's usage into the totals. Returns the delta.

        The codec reports ``input_tokens`` with a nested
        ``input_tokens_details.cached_tokens`` (only the cached count; the miss
        is derived). The flat ``prompt_cache_hit/miss_tokens`` spelling is also
        accepted, so usage stored by older versions still totals correctly.
        """
        if usage is None:
            return ModelUsage()

        prompt = _get(usage, "input_tokens")
        if prompt is None:
            prompt = _get(usage, "prompt_tokens") or 0

        hit = _get(usage, "prompt_cache_hit_tokens")
        miss = _get(usage, "prompt_cache_miss_tokens")
        if hit is None:
            hit = _get(_details(usage, "input_tokens_details"), "cached_tokens")
        if hit is None and miss is None:
            # Provider didn't report the split — assume worst case (all miss).
            hit, miss = 0, prompt
        elif hit is None:
            hit = max(prompt - (miss or 0), 0)
        elif miss is None:
            miss = max(prompt - hit, 0)

        output = _get(usage, "output_tokens")
        if output is None:
            output = _get(usage, "completion_tokens") or 0
        reasoning = (
            _get(_details(usage, "output_tokens_details"), "reasoning_tokens")
            or _get(_details(usage, "completion_tokens_details"), "reasoning_tokens")
            or 0
        )

        server_tools = _details(usage, "server_tool_use")
        searches = _get(server_tools, "tool_search") if server_tools is not None else None

        peak = self._peak_now()
        delta = ModelUsage(
            calls=1,
            cache_hit_tokens=hit,
            cache_miss_tokens=miss,
            output_tokens=output,
            reasoning_tokens=reasoning,
            web_searches=searches or 0,
            peak_cache_hit_tokens=hit if peak else 0,
            peak_cache_miss_tokens=miss if peak else 0,
            peak_output_tokens=output if peak else 0,
        )

        bucket = self.orchestrator if scope == "orchestrator" else self.subagent
        bucket[model] = bucket.get(model, ModelUsage()).merge(delta)

        self.turn = self.turn.merge(delta)
        self.turn_cost += delta.cost(model)

        if scope == "orchestrator":
            self.context_tokens = prompt
            self.context_cache_hit_tokens = hit
            self.context_cache_miss_tokens = miss
            self.context_tokens_estimated = False

        return delta

    def record_fetch(self, ok: bool = True) -> ModelUsage:
        """Count one DeepSeek Fetch API call (billed only when it succeeded)."""
        delta = ModelUsage(fetches=1 if ok else 0)
        if ok:
            self.subagent[FETCH_BUCKET] = self.subagent.get(FETCH_BUCKET, ModelUsage()).merge(delta)
            self.turn = self.turn.merge(delta)
            self.turn_cost += delta.tool_cost
        return delta

    def record_search(self, ok: bool = True) -> ModelUsage:
        """Count one DeepSeek Search API call (billed only when it returned results)."""
        delta = ModelUsage(web_searches=1 if ok else 0)
        if ok:
            self.subagent[SEARCH_BUCKET] = self.subagent.get(SEARCH_BUCKET, ModelUsage()).merge(delta)
            self.turn = self.turn.merge(delta)
            self.turn_cost += delta.tool_cost
        return delta

    # -- queries ----------------------------------------------------------

    def start_turn(self) -> None:
        self.turn = ModelUsage()
        self.turn_cost = 0.0

    def per_model(self) -> Dict[str, ModelUsage]:
        out: Dict[str, ModelUsage] = {}
        for bucket in (self.orchestrator, self.subagent):
            for model, u in bucket.items():
                out[model] = out.get(model, ModelUsage()).merge(u)
        return out

    def totals(self) -> ModelUsage:
        total = ModelUsage()
        for u in self.per_model().values():
            total = total.merge(u)
        return total

    def cost(self, surge: bool = False) -> float:
        return sum(u.cost(model, surge=surge) for model, u in self.per_model().items())

    def scope_cost(self, scope: str, surge: bool = False) -> float:
        bucket = self.orchestrator if scope == "orchestrator" else self.subagent
        return sum(u.cost(model, surge=surge) for model, u in bucket.items())

    def reset(self) -> None:
        self.orchestrator.clear()
        self.subagent.clear()
        self.context_tokens = 0
        self.context_cache_hit_tokens = 0
        self.context_cache_miss_tokens = 0
        self.context_tokens_estimated = False
        self.start_turn()


_tracker: Optional[UsageTracker] = None
# web.py records searches/fetches from worker threads. Two racing first calls
# must not build two trackers: the counts on the losing one would be dropped.
_TRACKER_LOCK = threading.Lock()


def get_usage() -> UsageTracker:
    global _tracker
    if _tracker is None:
        with _TRACKER_LOCK:
            if _tracker is None:
                _tracker = UsageTracker()
    return _tracker


def format_cost(usd: float) -> str:
    """Costs here are often sub-cent; don't round them away to $0.00."""
    if usd == 0:
        return "$0.00"
    if usd < 0.01:
        return f"${usd:.5f}"
    return f"${usd:.4f}"

"""
Token accounting and cost estimation.

Every API call in the process funnels its ``usage`` block through the global
tracker, so /cost, /settings and the auto-compaction trigger all read from the
same numbers. DeepSeek reports the prefix-cache split directly
(``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``), which matters a
lot here: a cache hit is ~50x cheaper than a miss.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# Pricing (USD per 1M tokens) — SPEC §3.2
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelPricing:
    cache_hit: float
    cache_miss: float
    output: float


PRICING: Dict[str, ModelPricing] = {
    "deepseek-v4-flash": ModelPricing(cache_hit=0.0028, cache_miss=0.14, output=0.28),
    "deepseek-v4-pro": ModelPricing(cache_hit=0.003625, cache_miss=0.435, output=0.87),
}

# Anything unknown is priced as Flash so estimates stay in the right ballpark.
DEFAULT_PRICING = PRICING["deepseek-v4-flash"]


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

    @property
    def input_tokens(self) -> int:
        return self.cache_hit_tokens + self.cache_miss_tokens

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost(self, model: str) -> float:
        p = pricing_for(model)
        return (
            self.cache_hit_tokens * p.cache_hit
            + self.cache_miss_tokens * p.cache_miss
            + self.output_tokens * p.output
        ) / 1_000_000

    def hit_rate(self) -> float:
        return (self.cache_hit_tokens / self.input_tokens) if self.input_tokens else 0.0

    def merge(self, other: "ModelUsage") -> "ModelUsage":
        return ModelUsage(
            calls=self.calls + other.calls,
            cache_hit_tokens=self.cache_hit_tokens + other.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens + other.cache_miss_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
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

    # Rolling per-turn deltas (reset at the top of each user turn)
    turn: ModelUsage = field(default_factory=ModelUsage)
    turn_cost: float = 0.0

    def record(self, model: str, usage: Any, scope: str = "orchestrator") -> ModelUsage:
        """Fold one API response's usage into the totals. Returns the delta.

        The Responses API names these fields differently from chat completions —
        ``input_tokens`` with a nested ``input_tokens_details.cached_tokens``
        rather than ``prompt_tokens`` with a flat hit/miss split — and reports
        only the cached count, leaving the miss to be derived. Both spellings are
        accepted so the tracker stays honest whichever endpoint a caller used.
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

        delta = ModelUsage(
            calls=1,
            cache_hit_tokens=hit,
            cache_miss_tokens=miss,
            output_tokens=output,
            reasoning_tokens=reasoning,
        )

        bucket = self.orchestrator if scope == "orchestrator" else self.subagent
        bucket[model] = bucket.get(model, ModelUsage()).merge(delta)

        self.turn = self.turn.merge(delta)
        self.turn_cost += delta.cost(model)

        if scope == "orchestrator":
            self.context_tokens = prompt
            self.context_cache_hit_tokens = hit
            self.context_cache_miss_tokens = miss

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

    def cost(self) -> float:
        return sum(u.cost(model) for model, u in self.per_model().items())

    def scope_cost(self, scope: str) -> float:
        bucket = self.orchestrator if scope == "orchestrator" else self.subagent
        return sum(u.cost(model) for model, u in bucket.items())

    def reset(self) -> None:
        self.orchestrator.clear()
        self.subagent.clear()
        self.context_tokens = 0
        self.context_cache_hit_tokens = 0
        self.context_cache_miss_tokens = 0
        self.start_turn()


_tracker: Optional[UsageTracker] = None


def get_usage() -> UsageTracker:
    global _tracker
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

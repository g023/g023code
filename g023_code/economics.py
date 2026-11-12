"""When compaction pays: a ski-rental rule over measured context growth.

Keeping history is "renting": every orchestrator call re-reads the whole
prefix at the cache-hit rate. Compacting is "buying": one summary (or a free
same-response ``[handoff]``) plus a one-time miss on the rebuilt body. The
deterministic ski-rental rule buys once accumulated rent equals the purchase
price. That is 2-competitive in the worst case. When context grows linearly
it lands on the EOQ optimum n* = sqrt(2C / (h*g)): carry h*g*n^2/2 equals C
exactly there.

Units, per call, regular rates (ds_pricing.json):
  rent  = (P - B) * hit             P = real context tokens, B = post-compact size
  buy   = [no handoff: P * hit + S * out] + (B - standing) * (miss - hit)

The standing prefix (instructions + tools) is byte-stable across a compaction
and stays a hit, so only the rebuilt body is re-missed. Every price doubles
in a surge window, so the rent/buy ratio, and so the decision, is identical
on and off peak. Regular rates suffice.

Information loss costs more than dollars, since a summary can drop a detail
the model then re-reads. ``fidelity`` (default 3) multiplies the buy price
for that, and ``floor_tokens`` refuses to compact a context that is still
small. ``compact_at_tokens`` stays the hard ceiling elsewhere.
The rule was checked against a brute-force simulation of every compaction point
during development.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .usage import pricing_for

# Summary output a summarizer call pays when no inline handoff exists
# (reasoning included). The prompt asks for terse; 2k is a cautious prior.
SUMMARY_OUTPUT_TOKENS = 2_000
# Rebuilt body (summary + sticky goal + kept tail) before one is measured.
BODY_TOKENS_PRIOR = 8_000
DEFAULT_FIDELITY = 3.0
DEFAULT_FLOOR_TOKENS = 64_000


def compaction_cost(
    context_tokens: int,
    body_tokens: int,
    *,
    inline_handoff: bool,
    model: str = "deepseek-flash",
    summary_tokens: int = SUMMARY_OUTPUT_TOKENS,
) -> float:
    """USD of one compaction: optional summarizer pass plus the rebuilt-body miss."""
    p = pricing_for(model)
    summarize = 0.0 if inline_handoff else (
        context_tokens * p.cache_hit + summary_tokens * p.output
    )
    bust = max(body_tokens, 0) * (p.cache_miss - p.cache_hit)
    return (summarize + bust) / 1_000_000


@dataclass
class CompactionMeter:
    """Accumulated rent since the last compaction; says when to buy.

    ``observe`` is fed the orchestrator's cumulative call count and the last
    real context gauge. It is idempotent per call count, so the orchestrator
    can call it from every place it checks the trigger.
    """

    standing_tokens: int = 0
    body_tokens: int = BODY_TOKENS_PRIOR
    fidelity: float = DEFAULT_FIDELITY
    floor_tokens: int = DEFAULT_FLOOR_TOKENS
    model: str = "deepseek-flash"
    rent: float = 0.0
    _last_calls: int = 0
    _measure_next: bool = False

    def reset(self, calls: int = 0) -> None:
        """A compaction (or /clear) happened: rent restarts, learn B next call."""
        self.rent = 0.0
        self._last_calls = calls
        self._measure_next = True

    def observe(self, calls: int, context_tokens: int) -> None:
        if calls < self._last_calls:  # usage was reset (/clear)
            self.reset(calls)
        new = calls - self._last_calls
        if new <= 0 or context_tokens <= 0:
            return
        self._last_calls = calls
        if self._measure_next:
            # First real gauge after a compaction is the rebuilt size.
            self._measure_next = False
            self.body_tokens = max(context_tokens - self.standing_tokens, 0)
        floor = self.standing_tokens + self.body_tokens
        hit = pricing_for(self.model).cache_hit
        self.rent += new * max(context_tokens - floor, 0) * hit / 1_000_000

    def price(self, context_tokens: int, *, inline_handoff: bool) -> float:
        return self.fidelity * compaction_cost(
            context_tokens, self.body_tokens, inline_handoff=inline_handoff, model=self.model
        )

    def due(self, context_tokens: int, *, inline_handoff: bool = False) -> bool:
        if context_tokens < self.floor_tokens:
            return False
        return self.rent >= self.price(context_tokens, inline_handoff=inline_handoff)

    def describe(self, context_tokens: int, inline_handoff: bool = False) -> str:
        buy = self.price(context_tokens, inline_handoff=inline_handoff)
        pct = (self.rent / buy * 100) if buy > 0 else 0.0
        return (
            f"rent ${self.rent:.5f} of ${buy:.5f} compaction price ({pct:.0f}%), "
            f"floor {self.floor_tokens:,} tok"
        )


def optimal_calls(growth_tokens: float, cost_usd: float, model: str = "deepseek-flash") -> Optional[float]:
    """EOQ n* = sqrt(2C/(h*g)): calls between compactions under linear growth."""
    h = pricing_for(model).cache_hit / 1_000_000
    if growth_tokens <= 0 or h <= 0 or cost_usd <= 0:
        return None
    return (2 * cost_usd / (h * growth_tokens)) ** 0.5

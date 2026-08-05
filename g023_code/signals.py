"""
Drift signals — what the loop noticed that nobody raised an exception about.

The client is a thin, unvalidating wrapper over the API's JSON on purpose: a
field DeepSeek adds tomorrow has to reach the model without a client release.
The price of that openness is that a *renamed* field fails quietly. ``output_text``
returns "", the model looks silent, and nothing anywhere raises. The realistic
worst case for this program is silent degradation, not a crash.

So this module records the three cheapest observations that would move first,
and does nothing else with them:

* **unknown item types** — the earliest possible warning of an additive change,
  and free to detect;
* **empty responses with no stated reason** — what a renamed content field looks
  like from outside, and also what a genuinely silent model looks like, which is
  why it is recorded rather than acted on;
* **prefix-cache hit rate against its own history** — a continuous number
  reported on every call, so it moves before anything shows up in the answers.

None of this is a diagnosis. A model behaviour change, a schema change, and
drift in our own prompts all present identically from here — same call, worse
output, no error — and these signals do not separate those causes. They only
make the change visible, and say when it started.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# A day's hit rate has to differ from the trailing baseline by at least this much
# before it is worth mentioning. Below it, ordinary session-to-session variation
# (a long session vs a short one, one compaction vs none) dominates.
HIT_RATE_ALERT_DROP = 0.15
# Fewer days than this is not a baseline, it is one other session.
MIN_BASELINE_DAYS = 2


@dataclass
class Observation:
    kind: str
    detail: str
    turn: int
    at: float = field(default_factory=time.time)


@dataclass
class SignalLog:
    """Session-local record. Deliberately in memory: this is a session's view."""

    observations: list[Observation] = field(default_factory=list)
    # type -> how many responses carried it
    unknown_types: dict[str, int] = field(default_factory=dict)
    empty_responses: int = 0

    def note_unknown_types(self, types: set[str], turn: int) -> None:
        for name in sorted(types):
            first = name not in self.unknown_types
            self.unknown_types[name] = self.unknown_types.get(name, 0) + 1
            if first:
                self.observations.append(
                    Observation(
                        kind="unknown_item_type",
                        detail=(
                            f"response contained an item of type {name!r}, which this "
                            "client does not read. It is echoed back verbatim and skipped, "
                            "so it is invisible in the trace rather than broken."
                        ),
                        turn=turn,
                    )
                )

    def note_empty_response(self, detail: str, turn: int) -> None:
        self.empty_responses += 1
        self.observations.append(Observation(kind="empty_response", detail=detail, turn=turn))

    def recent(self, limit: int = 20) -> list[Observation]:
        return self.observations[-limit:]

    def reset(self) -> None:
        self.observations.clear()
        self.unknown_types.clear()
        self.empty_responses = 0


def hit_rate_verdict(history: list[dict]) -> tuple[str, Optional[float], Optional[float]]:
    """Compare the newest day's hit rate against the days before it.

    Returns ``(verdict, latest, baseline)``. The verdict is deliberately plain
    about not knowing: with one day of history there is nothing to compare, and
    saying so beats reporting a number as if it meant something.
    """
    if not history:
        return ("no history yet — the hit rate is recorded from this session on", None, None)

    by_day: dict[str, tuple[int, int]] = {}
    for row in history:
        hit, miss = by_day.get(row["day"], (0, 0))
        by_day[row["day"]] = (hit + row["hit_tokens"], miss + row["miss_tokens"])

    days = sorted(by_day)
    latest_day = days[-1]
    hit, miss = by_day[latest_day]
    latest = hit / (hit + miss) if (hit + miss) else 0.0

    prior = days[:-1]
    if len(prior) < MIN_BASELINE_DAYS:
        return (
            f"{len(prior)} earlier day(s) recorded — not enough to call a baseline yet",
            latest,
            None,
        )

    p_hit = sum(by_day[d][0] for d in prior)
    p_miss = sum(by_day[d][1] for d in prior)
    baseline = p_hit / (p_hit + p_miss) if (p_hit + p_miss) else 0.0

    drop = baseline - latest
    if drop >= HIT_RATE_ALERT_DROP:
        return (
            f"down {drop:.0%} against the previous {len(prior)} days — something is "
            "breaking the prefix. That could be a changed prompt or tool list on this "
            "side, or a server-side change in how items serialise; this number does not "
            "say which",
            latest,
            baseline,
        )
    if latest - baseline >= HIT_RATE_ALERT_DROP:
        return (f"up {latest - baseline:.0%} against the previous {len(prior)} days", latest, baseline)
    return (f"steady against the previous {len(prior)} days", latest, baseline)


_log: Optional[SignalLog] = None


def get_signals() -> SignalLog:
    global _log
    if _log is None:
        _log = SignalLog()
    return _log

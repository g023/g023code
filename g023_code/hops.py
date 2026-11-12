"""Round-trip economics: ordered-batch contract, write checks, step budgets (docs/hops.md).

Under prefix-cached pricing a model call ("hop") is expensive mostly because
the model re-thinks: measured over 543 live runs, a hop's dollars were 55 %
output, 34 % uncached input and 11 % cached prefix. So this module saves hops,
not context:

* Ordered batches: calls in one response already run in program order
  (orchestrator._batch_segments). The prompt says so, and a failed write
  short-circuits the later checks/writes it was planned under (``&&``), so
  "edit + the test that checks it" is safe in ONE response.
* Write checks: Edit/WriteFile take an optional ``check`` command that runs
  once after the response's writes, only if all of them succeeded, and comes
  back on the write. A schema field moved behaviour where prompt prose did not.
* Step budgets: the model may declare ``[budget MIN-MAX +OVER]`` model calls
  for the step it starts; every batch ends with a tracker line, and an overrun
  past MAX+OVER gets one wrap-up note (a signal, never a hard stop).

Pure helpers; the orchestrator owns the state and the wiring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from .config import settings

# The line the standing prompt has always used for batching. With ordered
# batches on it is replaced (not appended to) so the prompt stays contradiction-free.
BATCH_LINE = (
    "Every model call replays the whole context, so when several tool calls do not depend on each\n"
    "other's results, issue them together in one response rather than one per response.\n"
)

ORDERED_BATCH_TEXT = (
    "Every model call replays the whole context and re-thinks, so make each response do as much as\n"
    "you already know how to do. Calls in one response run in the order written: reads overlap, and\n"
    "each Edit, WriteFile or Bash waits for every call before it. A call that only needs an earlier\n"
    "one to have happened, not to see its output, belongs in the same response: make all the edits\n"
    "you already know, then the command that checks them. If a write fails, the later Bash calls and\n"
    "writes to that file in the same response are skipped, so a check never runs on a half-applied change.\n"
)

BUDGET_ADDENDUM = (
    "\nStep budgets: each model call is the unit of cost. When you start a step of the work (locating,\n"
    "changing, verifying), write [budget MIN-MAX +OVER] in your reply: the fewest and most model calls\n"
    "you expect the step to take, and how many more you would spend only if truly needed. Tool results\n"
    "end with a [hop ...] tracker. Declare a new budget when you start a new step.\n"
)

_BUDGET_RE = re.compile(
    r"\[\s*budget\s*:?\s*(\d{1,3})(?:\s*(?:-|–|to)\s*(\d{1,3}))?(?:\s*\+\s*(\d{1,3}))?\s*\]",
    re.IGNORECASE,
)

TRACKER_PREFIX = "[hop "


def ordered_enabled() -> bool:
    return bool(getattr(settings, "ordered_batches", False))


def budget_enabled() -> bool:
    return bool(getattr(settings, "step_budget", False))


def system_text(base: str) -> str:
    """Apply the enabled addenda to the standing prompt."""
    if ordered_enabled() and BATCH_LINE in base:
        base = base.replace(BATCH_LINE, ORDERED_BATCH_TEXT)
    if budget_enabled():
        base += BUDGET_ADDENDUM
    return base


def prompt_key() -> tuple:
    return (ordered_enabled(), budget_enabled(), check_enabled())


# ---------------------------------------------------------------------------
# Ordered batches: && semantics
# ---------------------------------------------------------------------------


def write_failed(result: str) -> bool:
    """True when an Edit/WriteFile result says nothing was written."""
    import json

    try:
        data = json.loads(result or "")
    except (ValueError, TypeError):
        # Encoded/plain bodies are successes in this codebase; errors are JSON.
        return False
    if not isinstance(data, dict):
        return False
    return bool(data.get("error")) or data.get("ok") is False


def skipped_result(tool: str, failed: list[tuple[str, str]]) -> str:
    """The typed result of a call the short-circuit did not run."""
    import json

    first_tool, first_path = failed[0]
    return json.dumps(
        {
            "error": "skipped",
            "tool": tool,
            "after_failed": f"{first_tool} {first_path}".strip(),
            "hint": (
                "Not run: an earlier write in this response failed, and this call was planned on top "
                "of it. Fix that write, then issue this call again in the same response."
            ),
        }
    )


def should_skip(name: str, path: str, failed: list[tuple[str, str]], write_tools) -> bool:
    """A failed write skips later Bash calls and later writes to the same file."""
    if not failed:
        return False
    if name == "Bash":
        return True
    if name in write_tools:
        return any(p == path for _t, p in failed)
    return False


# ---------------------------------------------------------------------------
# Write checks: the check rides on the write (schema, not prose)
# ---------------------------------------------------------------------------

CHECK_ARG = "check"
CHECK_TOOLS = ("Edit", "WriteFile")
CHECK_MAX = 2  # distinct check commands run per response
CHECK_PROPERTY = {
    "type": "string",
    "description": (
        "Optional shell command that verifies this change (e.g. the test that covers it). It runs "
        "once, after the last write of this response, only if every write in the response "
        "succeeded; its result comes back with this call, so you need no extra turn to run it."
    ),
}


def check_enabled() -> bool:
    return bool(getattr(settings, "write_check", False))


def add_check_to_schemas(tools) -> list:
    """Schemas with an optional ``check`` on Edit/WriteFile (copies, never in place)."""
    import copy

    out = []
    for schema in tools:
        if schema.get("name") in CHECK_TOOLS:
            schema = copy.deepcopy(schema)
            params = schema.setdefault("parameters", {})
            params.setdefault("properties", {})[CHECK_ARG] = dict(CHECK_PROPERTY)
        out.append(schema)
    return out


def take_check(args: dict) -> Optional[str]:
    """Remove and return the check command from parsed arguments (None if absent/blank)."""
    if not isinstance(args, dict) or CHECK_ARG not in args:
        return None
    value = args.pop(CHECK_ARG)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def plan_checks(offered: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(carrier call id, command) to run: distinct commands, each carried by its LAST offerer."""
    last: dict[str, str] = {}
    order: list[str] = []
    for cid, cmd in offered:
        if cmd not in last:
            order.append(cmd)
        last[cmd] = cid
    return [(last[cmd], cmd) for cmd in order[:CHECK_MAX]]


def merge_check(write_result: str, command: str, check_result: Optional[str], reason: str = "") -> str:
    """Attach a check's result (or why it did not run) to the carrying write's result."""
    import json

    if check_result is None:
        entry: dict = {"command": command, "skipped": reason or "not run"}
    else:
        try:
            parsed = json.loads(check_result)
        except (ValueError, TypeError):
            parsed = None
        entry = dict(parsed) if isinstance(parsed, dict) else {"output": str(check_result)}
        entry = {"command": command, **{k: v for k, v in entry.items() if k != "command"}}
    try:
        data = json.loads(write_result or "")
    except (ValueError, TypeError):
        data = None
    if isinstance(data, dict):
        data["check"] = entry
        return json.dumps(data)
    return f"{write_result}\n[check] {json.dumps(entry)}"


# ---------------------------------------------------------------------------
# Step budgets
# ---------------------------------------------------------------------------


@dataclass
class Budget:
    low: int
    high: int
    over: int
    start_hop: int

    def label(self) -> str:
        span = f"{self.low}" if self.low == self.high else f"{self.low}-{self.high}"
        return span + (f" +{self.over}" if self.over else "")


def parse_budget(*texts: str) -> Optional[tuple[int, int, int]]:
    """Last ``[budget MIN-MAX +OVER]`` in the texts, normalised; None if absent."""
    found = None
    for text in texts:
        for m in _BUDGET_RE.finditer(text or ""):
            found = m
    if found is None:
        return None
    low = int(found.group(1))
    high = int(found.group(2)) if found.group(2) else low
    over = int(found.group(3)) if found.group(3) else 0
    low, high = min(low, high), max(low, high)
    low = max(1, min(low, 99))
    high = max(low, min(high, 99))
    over = max(0, min(over, 99))
    return low, high, over


@dataclass
class BudgetState:
    """Per-turn budget bookkeeping (reset each user turn)."""

    current: Optional[Budget] = None
    warned_for: Optional[int] = None  # start_hop of the budget already warned about
    history: list = field(default_factory=list)  # (label, used hops) of finished steps

    def observe(self, hop: int, *texts: str) -> None:
        parsed = parse_budget(*texts)
        if parsed is None:
            return
        low, high, over = parsed
        cur = self.current
        if cur is not None and (cur.low, cur.high, cur.over) == (low, high, over) and hop > cur.start_hop:
            # Restating the same budget is not a new step.
            return
        if cur is not None:
            self.history.append((cur.label(), hop - cur.start_hop))
        self.current = Budget(low, high, over, hop)

    def used(self, hop: int) -> int:
        return 0 if self.current is None else hop - self.current.start_hop + 1

    def tracker(self, hop: int, turn_cost: str) -> str:
        """One line for the end of a batch's last result."""
        parts = [f"{TRACKER_PREFIX}{hop}"]
        cur = self.current
        if cur is not None:
            parts.append(f"step {self.used(hop)} of {cur.label()}")
        parts.append(f"request {turn_cost}")
        line = " · ".join(parts) + "]"
        if cur is not None and self.used(hop) > cur.high + cur.over and self.warned_for != cur.start_hop:
            self.warned_for = cur.start_hop
            line += (
                f"\n[over budget: this step has taken {self.used(hop)} calls against {cur.label()}. "
                "Finish with what you have, or declare a new budget and say why.]"
            )
        return line

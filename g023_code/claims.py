"""Claim check: does the final answer's test claim match the turn's own evidence?

A final answer that says "all tests pass" is a claim about a run. The harness
recorded every test command of the turn (exit code, oracle summary, and how
many writes had happened before it), so it can compare the claim with the
last run at no model cost:

  * the last run failed            -> "the last test run ... exited 1 (1 failed, 4 passed)"
  * files changed after the last run -> "the last test run was before the final edit"
  * no test run at all, files written -> "no test command ran this turn"

A mismatch prints a short note under the answer. It does not change the
answer, cost a hop, or enter the model's history. Qualified claims ("all
other tests pass", "except test_env") are left alone. Setting
``claim_check`` (env G023_CLAIM_CHECK).
"""
from __future__ import annotations

import re
from typing import Optional

_PASS = re.compile(
    r"(?:\b(?:all|every|the\s+(?:full|whole|entire)|full|whole)\b[^.\n]{0,40}?\btests?\b[^.\n]{0,25}?"
    r"\b(?:pass|passes|passed|passing|green|succeed|succeeds|succeeded)\b)"
    r"|(?:\b(?:test\s+)?suite\b[^.\n]{0,25}?\b(?:pass|passes|passed|passing|green)\b)"
    r"|(?:\btests?\b[^.\n]{0,15}?\b(?:now\s+)?(?:pass|passes|passed|passing)\b)"
    r"|(?:\b\d+\s+passed\b)",
    re.I,
)
_QUALIFIED = re.compile(
    r"\b(?:fail\w*|error\w*|except|other\s+than|apart\s+from|aside\s+from|besides|but|pre-?existing|"
    r"unrelated|other|remaining|not|n't|skip\w*|xfail\w*)\b",
    re.I,
)
# Scoped claims ("the 6 new tests pass", "the ledger tests pass") talk about a
# subset; the last run's failures may legitimately be outside it.
_SCOPED = re.compile(
    r"\b(?:new|added|these|those|my|targeted|relevant|affected|related|selected|specific)\b"
    r"|\b\w+(?:/\w+)+\s+tests?\b|\btest_\w+",
    re.I,
)
_UNIVERSAL = re.compile(r"\b(?:all|every|full|whole|entire)\b|\btest\s+suite\b|^\W*(?:the\s+)?tests\s+(?:now\s+)?pass", re.I)
_COUNT = re.compile(r"\b(\d+)\s+(?:\w+\s+){0,3}?(?:tests?\s+)?(?:pass|passes|passed|passing)\b", re.I)


def enabled() -> bool:
    try:
        from .config import settings

        return bool(getattr(settings, "claim_check", False))
    except Exception:
        return False


def pass_claim(answer: str) -> Optional[str]:
    """The unqualified "tests pass" sentence of ``answer``, if any."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", answer or ""):
        m = _PASS.search(sentence)
        if m and not _QUALIFIED.search(sentence):
            return sentence.strip()[:160]
    return None


def check(answer: str, checks: list[dict], writes: int) -> Optional[str]:
    """A note when the answer's pass claim is not backed by the last test run."""
    claim = pass_claim(answer)
    if claim is None:
        return None
    if not checks:
        if writes:
            return f'The answer says "{claim}", but no test command ran this turn after files changed.'
        return None
    last = checks[-1]
    # Runs after the last write are all evidence: the answer may quote an
    # earlier green run (say, with an env var set) rather than the last one.
    current = [c for c in checks if int(c.get("seq", writes) or 0) >= writes]
    if any(c.get("exit") in (0, None) for c in current):
        return None
    m = _COUNT.search(claim)
    if m and any(re.search(rf"\b{m.group(1)}\s+passed\b", str(c.get("summary") or "")) for c in current):
        return None
    code = last.get("exit")
    summary = str(last.get("summary") or "")
    command = str(last.get("command") or "")
    if len(command) > 70:
        command = command[:69] + "…"
    if code not in (0, None):
        if _SCOPED.search(claim) and not re.search(r"\btest\s+suite\b", claim, re.I):
            return None
        if not m and not _UNIVERSAL.search(claim):
            return None
        detail = f" ({summary})" if summary else ""
        return f'The answer says "{claim}", but the last test run (`{command}`) exited {code}{detail}.'
    # Every run predates the last write and the last one passed.
    return f'The answer says "{claim}", but files changed after the last test run (`{command}`).'

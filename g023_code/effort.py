"""
Reasoning-effort encode/decode for DeepSeek V4.1 Flash (Anthropic-format wire).

The endpoint takes ``thinking: {type: enabled|disabled}`` plus
``output_config.effort``, whose real levels are ``low`` / ``high`` / ``max``
(DeepSeek maps ``minimal``->low, ``medium``/``xhigh``->high, ``ultra``->max
server-side). Numbers are rejected. So the user/model scale is an integer
``0``–``100`` and the wire value is always one of the real levels; this module
is that mapping. I/O stays out.

This harness never sends ``max``. ``MAX_EFFORT = 75`` (wire ``high``) is both
the thinking-on default and the ceiling: a ``[next_effort: 100]``,
``/thinking max``, ``/goal``, or Agent ``reasoning=max`` is stored and sent as
75 / ``high``. ``max`` often burns the whole output budget on thinking and
returns an empty answer.

The same reply can also carry a compact ``[handoff]...[/handoff]`` block
ahead of ``[next_effort: N]``. Both are stripped from user-visible text
and from history items so the next turn can reuse the handoff instead of
paying for a second summarizer call.

Wire mapping (stored values are capped at ``MAX_EFFORT`` first):

* ``0`` / off → ``none`` (the codec sends ``thinking: disabled``)
* ``1``–``50`` → ``low``
* ``51``–``100`` → ``high``   (75 is the thinking-on default and the ceiling)
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Thinking-on default on the 1–100 scale. Wire form is ``high``. Also the
# ceiling: this harness never stores or sends a value above 75.
DEFAULT_EFFORT = 75
MAX_EFFORT = 75
DISABLE_WIRE = "none"

# Alias → stored int. ``high`` / ``max`` / ``xhigh`` are all 75 so old configs
# that say ``"high"`` or ``"max"`` become the same default the sweetspot uses,
# and a model that asks for 100 still gets the ceiling.
ALIAS_TO_INT: dict[str, int] = {
    "none": 0,
    "off": 0,
    "disabled": 0,
    "minimal": 12,
    "low": 50,
    "medium": 62,
    "high": 75,
    "xhigh": 75,
    "max": 75,
}

# Levels output_config.effort accepts (DeepSeek folds minimal/medium/xhigh/ultra
# onto these server-side). ``none`` is the harness's "thinking disabled".
WIRE_VARIANTS: tuple[str, ...] = ("none", "low", "high", "max")
# Levels this harness is willing to put on a request (never ``max``).
HARNESS_WIRE_VARIANTS: tuple[str, ...] = ("none", "low", "high")

# Trailing-only control blocks. Locked shapes (the live model attached these
# on the same response):
#   [handoff] ... [/handoff]     — compact summary for the next turn
#   [next_effort: N]             — last line; effort for the next request
# HTML-comment and <meta> forms of next_effort are accepted if they appear as
# the last thing in the text.
_META_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?:\r?\n[ \t]*)?\[next_effort\s*[:=]\s*([^\]]+?)\]\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:\r?\n[ \t]*)?<!--\s*next_effort\s*[:=]\s*([^>]+?)\s*-->\s*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:\r?\n[ \t]*)?<meta>\s*next_effort\s*[:=]\s*([^<]+?)\s*</meta>\s*$",
        re.IGNORECASE,
    ),
)
_HANDOFF_COMPLETE = re.compile(
    r"(?:\r?\n[ \t]*)?\[handoff\]\s*(.*?)\s*\[/handoff\]\s*$",
    re.IGNORECASE | re.DOTALL,
)
_HANDOFF_OPEN = re.compile(r"(?:\r?\n[ \t]*)?\[handoff\]", re.IGNORECASE)
_HANDOFF_CLOSE = re.compile(r"\[/handoff\]", re.IGNORECASE)


def cap_effort(n: int) -> int:
    """Clamp a stored effort onto ``0``–``MAX_EFFORT``. ``0`` still means off."""
    if n <= 0:
        return 0
    return min(n, MAX_EFFORT)


def parse_effort_value(raw: Any) -> Optional[int]:
    """Return a stored effort ``0``–``MAX_EFFORT``, or ``None`` if ``raw`` is invalid.

    Accepts the full 0–100 user/model scale (and the API aliases, including
    ``max`` / ``xhigh``) then caps at ``MAX_EFFORT``. Values above 100 stay
    invalid. Does not raise. ``None`` means "leave the current effort unchanged".
    """
    if raw is None or isinstance(raw, bool):
        return None
    n: Optional[int] = None
    if isinstance(raw, int):
        n = raw if 0 <= raw <= 100 else None
    elif isinstance(raw, float):
        if raw == int(raw):
            parsed = int(raw)
            n = parsed if 0 <= parsed <= 100 else None
    elif isinstance(raw, str):
        token = raw.strip().lower()
        if token in ALIAS_TO_INT:
            n = ALIAS_TO_INT[token]
        elif token.isdigit() or (token.startswith("-") and token[1:].isdigit()):
            parsed = int(token)
            n = parsed if 0 <= parsed <= 100 else None
    if n is None:
        return None
    return cap_effort(n)


def effort_to_wire(effort: Any, *, enabled: bool = True) -> str:
    """The effort level actually sent: ``none`` (thinking off), ``low`` or ``high``.

    Never returns ``max``.
    """
    if not enabled:
        return DISABLE_WIRE
    n = parse_effort_value(effort)
    if n is None:
        n = DEFAULT_EFFORT
    n = cap_effort(n)
    if n <= 0:
        return DISABLE_WIRE
    return "low" if n <= 50 else "high"


def _split_effort_trailer(text: str) -> tuple[str, Optional[int]]:
    """Strip a trailing next-effort marker. Does not touch a handoff block."""
    if not text:
        return text, None
    for pat in _META_PATTERNS:
        match = pat.search(text)
        if not match:
            continue
        visible = text[: match.start()].rstrip()
        parsed = parse_effort_value(match.group(1).strip())
        return visible, parsed
    return text, None


def _split_handoff_trailer(text: str) -> tuple[str, Optional[str]]:
    """Strip a trailing ``[handoff]...[/handoff]`` block.

    A complete block returns its body. An unclosed ``[handoff]`` is hidden
    from visible text (so a live stream does not flash the trailer) but is
    not treated as a summary — compact then falls back to Flash or a local
    fold. Does not raise.
    """
    if not text:
        return text, None
    match = _HANDOFF_COMPLETE.search(text)
    if match:
        body = match.group(1).strip()
        return text[: match.start()].rstrip(), (body or None)
    opens = list(_HANDOFF_OPEN.finditer(text))
    if not opens:
        return text, None
    last = opens[-1]
    if _HANDOFF_CLOSE.search(text, last.end()):
        return text, None
    return text[: last.start()].rstrip(), None


def split_reply_meta(text: str) -> tuple[str, Optional[str], Optional[int]]:
    """Split trailing handoff and next-effort trailers from visible text.

    Returns ``(visible, handoff, next_effort)``. ``handoff`` is the compact
    summary body when a complete ``[handoff]...[/handoff]`` block is present.
    ``next_effort`` is ``0``–``MAX_EFFORT`` when that trailer is present
    *and* the value is valid (100 / ``max`` cap to 75); otherwise ``None``.
    Recognised trailers are always stripped, even when the effort value is
    invalid, so the user never sees the control blocks. Does not raise.
    """
    if not text:
        return text, None, None
    visible, effort = _split_effort_trailer(text)
    visible, handoff = _split_handoff_trailer(visible)
    return visible, handoff, effort


def visible_reply_text(text: str) -> str:
    """User-visible prefix of a (possibly still-streaming) assistant reply."""
    visible, _, _ = split_reply_meta(text)
    # Answer escrow (finish.py): "[deliver]" releases a kept answer that the
    # caller prints in full afterwards; the token itself is not for the eye.
    stripped = visible.lstrip()
    if stripped.lower().startswith("[deliver]"):
        return stripped[len("[deliver]"):].lstrip()
    return visible


def strip_reply_meta_from_items(items: list) -> tuple[Optional[str], Optional[int]]:
    """Drop trailing handoff/effort trailers from assistant ``output_text``.

    Mutates ``items`` in place. Returns ``(handoff, next_effort)`` from the
    last stripped part, or ``(None, None)``.
    """
    if not items:
        return None, None
    for item in reversed(items):
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        parts = item.get("content") or []
        if not isinstance(parts, list):
            continue
        for part in reversed(parts):
            if not isinstance(part, dict):
                continue
            if part.get("type") != "output_text":
                continue
            raw = part.get("text")
            if not isinstance(raw, str) or not raw:
                continue
            visible, handoff, effort = split_reply_meta(raw)
            if visible != raw:
                part["text"] = visible
                return handoff, effort
        # Only the last assistant message is a candidate for a trailing block.
        break
    return None, None


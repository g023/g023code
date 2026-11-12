"""Cache-aware history policy: when to keep, vault, micro-blank, or compact.

DeepSeek Flash regular rates (ds_pricing.json, 2026-09-10): cache hit $0.003/M,
miss $0.15/M (~50x), output $0.60/M (~200x a hit). A later request hits
only when it fully matches a persisted cache prefix unit from token 0
(Example 1: A+B then A+B+C hits; Example 2: A+B then A+C misses).

That ratio is the whole game. Once a token sits in the cached prefix, keeping
it is almost free. Rewriting it (micro-blank, Layer 3, post-turn replace)
turns those hits into misses on the tail from the rewrite point onward.
Output tokens are the remaining bill once the prefix is hot — retune effort
only when the prefix is already new (post-turn compact / /thinking / /clear).
/goal is not in that list. It releases the pin before its own turn so the
ceiling alias is sent, and the turn re-pins that alias. Releasing again
when /goal returns would miss the transcript just warmed, from token 0.
A stored [next_effort] is sent on the next user request only when the pin
is already clear: compact released it after replacing the input list, or
the user ran /thinking or /clear.

Worked example, off-peak Flash, chars/4 token guess used elsewhere in this
client:

- 500 cached tool-result tokens cost $0.0000015/turn as hits.
- Blanking them busts a 3 000-token tail that was also hits:
  3 000 * ($0.15 - $0.003) / 1e6 = $0.000441 one-time.
- Turns to recoup: 0.000441 / 0.0000015 ≈ 294.

Coding sessions do not last 294 turns. So: never rewrite a cached prefix
unless the window is actually full (must reclaim) or the leftover is uncached
(will be re-missed anyway) or the objective is done (next request is a new
orchestration). Large tool bodies are offloaded *before* they enter a cache
unit — append-only pointers, not a later middle rewrite.

Branched Agent workers are the other half: isolated transcripts never land on
the parent. When the parent publishes its frozen instructions+tools+effort,
children reuse that standing prefix and hit it; they still execute only the
tools their kind allows.
"""

from __future__ import annotations

from .usage import pricing_for

# Same guess the orchestrator uses for char→token status-bar math.
CHARS_PER_TOKEN = 4

# Last-call hit rate at or above this means leftover history is in the cached
# prefix and is cheap to keep. Below it, leftover will be re-missed.
HIT_RATE_KEEP = 0.5

# Tools whose results are unbounded and have a narrower re-run (a tighter
# command, a URL section, a sub-directory). SearchContent is bounded by its
# sample cap and its samples are the answer: vaulting it kept only the JSON
# envelope and blinded the search (1.5.0 development, bench task "pricing").
# FetchUrl and WebSearch are bounded too (an 8k default window or focus
# sections; at most 20 abstracts). Vaulting FetchUrl showed the model 1.2k
# chars of a page it had paid to fetch (1.5.0).
# ReadFile summaries and Agent payloads *are* the compact form.
VAULT_TOOLS = frozenset(
    {"Bash", "ListDir", "Glob", "AnalyzeImage"}
)
VAULT_MIN_CHARS = 2_500
VAULT_LEADIN = "[g023_vault "


def usd(
    hit_tokens: int,
    miss_tokens: int,
    output_tokens: int = 0,
    *,
    model: str = "deepseek-flash",
    surge: bool = False,
) -> float:
    """USD at the model's tidal rates. Tokens are integers; result is float."""
    p = pricing_for(model)
    hit = p.surge_cache_hit if surge else p.cache_hit
    miss = p.surge_cache_miss if surge else p.cache_miss
    out = p.surge_output if surge else p.output
    return (
        hit_tokens * hit + miss_tokens * miss + output_tokens * out
    ) / 1_000_000


def chars_to_tokens(chars: int) -> int:
    return max(0, int(chars) // CHARS_PER_TOKEN)


def hit_rate(hit_tokens: int, miss_tokens: int) -> float:
    total = hit_tokens + miss_tokens
    return (hit_tokens / total) if total else 0.0


def turns_to_recoup_rewrite(
    reclaimed_tokens: int,
    bust_tail_tokens: int,
    *,
    model: str = "deepseek-flash",
    surge: bool = False,
) -> float:
    """Future turns of hit-rate savings needed to recoup a one-time prefix bust.

    Returns +inf when reclaiming cached tokens cannot recoup (zero/negative
    savings). Callers treat a large number as "do not rewrite".
    """
    p = pricing_for(model)
    hit = p.surge_cache_hit if surge else p.cache_hit
    miss = p.surge_cache_miss if surge else p.cache_miss
    delta = miss - hit
    if reclaimed_tokens <= 0 or hit <= 0:
        return float("inf")
    bust = bust_tail_tokens * delta
    save_per_turn = reclaimed_tokens * hit
    if save_per_turn <= 0:
        return float("inf")
    return bust / save_per_turn


def should_micro_cached_prefix(*, window_pressure: bool) -> bool:
    """Layer-1 blanking of items already sent (so already cacheable).

    True only when the context window itself is the constraint. Incremental
    blanking of a cached prefix is Example 2 every turn and does not recoup
    (see module docstring).
    """
    return bool(window_pressure)


def should_vault_tool_output(
    name: str,
    text: str,
    *,
    raw_read: bool = False,
) -> bool:
    """Whether a tool result should be stored by hash and replaced with a pointer.

    Spares verbatim ReadFile (the model needs those bytes to Edit/WriteFile)
    and anything already a pointer. Fail closed on empty/unknown.
    """
    if raw_read or not text:
        return False
    if name not in VAULT_TOOLS:
        return False
    if text.startswith(VAULT_LEADIN):
        return False
    return len(text) >= VAULT_MIN_CHARS


def should_post_turn_compact(
    *,
    has_items: bool,
    objective_completed: bool,
    leftover_chars: int,
    leftover_min_chars: int,
    window_pressure: bool,
    last_hit_tokens: int,
    last_miss_tokens: int,
    layer3_this_turn: bool,
    this_turn_added_tools: bool,
) -> bool:
    """Whether a finished turn should replace history with a handoff summary.

    Keep the cached transcript when the last orchestrator call mostly hit and
    the window is not full. Compact when the objective is done (next request
    is a new orchestration), when the window is full, or when leftover bulk
    is uncached and large enough that the next turn would re-pay miss rates.
    """
    if not has_items:
        return False
    if objective_completed:
        return True
    if window_pressure:
        return True
    if layer3_this_turn and not objective_completed:
        if leftover_chars < leftover_min_chars and not this_turn_added_tools:
            return False
        if leftover_chars < leftover_min_chars:
            return False
        # Layer 3 already rewrote history. Only hand off again when leftover
        # is still large *and* that leftover is not sitting in a hot prefix.
        if hit_rate(last_hit_tokens, last_miss_tokens) >= HIT_RATE_KEEP:
            return False
        return leftover_chars >= leftover_min_chars
    if leftover_chars < leftover_min_chars:
        return False
    if hit_rate(last_hit_tokens, last_miss_tokens) >= HIT_RATE_KEEP:
        return False
    return True

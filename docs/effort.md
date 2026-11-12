# `effort.py` — reasoning-effort mapping and same-response trailers

`g023_code/effort.py` is the encode/decode layer between the
user/model effort scale and what DeepSeek V4.1 Flash's Anthropic endpoint
accepts. It also strips the two trailing control blocks the orchestrator may
attach to a final answer. I/O stays out of this module.

## Why a mapping exists

`/anthropic/v1/messages` takes `thinking: {"type": "enabled" | "disabled"}`
plus `output_config.effort`. The effort levels it really distinguishes are
`low`, `high` and `max`; DeepSeek folds `minimal` → `low`, `medium` /
`xhigh` → `high` and `ultra` → `max` server-side, and numbers are rejected.
Sending `medium` therefore buys `high`, silently — so the harness only ever
sends a level that means what it says (`HARNESS_WIRE_VARIANTS = ("none",
"low", "high")`).

Stored effort is an integer `0`–`75`; `api.reasoning_param` calls
`effort_to_wire` to pick the level, and the codec turns `none` into
`thinking: disabled`. This harness never sends `max`: `MAX_EFFORT = 75` is
both the thinking-on default and the ceiling. A `[next_effort: 100]`,
`/thinking max`, `/goal`, or Agent `reasoning=max` is stored and sent as
75 / `high`. `max` often burns the entire output budget on thinking and
returns an empty answer.

## Scale

Input accepts the 0–100 user/model scale and the level names; values above
75 cap to 75 before mapping.

| Stored | Wire |
|--------|------|
| `0` / off | `none` (thinking disabled) |
| `1`–`50` | `low` |
| `51`–`75` | `high` (75 is the thinking-on default and the ceiling) |

Aliases: `none`/`off` → 0, `minimal` → 12, `low` → 50, `medium` → 62,
`high`/`xhigh`/`max` → 75.

`DEFAULT_EFFORT = MAX_EFFORT = 75`. Old configs that say `"high"` or
`"max"` coerce to 75 via `ALIAS_TO_INT`. `parse_effort_value` never
raises: invalid input returns `None` ("leave the current effort
unchanged"); 76–100 return 75.

`/thinking` accepts `1-100` or `low`/`high`/`max`/`off` (max aliases to
75). `/goal` forces 75 for its own turn and releases the effort pin
first, so a session pinned lower is not stuck there. The turn re-pins
`high`. A `[next_effort: N]` on that turn is stored; an omitted trailer
restores the pre-goal dial. Neither one retunes the wire while the goal
transcript is still the prefix. The stored dial is sent when post-turn
compact has replaced that input list, or the user runs `/thinking` or
`/clear`.

## Same-response trailers

Locked shapes, trailing-only, on the same final answer:

```
[handoff]
## Goals / Files / Decisions / Done
[/handoff]
[next_effort: N]
```

`[handoff]` is only requested on turns whose user item ends with
`[handoff due]` (see `docs/orchestrator.md`, JIT handoff forecast).

The standing prompt uses `N` as a placeholder so Flash does not copy 75
on every turn (reasoning tokens are billed as output). 75 is the
thinking-on default and the ceiling; 25–50 is for routine follow-up; 0
disables thinking. Values above 75 are capped.

HTML-comment and `<meta>` forms of `next_effort` are accepted if they
appear as the last thing in the text.

- `split_reply_meta(text)` → `(visible, handoff, next_effort)`.
- `visible_reply_text(text)` — the prefix the live panel paints, so a
  streaming trailer does not flash.
- `strip_reply_meta_from_items(items)` — mutates assistant `output_text`
  parts in place; returns `(handoff, next_effort)` from the last stripped
  part.

A complete `[handoff]...[/handoff]` on a **final** (no-tool-call) reply
is stored as `_last_handoff` and reused by compact (no second Flash
call). Compact **consumes** it so a later pass cannot reuse a stale
summary. An **unclosed** `[handoff]` is hidden from the user but is
**not** treated as a summary — compact then falls back to a local fold
or Flash. A valid next-effort integer on that same final reply updates
in-memory `settings.reasoning_effort` for the following **user
request**. A trailer on a tool-call step is stripped from display and
history but is **not** applied — it is not this turn's remaining steps.

Recognised trailers are always stripped, even when the effort value is
invalid, so the user never sees the control blocks.

## Related

- `docs/api.md` — `reasoning_param` is the caller of `effort_to_wire`.
- `docs/orchestrator.md` — `_apply_reply_meta`, compact reuse of handoff.
- `docs/commands.md` — `/thinking` and `[next_effort: N]`.
- `tests/test_reasoning.py` — scale, wire aliases, trailer stripping.

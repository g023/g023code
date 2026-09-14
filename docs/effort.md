# `effort.py` — reasoning-effort mapping and same-response trailers

`g023_code/effort.py` (~255 lines) is the encode/decode layer between the
user/model scale and what `/responses` actually accepts. It also strips the
two trailing control blocks the orchestrator may attach to a final answer.
I/O stays out of this module.

## Why a mapping exists

A live probe of `POST https://api.deepseek.com/responses` on
`deepseek-flash` (2026-09-14) found:

- JSON numbers `0` / `1` / `75` / `100` are rejected (`reasoning.effort`
  is not valid JSON of that shape).
- Numeric strings `"0"` / `"1"` / `"75"` / `"100"` are rejected: unknown
  variant, expected one of `none`, `minimal`, `low`, `medium`, `high`,
  `xhigh`, `max`.
- Those string aliases are accepted. `none` is what disables thinking
  (`reasoning_tokens` 0, no reasoning item).

So stored effort is an integer `0`–`75`, but the wire value is always
one of those aliases. `api.reasoning_param` calls `effort_to_wire`.
This harness never sends `xhigh` or `max`: `MAX_EFFORT = 75` is both the
thinking-on default and the ceiling. A `[next_effort: 100]`, `/thinking
max`, `/goal`, or Agent `reasoning=max` is stored and sent as 75 / `high`.
`max` on this endpoint often burns the entire `max_output_tokens` budget
on thinking and returns an empty answer.

## Scale (Together's documented anchors: low=50, high=75, max=100)

Input still accepts the 0–100 user/model scale and the API aliases; values
above 75 cap to 75 before mapping.

| Stored | Wire |
|--------|------|
| `0` / off | `none` |
| `1`–`24` | `minimal` |
| `25`–`50` | `low` |
| `51`–`74` | `medium` |
| `75`–`100` | `high` (75 is the thinking-on default and the ceiling) |

`DEFAULT_EFFORT = MAX_EFFORT = 75`. Old configs that say `"high"` or
`"max"` coerce to 75 via `ALIAS_TO_INT`. `parse_effort_value` never
raises: invalid input returns `None` ("leave the current effort
unchanged"); 76–100 return 75.

`/thinking` accepts `1-100` or `low`/`high`/`max`/`off` (max aliases to
75). `/goal` forces 75 for its own turn. A `[next_effort: N]` on that
turn is kept for the next user request; an omitted trailer restores the
pre-goal dial.

## Same-response trailers

Locked shapes, trailing-only, on the same final answer:

```
[handoff]
## Goals / Files / Decisions / Done
[/handoff]
[next_effort: N]
```

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

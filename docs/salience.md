# `salience.py` — step-intent excerpts and the readable vault

When a tool result is too long to keep inline (Bash output past the cap, a
long ListDir/Glob, an old result moved out by the cost policy), the model
gets an excerpt plus a pointer to a file holding the full text. This module
decides **which lines go in the excerpt** and **where the full text lives**.

## Why

The old excerpt was blind: head 400 + tail 800 chars. On a corpus of 14
real long outputs with a known decisive line
only 3/14 decisive lines reached the model. The paid model then re-ran the
command, which costs a step (and is wrong for slow or non-idempotent commands).

## Intent is free

The model already wrote down what it is looking for: the reasoning and
commentary of the step that issued the call. `step_intent(reasoning,
commentary, fallback)` takes the last 600 chars of the reasoning plus the
commentary (no extra tokens, no extra call). `call_intent(step, args)` adds
the tool arguments only when the step text is shorter than 40 chars, because
command words diluted the ranking (11 → 10/14 when always added).

## Excerpt

- `candidate_lines(lines, intent)` — docindex BM25 over the lines against the
  intent, plus up to 4 error-looking lines (`_ERR`: error, Traceback,
  FAILED, exit codes, ...).
- `build_excerpt(text, intent, extra=())` — head 240 chars, up to 14 picked
  lines as `L<n>: ...` (≤ 1000 chars, original line numbers) and tail 500.
  `extra` lets a reranker (local model, `local_llm.pick_lines`) add picks.
- Measured through the real pipeline:
  blind 3/14, salient 11/14; with the local 1.7B rerank on top, 12/14.

## Readable vault

- `write_vault_file(body)` → `.g023/vault/<sha16>.txt` (content-addressed,
  2 MB cap, pruned to the newest 300 files); `read_vault_file`, `prune_vault`.
- `tools/registry.keep_full_output` writes the **raw** Bash stdout/stderr
  (`bash_full_body`) when the cap or compaction dropped text, and adds
  `full_output` / `full_lines` to the payload.
- `cache.offload_tool_output(text, intent=, extra=, salient=)` reuses that
  file (or writes the stored text), builds the excerpt and appends
  `readback_note`: "full output kept: <file> … ReadFile start_line/end_line
  or grep -n …; no need to re-run the command".

## Switch

`salience_excerpt` (env `G023_SALIENCE`, default on). Off restores the legacy
head/tail `_vault_excerpt`; the vault file and readback note stay either way.

## What it does not do

Live A/B (12 runs per arm on long-output
tasks): both arms solved everything at the same cost (~$0.0005/task). DeepSeek
usually filters its own output (`| tail`, `| grep`), so the excerpt is a
safety net for when raw long output does land, not a routine cost cut.

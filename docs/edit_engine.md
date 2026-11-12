# edit_engine.py — byte-faithful edits (1.5.0)

The one place file bytes are turned into text for editing and back. Used by
`tools/registry.py` (`Edit`, `WriteFile`) and `subagents/file_reader.py` (EOL tag).

## Fidelity
- `decode(bytes) -> FileText(text, eol, bom)`: a pure-CRLF file is edited as LF
  text and written back as CRLF; a mixed file is left exactly as it is. The BOM is
  kept. Non-UTF-8 raises `UnicodeDecodeError` (Edit refuses rather than mangling).
- `conform_new_content(content, existing)`: WriteFile over an existing CRLF/BOM
  file keeps that style when the model sends LF text.
- `save(path, data)`: temp file in the same directory, `copymode`, `os.replace`.
  A write never half-happens and the file mode (e.g. +x) survives.

## Match ladder (`apply_edit`)
1. exact (all occurrences counted; `replace_all` only works on this rung);
2. EOL-normalised;
3. trailing-whitespace-insensitive, line-aligned;
4. affine indentation map: `file_w = a + f*old_w`, proven on every non-blank line
   (tabs: a uniform prefix only). `new_string` is re-indented with the same map.
Fuzzy rungs apply only when the match is unique. `matched` in the result names the
rung, so a loose apply is never silent.

## Misses that carry their own fix
- not unique → `occurrences: ["L15 in Rect.scale", "L32 in Box.scale"]` plus a hint
  to retry with `line=N` (any line inside the intended occurrence).
- not found → `closest: {start_line, end_line, similarity, text}` (verbatim, at most
  1200 chars, similarity ≥ 0.55, files ≤ 2 MB), so the retry needs no re-read.

## Receipt
Success returns `lines: "a-b"` (span after the edit), `matched` (if not exact),
`line_endings` when CRLF was kept, and `syntax_error` / `syntax_context` when a
.py/.json file that parsed before no longer parses.

Tests: `tests/test_edit_engine.py`.

# docindex.py — long-document and large-codebase navigation (local, no API)

Added in 1.5.0. Shared by `subagents/file_reader.py` (ReadFile), `subagents/searcher.py` (SearchContent
breadcrumbs) and `web.py` (FetchUrl focus). Pure Python with no dependencies. Everything runs on the local CPU.

Why: each extra round trip re-reads the whole prompt prefix, so the cheapest answer is the one that comes
back in one hop with both "where" (address) and "what" (text). Locating needs no model when the file has
structure or lexical evidence.

## Pieces
| name | what |
|---|---|
| `terms(q)` / `tokens()` / `term_matches()` | word-boundary terms (camel/snake split). Stopwords are dropped unless the query is only stopwords. Terms of 4+ chars also match up to 3 suffix chars (save → saves, saved). |
| `quoted`, `LineStream`, `find_phrase` | exact phrase over a normalized word stream, so hits cross line breaks and punctuation |
| `LineIndex`, `bm25_sections`, `rank_texts` | word → line postings. BM25 over (name, a, b) spans uses prefix sums, with a boost for name terms. |
| `induce_outline(lines)` | plain-text heading tree; see below |
| `markdown_outline` | ATX and setext headings |
| `Outline.address(line)` | breadcrumb such as `THE TRAGEDY OF HAMLET > ACT III > Scene I.` |
| `find_nodes(outline, focus)` | section-path lookup. Every focus term must be on the path, and keyword+number pairs must line up (`act 3 scene 1` ≠ `act 1 scene 3`). Accepts roman or arabic numerals. |
| `fold(nodes, budget)` | BFS fold to a char budget. Rows look like `title  La-b  +N`, where +N counts the hidden children. |
| `enclosing_symbol` | innermost symbol span that contains a line |
| `doc_for(path, content, kind)` | LRU (6 entries) keyed by path, size, mtime and kind. The outline, index and stream are built lazily. |

## Heading induction
1. Candidates come from line shape: a keyword with a number (BOOK/PART/ACT/CHAPTER/SCENE...; roman, arabic or
   word numerals), a bare number on its own line, an ALL-CAPS line, a numbered heading, or Markdown.
2. A paragraph that repeats 3+ times is boilerplate and never a heading. Year lines (1000–2100) are skipped.
3. Keyword rank sets the level. `ACT I. SCENE 2.` opens or reuses its implied `ACT I` parent.
4. Title promotion: an ALL-CAPS line just before a numbering reset becomes the parent of that sequence.
   A byline (`by ...`) breaks ties. Labels repeated 3+ times and table rows (3 inner spaces) never qualify.
5. `THE END` closes all open nodes. Empty nodes are pruned.

Measured: 5.4 MB complete works → 37 works + sonnets + licence with acts and scenes in ~0.1 s.
The index builds in ~0.6 s.

## ReadFile focus order (file_reader `_local_focus`)
1. Exact phrase (quoted, or 2+ words with ≤12 hits): returns `matches` [{section, line, text}] plus the
   passage verbatim.
2. Outline node (`find_nodes`): verbatim if ≤4k chars, otherwise the head + `section_lines` + the subtree outline.
3. Exact symbol (handled in run_file_reader): the body verbatim if ≤2k chars, otherwise its first ≤2k chars
   plus `section_lines`.
4. Word BM25 over windows inside leaf sections (text) or symbols plus module gaps (code). The best match is
   narrowed to its best window.

An unfocused read of a long text shows `metadata.outline` (folded to 2.4k) and `sections`. Code with too many
symbols shows the outline with prefix-grouped names (`_fold_symbols`) and `symbols_total`.

## Search breadcrumbs
Each SearchContent sample gets `in` (enclosing function, or section path for text, up to 32 MB). The codec
prints a `@ where` header when it changes. C definitions rank first (`_is_definition` + `_C_DEF_LINE`).

Tests: `tests/test_docindex.py`. A 6-task long-document live bench was used during development
(see the README's "For researchers" section).

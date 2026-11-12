# `shapes.py` — shape digests for names, binaries, logs and data files

Stdlib only, no API call. Used by ListDir/Glob (`dirmap.py`, `searcher.py`) and
ReadFile (`subagents/file_reader.py`) to show *what a thing is* in a few hundred
characters instead of dumping it. A directory of 4,000 frames was 200 names, a PNG
was 12k chars of mojibake, and a minified line was 1.6M chars. Each is now one
short row.

## Name templates

| function | what |
|---|---|
| `group_names(names, sizes=None)` | → `(groups, singles)`. Names that differ only in digit runs collapse into `frame_{00000..03999}.png` (needs ≥3 members); opaque ids (hashes, uuids) group by shape (needs ≥5). Each group has `template`, `count`, `size`, `example`, `members`, and `gap` (`"1 missing: 5"`) when a numbered run has holes. |
| `listing_lines(paths, sizes=None, budget=40)` | Whole-path templates first (`vendor/pkg{0..29}/lib/mod{0..19}.js  x600`), then per-directory grouping of the rest. Glob's digest. |
| `ext_histogram(names, top)` / `human_size(n)` | `.py 10, .md 2` / `3.1 MB`. |

## Binary detection and decoding

- `sniff(head, path)` reads the first 8 KB (`SNIFF_BYTES`) and checks a magic table: images, zip family, tar, gzip/bz2/xz, SQLite, ELF/PE/Mach-O, Java class, pyc, PDF, WAV, MP4, npy, pickle. It falls back to heuristics and returns `None` for text.
- `text_encoding(head)` detects BOMs. UTF-16/32 counts as text and is decoded with that encoding.
- `looks_binary(head)` checks for NUL bytes, the share of control bytes, and UTF-8 validity.
- `describe_binary(path, fmt, size)` returns `{format, label, details, outline, hint}`. For example: image dimensions, depth and color; zip members (docx/xlsx/jar/wheel/epub aware); tar members; gzip inner sniff with duplicate head lines collapsed (`(xN)`); SQLite tables, columns and row counts; ELF class/endianness/machine; PDF version, pages, title and encryption; pyc Python version; npy dtype and shape; a pickle warning; and for unknown data, entropy, printable strings (only when entropy ≤ 7.5) and a hexdump.
- SQLite is opened with `mode=ro&immutable=1`, and a progress handler aborts slow queries. The file is never written to and no journal is created.

## Log templates (`line_templates`, `template_digest`)

- Each line is masked into a template key. Hex ids become `<hex>`, every digit run becomes a slot, and the key is capped at `KEY_CHARS=400` characters.
- Per template the digest keeps the count, the first line number, up to 6 line numbers, one example, and per slot the numeric range plus up to 8 distinct values. Templates are rendered as `INFO worker-{0..7} took {3..998} ms`.
- `template_digest` lists the frequent templates first. It then lists the **rare** templates (under 1% of lines), rarest first, with line numbers, because rare lines are where the errors are.
- **Budget with exact rare counts:** the scan stops after `TEMPLATE_SECONDS=2.5` s. The first `RARE_RECOUNT=8` rare templates are then recounted over the unscanned rest with one compiled regex per template (`_template_regex`), so `ERROR x402` is exact even on a 400k-line log whose template scan was partial. The `partial` note says which counts cover the whole file.

## Data digests

| function | what |
|---|---|
| `table_digest(lines, delimiter=None, sample_text=)` | CSV/TSV: delimiter sniffed from `sample_text`, header detection, per-column type (int/float/date/bool/str), range, distinct count, top values, null share, ragged rows. Profiles up to `TABLE_STAT_ROWS=200000` rows; `scanned_all` says whether that covered the file. |
| `jsonl_digest(lines)` | Record count plus a merged record shape. |
| `json_shape(value, budget)` | `{version: int =3, items: [50000 x {id: int 0..49999, ...}]}`. Arrays are stride-sampled at `ARRAY_SAMPLE=5000` (noted as "items sampled evenly"). |
| `clip_line(line, n, limit=2000)` | Clips one line and appends `...[line n continues: K more chars; focus='<phrase>' shows a window]`. |

Tests: `tests/test_smart_reads.py`.

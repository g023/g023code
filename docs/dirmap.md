# `dirmap.py` — bounded directory digests for ListDir

ListDir keeps its exact entry list while that list fits. When the walk is truncated,
or the directory has more than `DIGEST_MIN_ENTRIES=40` entries
(`tools/registry.py`) and the digest is at least 2x more compact, ListDir returns
`dirmap.digest(...)` instead.

```
frames/: 0 dirs, 4,001 files
frame_{00000..03999}.png  x4000  3.1 MB
README.md  12 B
```

- **Every direct child is accounted for.** Numbered and hashed names collapse into templates through `shapes.group_names`. Other names are listed while the directory's row share lasts (at least 8 rows), followed by `... +N more files (ext mix)`.
- **Sibling directories** with templated names become one row with their shared layout, for example `pkg{0..29}/  x30  (same layout; e.g. pkg0/: lib/: 20 files (20 .js), 370 B)`. When layouts differ, the row says `k/n share a layout`. Layouts are sampled from at most 200 members.
- **Recursive listings** expand breadth-first under a row budget (`FLAT_ROWS=60`, `RECURSIVE_ROWS=80`). Each subdirectory gets a fair share of rows. A directory that does not fit is summarised by `summarize_dir` (file counts, extension mix, size, and single-child chains followed, as editors do) and gets a `ListDir <dir> for all` pointer. Nothing is silently dropped.
- **Ignored directories** (`.git`, `node_modules`, and so on) appear under `skipped`.
- **Bounds:** `os.scandir` with `SCAN_MAX_ENTRIES=50000` entries per directory and `SCAN_SECONDS=2` in total. When time runs out, the payload says `partial`.

The payload is `{digest: true, dirs, files, size?, lines, legend, skipped?, partial?}`.
`tool_codec` renders `lines` as plain rows.

Measured on a benchmark fixture: ListDir `frames/` went from 11.6k chars (the first
200 names only) to 515 chars (every child counted). In the live bench, the
"how many frames, highest number" task went from 22 calls to 3.

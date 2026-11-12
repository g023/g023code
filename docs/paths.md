# `paths.py` — project-root confinement

`g023_code/paths.py` is the single resolver that refuses a
tool path outside `G023_PROJECT_ROOT`. Symlink escapes are included:
the check is after `Path.resolve()`.

## API

- `PathOutsideProject` — `ValueError` subclass.
- `resolve_in_project(path, root=...)` — empty / `.` / `None` means the
  project root. Absolute paths are accepted only when they stay inside
  the root after resolve. Raises `PathOutsideProject` otherwise.
- `confine_or_error(...)` → `(resolved, None)` or `(None, error message)`.
  Does not read the path.
- `rel_to_root(path)` — POSIX-ish relative path for working-set / search
  results; returns the absolute string if the path is outside.

ReadFile, ListDir, SearchContent, Glob, Edit, WriteFile, and `/undo`
all go through this (WriteFile still has an inline `relative_to` check
as well). `.g023/` itself is inside the project, so backups, logs, and
memory notes are reachable on purpose.

## Related

- `docs/tools.md` — executors that call `confine_or_error`.
- `docs/subagents.md` — FileReader and Searcher.
- `tests/test_root_confine.py`.

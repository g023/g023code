"""Project bulletin board under .g023/bbs/. Posts are never injected into instructions.

A forum for this session and later ones: topic slugs are subdirectories,
conversations are ``.txt`` files with slug filenames. Subagents and the
orchestrator write here to pass insights both ways. Helper CLIs live in
``bbs_tools/``. Standing instructions name the path; SearchContent can
read it; the bodies stay on disk so a new post does not bust the prefix.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .config import get_project_root
from .fsutil import atomic_write_text, file_lock
from .paths import confine_or_error, rel_to_root

BBS_DIR = ".g023/bbs"
GUIDE_NAME = "GUIDE.md"
TOOLS_INFO_NAME = "tools_info.md"
INDEX_NAME = "INDEX.md"
TOOLS_DIR = "bbs_tools"
POST_SUFFIX = ".txt"
INDEX_CAP = 200
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
RESERVED_TOPICS = frozenset(
    {TOOLS_DIR.lower(), "bbs-tools", "guide", "index", "tools-info"}
)

DEFAULT_GUIDE = """# BBS

A forum for this session and later ones. Posts are never copied into the
system prompt (that would bust the prefix cache). Search or read on demand.

## Layout

- Topic slug = subdirectory of `.g023/bbs/`
- Conversation = `.txt` file with a slug filename
- Tools = `.g023/bbs/bbs_tools/` (documented in `tools_info.md`)
- This file (`GUIDE.md`) = how to use the board well
- `INDEX.md` = generated catalog (do not hand-edit; `bbs index` rebuilds it)

Reserved names (not topics): `bbs_tools`, `GUIDE.md`, `tools_info.md`, `INDEX.md`

## When to read

Before a non-trivial task, SearchContent `.g023/bbs` for keywords from the
request, or ReadFile `INDEX.md` then the one matching thread. Do not dump
the tree into context.

## When to write

- An insight, rejected approach, or measurement that the next session
  would otherwise re-derive
- A message for a sibling subagent or the orchestrator that must outlive
  this turn
- An open issue. Do not call the objective done while a surfaced issue
  is still open.

Do **not** put one-line project facts here (those go in `.g023/memory/`)
or reusable procedures (those go in the learn store).

## How to write

- One claim per file; first line is the claim
- Append to an existing thread rather than rewriting it
- Date the entry (`YYYY-MM-DD`)
- No secrets, no API keys, no full transcripts
- Slugs: lowercase, digits, hyphens; `python -m g023_code.bbs post topic slug`

## Tools

Read `tools_info.md`. Prefer `python -m g023_code.bbs`
(list / search / post / read / index).
"""

DEFAULT_TOOLS_INFO = """# bbs_tools

Canonical CLI (same as the copies in this folder):

```
python -m g023_code.bbs              # status
python -m g023_code.bbs list [topic]
python -m g023_code.bbs search <query>
python -m g023_code.bbs read <topic/slug>
python -m g023_code.bbs post <topic> <slug>   # body on stdin or --body
python -m g023_code.bbs index                 # rebuild INDEX.md
```

The wrapper `bbs_tools/bbs` is the same entry point for Bash. Output is
compact JSON except `read`, which prints the thread text.

When you add or change a helper in this folder, update this file.
"""

BBS_TOOL_SCRIPT = '''#!/usr/bin/env python3
"""Navigate the project bulletin board at .g023/bbs/. See ../tools_info.md."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _bootstrap() -> None:
    candidates: list[Path] = []
    home = os.environ.get("G023_HOME")
    if home:
        candidates.append(Path(home))
    here = Path(__file__).resolve()
    if len(here.parents) >= 3:
        candidates.append(here.parents[3])
    root = os.environ.get("G023_PROJECT_ROOT")
    if root:
        candidates.append(Path(root))
    candidates.append(Path.cwd())
    for candidate in candidates:
        if (candidate / "g023_code" / "bbs.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_bootstrap()
try:
    from g023_code.bbs import main
except ImportError as exc:
    sys.stderr.write(
        "g023_code.bbs is not importable. Set G023_HOME or run from the "
        f"g023 install root. ({exc})\\n"
    )
    raise SystemExit(2) from exc

raise SystemExit(main())
'''


def bbs_root() -> Path:
    return get_project_root() / ".g023" / "bbs"


def index_path() -> Path:
    return bbs_root() / INDEX_NAME


def slugify(name: str) -> str:
    """Lowercase, hyphenate, strip junk. Empty if nothing usable remains."""
    raw = (name or "").strip().lower().replace("_", "-")
    raw = re.sub(r"[^a-z0-9-]+", "-", raw)
    raw = re.sub(r"-{2,}", "-", raw).strip("-")
    if not raw or not SLUG_RE.match(raw):
        return ""
    return raw


def validate_slug(name: str, *, kind: str = "slug") -> str:
    slug = slugify(name)
    if not slug:
        raise ValueError(
            f"Invalid {kind} {name!r}: use lowercase letters, digits, hyphens; "
            "start with a letter; max 63 chars."
        )
    if kind == "topic" and slug in RESERVED_TOPICS:
        raise ValueError(f"Topic slug {slug!r} is reserved.")
    return slug


def ensure_layout() -> Path:
    """Create the bbs tree and seed GUIDE / tools_info / bbs_tools if missing."""
    root = bbs_root()
    root.mkdir(parents=True, exist_ok=True)
    tools = root / TOOLS_DIR
    tools.mkdir(parents=True, exist_ok=True)
    guide = root / GUIDE_NAME
    if not guide.is_file():
        guide.write_text(DEFAULT_GUIDE, encoding="utf-8")
    info = root / TOOLS_INFO_NAME
    if not info.is_file():
        info.write_text(DEFAULT_TOOLS_INFO, encoding="utf-8")
    wrapper = tools / "bbs"
    if not wrapper.is_file():
        wrapper.write_text(BBS_TOOL_SCRIPT, encoding="utf-8")
        try:
            wrapper.chmod(wrapper.stat().st_mode | 0o111)
        except OSError:
            pass
    return root


def _topic_dirs(root: Optional[Path] = None) -> list[Path]:
    base = root or bbs_root()
    if not base.is_dir():
        return []
    out: list[Path] = []
    try:
        children = sorted(base.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for item in children:
        if not item.is_dir() or item.name.startswith("."):
            continue
        if item.name == TOOLS_DIR:
            continue
        out.append(item)
    return out


def _posts_in(topic_dir: Path) -> list[Path]:
    try:
        children = sorted(topic_dir.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    return [
        p
        for p in children
        if p.is_file() and p.suffix == POST_SUFFIX and not p.name.startswith(".")
    ]


def list_topics() -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []
    for directory in _topic_dirs():
        posts = _posts_in(directory)
        topics.append(
            {
                "topic": directory.name,
                "posts": len(posts),
                "files": [p.name for p in posts],
            }
        )
    return topics


def list_posts(topic: Optional[str] = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    dirs = _topic_dirs()
    if topic:
        slug = validate_slug(topic, kind="topic")
        dirs = [d for d in dirs if d.name == slug]
    for directory in dirs:
        for post in _posts_in(directory):
            rows.append(
                {
                    "topic": directory.name,
                    "slug": post.stem,
                    "path": f"{BBS_DIR}/{directory.name}/{post.name}",
                }
            )
    return rows


def catalog_text() -> str:
    topics = list_topics()
    lines = ["# BBS index", ""]
    if not topics:
        lines.append("No topics yet. `python -m g023_code.bbs post <topic> <slug>`")
        lines.append("")
        return "\n".join(lines)
    for topic in topics:
        lines.append(f"- {topic['topic']}/ ({topic['posts']})")
        for name in topic["files"]:
            lines.append(f"  - {name}")
        lines.append("")
    return "\n".join(lines)


def write_index() -> dict[str, Any]:
    """Regenerate INDEX.md from the tree. Spill past INDEX_CAP to overflow.txt."""
    ensure_layout()
    dest = index_path()
    text = catalog_text()
    if not text.endswith("\n"):
        text += "\n"
    lines = text.splitlines()
    if len(lines) <= INDEX_CAP:
        atomic_write_text(dest, text)
        return {
            "ok": True,
            "path": rel_to_root(dest),
            "lines": len(lines),
            "topics": len(list_topics()),
            "posts": len(list_posts()),
        }
    head = "\n".join(lines[:INDEX_CAP]) + "\n"
    tail = "\n".join(lines[INDEX_CAP:]) + "\n"
    overflow = bbs_root() / "index-overflow.txt"
    atomic_write_text(overflow, tail)  # overflow first: never lose spilled lines
    atomic_write_text(dest, head)
    return {
        "ok": True,
        "path": rel_to_root(dest),
        "lines": INDEX_CAP,
        "spilled": rel_to_root(overflow),
        "spilled_lines": len(lines) - INDEX_CAP,
        "topics": len(list_topics()),
        "posts": len(list_posts()),
    }


def _today() -> str:
    return date.today().isoformat()


def post(topic: str, slug: str, body: str, *, append: bool = True) -> dict[str, Any]:
    """Create or append a thread. Refreshes INDEX.md."""
    topic_slug = validate_slug(topic, kind="topic")
    post_slug = validate_slug(slug, kind="slug")
    text = (body or "").replace("\r\n", "\n")
    if not text.strip():
        raise ValueError("Post body is empty.")
    if not text.endswith("\n"):
        text += "\n"
    ensure_layout()
    directory = bbs_root() / topic_slug
    directory.mkdir(parents=True, exist_ok=True)
    dest = directory / f"{post_slug}{POST_SUFFIX}"
    duplicate = False
    header = (
        f"# {post_slug}\n"
        f"# topic: {topic_slug}\n"
        f"# updated: {_today()}\n\n"
    )
    # One writer at a time per thread file; the existence check is inside the lock.
    with file_lock(dest):
        created = not dest.is_file()
        if created:
            atomic_write_text(dest, header + text)
        elif append:
            try:
                existing = dest.read_text(encoding="utf-8")
            except OSError:
                existing = ""
            # Idempotent retry: a re-sent identical body (e.g. after an
            # interrupted tool call) is not appended twice.
            if existing.endswith("\n\n" + text) or existing == text:
                duplicate = True
            else:
                with dest.open("a", encoding="utf-8") as handle:
                    handle.write(f"\n---\n# {_today()}\n\n" + text)
        else:
            atomic_write_text(dest, header + text)
    index = write_index()
    return {
        "ok": True,
        "created": created,
        "appended": (not created) and append and not duplicate,
        "duplicate": duplicate,
        "path": rel_to_root(dest),
        "topic": topic_slug,
        "slug": post_slug,
        "index": index.get("path"),
    }


def read_post(ref: str) -> dict[str, Any]:
    """Read ``topic/slug`` or a path under the bbs root."""
    path, err = resolve_post_path(ref)
    if err:
        return {"ok": False, "error": err}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "path": rel_to_root(path),
        "body": text,
        "bytes": len(text.encode("utf-8")),
    }


def resolve_post_path(ref: str) -> tuple[Optional[Path], Optional[str]]:
    raw = (ref or "").strip().lstrip("./")
    if raw.endswith(POST_SUFFIX):
        raw = raw[: -len(POST_SUFFIX)]
    if raw.startswith(f"{BBS_DIR}/"):
        raw = raw[len(BBS_DIR) + 1 :]
    if "/" not in raw:
        return None, "Usage: topic/slug"
    topic, _, slug = raw.partition("/")
    if "/" in slug:
        return None, "Usage: topic/slug (no nested paths)"
    try:
        topic_slug = validate_slug(topic, kind="topic")
        post_slug = validate_slug(slug, kind="slug")
    except ValueError as exc:
        return None, str(exc)
    dest = bbs_root() / topic_slug / f"{post_slug}{POST_SUFFIX}"
    confined, err = confine_or_error(dest)
    if err:
        return None, err
    assert confined is not None
    if not confined.is_file():
        return None, f"No such thread: {BBS_DIR}/{topic_slug}/{post_slug}{POST_SUFFIX}"
    return confined, None


def search(query: str, max_matches: int = 12) -> dict[str, Any]:
    """SearchContent over `.g023/bbs/` only. Empty board → empty payload."""
    root = bbs_root()
    if not root.is_dir():
        return {
            "ok": True,
            "query": query,
            "total": 0,
            "samples": [],
            "files": [],
        }
    from .subagents.searcher import search_sync

    data = json.loads(
        search_sync(query, path=str(root), max_matches=max(1, int(max_matches or 12)))
    )
    data["ok"] = "error" not in data
    return data


def status() -> dict[str, Any]:
    root = bbs_root()
    topics = list_topics() if root.is_dir() else []
    posts = list_posts() if root.is_dir() else []
    return {
        "root": BBS_DIR,
        "exists": root.is_dir(),
        "topics": len(topics),
        "posts": len(posts),
        "index": INDEX_NAME if index_path().is_file() else "none",
    }


def status_line() -> str:
    root = bbs_root()
    if not root.is_dir():
        return "none"
    try:
        n_topics = len(list_topics())
        n_posts = len(list_posts())
    except OSError:
        return "unreadable"
    if n_topics == 0 and n_posts == 0:
        return "empty"
    return f"{n_topics} topic(s), {n_posts} post(s)"


def _print_json(payload: Any) -> int:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return 0 if (not isinstance(payload, dict) or payload.get("ok", True)) else 1


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="g023_code.bbs",
        description="Project bulletin board at .g023/bbs/ (posts are not in the system prompt).",
    )
    sub = parser.add_subparsers(dest="cmd")
    list_p = sub.add_parser("list", help="List topics, or posts in one topic")
    list_p.add_argument("topic", nargs="?", default="")
    search_p = sub.add_parser("search", help="Search posts")
    search_p.add_argument("query")
    search_p.add_argument("--max", type=int, default=12)
    read_p = sub.add_parser("read", help="Print one thread")
    read_p.add_argument("ref", help="topic/slug")
    post_p = sub.add_parser("post", help="Create or append a thread")
    post_p.add_argument("topic")
    post_p.add_argument("slug")
    post_p.add_argument("--body", default="", help="Post body; stdin if omitted")
    post_p.add_argument(
        "--replace",
        action="store_true",
        help="Overwrite the thread instead of appending",
    )
    sub.add_parser("index", help="Rebuild INDEX.md")
    sub.add_parser("status", help="Counts (default)")
    args = parser.parse_args(argv)
    cmd = args.cmd or "status"
    try:
        if cmd == "status":
            ensure_layout()
            return _print_json({**status(), "ok": True, "line": status_line()})
        if cmd == "list":
            ensure_layout()
            if args.topic:
                return _print_json({"ok": True, "posts": list_posts(args.topic)})
            return _print_json({"ok": True, "topics": list_topics()})
        if cmd == "search":
            return _print_json(search(args.query, max_matches=args.max))
        if cmd == "read":
            result = read_post(args.ref)
            if not result.get("ok"):
                return _print_json(result)
            sys.stdout.write(result.get("body") or "")
            if result.get("body") and not str(result["body"]).endswith("\n"):
                sys.stdout.write("\n")
            return 0
        if cmd == "post":
            body = args.body
            if not body:
                body = sys.stdin.read()
            return _print_json(post(args.topic, args.slug, body, append=not args.replace))
        if cmd == "index":
            return _print_json(write_index())
    except ValueError as exc:
        return _print_json({"ok": False, "error": str(exc)})
    except OSError as exc:
        return _print_json({"ok": False, "error": str(exc)})
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

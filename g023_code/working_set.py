"""Prefix-stable working set stored in session_facts and optionally mirrored in history.

The object is small, merge-by-id, and serialized with a fixed key order so
unchanged prefixes stay byte-stable. An existing history item is never rewritten
in place: live updates go to session_facts. Compact may refresh the snapshot
after the sticky goal as a new prefix.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

from .cache import get_cache
from .paths import rel_to_root

WORKING_SET_KEY = "working_set"
WORKING_SET_LEADIN = "Working set:\n"
MAX_OPEN_LEAVES = 8
MAX_FILES = 32

# A check may be wrapped: `VAR=x`, `timeout 60`, `uv run`, `poetry run`, or an
# interpreter path (`.venv/bin/python -m pytest`). A 1.5.0 development benchmark measured that the
# old start-anchored form missed all of those, so verify replay and the goal
# judge never saw the check.
_CMD_PREFIX = (
    r"(?:\w+=\S*\s+)*"
    r"(?:(?:timeout\s+(?:-{1,2}[\w-]+(?:[=\s]+(?:[A-Z]+|\d\S*))?\s+)*\d\S*|(?:uv|poetry|pipenv|hatch|pdm|rye)\s+run|"
    r"nice|time|env|xvfb-run|npx|bunx|pnpm\s+exec|yarn\s+exec)\s+)*"
    r"(?:[\w.~/-]*/)?"
)
_TEST_CMD = re.compile(
    r"(^|[;&|(]\s*)" + _CMD_PREFIX + r"("
    r"python3?(?:\.\d+)?\s+-m\s+pytest|pytest|py\.test|python3?(?:\.\d+)?\s+-m\s+unittest|"
    r"python3?(?:\.\d+)?\s+\S*test\S*\.py|"
    r"node\s+--check|"
    r"node\s+\S*test|"
    r"cargo\s+test|go\s+test|(?:npm|yarn|pnpm|bun)\s+(?:run\s+)?test|jest|vitest|"
    r"make\s+(?:test|check)|ctest|mvn\s+test|gradlew?\s+test|\./gradlew\s+test|tox|nox"
    r")\b",
    re.IGNORECASE,
)


def empty_working_set() -> dict[str, Any]:
    return {
        "goal": "",
        "constraints": [],
        "open": [],
        "files": [],
        "verify": {
            "command": "",
            "exit_code": None,
            "excerpt": "",
            "at": "",
            "failed_command": "",
            "failed_exit_code": None,
        },
    }


def _leaf(raw: dict) -> dict[str, Any]:
    evidence = raw.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    status = str(raw.get("status") or "pending")
    return {
        "id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "status": status,
        "evidence": [str(x) for x in evidence],
    }


def _file_ptr(raw: dict) -> dict[str, Any]:
    return {
        "path": str(raw.get("path") or ""),
        "hash": str(raw.get("hash") or ""),
        "last_range": raw.get("last_range"),
    }


def normalize(ws: Optional[dict]) -> dict[str, Any]:
    base = empty_working_set()
    if not isinstance(ws, dict):
        return base
    base["goal"] = str(ws.get("goal") or "")
    constraints = ws.get("constraints") or []
    if isinstance(constraints, str):
        constraints = [constraints]
    base["constraints"] = [str(c) for c in constraints]
    leaves = [_leaf(x) for x in (ws.get("open") or []) if isinstance(x, dict) and x.get("id")]
    open_ones = [x for x in leaves if x["status"] not in ("done", "cancelled")]
    closed = [x for x in leaves if x["status"] in ("done", "cancelled")]
    # No truncation here: dropping open leaves silently let the goal judge
    # "all done" with work still pending. The MAX_OPEN_LEAVES cap is
    # enforced where leaves are added (TodoWrite), with an explicit reject.
    base["open"] = open_ones + closed
    files = [_file_ptr(x) for x in (ws.get("files") or []) if isinstance(x, dict) and x.get("path")]
    base["files"] = files[-MAX_FILES:]
    verify = ws.get("verify") if isinstance(ws.get("verify"), dict) else {}
    command = str(verify.get("command") or "")
    exit_code = verify.get("exit_code")
    failed_command = str(verify.get("failed_command") or "")
    failed_exit = verify.get("failed_exit_code")
    # Facts written before failed_command existed: a non-zero last exit is
    # still an open failure. An explicit empty failed_command means a later
    # run of that same command already cleared it.
    if (
        not failed_command
        and "failed_command" not in verify
        and command
        and not _exit_ok(exit_code)
        and exit_code is not None
    ):
        failed_command = command
        failed_exit = exit_code
    base["verify"] = {
        "command": command,
        "exit_code": exit_code,
        "excerpt": str(verify.get("excerpt") or ""),
        "at": str(verify.get("at") or ""),
        "failed_command": failed_command,
        "failed_exit_code": failed_exit,
    }
    return base


def dumps_working_set(ws: dict) -> str:
    """Stable key order. Do not use sort_keys — that would reorder the object."""
    n = normalize(ws)
    return json.dumps(n, ensure_ascii=False, separators=(",", ":"))


# The model-facing item shows 12 hex of each file digest, like tool_codec; a
# prefix is a valid evidence citation (_evidence_cites_file). Persisted
# copies (cache fact, session log) keep the full digest.
ITEM_HASH_CHARS = 12


def working_set_item_content(ws: dict) -> str:
    n = normalize(ws)
    files = n.get("files")
    if isinstance(files, list):
        n = dict(n)
        n["files"] = [
            {**f, "hash": str(f.get("hash") or "")[:ITEM_HASH_CHARS]}
            if isinstance(f, dict) and f.get("hash") else f
            for f in files
        ]
    return WORKING_SET_LEADIN + json.dumps(n, ensure_ascii=False, separators=(",", ":"))


def is_working_set_item(item: dict) -> bool:
    if not isinstance(item, dict) or item.get("role") != "user":
        return False
    if item.get("type") in (
        "function_call",
        "function_call_output",
        "web_search_call",
        "reasoning",
    ):
        return False
    content = item.get("content")
    return isinstance(content, str) and content.startswith(WORKING_SET_LEADIN)


def load() -> dict[str, Any]:
    return normalize(get_cache().get_fact(WORKING_SET_KEY))


def save(ws: dict) -> dict[str, Any]:
    n = normalize(ws)
    get_cache().set_fact(WORKING_SET_KEY, n)
    return n


def reset() -> dict[str, Any]:
    return save(empty_working_set())


def find_working_set_item(items: list[dict]) -> Optional[dict]:
    for item in items:
        if is_working_set_item(item):
            return item
    return None


def mirror_working_set(
    items: list[dict],
    ws: Optional[dict] = None,
    sticky_goal: Optional[dict] = None,
) -> dict:
    """Save session_facts. Do not rewrite an existing history item.

    DeepSeek V4 Flash hits only when a later request fully matches a persisted
    cache prefix unit from token 0. Rewriting working-set bytes after the sticky
    goal is Example 2 (A+B then A+C): the miss starts at that slot.

    If no working-set item exists yet, append one at the tail (Example 1).
    Compact may later place the current snapshot after the sticky goal as a
    new prefix. ``sticky_goal`` is accepted for callers; it is not used to
    splice an item into the middle of ``input``.
    """
    ws = save(ws if ws is not None else load())
    existing = find_working_set_item(items)
    if existing is not None:
        return existing
    existing = {"role": "user", "content": working_set_item_content(ws)}
    items.append(existing)
    return existing


def _is_open(leaf: dict) -> bool:
    return leaf.get("status") not in ("done", "cancelled")


def split_open_overflow(existing: list[dict], incoming: list[dict]) -> tuple[list[dict], list[dict]]:
    """Split ``incoming`` into (fits, overflow) under ``MAX_OPEN_LEAVES``.

    Leaves are taken in order. A leaf overflows only if it would *add* an open
    leaf beyond the cap; updates, closes and cancels always fit (and a close
    earlier in the batch frees a slot for a later new leaf).
    """
    state: dict[str, bool] = {}
    for leaf in existing:
        if isinstance(leaf, dict) and leaf.get("id"):
            state[str(leaf["id"])] = _is_open(_leaf(leaf))
    fits: list[dict] = []
    overflow: list[dict] = []
    for leaf in incoming:
        if not isinstance(leaf, dict) or not leaf.get("id"):
            continue
        lid = str(leaf["id"])
        prev_open = state.get(lid, False)
        prev = next((x for x in existing if isinstance(x, dict) and str(x.get("id")) == lid), {})
        now_open = _is_open(_leaf({**prev, **leaf}))
        if now_open and not prev_open and sum(state.values()) >= MAX_OPEN_LEAVES:
            overflow.append(leaf)
            continue
        state[lid] = now_open
        fits.append(leaf)
    return fits, overflow


def merge_leaves(existing: list[dict], incoming: list[dict]) -> list[dict]:
    by_id: dict[str, dict] = {}
    for leaf in existing:
        if leaf.get("id"):
            by_id[str(leaf["id"])] = _leaf(leaf)
    for leaf in incoming:
        if not isinstance(leaf, dict) or not leaf.get("id"):
            continue
        prev = by_id.get(str(leaf["id"]), {})
        merged = _leaf({**prev, **leaf})
        by_id[merged["id"]] = merged
    return list(by_id.values())


def note_file(path: str, file_hash: str, last_range: Any = None) -> dict[str, Any]:
    ws = load()
    rel = rel_to_root(path)
    files = [f for f in ws["files"] if f.get("path") != rel]
    files.append({"path": rel, "hash": file_hash, "last_range": last_range})
    ws["files"] = files[-MAX_FILES:]
    return save(ws)


def _norm_cmd(command: str) -> str:
    return " ".join((command or "").split())


def _exit_ok(exit_code: Any) -> bool:
    return str(exit_code).strip() == "0"


def note_verify(command: str, exit_code: Any, excerpt: str) -> dict[str, Any]:
    """Record a test-like command.

    A later *different* command that exits 0 does not clear an earlier
    failure. ``node --check`` passing after ``pytest`` failed used to
    mark the goal complete. Only the same command, whitespace-normalised,
    exiting 0 clears ``failed_command``.
    """
    ws = load()
    prev = ws.get("verify") if isinstance(ws.get("verify"), dict) else {}
    failed_command = str(prev.get("failed_command") or "")
    failed_exit = prev.get("failed_exit_code")
    command = command or ""
    if _exit_ok(exit_code) and failed_command and _norm_cmd(command) == _norm_cmd(failed_command):
        failed_command = ""
        failed_exit = None
    elif not _exit_ok(exit_code):
        failed_command = command
        failed_exit = exit_code
    ws["verify"] = {
        "command": command,
        "exit_code": exit_code,
        "excerpt": (excerpt or "")[:500],
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "failed_command": failed_command,
        "failed_exit_code": failed_exit,
    }
    return save(ws)


def update_todos(
    leaves: list[dict],
    goal: Optional[str] = None,
    constraints: Optional[list] = None,
) -> dict[str, Any]:
    ws = load()
    if goal:
        ws["goal"] = goal
    if constraints is not None:
        ws["constraints"] = [str(c) for c in constraints]
    ws["open"] = merge_leaves(ws["open"], leaves)
    return save(ws)


def set_goal(goal: str) -> dict[str, Any]:
    ws = load()
    ws["goal"] = goal
    return save(ws)


def looks_like_test_command(command: str) -> bool:
    return bool(_TEST_CMD.search(command or ""))


def _path_in_files(path: str, files: dict[str, str]) -> bool:
    path = (path or "").strip()
    if not path:
        return False
    if path in files:
        return True
    rel = rel_to_root(path)
    return bool(rel) and rel in files


def _evidence_cites_file(item: str, files: dict[str, str]) -> bool:
    """True when evidence names a working-set file path.

    Canonical form is ``path@sha256:<full digest>``. Models also send
    ``path truncatedhash``, ``path hash``, and a wrong digest after
    ``@sha256:``. A path already in ``files[]`` is the lock — it was
    written this session. Unknown paths still fail.
    """
    item = (item or "").strip()
    if not item:
        return False
    if _path_in_files(item, files):
        return True
    path = item
    if "@sha256:" in item:
        path, _, digest = item.partition("@sha256:")
        path, digest = path.strip(), digest.strip()
        if digest and digest in files.values():
            return True
        if digest and any(
            h.startswith(digest) or digest.startswith(h) for h in files.values() if h
        ):
            return True
    else:
        path = item.split()[0].strip()
    return _path_in_files(path, files)


def evidence_satisfies_done(ws: dict, leaf: dict) -> bool:
    """True when a done leaf cites a file pointer or a passing verify command."""
    evidence = leaf.get("evidence") or []
    if isinstance(evidence, str):
        evidence = [evidence]
    evidence = [str(x).strip() for x in evidence if str(x).strip()]
    if not evidence:
        return False
    files = {
        str(f.get("path") or ""): str(f.get("hash") or "")
        for f in (ws.get("files") or [])
        if isinstance(f, dict) and f.get("path")
    }
    verify = ws.get("verify") if isinstance(ws.get("verify"), dict) else {}
    verify_ok = verify.get("exit_code") == 0 and bool(verify.get("command"))
    verify_cmd = str(verify.get("command") or "")
    for item in evidence:
        if _evidence_cites_file(item, files):
            return True
        if verify_ok and _evidence_cites_verify(item, verify_cmd):
            return True
    return False


def _evidence_cites_verify(item: str, verify_cmd: str) -> bool:
    """True when a citation points at the verify command that actually ran.

    A string that merely looks like pytest is not evidence. The recorded
    command has to be the one that exited 0, and the citation has to name it
    (or be the generic ``verify`` / ``verify:`` form models emit).
    """
    if item == verify_cmd or (verify_cmd and verify_cmd in item):
        return True
    if item.lower() == "verify" or item.lower().startswith("verify:"):
        return True
    probe = item
    if probe.lower().startswith("verify:"):
        probe = probe.split(":", 1)[1].strip()
    probe = probe.split("->", 1)[0].strip()
    if not probe or not verify_cmd:
        return False
    return probe == verify_cmd or probe in verify_cmd or verify_cmd in probe


def verify_failed(ws: dict) -> bool:
    """True while a test-like command is still failing.

    The last command may have exited 0. That does not count when a
    different command failed earlier and has not been re-run successfully.
    """
    verify = ws.get("verify") if isinstance(ws.get("verify"), dict) else {}
    return bool(str(verify.get("failed_command") or "").strip())


def evidence_hint(ws: dict, leaf: Optional[dict] = None) -> str:
    """What TodoWrite should tell the model when done_requires_evidence fires."""
    files = [
        f
        for f in (ws.get("files") or [])
        if isinstance(f, dict) and f.get("path")
    ]
    verify = ws.get("verify") if isinstance(ws.get("verify"), dict) else {}
    example = ""
    if files:
        path = str(files[-1].get("path") or "")
        digest = str(files[-1].get("hash") or "")
        if path and digest:
            example = f" Example: {path}@sha256:{digest[:ITEM_HASH_CHARS]}."
        elif path:
            example = f" Example: {path}."
    known = [str(f.get("path")) for f in files[:12]]
    return (
        "status=done needs evidence[] that cites a working-set file path "
        f"(path@sha256:<hash> or the path alone).{example} "
        f"Known files: {known}. "
        f"verify.command={verify.get('command')!r} exit={verify.get('exit_code')!r}."
    )


def judge_goal_completion(ws: Optional[dict] = None) -> dict[str, Any]:
    """Completed / not completed from working-set leaves and evidence.

    A final assistant message is not success. Pending open leaves, done leaves
    without evidence, an empty leaf list, a list that was only cancelled, or a
    failing verify command are not completed. No open leaves, at least one
    done leaf citing file/verify evidence, and no failing verify is completed.
    """
    ws = normalize(ws if ws is not None else load())
    leaves = [x for x in (ws.get("open") or []) if isinstance(x, dict)]
    pending = [x for x in leaves if x.get("status") not in ("done", "cancelled")]
    done = [x for x in leaves if x.get("status") == "done"]
    cancelled = [x for x in leaves if x.get("status") == "cancelled"]
    missing = [x for x in done if not evidence_satisfies_done(ws, x)]
    pending_ids = [str(x.get("id") or "") for x in pending]
    missing_ids = [str(x.get("id") or "") for x in missing]

    def _open(reason: str) -> dict[str, Any]:
        return {
            "completed": False,
            "status": "not completed",
            "reason": reason,
            "pending": pending_ids,
            "done_without_evidence": missing_ids,
        }

    if pending:
        return _open(f"{len(pending)} open leaves still pending")
    if missing:
        return _open("done leaves missing evidence")
    if not done:
        if cancelled:
            return _open("every leaf was cancelled; nothing was finished")
        return _open("no working-set leaves with evidence")
    if verify_failed(ws):
        verify = ws.get("verify") or {}
        cmd = verify.get("failed_command") or verify.get("command")
        code = verify.get("failed_exit_code")
        if code is None:
            code = verify.get("exit_code")
        return _open(f"verify failed ({cmd} exit {code})")
    return {
        "completed": True,
        "status": "completed",
        "reason": "no open leaves; done leaves have evidence",
        "pending": pending_ids,
        "done_without_evidence": missing_ids,
    }

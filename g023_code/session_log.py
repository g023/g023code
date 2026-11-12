"""Append-only JSONL session log. Ancestry lives here; compact records a view.

Invariants:

* replay == memory: ``replay_log(file)`` reproduces the orchestrator's history.
  ``SessionLog`` keeps a shadow of the replay (same reducer, ``_reduce``) and
  ``checkpoint(items)`` appends an ``output`` tail or a ``view`` record when
  memory diverged (rollback, interrupt), so a crash never resumes into a
  history the model did not see.
* Single writer per file. Resuming attaches to an existing file; two live REPLs
  resuming the *same* session would interleave appends. Each record is one
  ``write`` of one line (O_APPEND), so the file stays parseable, but the
  replayed history then mixes both conversations. New sessions get unique
  names via ``O_EXCL`` and never collide.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .checkpoint import sign as sign_handoff
from .checkpoint import verify as verify_handoff
from .config import get_project_root
from .fsutil import atomic_write_text, create_exclusive
from .working_set import (
    dumps_working_set,
    normalize as normalize_working_set,
)

LARGE_LOG_CHARS = 4096
WRITE_TOOLS = ("WriteFile", "Edit")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


OBJECTIVE_LOG_HEADER = "G023_OBJECTIVE_LOG 1"
OBJECTIVE_LOG_WS_MARK = "--- working_set ---"
OBJECTIVE_LOG_SUMMARY_MARK = "--- summary ---"


def sessions_dir(root: Optional[Path] = None) -> Path:
    d = (root or get_project_root()) / ".g023" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def logs_dir(root: Optional[Path] = None) -> Path:
    d = (root or get_project_root()) / ".g023" / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _strip_write_bodies(args: dict) -> dict:
    out = dict(args)
    for key in ("content", "old_string", "new_string"):
        value = out.get(key)
        if isinstance(value, str) and (key == "content" or len(value) > LARGE_LOG_CHARS):
            out[key] = {"omitted": True, "hash": _sha(value), "bytes": len(value.encode("utf-8"))}
    return out


def sanitize_item(item: dict) -> dict:
    """Copy an item for disk. Large *re-derivable* bodies become hash + byte count.

    Invariant: only text that can be recovered elsewhere is ever elided —
    tool outputs (re-run the tool) and WriteFile/Edit payloads (the file on
    disk holds them). Message ``content`` (user prompts, the compaction
    summary, the mid-turn restatement, assistant text) has no other copy, so
    it is always written in full; eliding it made /resume restore a hash.
    """
    out = dict(item)
    if out.get("type") == "function_call" and out.get("name") in WRITE_TOOLS:
        raw = out.get("arguments")
        if isinstance(raw, str):
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                if len(raw) > LARGE_LOG_CHARS:
                    out["arguments"] = json.dumps(
                        {"omitted": True, "hash": _sha(raw), "bytes": len(raw)}
                    )
                return out
            if isinstance(args, dict):
                out["arguments"] = json.dumps(
                    _strip_write_bodies(args), ensure_ascii=False
                )
    if out.get("type") == "function_call_output":
        output = out.get("output")
        if isinstance(output, str) and len(output) > LARGE_LOG_CHARS:
            out["output"] = json.dumps(
                {"omitted": True, "hash": _sha(output), "bytes": len(output)}
            )
    return out


def sanitize_output_text(output: str) -> Any:
    if isinstance(output, str) and len(output) > LARGE_LOG_CHARS:
        return {"omitted": True, "hash": _sha(output), "bytes": len(output)}
    return output


class SessionLog:
    """Append-only event log whose replay must equal the in-memory history.

    The log keeps a shadow of what :func:`replay_log` would rebuild (the same
    reducer, applied to each record as it is written). :meth:`checkpoint`
    compares that shadow with the live history and appends only what is
    missing: an ``output`` record for a pure tail extension, a ``view``
    record when something was rewritten in place. Every history mutation
    that is not an append (rollback, reseat, micro-compaction, the
    working-set mirror) is therefore captured, and /resume sees the same
    history the process had.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        self._terminate_torn_tail()
        atomic_write_text(self.path.parent / "CURRENT", self.path.name)
        self._shadow_items: list[dict] = []
        self._shadow_ws: Optional[dict] = None
        if self.path.stat().st_size:
            self._shadow_items, self._shadow_ws = replay_log(self.path)

    def _terminate_torn_tail(self) -> None:
        """A crash mid-append can leave a last line without its newline.

        Appending after it would glue the next record onto the torn bytes and
        replay would drop both. Terminating it first confines the loss to the
        record that was already torn.
        """
        try:
            size = self.path.stat().st_size
            if not size:
                return
            with open(self.path, "rb") as fh:
                fh.seek(size - 1)
                last = fh.read(1)
            if last != b"\n":
                fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND)
                try:
                    os.write(fd, b"\n")
                finally:
                    os.close(fd)
        except OSError:
            pass

    @classmethod
    def open_new(cls, root: Optional[Path] = None) -> "SessionLog":
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        # O_EXCL: two processes starting in the same second get two files.
        path = create_exclusive(sessions_dir(root), stamp, ".jsonl")
        return cls(path)

    def append(self, kind: str, **fields: Any) -> None:
        rec = {"ts": time.time(), "kind": kind}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        data = (line + "\n").encode("utf-8")
        # One O_APPEND write per record: concurrent appenders cannot tear a line.
        fd = os.open(str(self.path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        # Reduce the record exactly as replay will read it back.
        self._shadow_items, self._shadow_ws = _reduce(
            self._shadow_items, self._shadow_ws, json.loads(line)
        )

    def checkpoint(self, items: Iterable[dict]) -> Optional[str]:
        """Make replay equal ``items``; returns the record kind written, if any."""
        expected = json.loads(
            json.dumps([sanitize_item(i) for i in items], ensure_ascii=False, default=str)
        )
        shadow = self._shadow_items
        if expected == shadow:
            return None
        n = len(shadow)
        if len(expected) > n and expected[:n] == shadow:
            self.append("output", items=expected[n:])
            return "output"
        self.append("view", items=expected)
        return "view"

    def log_user(self, item: dict) -> None:
        self.append("user", item=sanitize_item(item))

    def log_output(self, items: Iterable[dict]) -> None:
        self.append("output", items=[sanitize_item(i) for i in items])

    def log_tool_result(self, call_id: str, output: str) -> None:
        self.append("tool_result", call_id=call_id, output=sanitize_output_text(output))

    def log_compact(self, items: Iterable[dict], working_set: Optional[dict] = None) -> None:
        self.append(
            "compact_boundary",
            view=[sanitize_item(i) for i in items],
            working_set=working_set,
        )

    def log_working_set(self, ws: dict) -> None:
        self.append("working_set", working_set=ws)

    def log_interrupt(self, items: Iterable[dict]) -> None:
        self.append("interrupt", items=[sanitize_item(i) for i in items])

    def log_prefix_fingerprint(self, instructions_sha256: str, tools_sha256: str) -> None:
        self.append(
            "prefix_fingerprint",
            instructions_sha256=instructions_sha256,
            tools_sha256=tools_sha256,
        )


def prefix_hashes(instructions: str, tools: list) -> tuple[str, str]:
    """Stable sha256 of the static prefix pieces sent on every request."""
    inst = hashlib.sha256((instructions or "").encode("utf-8")).hexdigest()
    dumped = json.dumps(tools, ensure_ascii=False, separators=(",", ":"))
    tools_h = hashlib.sha256(dumped.encode("utf-8")).hexdigest()
    return inst, tools_h


def load_prefix_fingerprint(path: Path) -> Optional[dict]:
    """First prefix_fingerprint record in the JSONL, or None."""
    if not path.is_file():
        return None
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("kind") == "prefix_fingerprint":
                return rec
    return None


def search_records(
    query: str,
    *,
    root: Optional[Path] = None,
    max_hits: int = 8,
) -> list[dict]:
    """Grep session JSONL without loading omitted bodies."""
    tokens = [t.lower() for t in re.split(r"[^a-zA-Z0-9]+", query or "") if t]
    if not tokens:
        return []
    d = (root or get_project_root()) / ".g023" / "sessions"
    if not d.is_dir():
        return []
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    hits: list[dict] = []
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("kind")
            if kind == "prefix_fingerprint":
                continue
            omitted_hash = None
            excerpt, omitted_hash = _record_excerpt(rec)
            hay = excerpt.lower()
            if not any(tok in hay for tok in tokens):
                continue
            hit = {
                "ts": rec.get("ts"),
                "kind": kind,
                "excerpt": excerpt[:400],
                "path": str(path),
            }
            if omitted_hash:
                hit["hash"] = omitted_hash
                hit["omitted"] = True
            hits.append(hit)
            if len(hits) >= max_hits:
                return hits
    return hits


def _record_excerpt(rec: dict) -> tuple[str, Optional[str]]:
    kind = rec.get("kind")
    omitted_hash = None

    def from_maybe_omitted(value: Any) -> tuple[str, Optional[str]]:
        if isinstance(value, dict) and value.get("omitted"):
            return (
                f"[omitted hash={value.get('hash')} bytes={value.get('bytes')}]",
                str(value.get("hash") or "") or None,
            )
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                parsed = None
            if isinstance(parsed, dict) and parsed.get("omitted"):
                return (
                    f"[omitted hash={parsed.get('hash')} bytes={parsed.get('bytes')}]",
                    str(parsed.get("hash") or "") or None,
                )
            return value[:400], None
        if value is None:
            return "", None
        text = json.dumps(value, ensure_ascii=False)
        return text[:400], None

    if kind == "user":
        item = rec.get("item") or {}
        return from_maybe_omitted(item.get("content") if isinstance(item, dict) else rec)
    if kind == "tool_result":
        return from_maybe_omitted(rec.get("output"))
    if kind == "output":
        bits = []
        for item in rec.get("items") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name") or item.get("type") or ""
            args = item.get("arguments") or item.get("content") or ""
            bits.append(f"{name} {args}"[:200])
        return " ".join(bits)[:400], None
    if kind == "compact_boundary":
        view = rec.get("view") or []
        return f"compact_boundary items={len(view)}", None
    if kind == "working_set":
        return "working_set " + json.dumps(rec.get("working_set") or {}, ensure_ascii=False)[:300], None
    blob = json.dumps(rec, ensure_ascii=False, default=str)
    return blob[:400], omitted_hash


def latest_session_file(root: Optional[Path] = None) -> Optional[Path]:
    d = (root or get_project_root()) / ".g023" / "sessions"
    if not d.is_dir():
        return None
    pointer = d / "CURRENT"
    if pointer.is_file():
        name = pointer.read_text(encoding="utf-8").strip()
        candidate = d / name
        if candidate.is_file():
            return candidate
    files = sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def _as_output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _reduce(
    items: list[dict], ws: Optional[dict], rec: dict
) -> tuple[list[dict], Optional[dict]]:
    """Apply one log record. The single definition of the log's semantics.

    Appends mutate ``items`` in place (O(1) per record); view records replace it.
    """
    kind = rec.get("kind")
    if kind == "user" and isinstance(rec.get("item"), dict):
        items.append(rec["item"])
    elif kind == "output":
        chunk = rec.get("items") or []
        if isinstance(chunk, list):
            items.extend(i for i in chunk if isinstance(i, dict))
    elif kind == "tool_result":
        items.append(
            {
                "type": "function_call_output",
                "call_id": rec.get("call_id") or "",
                "output": _as_output_text(rec.get("output")),
            }
        )
    elif kind == "compact_boundary":
        view = rec.get("view") or []
        if isinstance(view, list):
            items = [i for i in view if isinstance(i, dict)]
        if isinstance(rec.get("working_set"), dict):
            ws = rec["working_set"]
    elif kind == "working_set" and isinstance(rec.get("working_set"), dict):
        ws = rec["working_set"]
    elif kind in ("interrupt", "view"):
        view = rec.get("items")
        if isinstance(view, list):
            items = [i for i in view if isinstance(i, dict)]
    return items, ws


def replay_log(path: Path) -> tuple[list[dict], Optional[dict]]:
    items: list[dict] = []
    ws: Optional[dict] = None
    if not path.is_file():
        return items, ws
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                items, ws = _reduce(items, ws, rec)
    return items, ws


# How many older logs /resume looks through when the newest one is empty.
RESUME_LOOKBACK = 20


def load_latest_view(root: Optional[Path] = None) -> tuple[list[dict], Optional[dict], Optional[Path]]:
    """The newest session view that has items.

    /clear (and a process that only ran slash commands) opens a fresh log that
    becomes CURRENT. Resuming that empty log made "/clear, then /resume" a dead
    end, so an empty newest log falls back to the newest one with content.
    """
    path = latest_session_file(root)
    if path is None:
        return [], None, None
    items, ws = replay_log(path)
    if items:
        return items, ws, path
    others = [p for p in path.parent.glob("*.jsonl") if p != path]
    others.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for other in others[:RESUME_LOOKBACK]:
        try:
            o_items, o_ws = replay_log(other)
        except Exception:
            continue
        if o_items:
            return o_items, o_ws, other
    return items, ws, path


def _signed_fields(ts: str, completed: str, goal: str, ws_json: str, summary: str) -> dict[str, Any]:
    """What a handoff signature covers: every field a resume reads back (checkpoint.sign)."""
    return {
        "v": 1,
        "ts": ts,
        "completed": completed,
        "goal": goal.strip(),
        "working_set": ws_json.strip(),
        "summary": summary.strip(),
    }


def _objective_log_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime())


def write_objective_log(
    summary: str,
    *,
    working_set: Optional[dict] = None,
    goal: str = "",
    completed: Optional[bool] = None,
    root: Optional[Path] = None,
) -> Path:
    """Write the post-turn summary to ``.g023/logs/{dstamp_tstamp}.log``.

    The file is the handoff for a later process: system prompt stays in
    ``instructions``; this summary (+ working set) is the next ``input``.
    """
    d = logs_dir(root)
    stamp = _objective_log_stamp()
    goal_line = (goal or "").replace("\r", " ").replace("\n", " ").strip()
    if completed is True:
        done = "true"
    elif completed is False:
        done = "false"
    else:
        done = "unknown"
    ws_json = dumps_working_set(normalize_working_set(working_set)).strip()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    summary_text = (summary or "").strip()
    signed = sign_handoff(_signed_fields(ts, done, goal_line, ws_json, summary_text))
    sig_lines = f"key: {signed[0]}\nsig: {signed[1]}\n" if signed else ""
    body = (
        f"{OBJECTIVE_LOG_HEADER}\n"
        f"ts: {ts}\n"
        f"completed: {done}\n"
        f"goal: {goal_line}\n"
        f"{sig_lines}"
        f"\n{OBJECTIVE_LOG_WS_MARK}\n"
        f"{ws_json}\n"
        f"\n{OBJECTIVE_LOG_SUMMARY_MARK}\n"
        f"{summary_text}\n"
    )
    # Claim a fresh name atomically, then publish the pointer atomically.
    path = create_exclusive(d, stamp, ".log", text=body)
    atomic_write_text(d / "CURRENT", path.name)
    return path


def latest_objective_log(root: Optional[Path] = None) -> Optional[Path]:
    d = (root or get_project_root()) / ".g023" / "logs"
    if not d.is_dir():
        return None
    pointer = d / "CURRENT"
    if pointer.is_file():
        name = pointer.read_text(encoding="utf-8").strip()
        candidate = d / name
        if candidate.is_file():
            return candidate
    files = sorted(d.glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def parse_objective_log(path: Path) -> dict[str, Any]:
    """Parse a handoff log into summary / working_set / metadata.

    A file without the header is treated as the summary body so a hand-edited
    log still loads.
    """
    text = Path(path).read_text(encoding="utf-8")
    out: dict[str, Any] = {
        "summary": "",
        "working_set": None,
        "goal": "",
        "completed": None,
        "ts": "",
        "path": Path(path),
        "provenance": "unsigned",
    }
    if not text.startswith(OBJECTIVE_LOG_HEADER):
        out["summary"] = text.strip()
        return out
    rest = text[len(OBJECTIVE_LOG_HEADER) :].lstrip("\n")
    meta, _, tail = rest.partition(f"\n{OBJECTIVE_LOG_WS_MARK}\n")
    if not _:
        out["summary"] = rest.strip()
        return out
    completed_raw, kid, sig = "", "", ""
    for line in meta.splitlines():
        if line.startswith("ts:"):
            out["ts"] = line[3:].strip()
        elif line.startswith("completed:"):
            completed_raw = line.split(":", 1)[1].strip()
            raw = completed_raw.lower()
            if raw == "true":
                out["completed"] = True
            elif raw == "false":
                out["completed"] = False
        elif line.startswith("goal:"):
            out["goal"] = line.split(":", 1)[1].strip()
        elif line.startswith("key:"):
            kid = line[4:].strip()
        elif line.startswith("sig:"):
            sig = line[4:].strip()
    ws_raw, _, summary = tail.partition(f"\n{OBJECTIVE_LOG_SUMMARY_MARK}\n")
    if not _:
        ws_raw, _, summary = tail.partition(f"{OBJECTIVE_LOG_SUMMARY_MARK}\n")
    ws_raw = ws_raw.strip()
    if ws_raw:
        try:
            parsed = json.loads(ws_raw)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            out["working_set"] = normalize_working_set(parsed)
    out["summary"] = summary.strip()
    out["provenance"] = verify_handoff(
        _signed_fields(out["ts"], completed_raw, out["goal"], ws_raw, out["summary"]), kid, sig
    )
    return out


def load_latest_objective_log(root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    path = latest_objective_log(root)
    if path is None:
        return None
    try:
        return parse_objective_log(path)
    except OSError:
        return None

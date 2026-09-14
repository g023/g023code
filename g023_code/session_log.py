"""Append-only JSONL session log. Ancestry lives here; compact records a view."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import get_project_root
from .working_set import (
    dumps_working_set,
    is_working_set_item,
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
    """Copy an item for disk. Large write bodies become hash + byte count."""
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
    content = out.get("content")
    if (
        isinstance(content, str)
        and len(content) > LARGE_LOG_CHARS
        and not is_working_set_item(out)
    ):
        out["content"] = json.dumps(
            {"omitted": True, "hash": _sha(content), "bytes": len(content)}
        )
    return out


def sanitize_output_text(output: str) -> Any:
    if isinstance(output, str) and len(output) > LARGE_LOG_CHARS:
        return {"omitted": True, "hash": _sha(output), "bytes": len(output)}
    return output


class SessionLog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()
        pointer = self.path.parent / "CURRENT"
        pointer.write_text(self.path.name, encoding="utf-8")

    @classmethod
    def open_new(cls, root: Optional[Path] = None) -> "SessionLog":
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        path = sessions_dir(root) / f"{stamp}.jsonl"
        # Two sessions in the same second: suffix.
        if path.exists():
            path = sessions_dir(root) / f"{stamp}-{int(time.time() * 1000) % 1000}.jsonl"
        return cls(path)

    def append(self, kind: str, **fields: Any) -> None:
        rec = {"ts": time.time(), "kind": kind}
        rec.update(fields)
        line = json.dumps(rec, ensure_ascii=False, default=str)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

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
    """Stable sha256 of the static prefix pieces sent on /responses."""
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
            elif kind == "interrupt":
                view = rec.get("items")
                if isinstance(view, list):
                    items = [i for i in view if isinstance(i, dict)]
    return items, ws


def load_latest_view(root: Optional[Path] = None) -> tuple[list[dict], Optional[dict], Optional[Path]]:
    path = latest_session_file(root)
    if path is None:
        return [], None, None
    items, ws = replay_log(path)
    return items, ws, path


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
    path = d / f"{stamp}.log"
    if path.exists():
        path = d / f"{stamp}-{int(time.time() * 1000) % 1000}.log"
    goal_line = (goal or "").replace("\r", " ").replace("\n", " ").strip()
    if completed is True:
        done = "true"
    elif completed is False:
        done = "false"
    else:
        done = "unknown"
    ws_json = dumps_working_set(normalize_working_set(working_set))
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = (
        f"{OBJECTIVE_LOG_HEADER}\n"
        f"ts: {ts}\n"
        f"completed: {done}\n"
        f"goal: {goal_line}\n"
        f"\n{OBJECTIVE_LOG_WS_MARK}\n"
        f"{ws_json}\n"
        f"\n{OBJECTIVE_LOG_SUMMARY_MARK}\n"
        f"{(summary or '').rstrip()}\n"
    )
    path.write_text(body, encoding="utf-8")
    (d / "CURRENT").write_text(path.name, encoding="utf-8")
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
    }
    if not text.startswith(OBJECTIVE_LOG_HEADER):
        out["summary"] = text.strip()
        return out
    rest = text[len(OBJECTIVE_LOG_HEADER) :].lstrip("\n")
    meta, _, tail = rest.partition(f"\n{OBJECTIVE_LOG_WS_MARK}\n")
    if not _:
        out["summary"] = rest.strip()
        return out
    for line in meta.splitlines():
        if line.startswith("ts:"):
            out["ts"] = line[3:].strip()
        elif line.startswith("completed:"):
            raw = line.split(":", 1)[1].strip().lower()
            if raw == "true":
                out["completed"] = True
            elif raw == "false":
                out["completed"] = False
        elif line.startswith("goal:"):
            out["goal"] = line.split(":", 1)[1].strip()
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
    return out


def load_latest_objective_log(root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    path = latest_objective_log(root)
    if path is None:
        return None
    try:
        return parse_objective_log(path)
    except OSError:
        return None

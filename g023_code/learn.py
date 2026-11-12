"""Cross-session, cross-project procedural guides.

Files are the source of truth (not SQLite: /cache clear must not delete them).
Nothing in this module is written into Orchestrator._system.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from .config import get_project_root
from .fsutil import atomic_write_text, file_lock

ID_RE = re.compile(r"^[a-z0-9-]{2,64}$")
INDEX_HEADER = "# learn index — do not edit the body of guides here"
INDEX_LINE_RE = re.compile(
    r"^-\s*\[(?P<scope>project|env|user)\]\s+(?P<id>[a-z0-9-]+)\s+[—-]\s+"
    r"(?P<title>.+?)\.\s+When:\s*(?P<when>.+?)\s*$"
)
SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9]{8,}|sk-or-[A-Za-z0-9]+|api[_-]?key\s*[:=]|"
    r"Bearer\s+[A-Za-z0-9._\-]+|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----)",
    re.IGNORECASE,
)
AUTH_RE = re.compile(
    r"(set\s+\w+\s+to\s+(allow|ask|block)|"
    r"bash is allowed|"
    r"change(?:ing)?\s+tool permission|"
    r"\b(allow|ask|block)\b.{0,40}\b(bash|writefile|edit|permission))",
    re.IGNORECASE,
)
KINDS = ("procedure", "pitfall", "routing", "fact")
SCOPES = ("project", "env", "user")
INDEX_CAP = 80
GUIDE_FILE_CAP = 40
BODY_LINE_CAP = 80
BODY_CHAR_CAP = 4000
SEARCH_HIT_CAP = 5
SESSION_PUT_CAP = 3
TURN_GET_CAP = 2
NUDGE_PREFIX = "[learn] LearnSearch query="

_session_puts = 0
_turn_gets = 0


def enabled() -> bool:
    return os.environ.get("G023_LEARN", "1").strip().lower() not in ("0", "false", "off")


def home_root() -> Path:
    return Path.home() / ".g023" / "learn"


def project_root() -> Path:
    return get_project_root() / ".g023" / "learn"


def index_path(scope_root: Path) -> Path:
    return Path(scope_root) / "INDEX.md"


def guide_path(scope_root: Path, gid: str) -> Path:
    return Path(scope_root) / "guides" / f"{gid}.md"


def ledger_path(scope_root: Path) -> Path:
    return Path(scope_root) / "ledger.jsonl"


def reset_turn() -> None:
    global _turn_gets
    _turn_gets = 0


def reset_session_budget() -> None:
    global _session_puts, _turn_gets
    _session_puts = 0
    _turn_gets = 0


def secret_hit(text: str) -> bool:
    return bool(SECRET_RE.search(text or ""))


def _auth_hit(text: str) -> bool:
    return bool(AUTH_RE.search(text or ""))


def _tokenize(query: str) -> list[str]:
    return [t.lower() for t in re.split(r"[^a-zA-Z0-9]+", query or "") if t]


def _when_set(when: str) -> frozenset[str]:
    return frozenset(t.strip().lower() for t in (when or "").split(",") if t.strip())


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _append_ledger(scope_root: Path, rec: dict) -> None:
    path = ledger_path(scope_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.time(), **rec}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def parse_guide(text: str) -> Optional[dict]:
    if not text.startswith("---"):
        return None
    rest = text[3:]
    end = rest.find("\n---")
    if end < 0:
        return None
    block = rest[:end]
    body = rest[end + 4 :].lstrip("\n")
    meta: dict[str, Any] = {}
    list_key: Optional[str] = None
    current_item: Optional[dict] = None
    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        if list_key and line.startswith("  - "):
            if current_item:
                meta.setdefault(list_key, []).append(current_item)
            current_item = {}
            piece = line[4:].strip()
            if ":" in piece:
                k, _, v = piece.partition(":")
                current_item[k.strip()] = _scalar(v.strip())
            continue
        if list_key and current_item is not None and line.startswith("    "):
            piece = line.strip()
            if ":" in piece:
                k, _, v = piece.partition(":")
                current_item[k.strip()] = _scalar(v.strip())
            continue
        if current_item is not None and list_key:
            meta.setdefault(list_key, []).append(current_item)
            current_item = None
            list_key = None
        if ":" not in line or line.startswith(" "):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value == "":
            list_key = key
            meta[key] = []
            continue
        list_key = None
        meta[key] = _scalar(value)
    if current_item is not None and list_key:
        meta.setdefault(list_key, []).append(current_item)
    meta["body"] = body
    return meta


def _scalar(value: str) -> Any:
    if value in ("", "null", "None"):
        return ""
    if value.isdigit() or (value.startswith("-") and value[1:].isdigit()):
        return int(value)
    if (value.startswith("[") and value.endswith("]")) or (
        value.startswith("{") and value.endswith("}")
    ):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def render_guide(meta: dict, body: str) -> str:
    lines = ["---"]
    order = (
        "id",
        "kind",
        "scope",
        "title",
        "when",
        "not_when",
        "requires",
        "created",
        "updated",
        "evidence",
        "helpful",
        "harmful",
        "status",
    )
    for key in order:
        if key not in meta:
            continue
        val = meta[key]
        if key == "requires":
            if not val:
                continue
            lines.append("requires:")
            for item in val:
                if isinstance(item, dict):
                    lines.append(f"  - command: {json.dumps(str(item.get('command') or ''))}")
                    lines.append(f"    expect_exit: {int(item.get('expect_exit', 0))}")
                else:
                    lines.append(f"  - command: {json.dumps(str(item))}")
                    lines.append("    expect_exit: 0")
            continue
        if val is None or val == "":
            if key in ("not_when",):
                continue
            lines.append(f"{key}: ")
            continue
        lines.append(f"{key}: {val}")
    lines.append("---")
    lines.append("")
    text = (body or "").rstrip() + "\n"
    return "\n".join(lines) + text


def probe_ok(requires: list) -> bool:
    if not requires:
        return True
    for req in requires:
        if isinstance(req, str):
            cmd, expect = req, 0
        elif isinstance(req, dict):
            cmd = str(req.get("command") or "")
            try:
                expect = int(req.get("expect_exit", 0))
            except (TypeError, ValueError):
                expect = 0
        else:
            return False
        if not cmd.strip():
            return False
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                timeout=5,
                capture_output=True,
            )
        except Exception:
            return False
        if proc.returncode != expect:
            return False
    return True


def _parse_index_line(line: str) -> Optional[dict]:
    m = INDEX_LINE_RE.match(line.strip())
    if not m:
        return None
    return {
        "scope": m.group("scope"),
        "id": m.group("id"),
        "title": m.group("title").strip(),
        "when": m.group("when").strip(),
        "line": line.strip(),
    }


def _catalog_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []


def _catalog_count(lines: list[str]) -> int:
    return sum(1 for ln in lines if ln.startswith("- "))


def _load_guide(scope_root: Path, gid: str) -> Optional[dict]:
    path = guide_path(scope_root, gid)
    if not path.is_file():
        return None
    try:
        parsed = parse_guide(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    if not parsed:
        return None
    parsed["path"] = str(path)
    parsed["root"] = str(scope_root)
    return parsed


def _match_hit(query_tokens: list[str], rec: dict, guide: dict) -> bool:
    if not query_tokens:
        return False
    hay = " ".join(
        [
            str(rec.get("id") or ""),
            str(rec.get("title") or ""),
            str(rec.get("when") or ""),
            str(guide.get("id") or ""),
            str(guide.get("title") or ""),
            str(guide.get("when") or ""),
        ]
    ).lower()
    return any(tok in hay for tok in query_tokens)


def search(query: str, *, scope: str = "all") -> list[dict]:
    if not enabled():
        return []
    scope = (scope or "all").strip().lower()
    if scope not in ("all", "project", "env", "user"):
        scope = "all"
    tokens = _tokenize(query)
    hits: list[dict] = []
    seen: set[str] = set()
    # Project first so a project id can shadow a home id.
    ordered = []
    if scope in ("all", "project"):
        ordered.append(project_root())
    if scope in ("all", "env", "user"):
        ordered.append(home_root())
    for root in ordered:
        for rec in (_parse_index_line(ln) for ln in _catalog_lines(index_path(root))):
            if rec is None:
                continue
            if scope in ("env", "user") and rec["scope"] != scope:
                continue
            gid = rec["id"]
            if gid in seen:
                continue
            guide = _load_guide(root, gid)
            if not guide:
                continue
            status = str(guide.get("status") or "active")
            if status in ("stale", "tombstone"):
                continue
            if rec["scope"] != str(guide.get("scope") or rec["scope"]):
                continue
            if not _match_hit(tokens, rec, guide):
                continue
            if not probe_ok(guide.get("requires") or []):
                continue
            seen.add(gid)
            hits.append(
                {
                    "id": gid,
                    "scope": rec["scope"],
                    "title": rec["title"],
                    "when": rec["when"],
                    "line": rec["line"],
                    "kind": guide.get("kind") or "procedure",
                }
            )
            if len(hits) >= SEARCH_HIT_CAP:
                return hits
    return hits


def get(id: str, *, count_turn: bool = True) -> dict:
    gid = (id or "").strip()
    if not ID_RE.match(gid):
        return {"ok": False, "error": "invalid_id"}
    global _turn_gets
    if count_turn and _turn_gets >= TURN_GET_CAP:
        return {"ok": False, "error": "turn_get_cap"}
    for root in (project_root(), home_root()):
        guide = _load_guide(root, gid)
        if not guide:
            continue
        if str(guide.get("status") or "") == "tombstone":
            return {"ok": False, "error": "forgotten", "id": gid}
        if count_turn:
            _turn_gets += 1
        return {
            "ok": True,
            "id": gid,
            "meta": {k: v for k, v in guide.items() if k not in ("body", "path", "root")},
            "body": guide.get("body") or "",
            "path": guide.get("path"),
            "scope": guide.get("scope"),
        }
    return {"ok": False, "error": "not_found", "id": gid}


def _evidence_banned(evidence: str, source: str) -> bool:
    if source in ("user", "user:explicit"):
        return False
    ev = (evidence or "").strip()
    low = ev.lower()
    return (
        low.startswith("web:")
        or low.startswith("fetch:")
        or low.startswith("http://")
        or low.startswith("https://")
    )


def _duplicate_when(scope_root: Path, scope: str, when: str, gid: str) -> Optional[str]:
    want = _when_set(when)
    if not want:
        return None
    for rec in (_parse_index_line(ln) for ln in _catalog_lines(index_path(scope_root))):
        if rec is None or rec["id"] == gid:
            continue
        if rec["scope"] != scope:
            continue
        if _when_set(rec["when"]) == want:
            return rec["id"]
    return None


def _upsert_index(scope_root: Path, scope: str, gid: str, title: str, when: str) -> None:
    path = index_path(scope_root)
    with file_lock(path):  # read-modify-write: no lost update between processes
        _upsert_index_locked(path, scope, gid, title, when)


def _upsert_index_locked(path: Path, scope: str, gid: str, title: str, when: str) -> None:
    line = f"- [{scope}] {gid} — {title}. When: {when}"
    lines = _catalog_lines(path)
    if not lines:
        lines = [INDEX_HEADER, ""]
    replaced = False
    out: list[str] = []
    for ln in lines:
        parsed = _parse_index_line(ln)
        if parsed and parsed["id"] == gid:
            out.append(line)
            replaced = True
        else:
            out.append(ln)
    if not replaced:
        out.append(line)
    atomic_write_text(path, "\n".join(out).rstrip() + "\n")


def _remove_index_id(scope_root: Path, gid: str) -> None:
    path = index_path(scope_root)
    with file_lock(path):
        lines = _catalog_lines(path)
        if not lines:
            return
        out = []
        for ln in lines:
            parsed = _parse_index_line(ln)
            if parsed and parsed["id"] == gid:
                continue
            out.append(ln)
        atomic_write_text(path, "\n".join(out).rstrip() + "\n")


def _guide_count(scope_root: Path) -> int:
    d = Path(scope_root) / "guides"
    if not d.is_dir():
        return 0
    return sum(1 for p in d.glob("*.md") if p.is_file())


def _reject(scope_root: Optional[Path], error: str, extra: Optional[dict] = None) -> dict:
    rec = {"ok": False, "error": error}
    if extra:
        rec.update(extra)
    if scope_root is not None:
        payload = {"op": error if error == "secret_reject" else "reject", "error": error}
        if extra:
            payload.update({k: extra[k] for k in extra if k != "body"})
        try:
            _append_ledger(scope_root, payload)
        except OSError:
            pass
    return rec


def put(
    meta: dict,
    body: str,
    *,
    evidence: str = "",
    source: str = "orchestrator",
) -> dict:
    global _session_puts
    if not enabled():
        return {"ok": False, "error": "disabled"}
    if source in ("subagent", "agent"):
        return {"ok": False, "error": "orchestrator_only"}

    gid = str(meta.get("id") or "").strip()
    kind = str(meta.get("kind") or "").strip()
    scope = str(meta.get("scope") or "").strip()
    title = str(meta.get("title") or "").strip()
    when = str(meta.get("when") or "").strip()
    created = str(meta.get("created") or _today())
    evidence = str(evidence or meta.get("evidence") or "").strip()
    body = body if body is not None else str(meta.get("body") or "")

    required = {
        "id": gid,
        "kind": kind,
        "scope": scope,
        "title": title,
        "when": when,
        "created": created,
        "evidence": evidence,
    }
    if any(not v for v in required.values()):
        return _reject(None, "missing_fields")
    if scope not in SCOPES:
        return _reject(None, "invalid_scope")
    if kind not in KINDS:
        return _reject(None, "invalid_kind")
    if not ID_RE.match(gid):
        return _reject(None, "invalid_id")

    scope_root = project_root() if scope == "project" else home_root()
    try:
        scope_root.resolve().relative_to(
            (get_project_root() / ".g023" / "learn").resolve()
            if scope == "project"
            else home_root().resolve()
        )
    except ValueError:
        return _reject(None, "path_escape")

    if source not in ("user", "user:explicit") and _session_puts >= SESSION_PUT_CAP:
        return _reject(scope_root, "session_write_cap", {"id": gid})

    body_text = body.replace("\r\n", "\n")
    body_lines = body_text.splitlines()
    if len(body_lines) > BODY_LINE_CAP or len(body_text) > BODY_CHAR_CAP:
        return _reject(scope_root, "body_too_large", {"id": gid})

    blob = f"{title}\n{body_text}\n{evidence}"
    if secret_hit(blob):
        return _reject(scope_root, "secret_reject", {"id": gid})
    if _auth_hit(f"{title}\n{body_text}"):
        return _reject(scope_root, "auth_launder", {"id": gid})
    if _evidence_banned(evidence, source):
        return _reject(scope_root, "untrusted_evidence", {"id": gid})

    existing = _load_guide(scope_root, gid)
    if existing and str(existing.get("status") or "") == "tombstone":
        return _reject(scope_root, "forgotten", {"id": gid})

    dup = _duplicate_when(scope_root, scope, when, gid)
    if dup:
        return _reject(scope_root, "duplicate_when", {"id": gid, "existing_id": dup})

    replacing = existing is not None and str(existing.get("status") or "") != "tombstone"
    idx_lines = _catalog_lines(index_path(scope_root))
    already_indexed = any(
        (p := _parse_index_line(ln)) and p["id"] == gid for ln in idx_lines
    )
    if not already_indexed and _catalog_count(idx_lines) >= INDEX_CAP:
        return _reject(scope_root, "index_full", {"id": gid})
    if not replacing and _guide_count(scope_root) >= GUIDE_FILE_CAP:
        return _reject(scope_root, "guide_cap", {"id": gid})

    helpful = int(existing.get("helpful") or 0) if existing else 0
    harmful = int(existing.get("harmful") or 0) if existing else 0
    stored = {
        "id": gid,
        "kind": kind,
        "scope": scope,
        "title": title,
        "when": when,
        "not_when": str(meta.get("not_when") or ""),
        "requires": meta.get("requires") or [],
        "created": str(existing.get("created") or created) if existing else created,
        "updated": _today(),
        "evidence": evidence,
        "helpful": helpful,
        "harmful": harmful,
        "status": "active",
    }
    text = render_guide(stored, body_text)
    dest = guide_path(scope_root, gid)
    atomic_write_text(dest, text)
    _upsert_index(scope_root, scope, gid, title, when)
    _append_ledger(
        scope_root,
        {"op": "put", "id": gid, "scope": scope, "project_root": str(get_project_root())},
    )
    if source not in ("user", "user:explicit"):
        _session_puts += 1
    return {"ok": True, "id": gid, "path": str(dest), "scope": scope}


def forget(id: str) -> dict:
    gid = (id or "").strip()
    if not ID_RE.match(gid):
        return {"ok": False, "error": "invalid_id"}
    for root in (project_root(), home_root()):
        guide = _load_guide(root, gid)
        if not guide:
            continue
        meta = {k: v for k, v in guide.items() if k not in ("body", "path", "root")}
        meta["status"] = "tombstone"
        dest = guide_path(root, gid)
        with file_lock(dest):
            atomic_write_text(dest, render_guide(meta, "# forgotten\n"))
        _remove_index_id(root, gid)
        _append_ledger(root, {"op": "forget", "id": gid, "scope": meta.get("scope")})
        return {"ok": True, "id": gid, "status": "tombstone"}
    return {"ok": False, "error": "not_found", "id": gid}


def outcome(id: str, kind: str) -> dict:
    gid = (id or "").strip()
    flag = (kind or "").strip().lower()
    if flag not in ("helped", "harmed"):
        return {"ok": False, "error": "invalid_outcome"}
    if not ID_RE.match(gid):
        return {"ok": False, "error": "invalid_id"}
    for root in (project_root(), home_root()):
        if not _load_guide(root, gid):
            continue
        # Counters are read-modify-write: re-read under the guide's lock so two
        # concurrent votes (two REPLs, or parallel tool calls) both count.
        with file_lock(guide_path(root, gid)):
            guide = _load_guide(root, gid)
            if not guide:
                continue
            return _apply_outcome(root, gid, flag, guide)
    return {"ok": False, "error": "not_found", "id": gid}


def _apply_outcome(root: Path, gid: str, flag: str, guide: dict) -> dict:
    if str(guide.get("status") or "") == "tombstone":
        return {"ok": False, "error": "forgotten", "id": gid}
    helpful = int(guide.get("helpful") or 0)
    harmful = int(guide.get("harmful") or 0)
    if flag == "helped":
        helpful += 1
    else:
        harmful += 1
    status = str(guide.get("status") or "active")
    if harmful >= 2 and harmful > helpful:
        status = "stale"
    meta = {k: v for k, v in guide.items() if k not in ("body", "path", "root")}
    meta["helpful"] = helpful
    meta["harmful"] = harmful
    meta["status"] = status
    atomic_write_text(guide_path(root, gid), render_guide(meta, guide.get("body") or ""))
    if status == "stale":
        _remove_index_id(root, gid)
    _append_ledger(root, {"op": "outcome", "id": gid, "kind": flag, "status": status})
    return {
        "ok": True,
        "id": gid,
        "helpful": helpful,
        "harmful": harmful,
        "status": status,
    }


def catalog_nudge(tool_name: str, error_text: str = "") -> Optional[str]:
    name = (tool_name or "").strip()
    if not name or not enabled():
        return None
    needle = name.lower()
    for root in (project_root(), home_root()):
        for rec in (_parse_index_line(ln) for ln in _catalog_lines(index_path(root))):
            if rec is None:
                continue
            tags = _when_set(rec["when"])
            if needle in tags or needle in rec["id"] or needle in rec["when"].lower():
                return (
                    f'{NUDGE_PREFIX}"{name}" has catalog hits. '
                    "Call LearnGet only if the error matches."
                )
    return None


def status() -> dict:
    home = home_root()
    proj = project_root()
    return {
        "enabled": enabled(),
        "home": str(home),
        "home_exists": home.is_dir(),
        "home_index": _catalog_count(_catalog_lines(index_path(home))),
        "project": str(proj),
        "project_exists": proj.is_dir(),
        "project_index": _catalog_count(_catalog_lines(index_path(proj))),
        "session_puts_remaining": max(0, SESSION_PUT_CAP - _session_puts),
        "turn_gets_remaining": max(0, TURN_GET_CAP - _turn_gets),
    }

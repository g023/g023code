"""
SQLite-backed local cache for file summaries, vision results, and session memory.
Zero-cost re-reads and repeated vision queries.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Optional

from .config import get_cache_db
from .cost_policy import VAULT_LEADIN, VAULT_MIN_CHARS


def _sha256(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def _vault_excerpt(text: str, head: int = 400, tail: int = 800) -> str:
    """Head + a longer tail so pytest/log failures at the end stay visible."""
    if len(text) <= head + tail + 80:
        return text
    return (
        text[:head].rstrip()
        + "\n\n… [middle elided; a re-run returns the same body, so narrow the command, scope or range to see this part] …\n\n"
        + text[-tail:].lstrip()
    )


def _json_header(line: str) -> dict:
    """The one-line JSON header tool_codec puts on Bash results ({} if none)."""
    if not line.startswith("{"):
        return {}
    try:
        data = json.loads(line)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def is_vault_pointer(text: str) -> bool:
    return (text or "").startswith(VAULT_LEADIN)


class Cache:
    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = Path(db_path or get_cache_db())
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS file_cache (
                    file_hash   TEXT PRIMARY KEY,
                    file_path   TEXT NOT NULL,
                    summary     TEXT NOT NULL,
                    metadata    TEXT,
                    raw_content TEXT,
                    created_at  REAL NOT NULL,
                    accessed_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vision_cache (
                    image_hash    TEXT NOT NULL,
                    question_hash TEXT NOT NULL,
                    backend       TEXT NOT NULL,
                    response      TEXT NOT NULL,
                    created_at    REAL NOT NULL,
                    PRIMARY KEY (image_hash, question_hash, backend)
                );


                CREATE TABLE IF NOT EXISTS tool_vault (
                    body_hash   TEXT PRIMARY KEY,
                    body        TEXT NOT NULL,
                    chars       INTEGER NOT NULL,
                    created_at  REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    hit_count   INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS session_facts (
                    key         TEXT PRIMARY KEY,
                    value       TEXT NOT NULL,
                    created_at  REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS prefix_stats (
                    day         TEXT NOT NULL,
                    model       TEXT NOT NULL,
                    hit_tokens  INTEGER NOT NULL DEFAULT 0,
                    miss_tokens INTEGER NOT NULL DEFAULT 0,
                    calls       INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (day, model)
                );

                CREATE INDEX IF NOT EXISTS idx_file_path ON file_cache(file_path);
                CREATE INDEX IF NOT EXISTS idx_file_accessed ON file_cache(accessed_at);
                """
            )
            # 1.5.0: file bodies were stored but never read. Drop any left over
            # (a no-op once done; the freed pages are reused, so no VACUUM is needed).
            conn.execute("UPDATE file_cache SET raw_content = NULL WHERE raw_content IS NOT NULL")

    # ------------------------------------------------------------------
    # File cache
    # ------------------------------------------------------------------

    def get_file_summary(self, file_path: str | Path, content_hash: Optional[str] = None) -> Optional[dict]:
        path = str(Path(file_path).resolve())
        with self._conn() as conn:
            if content_hash:
                row = conn.execute(
                    "SELECT * FROM file_cache WHERE file_hash = ?", (content_hash,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM file_cache WHERE file_path = ? ORDER BY accessed_at DESC LIMIT 1",
                    (path,),
                ).fetchone()
            if not row:
                return None
            # touch
            conn.execute(
                "UPDATE file_cache SET accessed_at = ? WHERE file_hash = ?",
                (time.time(), row["file_hash"]),
            )
            return {
                "file_hash": row["file_hash"],
                "file_path": row["file_path"],
                "summary": row["summary"],
                "metadata": json.loads(row["metadata"] or "{}"),
                "raw_content": row["raw_content"],
            }

    def put_file_summary(
        self,
        file_path: str | Path,
        summary: str,
        metadata: Optional[dict] = None,
        raw_content: Optional[str] = None,
        content_hash: Optional[str] = None,
    ) -> str:
        path = str(Path(file_path).resolve())
        if content_hash is None:
            content_hash = _sha256(raw_content or summary)
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO file_cache
                (file_hash, file_path, summary, metadata, raw_content, created_at, accessed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_hash,
                    path,
                    summary,
                    json.dumps(metadata or {}, ensure_ascii=False),
                    # Bodies are never served from here (reads re-read the file and
                    # hash it), so the column stays NULL: it only grew the DB.
                    None,
                    now,
                    now,
                ),
            )
        return content_hash

    def invalidate_file(self, file_path: str | Path) -> int:
        """Drop every cached summary for one path. Returns the rows removed."""
        path = str(Path(file_path).resolve())
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM file_cache WHERE file_path = ?", (path,))
            return cur.rowcount or 0

    # ------------------------------------------------------------------
    # Vision cache
    # ------------------------------------------------------------------

    def get_vision(self, image_hash: str, question: str, backend: str) -> Optional[str]:
        q_hash = _sha256(question)
        with self._conn() as conn:
            row = conn.execute(
                "SELECT response FROM vision_cache WHERE image_hash=? AND question_hash=? AND backend=?",
                (image_hash, q_hash, backend),
            ).fetchone()
            return row["response"] if row else None

    def put_vision(self, image_hash: str, question: str, backend: str, response: str):
        q_hash = _sha256(question)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO vision_cache
                (image_hash, question_hash, backend, response, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (image_hash, q_hash, backend, response, time.time()),
            )

    # ------------------------------------------------------------------
    # Prefix-cache hit rate, bucketed by day
    # ------------------------------------------------------------------
    #
    # The hit rate is a continuous number the API reports on every call, and it
    # moves as soon as anything perturbs the prefix — a changed prompt, a
    # different tool list, or a server-side change in how items serialise. A
    # single session's number says little; the same number against the last two
    # weeks says whether something moved. Storing it is the only way to compare.

    def record_prefix_stats(self, model: str, hit_tokens: int, miss_tokens: int, calls: int = 1) -> None:
        if hit_tokens <= 0 and miss_tokens <= 0:
            return
        day = time.strftime("%Y-%m-%d", time.localtime())
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO prefix_stats (day, model, hit_tokens, miss_tokens, calls)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(day, model) DO UPDATE SET
                    hit_tokens  = hit_tokens  + excluded.hit_tokens,
                    miss_tokens = miss_tokens + excluded.miss_tokens,
                    calls       = calls       + excluded.calls
                """,
                (day, model, int(hit_tokens), int(miss_tokens), int(calls)),
            )

    def prefix_history(self, days: int = 14) -> list[dict]:
        """Per-day hit/miss totals, oldest first, with the hit rate computed."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT day, model, hit_tokens, miss_tokens, calls FROM prefix_stats "
                "ORDER BY day DESC LIMIT ?",
                (days * 4,),
            ).fetchall()
        out = []
        for r in rows:
            total = (r["hit_tokens"] or 0) + (r["miss_tokens"] or 0)
            out.append(
                {
                    "day": r["day"],
                    "model": r["model"],
                    "hit_tokens": r["hit_tokens"],
                    "miss_tokens": r["miss_tokens"],
                    "calls": r["calls"],
                    "hit_rate": (r["hit_tokens"] / total) if total else 0.0,
                }
            )
        out.sort(key=lambda d: d["day"])
        return out[-days:]

    # ------------------------------------------------------------------
    # Tool vault (content-addressed bulky results)
    # ------------------------------------------------------------------
    #
    # Large Bash / FetchUrl / ListDir bodies never enter the orchestrator
    # history as raw text. History holds a short pointer + excerpt; the full
    # body is keyed by sha256 so a repeat of the same bytes is free. This is
    # append-only: the pointer is what is written the first time, so there is
    # no later middle rewrite of a cached prefix (DeepSeek Example 2).

    def put_tool_body(self, body: str) -> str:
        """Store ``body`` by sha256. Same bytes are a no-op. Returns the hash."""
        body_hash = _sha256(body)
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO tool_vault (body_hash, body, chars, created_at, accessed_at, hit_count)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(body_hash) DO UPDATE SET accessed_at = excluded.accessed_at
                """,
                (body_hash, body, len(body), now, now),
            )
        return body_hash

    def get_tool_body(self, body_hash: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT body FROM tool_vault WHERE body_hash = ?",
                (body_hash,),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE tool_vault SET accessed_at = ?, hit_count = hit_count + 1 "
                "WHERE body_hash = ?",
                (time.time(), body_hash),
            )
            return row["body"]

    def offload_tool_output(
        self,
        text: str,
        *,
        min_chars: int = VAULT_MIN_CHARS,
        intent: str = "",
        extra: Iterable[int] = (),
        salient: bool = True,
    ) -> str:
        """Replace a bulky tool result with a pointer + excerpt.

        Short strings and existing pointers pass through unchanged so a
        second pass cannot rewrite history bytes. With ``salient`` the excerpt
        holds the lines that match the step's ``intent`` (salience.py) and the
        full body is a readable file under .g023/vault/; without it the old
        blind head + tail excerpt is used.
        """
        if not text or len(text) < min_chars or is_vault_pointer(text):
            return text
        body_hash = self.put_tool_body(text)
        if not salient:
            excerpt = _vault_excerpt(text)
            return f"{VAULT_LEADIN}sha256:{body_hash} chars={len(text)}]\n{excerpt}"
        from . import salience

        header, full, rel, lines = "", None, "", 0
        first, _, rest = text.partition("\n")
        meta = _json_header(first)
        if meta.get("full_output"):
            # Bash dropped text when capping: select from the raw output file.
            rel = str(meta["full_output"])
            full = salience.read_vault_file(rel)
            lines = int(meta.get("full_lines") or 0)
            header = first
        if full is None:
            rel, lines = salience.write_vault_file(text)
            full = text
        excerpt = salience.build_excerpt(full, intent, extra=extra)
        if header and not excerpt.startswith(header):
            excerpt = header + "\n" + excerpt
        note = salience.readback_note(rel, lines) if rel else ""
        return f"{VAULT_LEADIN}sha256:{body_hash} chars={len(full)}]\n{excerpt}" + (f"\n{note}" if note else "")

    # ------------------------------------------------------------------
    # Session facts (lightweight memory)
    # ------------------------------------------------------------------

    def set_fact(self, key: str, value: Any):
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO session_facts (key, value, created_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )

    def get_fact(self, key: str, default: Any = None) -> Any:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM session_facts WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return default
            try:
                return json.loads(row["value"])
            except Exception:
                return row["value"]

    def stats(self) -> dict[str, int]:
        """Row counts per cache, for /cache."""
        tables = {
            "files": "file_cache",
            "vision": "vision_cache",
            "vault": "tool_vault",
        }
        out: dict[str, int] = {}
        with self._conn() as conn:
            for label, table in tables.items():
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                out[label] = int(row["n"]) if row else 0
        return out

    def clear_all(self):
        """Purge re-derivable caches (summaries, vision answers, vault bodies).

        ``session_facts`` is not a cache: it holds the live working set (goal,
        todos, verify state). Purging caches used to reset the session too.
        The prefix-hit history is kept as the drift baseline.
        """
        with self._conn() as conn:
            conn.execute("DELETE FROM file_cache")
            conn.execute("DELETE FROM vision_cache")
            conn.execute("DELETE FROM tool_vault")


# Singleton
_cache: Optional[Cache] = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache

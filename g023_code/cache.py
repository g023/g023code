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
from typing import Any, Optional

from .config import get_cache_db


def _sha256(text: str | bytes) -> str:
    if isinstance(text, str):
        text = text.encode("utf-8")
    return hashlib.sha256(text).hexdigest()


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

                CREATE TABLE IF NOT EXISTS web_cache (
                    url_hash    TEXT PRIMARY KEY,
                    url         TEXT NOT NULL,
                    final_url   TEXT,
                    status      INTEGER,
                    headers     TEXT,
                    body        TEXT NOT NULL,
                    engine      TEXT,
                    profile     TEXT,
                    fetched_at  REAL NOT NULL,
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
                CREATE INDEX IF NOT EXISTS idx_web_url ON web_cache(url);
                CREATE INDEX IF NOT EXISTS idx_web_fetched ON web_cache(fetched_at);
                """
            )

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
                    raw_content,
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

    def purge_old_files(self, max_age_days: int = 7):
        cutoff = time.time() - (max_age_days * 86400)
        with self._conn() as conn:
            conn.execute("DELETE FROM file_cache WHERE accessed_at < ?", (cutoff,))

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
    # Web cache (URL fetches)
    # ------------------------------------------------------------------

    @staticmethod
    def web_key(url: str) -> str:
        return _sha256(url.strip())

    def get_web(self, url: str, touch: bool = True) -> Optional[dict]:
        """Return the cached response for a URL, regardless of age.

        Age is deliberately the caller's business — freshness policy belongs
        with the user's choice, not with storage.
        """
        key = self.web_key(url)
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM web_cache WHERE url_hash = ?", (key,)).fetchone()
            if not row:
                return None
            if touch:
                conn.execute(
                    "UPDATE web_cache SET accessed_at = ?, hit_count = hit_count + 1 WHERE url_hash = ?",
                    (time.time(), key),
                )
            return {
                "url": row["url"],
                "final_url": row["final_url"],
                "status": row["status"],
                "headers": json.loads(row["headers"] or "{}"),
                "body": row["body"],
                "engine": row["engine"],
                "profile": row["profile"],
                "fetched_at": row["fetched_at"],
                "age_seconds": time.time() - row["fetched_at"],
                "hit_count": row["hit_count"],
            }

    def touch_web(self, url: str):
        """Record that a cached copy was actually served."""
        with self._conn() as conn:
            conn.execute(
                "UPDATE web_cache SET accessed_at = ?, hit_count = hit_count + 1 WHERE url_hash = ?",
                (time.time(), self.web_key(url)),
            )

    def put_web(
        self,
        url: str,
        body: str,
        status: int,
        headers: Optional[dict] = None,
        final_url: Optional[str] = None,
        engine: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> str:
        key = self.web_key(url)
        now = time.time()
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO web_cache
                (url_hash, url, final_url, status, headers, body, engine, profile,
                 fetched_at, accessed_at, hit_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        COALESCE((SELECT hit_count FROM web_cache WHERE url_hash = ?), 0))
                """,
                (
                    key,
                    url,
                    final_url or url,
                    status,
                    json.dumps(headers or {}, ensure_ascii=False),
                    body,
                    engine,
                    profile,
                    now,
                    now,
                    key,
                ),
            )
        return key

    def list_web(self, limit: int = 30) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT url, status, engine, fetched_at, hit_count, LENGTH(body) AS size "
                "FROM web_cache ORDER BY fetched_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def clear_web(self, url: Optional[str] = None) -> int:
        with self._conn() as conn:
            if url:
                cur = conn.execute("DELETE FROM web_cache WHERE url_hash = ?", (self.web_key(url),))
            else:
                cur = conn.execute("DELETE FROM web_cache")
            return cur.rowcount

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
        tables = {"files": "file_cache", "vision": "vision_cache", "web": "web_cache"}
        out: dict[str, int] = {}
        with self._conn() as conn:
            for label, table in tables.items():
                row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                out[label] = int(row["n"]) if row else 0
        return out

    def clear_all(self):
        with self._conn() as conn:
            conn.execute("DELETE FROM file_cache")
            conn.execute("DELETE FROM vision_cache")
            conn.execute("DELETE FROM web_cache")
            conn.execute("DELETE FROM session_facts")


# Singleton
_cache: Optional[Cache] = None


def get_cache() -> Cache:
    global _cache
    if _cache is None:
        _cache = Cache()
    return _cache

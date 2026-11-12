"""
Machine-wide web layer: DeepSeek Search + Fetch APIs behind one global cache.

Every web request goes to DeepSeek (``POST {base}/v1/tools/search`` and
``/v1/tools/fetch``). The fee is per *successful request* ($0.75 / 1,000 each).
It does not depend on tokens, on ``count``, or on ``need_content``
(api-docs.deepseek.com/guides/search). A single fee is larger than the token
bill of a whole agent step, so this module pays each request at most once per
machine:

* **Global store.** ``<install>/global_cache/web.db`` (next to K.dat, not the
  launch folder) is shared by every project and every running instance, under
  SQLite WAL. Bodies are zlib-compressed.
* **Canonical keys.** Queries are keyed order-insensitively (case, spacing,
  term order and duplicates do not matter; ``OR`` queries keep their order).
  URLs drop fragments, tracking parameters and default ports.
* **Query-aware freshness.** "latest / news / price / version ..." searches
  live 6 h, everything else 7 days. Version-pinned pages (commit blobs, arXiv
  versions, versioned docs, PDFs) live 30 days, ordinary pages 1 day, and
  release/news pages 1 hour.
* **Search-primed pages.** Every network search asks for ``need_content`` and
  the default network ``count`` — same price — and files each hit's Markdown
  under its URL. Verified byte-identical to the Fetch API on a 44k-char page
  during development. A later fetch of any hit is free
  and instant.
* **Service cut-off.** Search and Fetch return some sources cut at exactly
  20,480 UTF-8 bytes, mid-sentence, with no flag (HTML extraction; the same
  site's plain Markdown is not cut). A body of exactly that size is treated as
  cut: one fetch of its plain-text twin (docs ``page.md``, GitHub ``blob`` ->
  ``raw``) replaces it when the twin is the same document and longer, and a
  page that stays cut says so in the rendering instead of passing as whole.
* **Negative cache.** Fetch refusals (403 policy, 404 no content) are kept
  for an hour. They are not billed, but they still cost a round trip and a model step.
* **Ledger.** Network vs cache serves per day, so ``/cache`` can show the
  money the cache saved across all instances.

Rendering is part of the contract. A search result shows the model-facing
``summary`` (DeepSeek's long abstract) and flags hits whose page is cached.
A page renders whole when it is small. When it is large, it renders as an
outline plus either a char window (``offset``) or the BM25-best sections for a
``focus``, so the history gets the section the answer needs instead of a 10k-token page.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import threading
import time
import unicodedata
import zlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import get_global_cache_dir

SEARCH_PATH = "/v1/tools/search"
FETCH_PATH = "/v1/tools/fetch"

QUERY_MAX_CHARS = 128          # API limit (Unicode characters)
URL_MAX_CHARS = 8192           # API limit
SHOW_DEFAULT = 5               # results rendered when the model does not say
SHOW_MAX = 20
NET_COUNT = 10                 # results requested per network search (price is per request)
NET_COUNT_MAX = 50             # API limit
SUMMARY_MAX_CHARS = 700        # per-result abstract cap in the rendering

HOUR = 3600
DAY = 86400
SEARCH_TTL_VOLATILE = 6 * HOUR
SEARCH_TTL_STABLE = 7 * DAY
SEARCH_TTL_EMPTY = 1 * HOUR    # empty result lists are not billed; keep briefly
PAGE_TTL_PINNED = 30 * DAY
PAGE_TTL_DEFAULT = 1 * DAY
PAGE_TTL_VOLATILE = 1 * HOUR
NEGATIVE_TTL = 1 * HOUR

PAGE_WHOLE_MAX_CHARS = 8_000   # pages this small render whole
PAGE_WINDOW_DEFAULT = 8_000    # default char window for larger pages
PAGE_WINDOW_MAX = 40_000
OUTLINE_MAX_LINES = 40
FOCUS_TOP = 3

_RETRY = {408, 429, 500, 502, 503, 504}

# Search need_content and /v1/tools/fetch return some sources cut at exactly
# this many UTF-8 bytes (observed 2026-11: docs HTML, GitHub blob pages); others
# come back whole at 58 KB+. A natural page of exactly this size is ~1 in 20k.
SOURCE_CAP_BYTES = 20_480
CUT_NOTE = ("the web service cut this page at 20,480 bytes; text after the last line here is missing "
            "from every cached copy. If the rest matters, look for a .md/raw version or another source.")


class WebError(RuntimeError):
    """A Search/Fetch request that produced no usable result."""

    def __init__(self, status: int, message: str, *, cached: bool = False):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status
        self.message = message
        self.cached = cached


# ---------------------------------------------------------------------------
# Canonical keys
# ---------------------------------------------------------------------------

_QTOKEN = re.compile(r'-?"[^"]*"|\S+')
_TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|ref_src|igshid)$", re.I)


def clean_query(query: str) -> str:
    """NFKC, trimmed, single-spaced: what is actually sent to the API."""
    q = unicodedata.normalize("NFKC", str(query or ""))
    return " ".join(q.split())


def canonical_query(query: str) -> str:
    """Order-insensitive cache key for a search query.

    Terms are lower-cased, de-duplicated and sorted, with trailing ``?!.,``
    dropped. Operators (``site:``, ``-term``, ``"phrase"``) are atomic tokens,
    so their meaning survives. A query that uses ``OR`` keeps its term order,
    because there order decides what the operator binds.
    """
    q = clean_query(query)
    raw = _QTOKEN.findall(q)
    tokens = []
    for t in raw:
        if t == "OR":
            tokens.append(t)
            continue
        t = t.lower()
        if not t.endswith('"'):
            t = t.rstrip("?!.,;")
        if t:
            tokens.append(t)
    if "OR" in tokens:
        return " ".join(tokens)
    return " ".join(sorted(set(tokens)))


_OPERATOR = re.compile(r'^(-|"|[a-z]+:)')
_QSTOP = frozenset(
    "a an and are as at be by can do does for from how i in is it of on or the to what when where which "
    "who why will with vs versus doc docs documentation official reference manual".split()
)
_QSYN = {
    "py": "python", "js": "javascript", "ts": "typescript", "postgresql": "postgres", "pg": "postgres",
    "k8s": "kubernetes", "golang": "go", "rs": "rust", "err": "error", "errors": "error",
}
_QSPLIT = re.compile(r"[./_\-:]+")
# Reuse economics: a wrong reuse costs ~1.5k junk tokens + one step (~$0.0003 off-peak),
# a right one saves the $0.00075 fee, so reuse pays when it helps > ~30% of the time.
SIMILAR_MIN_JACCARD = 0.75       # near-duplicate queries reuse a fresh cached search
SIMILAR_SUBSET_JACCARD = 0.6     # ...or this, when one query nearly contains the other
SIMILAR_SUBSET_OVERLAP = 0.85    # |A&B| / min(|A|,|B|)
SIMILAR_SCAN = 400               # recent searches compared per lookup


def _stem(t: str) -> str:
    """Tiny suffix stripper: enough to equate cancel/cancelled/cancels, task/tasks."""
    for suf in ("ations", "ation", "ingly", "ings", "ing", "edly", "ied", "ies", "ed", "es", "s"):
        if len(t) > len(suf) + 3 and t.endswith(suf):
            t = t[: -len(suf)]
            break
    if len(t) > 4 and t[-1] == t[-2] and t[-1] not in "aeiou":  # cancell -> cancel
        t = t[:-1]
    if len(t) > 4 and t.endswith("e"):  # lifetime/lifetimes, cache/caching
        t = t[:-1]
    return t


def query_terms(query: str) -> Tuple[frozenset, frozenset, frozenset]:
    """(plain stemmed terms, scope operators, numeric tokens) of a query.

    Scope operators (site:, -term, lang:, after:, OR) change *which* pages may
    match, so they must agree exactly. A quoted phrase only tightens precision,
    so its words count as plain terms. Numeric tokens (versions, years, ids)
    are kept apart: they veto a match only when both queries carry them.
    """
    plain, ops, nums = set(), set(), set()
    for t in _QTOKEN.findall(canonical_query(query)):
        if t.startswith('"') and t.endswith('"') and len(t) > 1:
            words = t.strip('"').split()
        elif t == "OR" or _OPERATOR.match(t):
            ops.add(t)
            continue
        else:
            words = [t]
        for w in words:
            if any(ch.isdigit() for ch in w):
                nums.add(w)
                continue
            for part in [w] if w.isalnum() else _QSPLIT.split(w):
                part = _QSYN.get(part, part)
                if part and part not in _QSTOP:
                    plain.add(_stem(part))
    return frozenset(plain), frozenset(ops), frozenset(nums)


def query_similarity(a: str, b: str) -> float:
    """Jaccard of stemmed terms, or 0 when scope operators differ or both queries
    name different versions/years. A near-subset (one query refines the other)
    scores as a match."""
    pa, oa, na = query_terms(a)
    pb, ob, nb = query_terms(b)
    if oa != ob or "OR" in oa or not pa or not pb:
        return 0.0
    if na and nb and na != nb:
        return 0.0
    pa, pb = pa | na, pb | nb  # a lone version on one side is just one more term
    inter = len(pa & pb)
    jac = inter / len(pa | pb)
    if SIMILAR_SUBSET_JACCARD <= jac < SIMILAR_MIN_JACCARD and \
            inter / min(len(pa), len(pb)) >= SIMILAR_SUBSET_OVERLAP:
        return SIMILAR_MIN_JACCARD  # a near-subset counts as a match
    return jac


def canonical_url(url: str) -> str:
    """Cache key for a page: scheme+host lower-cased, no fragment, no tracking params."""
    u = str(url or "").strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.I):
        u = "https://" + u.lstrip("/")
    parts = urlsplit(u)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc = f"{host}:{port}"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------

_VOLATILE_Q = re.compile(
    r"\b(latest|newest|recent|recently|today|tonight|yesterday|tomorrow|now|current|currently|"
    r"breaking|news|headlines?|price|prices|pricing|stock|stocks|weather|forecast|scores?|live|"
    r"releases?|released|version|versions|changelog|update|updates|updated|status|outage|"
    r"trending|this\s+(?:week|month|year)|upcoming|deadline|schedule)\b",
    re.I,
)
_PINNED_URL = re.compile(
    r"(/blob/[0-9a-f]{7,40}/|/tree/[0-9a-f]{7,40}/|/commit/[0-9a-f]{7,40}|arxiv\.org/(abs|pdf)/[\w.]+v\d+|"
    r"doi\.org/|\.pdf(\?|$)|[?&]oldid=\d+|/v?\d+\.\d+(\.\d+)?/|web\.archive\.org/web/\d+)",
    re.I,
)
_VOLATILE_URL = re.compile(
    r"(/releases/?($|latest)|/changelog|/news|/latest|/status|/feed|/rss|"
    r"pypi\.org/project/[^/]+/?$|npmjs\.com/package/[^/]+/?$|/tags/?$)",
    re.I,
)


def ttl_for_query(query: str, *, now: Optional[float] = None) -> int:
    """Seconds a cached search stays fresh. Time-sensitive wording -> 6 h, else 7 days."""
    q = clean_query(query)
    if _VOLATILE_Q.search(q) or re.search(r"\bafter:", q, re.I):
        return SEARCH_TTL_VOLATILE
    year = time.localtime(now if now is not None else time.time()).tm_year
    for y in re.findall(r"\b(20\d\d)\b", q):
        if int(y) >= year:
            return SEARCH_TTL_VOLATILE
    return SEARCH_TTL_STABLE


def ttl_for_url(url: str) -> int:
    """Seconds a cached page stays fresh: pinned 30 d, release/news 1 h, else 1 d."""
    u = canonical_url(url)
    if _VOLATILE_URL.search(u):
        return PAGE_TTL_VOLATILE
    if _PINNED_URL.search(u):
        return PAGE_TTL_PINNED
    return PAGE_TTL_DEFAULT


# ---------------------------------------------------------------------------
# Global store
# ---------------------------------------------------------------------------


def _z(text: str) -> bytes:
    return zlib.compress((text or "").encode("utf-8"), 6)


def _unz(blob: Any) -> str:
    if blob is None:
        return ""
    if isinstance(blob, str):
        return blob
    try:
        return zlib.decompress(blob).decode("utf-8")
    except zlib.error:
        return bytes(blob).decode("utf-8", "replace")


def _day() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


class WebStore:
    """SQLite store shared by every g023 instance on the machine (WAL mode)."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else get_global_cache_dir() / "web.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(
                """
                CREATE TABLE IF NOT EXISTS search (
                    key         TEXT PRIMARY KEY,   -- canonical_query
                    query       TEXT NOT NULL,      -- as first sent
                    results     TEXT NOT NULL,      -- JSON list, no page content
                    requested   INTEGER NOT NULL,   -- count asked of the API
                    fetched_at  REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    hits        INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS page (
                    key         TEXT PRIMARY KEY,   -- canonical_url
                    url         TEXT NOT NULL,
                    title       TEXT,
                    body        BLOB,               -- zlib Markdown; empty for refusals
                    chars       INTEGER NOT NULL DEFAULT 0,
                    status      INTEGER NOT NULL,   -- 200, or the refusal status
                    error       TEXT,
                    source      TEXT NOT NULL,      -- fetch | search | import
                    fetched_at  REAL NOT NULL,
                    accessed_at REAL NOT NULL,
                    hits        INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS ledger (
                    day         TEXT NOT NULL,
                    kind        TEXT NOT NULL,      -- search | fetch
                    network     INTEGER NOT NULL DEFAULT 0,  -- billed requests
                    cached      INTEGER NOT NULL DEFAULT 0,  -- served from the store
                    primed      INTEGER NOT NULL DEFAULT 0,  -- fetches served by search prefetch
                    PRIMARY KEY (day, kind)
                );
                CREATE INDEX IF NOT EXISTS idx_page_fetched ON page(fetched_at);
                CREATE INDEX IF NOT EXISTS idx_search_fetched ON search(fetched_at);
                """
            )

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- search ----------------------------------------------------------

    def get_search(self, key: str, touch: bool = True) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM search WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            if touch:
                c.execute("UPDATE search SET accessed_at=?, hits=hits+1 WHERE key=?", (time.time(), key))
            return {
                "query": row["query"],
                "results": json.loads(row["results"] or "[]"),
                "requested": row["requested"],
                "fetched_at": row["fetched_at"],
                "age": time.time() - row["fetched_at"],
                "hits": row["hits"],
            }

    def similar_search(self, query: str, min_jaccard: float = SIMILAR_MIN_JACCARD) -> Optional[dict]:
        """Best near-duplicate cached search (recently used first), or None. No touch."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT key, query FROM search ORDER BY accessed_at DESC LIMIT ?", (SIMILAR_SCAN,)
            ).fetchall()
        best, score = None, min_jaccard
        for r in rows:
            sim = query_similarity(query, r["query"])
            if sim >= score:
                best, score = r["key"], sim
        if best is None:
            return None
        row = self.get_search(best, touch=False)
        if row:
            row["key"], row["similarity"] = best, round(score, 2)
        return row

    def put_search(self, key: str, query: str, results: list, requested: int) -> None:
        now = time.time()
        with self._conn() as c:
            c.execute(
                "INSERT INTO search (key, query, results, requested, fetched_at, accessed_at, hits) "
                "VALUES (?,?,?,?,?,?,0) ON CONFLICT(key) DO UPDATE SET query=excluded.query, "
                "results=excluded.results, requested=excluded.requested, fetched_at=excluded.fetched_at, "
                "accessed_at=excluded.accessed_at",
                (key, query, json.dumps(results, ensure_ascii=False), int(requested), now, now),
            )

    # -- pages -----------------------------------------------------------

    def get_page(self, key: str, touch: bool = True) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM page WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            if touch:
                c.execute("UPDATE page SET accessed_at=?, hits=hits+1 WHERE key=?", (time.time(), key))
            return {
                "url": row["url"],
                "title": row["title"] or "",
                "body": _unz(row["body"]),
                "chars": row["chars"],
                "status": row["status"],
                "error": row["error"],
                "source": row["source"],
                "fetched_at": row["fetched_at"],
                "age": time.time() - row["fetched_at"],
                "hits": row["hits"],
            }

    def put_page(
        self,
        key: str,
        url: str,
        *,
        title: str = "",
        body: str = "",
        status: int = 200,
        error: Optional[str] = None,
        source: str = "fetch",
        fetched_at: Optional[float] = None,
        keep_newer: bool = False,
    ) -> bool:
        """Store a page (or a refusal). ``keep_newer`` never replaces a good copy that is
        newer or still within its TTL (search prefetch must not churn fresh fetches)."""
        when = fetched_at if fetched_at is not None else time.time()
        with self._conn() as c:
            if keep_newer:
                old = c.execute("SELECT status, fetched_at FROM page WHERE key=?", (key,)).fetchone()
                if old and old["status"] == 200 and (
                    old["fetched_at"] >= when or time.time() - old["fetched_at"] <= ttl_for_url(key)
                ):
                    return False
            c.execute(
                "INSERT INTO page (key, url, title, body, chars, status, error, source, fetched_at, accessed_at, hits) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,0) ON CONFLICT(key) DO UPDATE SET url=excluded.url, "
                "title=excluded.title, body=excluded.body, chars=excluded.chars, status=excluded.status, "
                "error=excluded.error, source=excluded.source, fetched_at=excluded.fetched_at, "
                "accessed_at=excluded.accessed_at",
                (key, url, title, _z(body), len(body or ""), int(status), error, source, when, time.time()),
            )
        return True

    def has_page(self, key: str) -> Optional[int]:
        """Chars of a good cached page under ``key``, else None (no touch)."""
        with self._conn() as c:
            row = c.execute("SELECT chars FROM page WHERE key=? AND status=200", (key,)).fetchone()
            return int(row["chars"]) if row else None

    # -- ledger ----------------------------------------------------------

    def bump(self, kind: str, column: str, n: int = 1) -> None:
        if column not in ("network", "cached", "primed") or n <= 0:
            return
        with self._conn() as c:
            c.execute(
                f"INSERT INTO ledger (day, kind, {column}) VALUES (?,?,?) "
                f"ON CONFLICT(day, kind) DO UPDATE SET {column}={column}+excluded.{column}",
                (_day(), kind, int(n)),
            )

    def ledger(self) -> Dict[str, Dict[str, int]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT kind, SUM(network) n, SUM(cached) c, SUM(primed) p FROM ledger GROUP BY kind"
            ).fetchall()
        return {r["kind"]: {"network": r["n"] or 0, "cached": r["c"] or 0, "primed": r["p"] or 0} for r in rows}

    # -- maintenance -----------------------------------------------------

    def stats(self) -> Dict[str, int]:
        with self._conn() as c:
            s = c.execute("SELECT COUNT(*) n FROM search").fetchone()["n"]
            p = c.execute("SELECT COUNT(*) n, COALESCE(SUM(chars),0) ch FROM page WHERE status=200").fetchone()
            neg = c.execute("SELECT COUNT(*) n FROM page WHERE status<>200").fetchone()["n"]
        size = self.path.stat().st_size if self.path.exists() else 0
        return {"searches": s, "pages": p["n"], "page_chars": p["ch"], "refusals": neg, "bytes": size}

    def list_pages(self, limit: int = 30) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT url, status, chars, source, fetched_at, hits FROM page ORDER BY fetched_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_searches(self, limit: int = 30) -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT query, requested, fetched_at, hits, results FROM search ORDER BY fetched_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {**{k: r[k] for k in ("query", "requested", "fetched_at", "hits")}, "n": len(json.loads(r["results"] or "[]"))}
            for r in rows
        ]

    def clear(self, what: str = "all") -> int:
        n = 0
        with self._conn() as c:
            if what in ("all", "pages"):
                n += c.execute("DELETE FROM page").rowcount or 0
            if what in ("all", "searches"):
                n += c.execute("DELETE FROM search").rowcount or 0
        return n

    def prune(self, max_age_days: int = 90) -> int:
        """Drop entries not *used* for ``max_age_days`` (fresh or not)."""
        cutoff = time.time() - max_age_days * DAY
        with self._conn() as c:
            n = c.execute("DELETE FROM page WHERE accessed_at < ?", (cutoff,)).rowcount or 0
            n += c.execute("DELETE FROM search WHERE accessed_at < ?", (cutoff,)).rowcount or 0
        return n

    def import_legacy(self, db_path: Path) -> int:
        """Move DeepSeek-Fetch (Markdown) rows of an old per-project ``web_cache``
        into this store and drop that table. HTML rows from the retired local
        engine are discarded: the page store holds Markdown only."""
        db_path = Path(db_path)
        if not db_path.exists():
            return 0
        moved = 0
        try:
            src = sqlite3.connect(str(db_path), timeout=30)
            src.row_factory = sqlite3.Row
            try:
                exists = src.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_cache'"
                ).fetchone()
                if not exists:
                    return 0
                for r in src.execute("SELECT url, headers, body, engine, status, fetched_at FROM web_cache"):
                    md = (r["engine"] or "").startswith("deepseek") or "markdown" in (r["headers"] or "")
                    if not md or (r["status"] or 0) != 200 or not r["body"]:
                        continue
                    try:
                        title = json.loads(r["headers"] or "{}").get("x-title", "")
                    except ValueError:
                        title = ""
                    if self.put_page(
                        canonical_url(r["url"]), r["url"], title=title, body=r["body"],
                        source="import", fetched_at=r["fetched_at"], keep_newer=True,
                    ):
                        moved += 1
                src.execute("DROP TABLE web_cache")
                src.commit()
            finally:
                src.close()
        except sqlite3.Error:
            return moved
        return moved


_store: Optional[WebStore] = None
_imported: set = set()
# get_store runs on worker threads too (fetch/search go through asyncio.to_thread).
# The lock makes "one handle, legacy rows migrated before any caller reads" hold:
# without it a second thread could see the path in _imported and read the store
# while the first thread was still moving rows in.
_STORE_LOCK = threading.Lock()


def get_store() -> WebStore:
    """The process-wide handle; migrates the launch project's old web_cache once."""
    global _store
    with _STORE_LOCK:
        if _store is None:
            _store = WebStore()
        try:
            from .config import get_cache_db

            legacy = get_cache_db()
            if legacy not in _imported:
                _imported.add(legacy)
                _store.import_legacy(legacy)
        except Exception:
            pass
        return _store


def reset_store() -> None:
    """Forget the process handle (tests, G023_HOME changes)."""
    global _store
    with _STORE_LOCK:
        _store = None
        _imported.clear()


# ---------------------------------------------------------------------------
# DeepSeek transport
# ---------------------------------------------------------------------------


def _error_message(text: str) -> str:
    try:
        return str(json.loads(text)["error"]["message"])
    except Exception:
        return (text or "")[:200]


def _retry_after(headers: Any) -> Optional[float]:
    try:
        value = headers.get("retry-after")
        return float(value) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def _post(path: str, payload: dict, *, timeout: float = 60, retries: int = 2) -> dict:
    """POST to a DeepSeek tools endpoint; retries 408/429/5xx and transport errors."""
    import httpx

    from .config import load_api_key, settings

    endpoint = settings.base_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {load_api_key()}", "Content-Type": "application/json"}
    last = ""
    status = 0
    with httpx.Client(timeout=httpx.Timeout(timeout, connect=15.0)) as client:
        for attempt in range(retries + 1):
            try:
                r = client.post(endpoint, headers=headers, json=payload)
            except Exception as e:  # transport failure: retry like a 5xx
                last, status = str(e), 0
                if attempt < retries:
                    time.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.5))
                    continue
                break
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    raise WebError(502, "non-JSON response") from None
            last, status = _error_message(r.text), r.status_code
            if r.status_code in _RETRY and attempt < retries:
                wait = _retry_after(r.headers) or (2 ** attempt) + random.uniform(0, 1)
                time.sleep(min(wait, 10))
                continue
            break
    raise WebError(status, last or "request failed")


def _record(kind: str, ok: bool) -> None:
    try:
        from .usage import get_usage

        if kind == "search":
            get_usage().record_search(ok=ok)
        else:
            get_usage().record_fetch(ok=ok)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_RESULT_KEYS = ("title", "url", "summary", "snippet", "date", "site_name", "authority", "rerank_score")


@dataclass
class SearchOutcome:
    query: str
    results: List[dict]
    source: str              # network | cache | stale
    age: float = 0.0
    primed: int = 0          # pages filed by this network search
    note: str = ""
    cached_pages: Dict[str, int] = field(default_factory=dict)  # canonical url -> chars
    cut_pages: set = field(default_factory=set)  # cached pages the service cut at SOURCE_CAP_BYTES


# Single flight: two identical calls in one parallel batch (each runs in its
# own worker thread) used to both miss the cache and both pay the per-request
# fee. Per-key locks make the second wait for the first and hit its cache row.
_FLIGHT_GUARD = threading.Lock()
# key -> [lock, holders]. An entry lives exactly as long as someone holds or
# waits on it, so the table is bounded by in-flight calls. (Evicting "unlocked"
# locks could drop one already handed out but not yet acquired, letting a
# second caller create a fresh lock for the same key and pay twice.)
_FLIGHT_LOCKS: Dict[str, list] = {}


@contextmanager
def _flight_lock(key: str) -> Iterator[None]:
    with _FLIGHT_GUARD:
        entry = _FLIGHT_LOCKS.get(key)
        if entry is None:
            entry = _FLIGHT_LOCKS[key] = [threading.Lock(), 0]
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _FLIGHT_GUARD:
            entry[1] -= 1
            if entry[1] == 0 and _FLIGHT_LOCKS.get(key) is entry:
                del _FLIGHT_LOCKS[key]


def search(
    query: str,
    count: int = SHOW_DEFAULT,
    *,
    fresh: bool = False,
    store: Optional[WebStore] = None,
    transport=None,
) -> SearchOutcome:
    """Cached web search. Raises WebError when nothing usable is available."""
    with _flight_lock("search:" + canonical_query(clean_query(query or ""))):
        return _search(query, count, fresh=fresh, store=store, transport=transport)


def _search(
    query: str,
    count: int = SHOW_DEFAULT,
    *,
    fresh: bool = False,
    store: Optional[WebStore] = None,
    transport=None,
) -> SearchOutcome:
    store = store or get_store()
    post = transport or _post
    q = clean_query(query)
    if not q:
        raise WebError(400, "query is empty")
    if len(q) > QUERY_MAX_CHARS:
        raise WebError(400, f"query is {len(q)} chars; the limit is {QUERY_MAX_CHARS} — shorten it")
    count = max(1, min(int(count or SHOW_DEFAULT), SHOW_MAX))
    key = canonical_query(q)
    row = store.get_search(key, touch=False)
    if row and not fresh:
        ttl = SEARCH_TTL_EMPTY if not row["results"] else ttl_for_query(q)
        enough = len(row["results"]) >= count or len(row["results"]) < row["requested"]
        if row["age"] <= ttl and enough:
            store.get_search(key, touch=True)
            store.bump("search", "cached")
            return _with_pages(SearchOutcome(q, row["results"], "cache", row["age"]), store)
    if not fresh and not (row and row["results"]):
        near = store.similar_search(q)
        if near and near["results"] and near["age"] <= ttl_for_query(near["query"]) and \
                min(ttl_for_query(q), ttl_for_query(near["query"])) >= near["age"] and \
                (len(near["results"]) >= count or len(near["results"]) < near["requested"]):
            store.get_search(near["key"], touch=True)
            store.bump("search", "cached")
            note = f'similar cached search "{near["query"]}" (overlap {near["similarity"]}); fresh=true to search anew'
            return _with_pages(SearchOutcome(q, near["results"], "cache", near["age"], note=note), store)
    want = min(max(count, NET_COUNT), NET_COUNT_MAX)
    try:
        data = post(SEARCH_PATH, {"query": q, "count": want, "need_content": True})
    except WebError as e:
        if row and row["results"]:
            return _with_pages(
                SearchOutcome(q, row["results"], "stale", row["age"], note=f"network failed ({e}); serving the cached copy"),
                store,
            )
        raise
    raw = list((data or {}).get("results") or [])
    _record("search", ok=bool(raw))
    if raw:
        store.bump("search", "network")
    results: List[dict] = []
    seen: set = set()
    primed = 0
    now = time.time()
    for r in raw:
        url = str(r.get("url") or "")
        if not url:
            continue
        ck = canonical_url(url)
        if ck in seen:
            continue
        seen.add(ck)
        results.append({k: r.get(k) for k in _RESULT_KEYS if r.get(k) not in (None, "")} | {"url": ck})
        content = str(r.get("content") or "")
        if content.strip():
            if store.put_page(ck, ck, title=str(r.get("title") or ""), body=content, source="search",
                              fetched_at=now, keep_newer=True):
                primed += 1
    store.put_search(key, q, results, want)
    return _with_pages(SearchOutcome(q, results, "network", 0.0, primed=primed), store)


def _with_pages(out: SearchOutcome, store: WebStore) -> SearchOutcome:
    for r in out.results:
        n = store.has_page(r["url"])
        if n:
            out.cached_pages[r["url"]] = n
            # Only bodies whose char count allows exactly SOURCE_CAP_BYTES bytes are decompressed.
            if SOURCE_CAP_BYTES // 4 <= n <= SOURCE_CAP_BYTES:
                row = store.get_page(r["url"], touch=False)
                if row and source_truncated(row["body"]):
                    out.cut_pages.add(r["url"])
    return out


def _age(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 90:
        return f"{s}s"
    if s < 90 * 60:
        return f"{s // 60}m"
    if s < 36 * 3600:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _source_label(source: str, age: float) -> str:
    return "network" if source == "network" else f"{source} {_age(age)} old"


def render_search(out: SearchOutcome, count: int = SHOW_DEFAULT) -> str:
    """Model-facing search result: JSON header line, then one block per hit."""
    count = max(1, min(int(count or SHOW_DEFAULT), SHOW_MAX))
    shown = out.results[:count]
    head: Dict[str, Any] = {"query": out.query, "results": len(shown), "source": _source_label(out.source, out.age)}
    if len(out.results) > len(shown):
        head["more"] = len(out.results) - len(shown)
    if out.note:
        head["note"] = out.note
    lines = [json.dumps(head, ensure_ascii=False)]
    if not shown:
        lines.append("No results. Try broader terms or drop operators.")
    for i, r in enumerate(shown, 1):
        meta = [r["url"]]
        if r.get("site_name"):
            meta.append(str(r["site_name"]))
        if r.get("date"):
            meta.append(str(r["date"])[:10])
        chars = out.cached_pages.get(r["url"])
        if chars:
            cut = r["url"] in out.cut_pages
            meta.append(f"page cached ({_size(chars)} chars{', cut at 20 KB' if cut else ''})")
        text = " ".join(str(r.get("summary") or r.get("snippet") or "").split())
        if len(text) > SUMMARY_MAX_CHARS:
            text = text[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
        lines.append(f"[{i}] {r.get('title') or '(untitled)'}\n    {' · '.join(meta)}\n    {text}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------


def source_truncated(body: str) -> bool:
    """True when ``body`` has the exact size the web service cuts some pages at."""
    return bool(body) and len(body) <= SOURCE_CAP_BYTES and len(body.encode("utf-8", "surrogatepass")) == SOURCE_CAP_BYTES


_GH_BLOB = re.compile(r"^/([^/]+)/([^/]+)/blob/(.+)$")
_HAS_EXT = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def plain_twins(url: str) -> List[str]:
    """Plain-text forms of ``url`` that the service returns uncut: GitHub ``blob`` -> raw file,
    docs page without an extension -> ``page.md`` (Mintlify, GitBook, platform docs, llms.txt sites)."""
    try:
        parts = urlsplit(canonical_url(url))
    except Exception:
        return []
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if host in ("github.com", "www.github.com"):
        m = _GH_BLOB.match(path)
        return [f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"] if m else []
    if parts.query or not path.strip("/") or path.endswith("/") or _HAS_EXT.search(path):
        return []
    return [urlunsplit((parts.scheme, parts.netloc, path + ".md", "", ""))]


_LINK_NOISE = re.compile(r"\]\([^)]*\)|https?://\S+|<[^>]*>")


def _norm_text(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", _LINK_NOISE.sub(" ", unicodedata.normalize("NFKC", s).lower()))


def same_document(cut: str, alt: str, probes: int = 8, width: int = 40) -> bool:
    """``alt`` is the same document as the cut ``cut`` and at least as long: at least half of
    ``probes`` anchor runs taken from ``cut`` (after its first tenth, which may be site chrome)
    occur in ``alt``. Markup, link targets and spacing are ignored, so HTML-extracted text
    matches its Markdown source."""
    a, b = _norm_text(cut), _norm_text(alt)
    if not a or len(b) < len(a):
        return False
    lo = len(a) // 10
    span = len(a) - lo - width
    if span <= 0:
        return a in b
    hits = sum(1 for i in range(probes) if a[lo + span * i // (probes - 1):][:width] in b)
    return hits * 2 >= probes


@dataclass
class Page:
    url: str
    title: str
    body: str
    source: str              # network | cache | stale | primed
    age: float = 0.0
    note: str = ""

    @property
    def cut(self) -> bool:
        """The body ends where the web service cut it, not where the page ends."""
        return source_truncated(self.body)


def fetch(
    url: str,
    *,
    mode: str = "auto",
    max_age: Optional[int] = None,
    store: Optional[WebStore] = None,
    transport=None,
) -> Page:
    """Cached page fetch. ``mode``: auto | fresh | cache. Raises WebError on refusal."""
    try:
        key = canonical_url(str(url or "").strip())
    except Exception:
        key = str(url or "")
    with _flight_lock("fetch:" + key):
        return _fetch(url, mode=mode, max_age=max_age, store=store, transport=transport)


def _fetch(
    url: str,
    *,
    mode: str = "auto",
    max_age: Optional[int] = None,
    store: Optional[WebStore] = None,
    transport=None,
) -> Page:
    store = store or get_store()
    post = transport or _post
    mode = (mode or "auto").lower()
    raw_url = str(url or "").strip()
    if not raw_url:
        raise WebError(400, "url is empty")
    key = canonical_url(raw_url)
    parts = urlsplit(key)
    raw = urlsplit(raw_url if "://" in raw_url else "https://" + raw_url)
    if parts.scheme not in ("http", "https") or not parts.hostname or "@" in raw.netloc or len(key) > URL_MAX_CHARS:
        raise WebError(400, "URL must be http(s) with a host, no credentials, <= 8192 chars")
    row = store.get_page(key, touch=False)
    ttl = int(max_age) if max_age is not None and int(max_age) >= 0 else ttl_for_url(key)
    if row and mode != "fresh":
        if row["status"] == 200 and (mode == "cache" or row["age"] <= ttl):
            store.get_page(key, touch=True)
            primed = row["source"] == "search"
            page = Page(row["url"], row["title"], row["body"], "primed" if primed else "cache", row["age"])
            if page.cut and mode != "cache":
                # Re-fetching the same URL returns the same cut copy; only the twin can help.
                full = _recover_cut(page, store, post)
                if full is not None:
                    return full
            store.bump("fetch", "primed" if primed else "cached")
            return _flag_cut(page)
        if row["status"] != 200 and row["age"] <= NEGATIVE_TTL and mode != "cache":
            raise WebError(row["status"], f"{row['error']} (cached refusal, {_age(row['age'])} old)", cached=True)
    if mode == "cache":
        raise WebError(0, "nothing cached for this URL; use cache_mode='auto' or 'fresh'")
    try:
        data = post(FETCH_PATH, {"url": key})
    except WebError as e:
        good = bool(row and row["status"] == 200)
        if e.status in (403, 404) and not good:  # never overwrite a good copy with a refusal
            store.put_page(key, key, status=e.status, error=e.message, source="fetch")
        if good:
            return Page(row["url"], row["title"], row["body"], "stale", row["age"],
                        note=f"network failed ({e}); serving the cached copy")
        raise
    content = str((data or {}).get("content") or "")
    title = str((data or {}).get("title") or "")
    _record("fetch", ok=bool(content))
    if not content.strip():
        if row and row["status"] == 200:
            return Page(row["url"], row["title"], row["body"], "stale", row["age"],
                        note="the page returned no content now; serving the cached copy")
        store.put_page(key, key, status=404, error="no extractable content", source="fetch")
        raise WebError(404, "no extractable content")
    store.bump("fetch", "network")
    store.put_page(key, key, title=title, body=content, source="fetch")
    page = Page(key, title, content, "network", 0.0)
    if page.cut:
        full = _recover_cut(page, store, post)
        if full is not None:
            return full
    return _flag_cut(page)


def _flag_cut(page: Page) -> Page:
    if page.cut and CUT_NOTE not in page.note:
        page.note = (page.note + "; " if page.note else "") + CUT_NOTE
    return page


def _recover_cut(page: Page, store: WebStore, post) -> Optional[Page]:
    """One fetch of the first untried plain-text twin of a cut page. On a same-document,
    longer body: store it under the page's own key (later fetches are free and whole) and
    return it. A twin is tried once: its outcome (body or refusal) stays cached under its own key."""
    for twin in plain_twins(page.url):
        tkey = canonical_url(twin)
        old = store.get_page(tkey, touch=False)
        if old is not None:
            body = old["body"] if old["status"] == 200 else ""
        else:
            try:
                data = post(FETCH_PATH, {"url": tkey})
            except WebError as e:
                if e.status in (403, 404):
                    store.put_page(tkey, tkey, status=e.status, error=e.message, source="fetch")
                continue
            body = str((data or {}).get("content") or "")
            _record("fetch", ok=bool(body))
            if not body.strip():
                store.put_page(tkey, tkey, status=404, error="no extractable content", source="fetch")
                continue
            store.bump("fetch", "network")
            store.put_page(tkey, tkey, title=str((data or {}).get("title") or ""), body=body, source="fetch")
        if body and same_document(page.body, body):
            key = canonical_url(page.url)
            store.put_page(key, key, title=page.title, body=body, source="fetch")
            note = f"the service cut this URL at 20,480 bytes; the full text came from {tkey}"
            return _flag_cut(Page(key, page.title, body, "network", 0.0, note=note))
    return None


# ---------------------------------------------------------------------------
# Page rendering: outline, char window, BM25 focus
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t#]*$", re.M)


def sections(body: str) -> List[Tuple[str, int, int, int]]:
    """(heading, level, start, end) char spans. Pages without headings get ~2k paragraph chunks."""
    marks = [(m.start(), len(m.group(1)), m.group(2).strip()) for m in _HEADING.finditer(body)]
    out: List[Tuple[str, int, int, int]] = []
    if marks:
        if marks[0][0] > 0 and body[: marks[0][0]].strip():
            out.append(("(top)", 0, 0, marks[0][0]))
        for i, (pos, lvl, text) in enumerate(marks):
            end = marks[i + 1][0] if i + 1 < len(marks) else len(body)
            out.append((text[:120], lvl, pos, end))
        return out
    pos = 0
    n = len(body)
    while pos < n:
        end = min(n, pos + 2000)
        if end < n:
            cut = body.rfind("\n\n", pos + 800, end)
            end = cut + 2 if cut > 0 else end
        first = body[pos:end].strip().split("\n", 1)[0][:60]
        out.append((first or f"chars {pos}", 0, pos, end))
        pos = end
    return out


def outline(body: str, max_lines: int = OUTLINE_MAX_LINES) -> List[str]:
    secs = sections(body)
    if len(secs) > max_lines:
        top = min(s[1] for s in secs if s[1]) if any(s[1] for s in secs) else 0
        keep = [s for s in secs if s[1] <= top + 1] or secs
        secs = keep[:max_lines]
    return [f"@{a} {'#' * lvl + ' ' if lvl else ''}{h} ({_size(b - a)})" for h, lvl, a, b in secs]


def _size(n: int) -> str:
    return f"{round(n / 1000)}k" if n >= 1000 else str(n)


def focus_sections(body: str, focus: str, top: int = FOCUS_TOP) -> List[Tuple[float, str, int, int]]:
    """BM25 over sections (heading matches count 3x), best ``top`` as (score, heading, start, end).

    Shared ranker (docindex): word-boundary tf, so "be" does not count "beauty", and a
    query made only of stopwords ("to be or not to be") keeps them.
    """
    from . import docindex

    terms = docindex.terms(focus)
    secs = sections(body)
    if not terms or not secs:
        return []
    ranked = docindex.rank_texts([(h, body[a:b]) for h, _lvl, a, b in secs], terms, top=top)
    return [(score, secs[k][0], secs[k][2], secs[k][3]) for score, k in ranked]


def render_page(page: Page, *, focus: str = "", offset: int = 0, max_chars: int = PAGE_WINDOW_DEFAULT) -> str:
    """Model-facing page: JSON header, then the chosen text after ``-----``."""
    body = page.body or ""
    total = len(body)
    max_chars = max(500, min(int(max_chars or PAGE_WINDOW_DEFAULT), PAGE_WINDOW_MAX))
    head: Dict[str, Any] = {"url": page.url}
    if page.title:
        head["title"] = page.title
    head["chars"] = total
    head["source"] = _source_label(page.source, page.age) if page.source != "primed" else f"cache (search prefetch) {_age(page.age)} old"
    note = page.note
    if page.cut:
        head["complete"] = False
        if CUT_NOTE not in note:
            note = (note + "; " if note else "") + CUT_NOTE
    if note:
        head["note"] = note
    parts: List[str] = []
    if total <= min(max_chars, PAGE_WHOLE_MAX_CHARS) and not offset:
        head["shown"] = "all"
        return json.dumps(head, ensure_ascii=False) + "\n-----\n" + body
    if focus and not offset:
        best = focus_sections(body, focus)
        if best:
            chosen = sorted(best, key=lambda x: x[2])
            budget = max_chars
            spans = []
            for _s, h, a, b in chosen:
                if budget <= 0:
                    break
                take = min(b - a, budget)
                spans.append((a, a + take, h))
                budget -= take
            head["shown"] = "focus: " + ", ".join(f"{a}-{b}" for a, b, _ in spans)
            head["ranked"] = [f"@{a} {h} ({s})" for s, h, a, _b in best]
            text = "\n\n[…]\n\n".join(body[a:b].strip() for a, b, _ in spans)
            parts = [json.dumps(head, ensure_ascii=False), "-----", text]
            parts += ["-----", "outline (offset=@N reads from there):"] + outline(body)
            return "\n".join(parts)
        head["focus"] = "no section matched; showing the start"
    start = max(0, min(int(offset or 0), total))
    end = min(total, start + max_chars)
    if end < total:
        cut = body.rfind("\n", start + max_chars // 2, end)
        end = cut + 1 if cut > 0 else end
    head["shown"] = f"{start}-{end}"
    if end < total:
        head["next_offset"] = end
    parts = [json.dumps(head, ensure_ascii=False), "-----", body[start:end]]
    if total > max_chars and start == 0:
        parts += ["-----", "outline (offset=@N or focus='topic' to read more):"] + outline(body)
    return "\n".join(parts)


def parse_header(text: str) -> Optional[dict]:
    """The JSON header line of a rendered search/page (None if the first line is not a JSON object)."""
    first = (text or "").split("\n", 1)[0]
    try:
        data = json.loads(first)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None

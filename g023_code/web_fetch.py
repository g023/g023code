"""
Browser-faithful URL fetching.

Goal: a fetch looks like a real person's browser opening the page — same TLS
handshake, same protocol, same headers in the same order, same cookie
behaviour — so ordinary bot-detection heuristics see nothing unusual.

Engines, best first:
  1. curl_cffi  — impersonates a real Chrome TLS/JA3 + HTTP/2 fingerprint.
  2. httpx+h2   — correct headers and HTTP/2, generic TLS fingerprint.
  3. httpx      — correct headers over HTTP/1.1.

Nothing here identifies g023: no custom headers, no tell-tale User-Agent.
There is no JavaScript engine, so pages that build their body client-side
return the shell — that is a capability limit, not a disguise problem.
"""

from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from .config import get_scratch_dir

# ---------------------------------------------------------------------------
# Optional dependencies — every one of them is a fidelity upgrade, not a need
# ---------------------------------------------------------------------------

try:  # real Chrome TLS fingerprint
    from curl_cffi import requests as _cffi  # type: ignore
    HAS_CURL_CFFI = True
except Exception:  # pragma: no cover - depends on install
    _cffi = None
    HAS_CURL_CFFI = False

try:
    import httpx
    HAS_HTTPX = True
except Exception:  # pragma: no cover
    httpx = None  # type: ignore
    HAS_HTTPX = False

try:  # HTTP/2 for httpx
    import h2  # noqa: F401
    HAS_H2 = True
except Exception:
    HAS_H2 = False

try:
    import brotli  # noqa: F401
    HAS_BROTLI = True
except Exception:
    try:
        import brotlicffi  # noqa: F401
        HAS_BROTLI = True
    except Exception:
        HAS_BROTLI = False

try:
    import zstandard  # noqa: F401
    HAS_ZSTD = True
except Exception:
    HAS_ZSTD = False


# ---------------------------------------------------------------------------
# Browser profiles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BrowserProfile:
    """One internally consistent browser identity.

    Every field has to agree with every other field: a Chrome 146 UA next to a
    Chrome 131 TLS handshake, or a Windows UA next to a macOS platform hint, is
    exactly the contradiction fingerprinting checks look for.

    When curl_cffi is driving, it supplies the UA and client hints itself so
    they always match the TLS handshake it performs; the fields here are the
    httpx fallback's identity and are kept in step with the same browser
    version on purpose.
    """

    key: str
    user_agent: str
    accept_language: str
    sec_ch_ua: Optional[str]          # None for Firefox (does not send hints)
    platform: str                     # "Windows" | "macOS" | "Linux"
    impersonate: Optional[str]        # curl_cffi target name
    is_chromium: bool = True

    @property
    def sec_ch_ua_platform(self) -> str:
        return f'"{self.platform}"'


# Deliberately small, and pinned to the versions curl_cffi impersonates so the
# two engines present the same browser. A stale Chrome version is itself a
# signal, so these need bumping when curl_cffi gains newer targets.
_CHROME_VERSION = "146"
_CHROME_UA_SUFFIX = (
    f"AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{_CHROME_VERSION}.0.0.0 Safari/537.36"
)
_CHROME_SEC_CH_UA = (
    f'"Chromium";v="{_CHROME_VERSION}", "Not-A.Brand";v="24", '
    f'"Google Chrome";v="{_CHROME_VERSION}"'
)

PROFILES: Tuple[BrowserProfile, ...] = (
    BrowserProfile(
        key="chrome-windows",
        user_agent=f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) {_CHROME_UA_SUFFIX}",
        accept_language="en-US,en;q=0.9",
        sec_ch_ua=_CHROME_SEC_CH_UA,
        platform="Windows",
        impersonate="chrome",
    ),
    BrowserProfile(
        key="chrome-macos",
        user_agent=f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) {_CHROME_UA_SUFFIX}",
        accept_language="en-US,en;q=0.9",
        sec_ch_ua=_CHROME_SEC_CH_UA,
        platform="macOS",
        impersonate="chrome",
    ),
    BrowserProfile(
        key="firefox-macos",
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:147.0) "
            "Gecko/20100101 Firefox/147.0"
        ),
        accept_language="en-US,en;q=0.5",
        sec_ch_ua=None,
        platform="macOS",
        impersonate="firefox",
        is_chromium=False,
    ),
)

DEFAULT_PROFILE = PROFILES[0]


def get_profile(key: Optional[str] = None) -> BrowserProfile:
    if not key or key == "auto":
        return DEFAULT_PROFILE
    for p in PROFILES:
        if p.key == key:
            return p
    return DEFAULT_PROFILE


def _accept_encoding() -> str:
    """Only advertise encodings we can actually decode.

    Claiming brotli and then failing to read the body is both a breakage and a
    louder signal than simply not claiming it.
    """
    encodings = ["gzip", "deflate"]
    if HAS_BROTLI:
        encodings.append("br")
    if HAS_ZSTD:
        encodings.append("zstd")
    return ", ".join(encodings)


def _sec_fetch_site(url: str, referer: Optional[str]) -> str:
    """What a browser would report given where the click came from."""
    if not referer:
        return "none"
    try:
        u, r = urlparse(url), urlparse(referer)
    except ValueError:
        return "cross-site"
    if (u.scheme, u.netloc) == (r.scheme, r.netloc):
        return "same-origin"
    if _registrable(u.netloc) == _registrable(r.netloc):
        return "same-site"
    return "cross-site"


def _registrable(host: str) -> str:
    parts = host.lower().split(":")[0].split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def build_headers(
    url: str,
    profile: BrowserProfile,
    referer: Optional[str] = None,
    include_host: bool = True,
) -> List[Tuple[str, str]]:
    """Headers for a top-level navigation, in the order the browser sends them.

    Order is part of the fingerprint — an alphabetised header block is a
    common giveaway — so this returns a list, not a dict.
    """
    parsed = urlparse(url)
    site = _sec_fetch_site(url, referer)
    headers: List[Tuple[str, str]] = []

    if include_host:
        headers.append(("Host", parsed.netloc))

    if profile.is_chromium:
        if profile.sec_ch_ua:
            headers.append(("sec-ch-ua", profile.sec_ch_ua))
        headers.append(("sec-ch-ua-mobile", "?0"))
        headers.append(("sec-ch-ua-platform", profile.sec_ch_ua_platform))
        headers.append(("Upgrade-Insecure-Requests", "1"))
        headers.append(("User-Agent", profile.user_agent))
        headers.append((
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7",
        ))
        if referer:
            headers.append(("Referer", referer))
        headers.append(("Sec-Fetch-Site", site))
        headers.append(("Sec-Fetch-Mode", "navigate"))
        headers.append(("Sec-Fetch-User", "?1"))
        headers.append(("Sec-Fetch-Dest", "document"))
        headers.append(("Accept-Encoding", _accept_encoding()))
        headers.append(("Accept-Language", profile.accept_language))
    else:  # Firefox order differs, and it sends no client hints
        headers.append(("User-Agent", profile.user_agent))
        headers.append((
            "Accept",
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        ))
        headers.append(("Accept-Language", profile.accept_language))
        headers.append(("Accept-Encoding", _accept_encoding()))
        if referer:
            headers.append(("Referer", referer))
        headers.append(("Upgrade-Insecure-Requests", "1"))
        headers.append(("Sec-Fetch-Dest", "document"))
        headers.append(("Sec-Fetch-Mode", "navigate"))
        headers.append(("Sec-Fetch-Site", site))
        headers.append(("Sec-Fetch-User", "?1"))

    return headers


# ---------------------------------------------------------------------------
# Cookie jar — a browser that never remembers anything is conspicuous
# ---------------------------------------------------------------------------

class CookieStore:
    """Per-host cookie persistence in .g023/cookies.json."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else get_scratch_dir() / "cookies.json"
        self._data: Dict[str, Dict[str, str]] = {}
        self._load()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = {k: v for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def for_url(self, url: str) -> Dict[str, str]:
        host = _registrable(urlparse(url).netloc)
        return dict(self._data.get(host, {}))

    def update(self, url: str, cookies: Dict[str, str]):
        if not cookies:
            return
        host = _registrable(urlparse(url).netloc)
        self._data.setdefault(host, {}).update(cookies)
        self.save()

    def clear(self, host: Optional[str] = None):
        if host:
            self._data.pop(_registrable(host), None)
        else:
            self._data = {}
        self.save()


# ---------------------------------------------------------------------------
# Pacing — back-to-back millisecond-perfect requests read as automation
# ---------------------------------------------------------------------------

_last_request_at: Dict[str, float] = {}
_MIN_GAP = 1.2  # seconds between requests to the same host


def _pace(url: str) -> float:
    """Return how long to wait so this host isn't hit at machine cadence."""
    host = urlparse(url).netloc
    last = _last_request_at.get(host)
    now = time.time()
    delay = 0.0
    if last is not None:
        elapsed = now - last
        if elapsed < _MIN_GAP:
            delay = (_MIN_GAP - elapsed) + random.uniform(0.05, 0.45)
    _last_request_at[host] = now + delay
    return delay


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class FetchResult:
    url: str
    final_url: str
    status: int
    headers: Dict[str, str]
    body: str
    engine: str
    profile: str
    elapsed_ms: int
    user_agent: str = ""
    fetched_at: float = field(default_factory=time.time)
    from_cache: bool = False
    cached_at: Optional[float] = None
    truncated: bool = False

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "status": self.status,
            "headers": self.headers,
            "body": self.body,
            "engine": self.engine,
            "profile": self.profile,
            "elapsed_ms": self.elapsed_ms,
            "fetched_at": self.fetched_at,
            "truncated": self.truncated,
        }


class FetchError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

def engine_name() -> str:
    """Which engine a fetch would use right now, and how faithful it is."""
    if HAS_CURL_CFFI:
        return "curl_cffi"
    if HAS_HTTPX and HAS_H2:
        return "httpx-h2"
    if HAS_HTTPX:
        return "httpx-h1"
    return "none"


def fidelity_report() -> dict:
    """Honest account of what the current install can and cannot disguise."""
    engine = engine_name()
    return {
        "engine": engine,
        "tls_fingerprint": "chrome-accurate" if engine == "curl_cffi" else "generic (python TLS)",
        "http2": engine in ("curl_cffi", "httpx-h2"),
        "header_order": "browser-accurate",
        "cookies": "persisted per host",
        "brotli": HAS_BROTLI,
        "zstd": HAS_ZSTD,
        "javascript": False,
        "notes": (
            "Install curl_cffi for a matching TLS/JA3 fingerprint — it is the "
            "single biggest difference from a real browser."
            if engine != "curl_cffi"
            else "TLS, HTTP/2 and header order all match real Chrome."
        ),
    }


MAX_BYTES = 3_000_000


def _fetch_curl_cffi(
    url: str,
    profile: BrowserProfile,
    referer: Optional[str],
    timeout: int,
    cookies: Dict[str, str],
) -> Tuple[FetchResult, Dict[str, str]]:
    """curl_cffi drives a real Chrome TLS stack, so let it own the low-level
    handshake and only supply the navigation-specific headers."""
    extra = {"Accept-Language": profile.accept_language}
    if referer:
        extra["Referer"] = referer
        extra["Sec-Fetch-Site"] = _sec_fetch_site(url, referer)

    started = time.time()
    with _cffi.Session(impersonate=profile.impersonate or "chrome") as session:  # type: ignore
        if cookies:
            for name, value in cookies.items():
                session.cookies.set(name, value)
        r = session.get(
            url,
            headers=extra,
            timeout=timeout,
            allow_redirects=True,
            max_redirects=10,
        )
        new_cookies = {c.name: c.value for c in session.cookies.jar}

    # curl_cffi picks the UA that matches the TLS stack it just used, which may
    # be a different OS than the profile names. Record what actually went out.
    sent_ua = ""
    try:
        sent_ua = dict(r.request.headers).get("User-Agent", "")  # type: ignore[union-attr]
    except Exception:
        pass

    body = r.text or ""
    truncated = False
    if len(body) > MAX_BYTES:
        body, truncated = body[:MAX_BYTES], True

    return (
        FetchResult(
            url=url,
            final_url=str(r.url),
            status=r.status_code,
            headers={k.lower(): v for k, v in dict(r.headers).items()},
            body=body,
            engine="curl_cffi",
            profile=profile.key,
            elapsed_ms=int((time.time() - started) * 1000),
            truncated=truncated,
            user_agent=sent_ua or profile.user_agent,
        ),
        new_cookies,
    )


def _fetch_httpx(
    url: str,
    profile: BrowserProfile,
    referer: Optional[str],
    timeout: int,
    cookies: Dict[str, str],
) -> Tuple[FetchResult, Dict[str, str]]:
    headers = build_headers(url, profile, referer)
    started = time.time()

    with httpx.Client(  # type: ignore
        http2=HAS_H2,
        timeout=timeout,
        follow_redirects=True,
        max_redirects=10,
        cookies=cookies or None,
        verify=True,
    ) as client:
        request = client.build_request("GET", url)
        # Replace wholesale: httpx merges its own defaults ahead of ours, which
        # would put the headers in a non-browser order.
        request.headers = httpx.Headers(headers)  # type: ignore
        if cookies:
            request.headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        r = client.send(request, follow_redirects=True)
        # Iterate the jar rather than dict(client.cookies): the same cookie name
        # can be set for several domains or paths, which makes the dict view
        # raise CookieConflict.
        new_cookies = {c.name: c.value for c in client.cookies.jar}

    body = r.text or ""
    truncated = False
    if len(body) > MAX_BYTES:
        body, truncated = body[:MAX_BYTES], True

    return (
        FetchResult(
            url=url,
            final_url=str(r.url),
            status=r.status_code,
            headers={k.lower(): v for k, v in r.headers.items()},
            body=body,
            engine="httpx-h2" if HAS_H2 else "httpx-h1",
            profile=profile.key,
            elapsed_ms=int((time.time() - started) * 1000),
            truncated=truncated,
            user_agent=profile.user_agent,
        ),
        new_cookies,
    )


def fetch(
    url: str,
    profile_key: Optional[str] = None,
    referer: Optional[str] = None,
    timeout: int = 30,
    retries: int = 2,
    use_cookies: bool = True,
) -> FetchResult:
    """Fetch a URL as a browser would. Blocking; callers wrap in a thread."""
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")

    profile = get_profile(profile_key)
    store = CookieStore() if use_cookies else None
    cookies = store.for_url(url) if store else {}

    delay = _pace(url)
    if delay > 0:
        time.sleep(delay)

    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            if HAS_CURL_CFFI:
                result, new_cookies = _fetch_curl_cffi(url, profile, referer, timeout, cookies)
            elif HAS_HTTPX:
                result, new_cookies = _fetch_httpx(url, profile, referer, timeout, cookies)
            else:
                raise FetchError("No HTTP engine available — pip install httpx")

            if store:
                store.update(result.final_url, new_cookies)

            # Honour rate limiting like a browser would rather than hammering.
            if result.status in (429, 503) and attempt < retries:
                wait = _retry_after(result.headers) or (2 ** attempt) + random.uniform(0, 1)
                time.sleep(min(wait, 30))
                continue
            return result
        except Exception as e:  # network flakiness, TLS resets, timeouts
            last_error = e
            if attempt < retries:
                time.sleep((2 ** attempt) * 0.5 + random.uniform(0, 0.5))
                continue
    raise FetchError(f"Fetch failed after {retries + 1} attempts: {last_error}")


def _retry_after(headers: Dict[str, str]) -> Optional[float]:
    value = headers.get("retry-after")
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Extraction — the orchestrator gets prose, never a raw DOM
# ---------------------------------------------------------------------------

_SKIP_TAGS = {"script", "style", "noscript", "svg", "canvas", "template", "iframe"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "header", "footer", "main", "aside",
    "ul", "ol", "table", "tr", "blockquote", "pre", "form", "nav", "figure",
}


class _Extractor(HTMLParser):
    def __init__(self, base_url: str, markdown: bool = False):
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.markdown = markdown
        self.parts: List[str] = []
        self.links: List[Dict[str, str]] = []
        self.title: str = ""
        self.description: str = ""
        self._skip_depth = 0
        self._in_title = False
        self._link_text: List[str] = []
        self._link_href: Optional[str] = None

    def handle_starttag(self, tag, attrs):
        attrs_d = {k.lower(): (v or "") for k, v in attrs}
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if tag == "title":
            self._in_title = True
        elif tag == "meta":
            name = (attrs_d.get("name") or attrs_d.get("property") or "").lower()
            if name in ("description", "og:description") and not self.description:
                self.description = attrs_d.get("content", "").strip()
        elif tag == "a":
            href = attrs_d.get("href", "").strip()
            if href and not href.startswith(("javascript:", "#")):
                self._link_href = urljoin(self.base_url, href)
                self._link_text = []
        elif tag in ("br",):
            self.parts.append("\n")
        elif tag == "li":
            self.parts.append("\n- " if self.markdown else "\n• ")
        elif re.fullmatch(r"h[1-6]", tag):
            level = int(tag[1])
            self.parts.append("\n\n" + ("#" * level + " " if self.markdown else ""))
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._in_title = False
        elif tag == "a" and self._link_href:
            text = " ".join("".join(self._link_text).split())
            self.links.append({"text": text[:120], "url": self._link_href})
            if self.markdown and text:
                self.parts.append(f"[{text}]({self._link_href})")
            else:
                self.parts.append(text)
            self._link_href = None
            self._link_text = []
        elif tag in _BLOCK_TAGS or re.fullmatch(r"h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip()
            return
        if self._link_href is not None:
            self._link_text.append(data)
            return
        self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = unescape(raw)
        # Collapse runs of whitespace inside lines, and blank lines to two.
        lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in raw.splitlines()]
        out: List[str] = []
        blanks = 0
        for ln in lines:
            if ln:
                out.append(ln)
                blanks = 0
            else:
                blanks += 1
                if blanks == 1 and out:
                    out.append("")
        return "\n".join(out).strip()


def extract(result: FetchResult, mode: str = "text") -> Dict[str, Any]:
    """Turn a response into something worth putting in a context window."""
    content_type = result.headers.get("content-type", "")
    is_html = "html" in content_type or result.body.lstrip()[:200].lower().startswith(
        ("<!doctype html", "<html")
    )

    if mode == "raw" or not is_html:
        return {"content": result.body, "kind": "raw" if not is_html else "html"}

    parser = _Extractor(result.final_url, markdown=(mode == "markdown"))
    try:
        parser.feed(result.body)
        parser.close()
    except Exception:
        pass  # malformed HTML — keep whatever was parsed so far

    if mode == "links":
        seen, links = set(), []
        for link in parser.links:
            if link["url"] not in seen:
                seen.add(link["url"])
                links.append(link)
        return {"title": parser.title, "links": links[:200], "kind": "links"}

    return {
        "title": parser.title,
        "description": parser.description,
        "content": parser.text(),
        "link_count": len(parser.links),
        "kind": mode,
    }

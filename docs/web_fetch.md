# `web_fetch.py` — the FetchUrl engine

`g023_code/web_fetch.py` makes HTTP look like a real browser and turns
HTML into the clean text the model sees. It backs the FetchUrl tool
(`tools/registry._exec_fetch_url`) and `/fetch`.

## Browser simulation

- `BrowserProfile` — curated header sets (Chrome-likes) with `sec-ch-ua`
  platform variants; `get_profile(key)` picks one.
- `build_headers(url, profile, referer, include_host)` — one consistent
  header block: user-agent, Accept, language, `sec-fetch-*`
  (`_sec_fetch_site`), `Accept-Encoding` (`_accept_encoding`: gzip,
  br, zstd — only what the installed codecs can decode), host header.
  `_registrable(host)` gives the eTLD+1 for cookie scoping.
- `CookieStore` — `.g023/cookies.json` persistence (`for_url`, `update`,
  `clear`) so sessions survive restarts; used when `use_cookies=True`.
- `_pace(url)` — a small politeness delay between hits to the same host.

## Fetching

- `fetch(url, profile_key, referer, timeout, retries, use_cookies)` →
  `FetchResult` (status, final_url, headers, body, engine, profile,
  latency) or raises `FetchError`.
- Two engines behind the scenes:
  - `_fetch_curl_cffi` — **primary**; curl_cffi impersonates Chrome's
    TLS/JA3 fingerprint (the important one for anti-bot sites).
  - `_fetch_httpx` — fallback; plain httpx with optional HTTP/2 (`h2`).
- `_retry_after(headers)` — honour `Retry-After` on 429.
- `engine_name()` / `fidelity_report()` — which engine ran and what
  fingerprinting features are installed (the `/fetch status` screen;
  missing optional deps are reported, not fatal).

## Extraction

- `extract(result, mode)` — mode selects text, markdown, links, or raw
  from the cached body (re-fetch is avoided when the body is cached).
- `_Extractor` — a `HTMLParser` subclass that walks the DOM and produces
  readable text: skips script/style, inserts spacing at block boundaries,
  unescapes entities (`html.unescape`), resolves relative URLs
  (`urljoin`).

## Caching contract

`fetch` stores into `cache.put_web(...)`; `extract` prefers the cached
body. The cache stores **everything and never ages out** — see
`cache.md`: freshness is the caller's decision (the CLI asks the user
before a fresh fetch).

## Related

- `docs/tools.md` — `_exec_fetch_url` and the permission prompt wrapper.
- `docs/cache.md` — `web_cache` table.
- `docs/cli.md` — `/fetch` status/list/clear screens.
- `requirements.txt` — the optional deps (curl_cffi, h2, brotli,
  zstandard) this module degrades without.

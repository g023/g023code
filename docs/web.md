# web.py — machine-wide web search and page cache

`g023_code/web.py` is the only way g023 reaches the web. It wraps DeepSeek's
**Search API** (`POST /v1/tools/search`) and **Fetch API** (`POST /v1/tools/fetch`)
behind one SQLite store that every g023 instance on the machine shares. Version 1.5.0
replaced two older paths:
- the server-side `web_search` tool, whose results were opaque, uncacheable, and
  replayed at about 2.2k tokens per turn;
- the local browser engine (`web_fetch.py`, curl_cffi).

## Why

| Fact (measured live against the API) | Consequence |
|---|---|
| Each Search or Fetch request costs $0.75 / 1k, whatever `count` is | Pay once per machine; ask for 10 hits even when showing 5 |
| `need_content=true` returns every hit's full Markdown, byte-identical to Fetch (44k chars verified) | One search fee also pre-fills the page cache for every hit (**search-primed fetch**) |
| A server-side search cost $0.001398 per step and replayed about 2.2k opaque tokens every turn | Client tool: a compact 5-hit render of about 1.2k tokens, cacheable |
| FetchUrl output used to be vaulted to 1.2k chars | Pages render themselves within a bound (outline, char window, BM25 focus), so they are never vaulted |
| Fetch and Search `need_content` cut some sources at exactly 20,480 UTF-8 bytes, mid-sentence, with no flag | Detect the exact size; recover once from a plain-text twin; otherwise say `"complete": false` |

## Store

`WebStore(path=None)` defaults to `get_global_cache_dir()/web.db`, which is
`$G023_GLOBAL_CACHE_DIR` or `<install>/global_cache/web.db`. The store uses WAL
mode and a 30 s busy timeout, so many processes can share it safely.

| table | key | holds |
|---|---|---|
| `search` | `canonical_query` | query, results JSON (no page bodies), `requested` count, times, hits |
| `page` | `canonical_url` | title, zlib body, chars, status (200 or a refusal), error, source `fetch\|search\|import`, times, hits |
| `ledger` | (day, kind) | `network` (paid), `cached`, `primed` serves, which `/cache` turns into $ saved |

Methods:
- `get_search` / `put_search` / `similar_search`
- `get_page` / `put_page(keep_newer=)` / `has_page`
- `bump`, `ledger`, `stats`, `list_pages`, `list_searches`
- `clear`, `prune(90d unused)`, `import_legacy(cache.db)`

`get_store()` is the process singleton. On first use per project it moves that
project's old `.g023/cache.db` `web_cache` Markdown rows into the store, then
drops the table.

## Keys and freshness

- `canonical_query`:
  - NFKC normalisation, lower case, de-duplicated and sorted terms, with trailing `?!.,;` stripped.
  - `"phrases"` and operators stay atomic.
  - `OR` queries keep their term order.
- `canonical_url`:
  - Scheme and host are lower-cased; default ports and the fragment are dropped.
  - Tracking parameters (`utm_*`, `fbclid`, `gclid`, …) are removed.
  - A bare host gets `https://`.
- `ttl_for_query`:
  - **6 h** when the query looks volatile (latest/news/price/release/version/status…, `after:`, or a year ≥ this year).
  - Otherwise **7 days**; empty results keep for 1 h.
- `ttl_for_url`:
  - **1 h** for volatile URLs (news, status pages, feeds…).
  - **30 days** for pinned URLs (commit SHAs, arXiv `vN`, DOIs, PDFs, versioned docs paths, archive snapshots).
  - Otherwise **1 day**. Refusals (403/404/empty) are negatively cached for **1 h**.

## Near-duplicate query reuse

Agents rarely repeat a query verbatim. They reword it ("cancel others"
becomes "other tasks cancelled docs.python.org"). `query_similarity`
compares two queries by the **Jaccard similarity of stemmed terms**:
- Dotted and underscored names are split (`asyncio.TaskGroup` gives `asyncio` and `taskgroup`).
- A few synonyms are mapped (postgresql→postgres, py→python…).
- Doc words (docs, documentation, official…) and stopwords are dropped.
- A tiny suffix stripper equates cancel/cancelled and lifetime/lifetimes.

Rules:
- **Scope operators must match exactly**: `site:`, `-term`, `lang:`, `after:`/`before:`. `OR` queries never match.
- **Numbers** (versions, years, ids) veto a match only when *both* queries carry them and they differ.
- **Quoted phrases** count as their words.
- **Match**: Jaccard ≥ 0.75, or ≥ 0.6 when one query nearly contains the other (overlap ≥ 0.85).

When a near-duplicate is still fresh and has enough hits, it is served. The
header note names the original query, so the model can pass `fresh=true` if
the reuse does not fit.

**Why the threshold can be loose:** a wrong reuse costs about 1.5k junk tokens
plus one step (~$0.0003 off-peak). A right one saves the $0.00075 fee. Reuse
therefore pays whenever it helps more than about 30% of the time.

Replayed against the three live cold/warm agent runs, the matcher reused 3 of
the 5 warm-turn searches (exact keys reused 0). The other two added real
constraints such as `"first exception"`.

## search(query, count=5, *, fresh=False, store=None, transport=None) → SearchOutcome

Steps:
1. Validate the query (1–128 chars).
2. On an exact-key hit that is fresh and has enough results (count met, or
   the API had nothing more), serve `source="cache"`.
3. Otherwise try a near-duplicate.
4. Otherwise make one paid request with `count=max(count, 10)` and `need_content=true`:
   - De-dupe hits by canonical URL.
   - File each hit's content into `page` (`source="search"`), without replacing
     a good page that is newer or still within its TTL.
   - Bill only when there are results.
   - On network failure, serve a stale copy if one exists.

`render_search(out, count)` produces:
- One JSON header line: `{query, results, source, more?, note?}`.
- Then per hit: `[i] title`, then `url · site · date · page cached (Nk chars)`, then an abstract of at most 700 chars.

## fetch(url, *, mode="auto"|"fresh"|"cache", max_age=None, …) → Page

Rejects:
- non-http(s) URLs and URLs without a host;
- credentials in the URL (checked on the *raw* URL before canonicalising);
- URLs longer than 8192 chars.

Serving order:
1. A good cached page within its TTL (or `max_age`): `source="cache"`, or `"primed"` when a search filed it.
2. Otherwise a fresh negative entry raises `WebError(cached=True)`.
3. Otherwise one paid Fetch request.

Failure handling:
- A 403/404 or empty answer is negatively cached, unless a good copy exists. A refusal never overwrites a good page.
- If a good copy exists, the fetch falls back to it as `source="stale"` with a note.

## Service cut-off (`SOURCE_CAP_BYTES = 20_480`)

Some sources come back from both APIs cut at exactly 20,480 UTF-8 bytes. Seen
on a docs site's HTML page, GitHub blob pages and a blog; an arXiv page of
58,541 bytes and the docs site's own `.md` copy (31,050 bytes) came back whole.
The service gives no sign of the cut, so a 20 KB page looked complete.

- `source_truncated(body)` — true when the body is exactly the cap in UTF-8 bytes.
- `plain_twins(url)` — `github.com/o/r/blob/<ref>/<path>` → `raw.githubusercontent.com/o/r/<ref>/<path>`;
  an extension-less path with no query and no trailing slash → `<url>.md`. Nothing else is guessed.
- `same_document(cut, alt)` — checks that the twin is the same document: 8 anchor
  probes, 40 chars each, taken from the cut copy with markup and link targets
  ignored. The twin must also be at least as long as the cut copy. A 404 page or
  another page never replaces the cut copy.
- `fetch` → `_recover_cut(page)` makes **one** twin request. The twin's verdict is
  cached under the twin's own key (403/404 refusals included), so later fetches
  never spend on it again. A recovered body is stored under the original URL
  with the note "the full text came from …". `mode="cache"` never spends on a twin.
- A page that stays cut: `Page.cut` is true, `render_page` adds `"complete": false`
  and `CUT_NOTE`, and WebSearch shows `page cached (N chars, cut at 20 KB)`.
  A search-primed cut page sends the next FetchUrl straight to the twin, because
  re-fetching the original would return the same cut copy.

Live check: a Claude docs page went from 20,336 to 30,890 chars (its focus
section "Safeguard refusals" became findable). A GitHub blob went from 20,480 to
35,037 bytes. A blog with no twin is flagged `complete: false`. Tests:
`tests/test_web_cut.py`.

## render_page(page, *, focus="", offset=0, max_chars=8000)

The output is a JSON header line, then `-----`, then the text.

| page | shown |
|---|---|
| ≤ 8k chars | whole (`shown: "all"`) |
| `focus="topic"` | top 3 sections by BM25 (headings weigh 3x), cut to `max_chars` and joined with `[…]`, plus `ranked` and the outline |
| otherwise | a window of `max_chars` from `offset` (ending on a line break), plus `next_offset` |

The window from 0 also carries the outline (`@offset heading (size)`, up to 40 lines).

`sections(body)` splits on Markdown headings. Pages without headings are cut
into paragraph chunks of about 2k chars.

Live: PEP 654 (54k chars) with `focus="except* syntax ExceptionGroup split"`
rendered 10k chars covering the two right sections.

## Tools, CLI, UI

- **`WebSearch`** (SAFE) and **`FetchUrl`** (follows `permission_default`; ASK by default) run through
  `tools/registry._exec_web_search` / `_exec_fetch_url`.
- **`confirm_fetch`** offers the cached copy when one exists.
- **Web tools are not in `VAULT_TOOLS`**, because their output is already bounded.
- **`/search [fresh] <query>`**, **`/fetch <url>`**.
- **`/cache`** shows the web store and the paid/cached/primed counts and $ saved.
  `/cache web` lists entries; `/cache web clear` and `/cache web prune` maintain the store.
  `/cache clear` leaves the web store alone.
- **`ui.describe_result`** reads the header line, for example `5 results · cache` or `44,467 chars · focus · primed`.

## Tests

`tests/test_web.py` uses a fake transport, so no network is needed. It covers:
- keys and TTLs;
- pay-once and primed fetch;
- near-duplicate reuse and the similarity rules;
- billing only on non-empty results;
- the refetch triggers (`fresh`, bigger counts) and treating exhausted results as enough;
- stale fallback;
- negative cache, cache-only mode, and refusals that keep the good copy;
- credential rejection and prefetch that does not churn;
- rendering (whole, window, focus, chunking);
- two handles on one DB, the global dir, clear/prune/stats, and the legacy import;
- the executors, VAULT/SAFE membership, and UI summaries.

Live checks (development probes against the real API):
- a scripted run of 5 search/fetch operations cost 2 paid requests;
- a warm agent turn that read pages its searches had returned paid 0 fetch fees.

## Single flight

`search` and `fetch` take a per-key lock (`search:<canonical query>`,
`fetch:<canonical url>`) before checking the cache. Identical calls in one
parallel batch therefore make one paid request; the others read its cache row.
Each entry is reference-counted by its holders and waiters and removed when the last one leaves, so the table is bounded by in-flight calls and a lock already handed out can never be evicted (the old idle-prune could, letting a second caller pay twice).

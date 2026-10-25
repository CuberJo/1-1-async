# Algorithm

## Day 1 — basic asynchronous HTTP client

### Components

- `AsyncCrawler` ([src/crawler.py](../src/crawler.py)) — the page fetcher.
- `FetchResult` ([src/models.py](../src/models.py)) — result of fetching one URL: content, HTTP status, error, elapsed time.
- [src/utils.py](../src/utils.py) — logging setup and the results table.

### Fetching one URL (`fetch_url`)

1. On the first request an `aiohttp.ClientSession` is created lazily (inside the running event loop) with:
   - `TCPConnector(limit=max_concurrent)` — a connection pool; keep-alive connections are reused across requests to the same host;
   - `ClientTimeout(connect, sock_read, total)` — separate timeouts for connecting, reading and the whole request;
   - `raise_for_status=True` — 4xx/5xx responses become `ClientResponseError`.
2. The request runs under `asyncio.Semaphore(max_concurrent)`: at most `max_concurrent` requests are in flight at once.
3. Exceptions are caught and stored in `FetchResult.error`, so the program never crashes:
   - `ClientResponseError` → `HTTP <code> <reason>` (caught first, since it subclasses `ClientError`);
   - `asyncio.TimeoutError` → `Timeout`;
   - `ClientError` → `<type>: <message>` (DNS failure, connection refused, etc.).
4. The result is saved to `crawler.results[url]`; the method returns the page text, or `""` on error.

### Concurrent fetching (`fetch_urls`)

A coroutine is created for every URL and all of them run via `asyncio.gather`. The semaphore caps concurrency, so the total time is roughly the time of the slowest request (when `len(urls) <= max_concurrent`) rather than the sum of all times, as with sequential fetching. It returns a `{url: html}` dict for successful fetches only; details for every URL are in `crawler.results`.

### aiohttp 3.14 quirk

If a keep-alive connection sits idle in the pool longer than `sock_read`, the next request on it fails instantly with `SocketTimeoutError`. That is why the connector's `keepalive_timeout` is set to `0.8 * sock_read`: the connection is closed before it goes stale.

## Day 2 — HTML parsing

### Components

- `HTMLParser` ([src/html_parser.py](../src/html_parser.py)) — extracts data from HTML.
- `normalize_url` — turns an `href` into an absolute URL or rejects it.
- `AsyncCrawler.fetch_and_parse` ([src/crawler.py](../src/crawler.py)) — fetch + parse.
- `summarize_page`, `print_parse_stats` ([src/utils.py](../src/utils.py)) — short summary and statistics table.

### Parsing a page (`parse_html`)

1. Parsing is CPU-bound, so `parse_html` runs it via `asyncio.to_thread` and the event loop keeps serving other requests.
2. Empty input returns an empty result with `errors: ["empty html"]`.
3. BeautifulSoup with `lxml` builds the tree. lxml repairs broken markup the way a browser does (unclosed tags get closed, misnested ones are reordered), so invalid HTML still parses. If lxml is not installed, it falls back to the built-in `html.parser` with a warning.
4. If the page has `<base href>`, relative links resolve against it instead of the page URL.
5. Every extractor runs in its own `try/except`. A failure is logged as a warning and recorded in `errors` as `"<field>: <exception>"`; the field keeps its empty value and the rest are still extracted — a partial result instead of a crash.
6. A missing `<title>` is logged as a warning.

### Links (`extract_links`, `normalize_url`)

For every `<a href>`:

1. Skip empty values, `#anchors` and the `javascript:`, `mailto:`, `tel:`, `data:`, `ftp:`, `file:` schemes.
2. `urljoin(base_url, href)` makes the URL absolute: `/a`, `a.html`, `../a`, `?q=1`, `//host/a` all resolve correctly.
3. `urldefrag` drops the `#fragment` — it points to the same page.
4. Validation: the scheme is `http`/`https`, there is a host, the port is a valid number.
5. With `same_domain_only=True`, links to other hosts are dropped.
6. Duplicates are removed while document order is kept.

Image `src` (or `data-src` for lazy loading) goes through the same `normalize_url`.

### Text and structured data

- `extract_text` takes the `<body>` (or the elements matching a CSS selector), drops `script`, `style`, `noscript`, `template`, `svg` and collapses whitespace. It works on a copy, so the shared soup is not modified and extractor order doesn't matter.
- `extract_metadata` — `<title>`, `meta description`, `meta keywords` (split by commas into a list), `og:*`, `<html lang>`, `<link rel=canonical>`.
- `extract_headings` — h1–h3 in document order as `{"level", "text"}`.
- `extract_tables` — a table's own rows (nested tables are collected separately); the first row made of `<th>` only becomes `headers`.
- `extract_lists` — `<ul>`/`<ol>` with the items' own text; nested lists are returned as separate entries.

### `fetch_and_parse`

1. The page is fetched via the day 1 `_fetch`; `FetchResult.final_url` now stores the address after redirects.
2. On a fetch error it returns an empty result with `errors: ["fetch: <reason>"]`.
3. Otherwise the HTML is parsed against `final_url` — after a redirect, relative links resolve against the page they actually come from — and `url` is set back to the requested address.

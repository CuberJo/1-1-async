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

## Day 3 — crawl queue and concurrency control

### Components

- `CrawlerQueue` ([src/crawler_queue.py](../src/crawler_queue.py)) — the URL queue and per-URL state.
- `SemaphoreManager` ([src/semaphore_manager.py](../src/semaphore_manager.py)) — global and per-domain limits.
- `URLFilter`, `canonicalize` ([src/url_filter.py](../src/url_filter.py)) — which links to follow and how to compare them.
- `AsyncCrawler.crawl` ([src/crawler.py](../src/crawler.py)) — workers that drive the crawl.
- [src/crawl_demo.py](../src/crawl_demo.py) — the demo.

### Queue (`CrawlerQueue`)

- A `heapq` of `(-priority, counter, url)`: higher priority pops first, the counter keeps FIFO order among equal priorities.
- `_seen` holds every URL ever added, so a URL that is queued, in progress, processed or failed is never added again.
- A URL moves queued → `in_progress` → `processed` | `failed`.
- `get_next()` returns the next URL. If the heap is empty but some URLs are still in progress, it waits on an `asyncio.Event` (set by `add_url`, `mark_processed`, `mark_failed`): those pages may still add links. When both are empty, the crawl is over and it returns `None`. After `close()` it returns `None` at once.

### Concurrency (`SemaphoreManager`)

`acquire(url)` is an async context manager that takes the domain's semaphore (`max_per_domain`, created on first use) and then the global one (`max_concurrent`). The domain slot comes first: a request waiting for a busy domain must not hold a global slot that a request to another domain could use. Inside, it counts active requests per domain and records peaks. `_fetch` now runs under `acquire(url)` instead of the day 1 semaphore. `www.` is ignored when comparing domains.

### Crawl (`crawl`)

1. State is reset: a new queue, `visited_urls`, `processed_urls`, `failed_urls`.
2. Start URLs are validated with `normalize_url`, canonicalized and queued with depth 0. Their hosts become the allowed domains for `same_domain_only`. Start URLs skip `include/exclude` — they are what the user asked for.
3. `max_concurrent` workers run in parallel. Each one loops:
   1. `get_next()`; `None` → the worker exits.
   2. Count the page against `max_pages` (failed pages count too); when the limit is reached, `queue.close()` stops the others from taking more. The remaining URLs stay in the queue.
   3. Add the URL to `visited_urls`, run `fetch_and_parse`.
   4. Fetch error → `failed_urls[url] = error`, `mark_failed`. Any unexpected exception is handled the same way, so one bad page never stops the crawl.
   5. Success → `processed_urls[url] = page` with `page["depth"]`. After a redirect the target URL is also marked visited/seen, so it isn't fetched a second time.
   6. Every link is canonicalized and checked: rejected by `URLFilter` → skipped; `depth >= max_depth` → skipped; otherwise queued with `depth + 1` and priority `-(depth + 1)`, so shallower pages go first (breadth-first order).
   7. `mark_processed`. Links are queued **before** this call: while the page is in progress, idle workers wait in `get_next()` instead of deciding the crawl is over.
4. A progress task prints a line every `progress_interval` seconds. On an error or cancellation all tasks are cancelled and awaited, so nothing outlives `crawl()`.

### Deduplication (`canonicalize`)

The parser already makes links absolute and drops the `#fragment`. `canonicalize` additionally lowercases the scheme and host, drops the default port and turns an empty path into `/`, so `https://Example.com`, `https://example.com/` and `https://example.com:443/#top` are the same queue entry. Query strings and path case are kept: they can point to different pages.

### Filters (`URLFilter.is_allowed`)

In order: only `http/https` with a host; no binary file extensions (images, PDF, archives, media, CSS/JS...); `same_domain_only` → the host must be one of the start hosts; no `exclude_patterns` match (`re.search`); if `include_patterns` are given, at least one must match.

### Statistics

`get_stats()` = queue counters (`queued`, `in_progress`, `processed`, `failed`, `seen`) + `visited`, `skipped` (`filtered`, `too_deep`), `elapsed`, `pages_per_sec` = (processed + failed) / elapsed, and the `SemaphoreManager` counters (active, peaks, total requests).

## Day 4 — rate limiting and politeness

### Components

- `RateLimiter`, `backoff_delay` ([src/rate_limiter.py](../src/rate_limiter.py)) — spacing between requests and retry delays.
- `RobotsParser`, `parse_robots` ([src/robots_parser.py](../src/robots_parser.py)) — robots.txt loading, parsing and checks.
- `AsyncCrawler` ([src/crawler.py](../src/crawler.py)) — applies both before every request, rotates User-Agents, retries temporary errors.
- [src/polite_demo.py](../src/polite_demo.py) — the demo.

### Rate limiting (`RateLimiter.acquire`)

The limiter hands out start times ("slots"). `_next[key]` is the earliest time the next request with that key may start. The key is the domain, or one shared key when `per_domain=False`.

1. `start = max(now, _next[key])`, then `_next[key] = start + interval`, where `interval = max(1 / requests_per_second, min_delay) + uniform(0, jitter)`.
2. If the domain has a `Crawl-delay`, the domain's own slot is taken into account too, with `max(interval, crawl_delay)`. In global mode a slow site therefore doesn't slow down the others.
3. The caller sleeps `start - now`.

There is no `await` between reading and updating `_next`, so concurrent callers can't take the same slot. They are served in arrival order, one interval apart. N requests to one domain take `(N - 1) * interval`. Different domains don't wait for each other.

`acquire` is called in `_fetch_once` **inside** the semaphores, right before the request. A task waiting for its slot does hold a concurrency slot. In exchange, nothing can delay the request after its slot comes up. If the limiter ran before the semaphores instead, requests that had waited for a free slot would start together, in a burst.

Statistics: each request records its start time. `current_rps` = (n − 1) / span over the requests of the last `window` seconds. `avg_gap` / `min_gap` are measured between consecutive requests to the same domain. `avg_wait` is the average sleep. `history` keeps the last 1000 requests for reports. `crawl()` resets the statistics but keeps the slots, so a new crawl stays polite to sites it has just visited.

### robots.txt (`RobotsParser`)

Parsing (`parse_robots`):
- `#` comments and lines without `:` are ignored; field names are case-insensitive.
- `User-agent` lines in a row form one group. A `User-agent` line after rules starts a new group. Rules before the first `User-agent` are ignored.
- `Allow` / `Disallow` patterns become regexes: `*` is any sequence, a trailing `$` anchors the end, otherwise it's a prefix match. An empty `Disallow` means no rule. Percent-encoding is decoded in both the pattern and the path.
- `Crawl-delay` belongs to the group, `Sitemap` to the whole file.

Matching (`can_fetch`):
1. The group is chosen by our User-Agent's product token (`MyBot/1.0 (...)` → `mybot`). The longest group name contained in it wins, otherwise `*`. Groups with the same name are combined. A specific group fully replaces `*`.
2. Among the group's rules that match the path (with the query string), the longest pattern wins. On a tie `Allow` wins. No match → allowed. `/robots.txt` itself is always allowed.

Loading (`fetch_robots`): one download per origin (scheme + host + port), cached. An `asyncio.Lock` per origin makes concurrent callers wait for one shared download. The body is read up to 500 KiB. 2xx → rules from the file; 4xx → everything allowed; 429/5xx → everything disallowed (RFC 9309: the site is unavailable); a network error → everything allowed, and the page request reports the real error.

### Crawler integration

- `_fetch(url)`: with `respect_robots`, `_robots_allowed` first loads/reads the cached rules and passes `Crawl-delay` to `RateLimiter.set_domain_delay`. If the URL is disallowed, it's added to `blocked_urls`, logged once as a warning, and a `FetchResult(blocked=True, error="Blocked by robots.txt")` is returned without a request.
- In `crawl()` the worker checks robots.txt **before** counting a page against `max_pages`. Blocked URLs go to the new `blocked` queue state, not to `failed`.
- The User-Agent is set per request: `next()` on a cycle over `user_agent` (a string or a list). robots.txt rules are always checked for the first one, so the decisions don't change between requests.

### Retries with exponential backoff

`_fetch` calls `_fetch_once` up to `max_retries + 1` times. Errors are retried only if they are temporary: timeouts, HTTP 429/500/502/503/504, `ServerDisconnectedError`, `ClientPayloadError`. A 404 or a DNS error fails at once. The delay before retry `n` (from 0) is `min(backoff_max, max(backoff_base * 2**n + uniform(0, jitter), Retry-After))`. The sleep happens outside the semaphores, so a backing-off task holds no slot. Each retry also goes through the rate limiter. `FetchResult.attempts` and `crawler.retries` count the attempts. Since day 5 this loop lives in `RetryStrategy` (below); the day 4 options build one.

## Day 5 — error handling and retries

### Components

- `CrawlerError` and its subclasses, `classify_exception` ([src/errors.py](../src/errors.py)).
- `RetryStrategy`, `ErrorPolicy`, `RetryStats` ([src/retry_strategy.py](../src/retry_strategy.py)).
- `CircuitBreaker` ([src/circuit_breaker.py](../src/circuit_breaker.py)).
- `AsyncCrawler` ([src/crawler.py](../src/crawler.py)) — runs every fetch through the strategy.
- [src/retry_demo.py](../src/retry_demo.py) — the demo.

### Classification (`classify_exception`)

Checked in this order, because some aiohttp exceptions belong to several families:

1. Already a `CrawlerError` → as is. `CancelledError` → `None` (it must propagate).
2. `TooManyRedirects` (subclasses `ClientResponseError`) → `PermanentError`.
3. `ClientResponseError` → by status: 408, 425, 429 and every 5xx → `TransientError`, other 4xx → `PermanentError`. `Retry-After` (seconds) is kept.
4. `asyncio.TimeoutError` (`ServerTimeoutError` is a `ClientError` too) → `TransientError("Timeout")`.
5. `ClientSSLError`, `InvalidURL` → `PermanentError`: they won't fix themselves.
6. Other `ClientError`, `ConnectionError`, `OSError` → `NetworkError` (refused, reset, DNS, a cut-off body).
7. `UnicodeDecodeError` → `ParseError`.
8. Anything else → `None`: a bug, re-raised unchanged and never retried.

The messages are the ones of day 1 (`HTTP 404 Not Found`, `Timeout`, `ClientConnectorError: ...`), so `failed_urls` reads the same.

### `RetryStrategy.execute_with_retry(coro_func, *args, **kwargs)`

It takes a coroutine *function* and its arguments: a coroutine object can be awaited only once, so it is rejected with `TypeError`.

For attempt `a = 1, 2, ...`:

1. Await `coro_func(...)`. Success → count it (and as a "saved" fetch if `a > 1`) and return.
2. Classify the exception; `None` → re-raise.
3. Look up the `ErrorPolicy`: by HTTP status first, then by the error's class and its bases. The retry limit for this error is `min(policy.max_retries, max_retries)`.
4. If the error is one of `retry_on` (the classified error or its original exception, so raw types like `asyncio.TimeoutError` work too) and `a <= limit`: delay = `backoff_delay(a - 1, base_delay * policy.delay_multiplier, max_delay, jitter, retry_after, factor=policy.backoff_factor or backoff_factor)`, log a warning, sleep, repeat.
5. Otherwise record the final failure (and the URL in `permanent_errors` for a `PermanentError`), set `error.attempts` and raise it.

Default policies: `NetworkError` — at most 2 retries, delay ×2 (a network problem takes longer to clear and rarely does after a few tries); 429 — delay ×4 on top of `Retry-After`; 500 — 1 retry (often a bug rather than overload).

`RetryStats`: `errors_by_type` / `errors_by_status` count every failed attempt, `history` keeps the last 1000 failed attempts (URL, attempt, type, status, message, duration, delay before the next one). `retry_time` sums, for every retry, the backoff sleep plus the repeated attempt; `avg_retry_time = retry_time / retries`.

### Crawler integration

- `_fetch(url)` creates one `FetchResult` and passes the strategy a closure that increments `result.attempts` and calls `_fetch_once(result)`. A final `CrawlerError` fills `error`, `error_type`, `status`, `retryable`, `retry_after`. The sleeps happen in the strategy, outside the semaphores.
- `_fetch_once` passes `ClientTimeout` per request: `connect`, `sock_read` and `total` are multiplied by `timeout_growth ** (attempt - 1)`, so a slow server gets more time on each retry. Exceptions are classified there and re-raised.
- `crawl()` resets the strategy's statistics together with the other counters; `crawler.retries` reads them.

### Circuit breaker

State per domain: `closed` → `open` → `half_open` → `closed` / `open`.

- `closed`: outcomes of the last `window` requests are kept in a deque. When at least `min_requests` are known and `failures / known >= failure_rate`, the circuit opens.
- `open`: `allow_request` refuses; `_fetch_once` raises `CircuitOpenError` without a request. It is not in `retry_on`, so the fetch fails at once instead of sleeping on a dead site.
- After `recovery_timeout` the state reads as `half_open`: exactly one trial request passes (`probing` flag), the others are still refused. Success → `closed` with a clean window; failure → `open` for another `recovery_timeout`.

Only `TransientError` and `NetworkError` count as failures: a 404 or a parse error means the server answered. The check runs inside the semaphores, after the rate limiter, right before the request: while a task waited for its slot, the others may have found the domain down.

## Day 6 — saving data

### Components

- `DataStorage` ([src/storage.py](../src/storage.py)) — base class: buffer, batches, retries, statistics.
- `JSONStorage`, `CSVStorage` — files through `aiofiles`, built on `FileStorage`.
- `SQLiteStorage` — `aiosqlite` (a connection in its own thread).
- `MultiStorage` — the same records to several storages.

### The record

`make_record(page, fetch)` takes the standard fields from the parsed page (`url`, `title`, `text`, `links`, `metadata`) and from `FetchResult` (`status_code`, `content_type`). `crawled_at` is the current UTC time. `normalize_record` runs on every `save()`: it fills in missing fields, converts `status_code` to int and an ISO string `crawled_at` to a datetime, and keeps extra keys after the standard ones. A record without a url is rejected. `decode_record` reverses what a format did to the record (JSON strings back to lists and dicts, ISO strings back to datetime, empty CSV cells back to `None`). So `load()` returns the same records from every format, and the demo and tests compare them directly.

### `save` → `flush` → `_write_with_retry`

1. `save(data)`: normalize. An invalid record, or a closed storage, is counted as failed, logged, and `save` returns `False`. Otherwise the record is appended to the buffer.
2. When the buffer reaches `batch_size`, `flush()` runs: under an `asyncio.Lock` it takes the whole buffer and writes it. Records saved during the write go into the next round of the same `flush`. Workers whose save fills the buffer wait for the lock, which gives backpressure: the buffer can't grow without bound when writes are slow.
3. `_write_with_retry(batch)`: call `_write_batch`. On any `Exception`, sleep `retry_delay * 2**attempt` and repeat, up to `max_retries` times. Then count the batch as failed and log its first URLs. Nothing is raised.

### Retries without duplicates

A retry gets the same batch, so a write must be repeatable:

- Files: `FileStorage` keeps one `r+b`/`w+b` handle and an end offset `_end`. A batch is `seek(_end)`, `write(data + tail)`, `truncate()`, `flush()`, and only then is `_end += len(data)`. If a write fails halfway, the retry starts at the same offset and overwrites the partial data.
- JSON array: the file is always `[` + elements + `\n]\n`. The closing bracket is the `tail`, and the next batch overwrites it starting with `,\n`. That is why the file is valid JSON after every batch. With `append=True` the bracket is found in the last 4 KB of the file.
- JSON Lines: one `json.dumps` per line (`ensure_ascii=False`). When appending to a file whose last line was cut off, a newline is added first.
- CSV: rows are written by `csv.DictWriter` into a `StringIO` and then encoded with `str.encode(encoding, errors)`. Codecs with a BOM (`utf-8-sig`, `utf-16`) add it on every `encode`, so it is stripped from any batch that doesn't start at offset 0. The header is written when `_end == 0`. The column list comes from `fieldnames`, then from the first line of an appended file, then from the keys of the first record.
- SQLite: `executemany(UPSERT)` + `commit()` is one transaction; on an error, `rollback()`. A retry finds no rows from the failed attempt, and `ON CONFLICT(url) DO UPDATE` covers the rest.

### SQLite schema

`pages(id, url UNIQUE, domain, title, text, links JSON, metadata JSON, crawled_at ISO UTC, status_code, content_type)` with indexes on `domain`, `status_code`, `crawled_at` and `title`. `crawled_at` is converted to UTC before it is written, so text comparison matches time order. `find()` builds `WHERE` from parameters only (`LIKE` with `%` and `_` escaped). `summary()` uses `json_array_length(links)`. The connection runs with `journal_mode=WAL` and `synchronous=NORMAL`.

### Crawler integration

- `_worker` calls `storage.save(make_record(page, fetch))` after `mark_processed`, for successful pages only.
- `crawl()` calls `storage.flush()` in its `finally`, so the data is on disk when `crawl()` returns. `close()` closes the storage.
- Both calls go through `_call_storage`, which catches and counts exceptions (`storage_errors`). That matters only for a storage that breaks the "never raise" contract.

### Performance

Batches turn N writes into N / batch_size: one `write` + `flush` per batch for files, one transaction instead of N for SQLite. File I/O runs in the `aiofiles` thread pool and SQLite in the `aiosqlite` thread, so the event loop keeps fetching pages during writes. The demo's benchmark compares `batch_size=1` with `batch_size=100` (on the author's machine: JSON Lines ×13, CSV ×9, SQLite ×4).


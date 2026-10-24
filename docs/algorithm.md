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

# Async Crawler

An asynchronous web crawler built on `aiohttp`, developed day by day.

## Day 1 — basic asynchronous HTTP client

The `AsyncCrawler` class:

- `fetch_url(url)` — fetch a single page;
- `fetch_urls(urls)` — fetch a list of URLs concurrently, limited by `max_concurrent`;
- `close()` — close the session (`async with` is supported too).

Errors (HTTP 4xx/5xx, timeouts, network errors) are handled without crashing the program and are logged. See [docs/algorithm.md](docs/algorithm.md) for details.

## Day 2 — HTML parsing and data extraction

The `HTMLParser` class (BeautifulSoup + lxml):

- `parse_html(html, url)` — parse a page into a dict (runs in a thread, does not block the event loop);
- `extract_links(soup, base_url)` — unique absolute links; `HTMLParser(same_domain_only=True)` drops external ones;
- `extract_text(soup, selector=None)` — visible text of the page or of a CSS selector;
- `extract_metadata(soup)` — title, description, keywords (+ lang, canonical, `og:*`);
- `extract_images`, `extract_headings` (h1–h3), `extract_tables`, `extract_lists` (ul/ol).

`AsyncCrawler.fetch_and_parse(url)` fetches and parses a page and returns `url`, `title`, `text`, `links`, `metadata` (plus `images`, `headings`, `tables`, `lists`, `errors`). Broken HTML, failing extractors and fetch errors never raise: the result is partial and the reasons are in `errors`.

## Day 3 — concurrency control and the crawl queue

- `CrawlerQueue` — priority queue of URLs: `add_url(url, priority=0)`, `async get_next()`, `mark_processed(url)`, `mark_failed(url, error)`, `get_stats()`. Higher priority comes first, every URL is accepted only once.
- `SemaphoreManager` — a global limit (`max_concurrent`) plus a limit per domain (`max_per_domain`), tracks active and peak requests.
- `URLFilter` — `same_domain_only`, `include_patterns`, `exclude_patterns` (regexes); links to images, PDFs, archives etc. are always skipped.
- `AsyncCrawler.crawl(start_urls, max_pages=100, ...)` — breadth-first crawl with a `max_depth` limit and no duplicates. State is kept in `visited_urls`, `processed_urls` (url → parsed page with `depth`) and `failed_urls` (url → error). Progress (pages, queue, errors, pages/s) is printed every `progress_interval` seconds; `get_stats()` returns the same numbers.

## Day 4 — rate limiting and politeness

- `RateLimiter(requests_per_second=1.0, per_domain=True, min_delay=0.0, jitter=0.0)` — `await acquire(domain)` waits until the next request may start. Each domain has its own limit, or one limit is shared by all requests with `per_domain=False`. The interval is `max(1 / requests_per_second, min_delay)` plus a random `0..jitter` seconds. `set_domain_delay(domain, seconds)` adds a per-domain minimum on top (used for `Crawl-delay`).
- `RobotsParser` — `async fetch_robots(url) -> dict` downloads robots.txt once per site and caches it; `can_fetch(url, user_agent="*")` and `get_crawl_delay(user_agent="*", url=None)` answer from the cache. Supports user-agent groups, `Allow`/`Disallow` with `*` and `$`, longest-match precedence, `Crawl-delay`, `Sitemap`.
- `AsyncCrawler` has new options:
  - `requests_per_second`, `per_domain_rate`, `min_delay`, `jitter` set the rate limit, applied right before every request;
  - `respect_robots=True` checks robots.txt before every request. Disallowed URLs are not requested; they are logged and collected in `blocked_urls`. `Crawl-delay` is passed to the rate limiter;
  - `user_agent` is a string or a list of strings that rotate. robots.txt rules are checked for the first one;
  - `max_retries`, `backoff_base`, `backoff_max` retry timeouts, 429 and 5xx after `base * 2**attempt` seconds, and honour `Retry-After`.

  All new options default to "off", so the crawler behaves as on day 3 unless you set them.
- Monitoring: `get_stats()["rate"]` has `current_rps`, `avg_gap` / `min_gap` (time between requests to one domain), `avg_wait`, `domain_delays`. It also has `robots_blocked` and `retries`. The progress line shows the blocked count, req/s and the average gap.

## Day 5 — error handling and automatic retries

- Error classes ([src/errors.py](src/errors.py)): `TransientError` (timeouts, 408/429/5xx), `PermanentError` (other 4xx, invalid URL, bad certificate, too many redirects), `NetworkError` (connection refused/reset, DNS), `ParseError` (a response that is not valid text). `classify_exception(exc, url)` maps aiohttp and OS exceptions to them; anything else (a bug) is not a request failure and propagates unchanged. Each error keeps `url`, `status`, `retry_after` and the `original` exception.
- `RetryStrategy(max_retries=3, backoff_factor=2.0, retry_on=None, base_delay=0.5, max_delay=30, jitter=0, policies=None, status_policies=None, timeout_growth=1.5)` — `await execute_with_retry(coro_func, *args, **kwargs)`:
  - retries only the `retry_on` types (default: `TransientError`, `NetworkError`), never `PermanentError` / `ParseError` unless you add them;
  - the delay before retry `n` is `base_delay * multiplier * factor**n` (+ jitter), at least `Retry-After`, at most `max_delay`;
  - an `ErrorPolicy(max_retries, delay_multiplier, backoff_factor)` per error type or HTTP status adjusts that: by default `NetworkError` gets at most 2 retries and twice the delay, 429 waits 4× longer, 500 is retried once;
  - every failed attempt is logged (type, URL, attempt n/max, time until the next one), the final result too; `strategy.stats` counts errors by type and status, retries, fetches saved by a retry, the average time per retry, URLs with permanent errors.
- `CircuitBreaker(failure_rate=0.5, min_requests=5, window=10, recovery_timeout=30)` ([src/circuit_breaker.py](src/circuit_breaker.py)) — per domain: when the share of failures among the last `window` requests reaches `failure_rate`, the domain is blocked for `recovery_timeout` seconds, then one trial request decides whether it is back.
- `AsyncCrawler` has new options `retry_strategy` and `circuit_breaker`. Without a strategy it builds one from the day 4 `max_retries` / `backoff_base` / `backoff_max`, so the defaults still don't retry. Repeated attempts get `timeout_growth`× longer timeouts. `FetchResult` has `error_type`; `get_stats()` has `errors` and `circuits`; `get_error_report()` returns stats, failed URLs and the attempts log for saving. `fetch_once(url)` makes one request and raises the classified error — for a strategy of your own.

## Structure

```
├── src/
│   ├── main.py         demo: sequential vs concurrent fetching
│   ├── parse_demo.py   demo: fetching and parsing pages
│   ├── crawl_demo.py   demo: crawling a site with live progress
│   ├── polite_demo.py  demo: robots.txt, rate limiting, delay statistics
│   ├── retry_demo.py   demo: error types, retries, circuit breaker, error report
│   ├── crawler.py      AsyncCrawler class
│   ├── crawler_queue.py     CrawlerQueue class
│   ├── semaphore_manager.py SemaphoreManager class
│   ├── rate_limiter.py RateLimiter, exponential backoff
│   ├── robots_parser.py     RobotsParser, robots.txt parsing
│   ├── errors.py       error classes and classification
│   ├── retry_strategy.py    RetryStrategy, ErrorPolicy, RetryStats
│   ├── circuit_breaker.py   CircuitBreaker
│   ├── url_filter.py   URLFilter, URL canonicalization
│   ├── html_parser.py  HTMLParser class
│   ├── models.py       FetchResult
│   └── utils.py        logging setup and report output
├── tests/
│   ├── test_crawler.py
│   ├── test_parser.py
│   ├── test_queue.py   queue, semaphores, filters
│   ├── test_crawl.py   crawl(): depth, filters, duplicates, stats
│   ├── test_rate_limiter.py  rate limits, delays, jitter, backoff
│   ├── test_robots.py  robots.txt parsing, matching, loading, cache
│   ├── test_polite_crawl.py  crawler + robots.txt, rate limits, User-Agent, retries
│   └── test_retry.py   error classes, retry strategies, backoff, circuit breaker, error stats
└── docs/
    └── algorithm.md
```

## Installation

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS
pip install -r requirements.txt
```

## Usage

```bash
python src/main.py         # day 1 demo (needs internet)
python src/parse_demo.py   # day 2 demo (needs internet)
python src/crawl_demo.py   # day 3 demo (needs internet), saves output/crawl_results.json
python src/crawl_demo.py https://books.toscrape.com --max-pages 30 --max-depth 1
python src/polite_demo.py  # day 4 demo on a local site with robots.txt (no internet needed)
python src/polite_demo.py https://docs.python.org/3/ --max-pages 15 --rps 1
python src/retry_demo.py   # day 5 demo on local sites with errors, saves output/error_report.json
python src/retry_demo.py https://httpbin.org/status/503 https://httpbin.org/status/404 --read-timeout 2
pytest                     # tests (local server, no internet needed)
```

## Example

```python
import asyncio
from crawler import AsyncCrawler

async def main():
    async with AsyncCrawler(max_concurrent=5) as crawler:
        results = await crawler.fetch_urls([
            "https://example.com",
            "https://httpbin.org/delay/1",
        ])
    print(f"Fetched {len(results)} pages")

asyncio.run(main())
```

```python
async def main():
    async with AsyncCrawler() as crawler:
        page = await crawler.fetch_and_parse("https://www.python.org")
    print(page["title"], len(page["links"]), page["metadata"]["description"])
```

```python
async def main():
    async with AsyncCrawler(max_concurrent=10, max_per_domain=5, max_depth=2) as crawler:
        results = await crawler.crawl(
            start_urls=["https://quotes.toscrape.com/"],
            max_pages=50,
            same_domain_only=True,
            exclude_patterns=[r"/login"],
        )
    print(f"Processed: {len(results)} pages, failed: {len(crawler.failed_urls)}")
```

```python
async def main():
    async with AsyncCrawler(
        max_concurrent=5,
        requests_per_second=2.0,  # 2 requests per second per domain
        respect_robots=True,
        min_delay=0.5,            # at least 0.5s between requests
        jitter=0.2,               # plus up to 0.2s at random
        user_agent="MyBot/1.0",
        max_retries=2,
    ) as crawler:
        await crawler.crawl(["https://docs.python.org/3/"], max_pages=20)
    stats = crawler.get_stats()
    print(stats["rate"]["avg_gap"], stats["robots_blocked"], sorted(crawler.blocked_urls))
```

```python
from circuit_breaker import CircuitBreaker
from errors import NetworkError, TransientError
from retry_strategy import RetryStrategy

async def main():
    retry_strategy = RetryStrategy(
        max_retries=3,
        backoff_factor=2.0,
        retry_on=[TransientError, NetworkError],
    )
    async with AsyncCrawler(retry_strategy=retry_strategy, circuit_breaker=CircuitBreaker()) as crawler:
        await crawler.crawl(["https://quotes.toscrape.com/"], max_pages=20)
        # Or one URL with a strategy of your own: raises on a final failure
        html = await retry_strategy.execute_with_retry(crawler.fetch_once, "https://example.com")
    errors = crawler.get_stats()["errors"]
    print(errors["errors_by_type"], errors["successful_retries"], list(errors["permanent_errors"]))
```

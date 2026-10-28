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

## Day 6 — saving data: JSON, CSV, SQLite

[src/storage.py](src/storage.py):

- The standard record is `url`, `title`, `text`, `links`, `metadata`, `crawled_at` (datetime), `status_code`, `content_type`. `make_record(page, fetch)` builds one from a parsed page; `normalize_record(data)` fills missing fields and checks the types.
- `DataStorage` is the abstract base class. `async save(data)` adds a record to a buffer and writes a full buffer (`batch_size`) as one batch. `async flush()` writes whatever is buffered, `async close()` flushes and releases the file or connection, and `async load()` / `iter_records()` read the records back. A failed write is retried `max_retries` times after `retry_delay * 2**attempt` seconds, then logged and counted in `stats["failed"]`. `save()` never raises. Subclasses implement `_write_batch(records)`.
- `JSONStorage(path, indent=None, lines=None, append=False, batch_size=20)` writes JSON Lines (the default for `.jsonl`, one record per line, streams any size) or a JSON array (`indent=2` for pretty output). The array is valid after every batch, not only after `close()`. Files are written with `aiofiles`.
- `CSVStorage(path, encoding="utf-8", errors="replace", delimiter=",", fieldnames=None, append=False, batch_size=50)` takes its header from the first record, or from the existing file when appending. Lists and dicts are stored as JSON. It supports any encoding (`utf-8-sig` for Excel, `cp1251`, `utf-16`) and writes the BOM once. Commas, quotes and line breaks are quoted, and NUL characters are removed.
- `SQLiteStorage(path, batch_size=100)` uses `aiosqlite`. `init_db()` creates the `pages` table and indexes on `domain`, `status_code`, `crawled_at` and `title`. A batch is one `executemany` in one transaction. `url` is unique, so a page crawled again updates its row. Query it with `get(url)`, `count()`, `find(domain=, status_code=, title_contains=, since=, limit=)` and `summary()` (counts by domain, status and content type, plus the pages with the most links).
- `MultiStorage(*storages)` sends every record to several storages, so one crawl can produce every format.
- `AsyncCrawler(storage=...)` saves every successfully processed page. `crawl()` flushes the storage at the end, and `crawler.close()` closes it. Even a broken storage doesn't stop the crawl: `crawler.storage_errors` counts what it raised. `get_stats()["storage"]` has `saved`, `failed`, `batches`, `retries`, `buffered` and `write_time`. `FetchResult` has a new `content_type` field.

## Day 7 — advanced features and integration

- `AdvancedCrawler` ([src/advanced_crawler.py](src/advanced_crawler.py), also `from crawler import AdvancedCrawler`) puts everything from days 1–6 together and adds configuration, sitemaps, statistics, reports, live progress and logging. `AdvancedCrawler.from_config("config.yaml")`, `await crawl()` (arguments override the config), `get_stats()`, `export_to_json(filename)`, `export_to_html_report(filename)`, `await run()` (crawl + the files named in the config), `await close()`.
- `SitemapParser` ([src/sitemap_parser.py](src/sitemap_parser.py)): `await fetch_sitemap(url) -> list[str]` handles regular sitemaps and sitemap indexes. Indexes are followed recursively with the children loaded concurrently, and cycles are skipped. It also reads `.xml.gz` and plain-text sitemaps. `find_sitemaps(site)` reads the `Sitemap:` lines of robots.txt and falls back to `/sitemap.xml`. Pages from sitemaps become start URLs after the same filters as discovered links.
- `CrawlerStats` ([src/crawler_stats.py](src/crawler_stats.py)) tracks pages, successes, failures, robots.txt blocks, average speed, average response time, status codes, top domains, errors by type, depths, a per-second timeline, the slowest pages and the run time. Memory doesn't grow with the page count.
- The HTML report ([src/report.py](src/report.py)) is one self-contained file. It has stat tiles, a throughput chart, bar charts of status codes, domains, error types and depths, and tables of domains, slowest pages, failed URLs and settings. Every chart shows a tooltip on hover. It follows the light/dark theme and works on phones.
- Configuration in YAML or JSON ([src/config.py](src/config.py), example in [config.yaml](config.yaml), guide in [docs/configuration.md](docs/configuration.md)). Unknown keys and wrong types are errors that name the key, for example `crawler.max_pages must be an integer`.
- The command line ([src/cli.py](src/cli.py)) is `python src/crawler.py --urls ... --max-pages ... --max-depth ... --output ... --config ... --respect-robots ... --rate-limit ...`, plus `--sitemap`, `--stats`, `--report`, `--include` / `--exclude`, `--proxy`, `--parse-workers`, `-v` / `-q`.
- Logging (`utils.configure_logging`) goes to the console and to a rotating file, each with its own level. The file gets full timestamps and can be text or JSON Lines.
- Progress shows a bar, percent, current speed in pages/s, ETA, active requests, queue, errors and blocks. On a terminal it redraws in place; in a pipe it prints one line per update.
- New `AsyncCrawler` options: `proxy`, `headers`, `cookies` (a cookie jar per session, so logins work), `keep_pages=False` (pages live only in the storage, so memory stays flat), and `parse_workers=N` (parse in N processes).
- Performance ([src/benchmark.py](src/benchmark.py)), on a local site with 50 ms latency per page, concurrency 20 and 8 CPUs:

  | run | 100 pages | 500 pages | 1000 pages | peak memory, 1000 pages |
  |---|---|---|---|---|
  | sync (urllib, same parser) | 8.9 s | 45.7 s | – | – |
  | async | 1.7 s (×5.1) | 8.3 s (×5.5) | 16.5 s, 61 p/s | 34.7 MB |
  | async, `parse_workers=7` | 2.6 s | 4.1 s (×11.2) | 5.8 s, 171 p/s | 21.6 MB* |
  | async + storage, `keep_pages=False` | 1.9 s | 8.6 s | 16.9 s | 19.0 MB |

  The bottleneck was HTML parsing. It costs ~10 ms of CPU per page, and the parse threads share one core under the GIL. Moving it to a process pool removed that limit. Starting the processes takes ~1 s, so it is off by default. Details are in [docs/algorithm.md](docs/algorithm.md). *Main process only; the workers hold their own memory.

API reference: [docs/api.md](docs/api.md).

## Structure

```
├── src/
│   ├── main.py         demo: sequential vs concurrent fetching
│   ├── parse_demo.py   demo: fetching and parsing pages
│   ├── crawl_demo.py   demo: crawling a site with live progress
│   ├── polite_demo.py  demo: robots.txt, rate limiting, delay statistics
│   ├── retry_demo.py   demo: error types, retries, circuit breaker, error report
│   ├── storage_demo.py demo: saving to JSON/CSV/SQLite, reading back, queries, benchmark
│   ├── advanced_demo.py demo: AdvancedCrawler from a config, sitemaps, report (day 7)
│   ├── benchmark.py    performance: sync vs async, memory, 100/500/1000 pages
│   ├── crawler.py      AsyncCrawler class; the command line when run as a script
│   ├── advanced_crawler.py  AdvancedCrawler: all components together
│   ├── cli.py          argparse command line
│   ├── config.py       CrawlerConfig: YAML/JSON settings, validation
│   ├── sitemap_parser.py    SitemapParser
│   ├── crawler_stats.py     CrawlerStats, JSON export
│   ├── report.py       HTML report
│   ├── progress.py     ProgressMonitor: progress bar, speed, ETA
│   ├── crawler_queue.py     CrawlerQueue class
│   ├── semaphore_manager.py SemaphoreManager class
│   ├── rate_limiter.py RateLimiter, exponential backoff
│   ├── robots_parser.py     RobotsParser, robots.txt parsing
│   ├── errors.py       error classes and classification
│   ├── retry_strategy.py    RetryStrategy, ErrorPolicy, RetryStats
│   ├── circuit_breaker.py   CircuitBreaker
│   ├── storage.py      DataStorage, JSONStorage, CSVStorage, SQLiteStorage, MultiStorage
│   ├── url_filter.py   URLFilter, URL canonicalization
│   ├── html_parser.py  HTMLParser class
│   ├── models.py       FetchResult
│   └── utils.py        logging setup (console + rotating file) and report output
├── tests/
│   ├── test_crawler.py
│   ├── test_parser.py
│   ├── test_queue.py   queue, semaphores, filters
│   ├── test_crawl.py   crawl(): depth, filters, duplicates, stats
│   ├── test_rate_limiter.py  rate limits, delays, jitter, backoff
│   ├── test_robots.py  robots.txt parsing, matching, loading, cache
│   ├── test_polite_crawl.py  crawler + robots.txt, rate limits, User-Agent, retries
│   ├── test_retry.py   error classes, retry strategies, backoff, circuit breaker, error stats
│   ├── test_storage.py JSON/CSV/SQLite round trips, encodings, batches, write errors, crawler
│   ├── test_sitemap.py sitemaps: urlset, index, recursion, gzip, errors, discovery
│   ├── test_stats.py   statistics, JSON/HTML export, progress line, logging
│   ├── test_config.py  config files, validation, command line options
│   └── test_advanced.py AdvancedCrawler: sitemaps, exports, proxy, cookies, CLI end to end
├── config.yaml         example configuration
└── docs/
    ├── algorithm.md
    ├── configuration.md
    └── api.md
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
python src/storage_demo.py # day 6 demo on a local site: output/pages.jsonl, .json, .csv, crawler.db
python src/storage_demo.py https://quotes.toscrape.com/ --max-pages 20 --benchmark 0
python src/advanced_demo.py # day 7 demo on a local site: sitemaps, retries, output/report.html
python src/crawler.py --urls https://example.com --max-pages 100 --output output/results.json
python src/crawler.py --config config.yaml   # everything from the config file
python src/crawler.py --config config.yaml --max-pages 20 --report output/report.html -v
python src/crawler.py --sitemap https://example.com/sitemap.xml --max-depth 0 --output output/pages.csv
python src/crawler.py --help
python src/benchmark.py    # performance tests, saves output/benchmark.json (~3 min)
pytest                     # tests (local server, no internet needed)
```

Command line options override the config file. `--output` picks the format from the extension: `.json`, `.jsonl`, `.csv`, `.db`. The exit code is 0 on success, 1 when no page could be fetched and 2 for invalid options.

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

```python
from storage import CSVStorage, JSONStorage, MultiStorage, SQLiteStorage

async def main():
    storage = MultiStorage(
        JSONStorage("output/results.json", indent=2),
        CSVStorage("output/results.csv", encoding="utf-8-sig"),
        SQLiteStorage("output/crawler.db"),
    )
    crawler = AsyncCrawler(storage=storage)
    await crawler.crawl(start_urls=["https://quotes.toscrape.com/"], max_pages=20)
    await crawler.close()  # closes the storages too

    async with SQLiteStorage("output/crawler.db") as db:
        print(await db.summary())
        print([p["title"] for p in await db.find(title_contains="Quotes", limit=5)])
    pages = await JSONStorage("output/results.json").load()  # crawled_at is a datetime again
```

```python
import asyncio
from crawler import AdvancedCrawler

async def main():
    crawler = AdvancedCrawler.from_config("config.yaml")

    await crawler.crawl()

    stats = crawler.get_stats()
    print(f"Обработано: {stats['total_pages']} страниц")
    print(f"Успешно: {stats['successful']}")
    print(f"Ошибок: {stats['failed']}")

    crawler.export_to_html_report("report.html")
    await crawler.close()

asyncio.run(main())
```

```python
from config import CrawlerConfig
from sitemap_parser import SitemapParser

async def main():
    # Settings in code instead of a file
    config = CrawlerConfig.from_dict({
        "start_urls": ["https://quotes.toscrape.com/"],
        "crawler": {"max_pages": 50, "rate_limit": 2, "keep_pages": False},
        "filters": {"exclude_patterns": ["/login"]},
        "storage": ["output/pages.jsonl"],
    })
    async with AdvancedCrawler(config) as crawler:
        await crawler.crawl(max_depth=1)  # arguments override the config
        crawler.export_to_json("output/stats.json")
        print(crawler.get_stats()["top_domains"])

    # Sitemaps on their own
    async with SitemapParser() as sitemaps:
        urls = await sitemaps.fetch_sitemap("https://www.python.org/sitemap.xml")
        print(len(urls), sitemaps.errors)
```

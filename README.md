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

## Structure

```
├── src/
│   ├── main.py         demo: sequential vs concurrent fetching
│   ├── parse_demo.py   demo: fetching and parsing pages
│   ├── crawl_demo.py   demo: crawling a site with live progress
│   ├── crawler.py      AsyncCrawler class
│   ├── crawler_queue.py     CrawlerQueue class
│   ├── semaphore_manager.py SemaphoreManager class
│   ├── url_filter.py   URLFilter, URL canonicalization
│   ├── html_parser.py  HTMLParser class
│   ├── models.py       FetchResult
│   └── utils.py        logging setup and report output
├── tests/
│   ├── test_crawler.py
│   ├── test_parser.py
│   ├── test_queue.py   queue, semaphores, filters
│   └── test_crawl.py   crawl(): depth, filters, duplicates, stats
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

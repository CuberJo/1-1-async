"""Asynchronous HTTP client built on aiohttp (day 1) with HTML parsing (day 2)
and a queue-driven crawl with depth and concurrency control (day 3)."""

import asyncio
import logging
import time

import aiohttp

from crawler_queue import CrawlerQueue
from html_parser import HTMLParser, normalize_url
from models import FetchResult
from semaphore_manager import SemaphoreManager
from url_filter import URLFilter, canonicalize

logger = logging.getLogger("crawler")


class AsyncCrawler:
    """Asynchronous page fetcher and crawler with concurrency limits."""

    def __init__(
        self,
        max_concurrent: int = 10,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        total_timeout: float = 30.0,
        parser: HTMLParser | None = None,
        max_depth: int = 2,
        max_per_domain: int | None = None,
        progress_interval: float | None = 1.0,
    ):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        self.max_concurrent = max_concurrent
        self.timeout = aiohttp.ClientTimeout(
            total=total_timeout,
            connect=connect_timeout,
            sock_read=read_timeout,
        )
        # Global limit plus a limit per domain (defaults to the global one)
        self.semaphores = SemaphoreManager(max_concurrent, max_per_domain)
        self._session: aiohttp.ClientSession | None = None
        self.parser = parser or HTMLParser()
        # Detailed results of the latest requests (status, error, timing)
        self.results: dict[str, FetchResult] = {}

        # Crawl state (day 3), reset at the start of every crawl()
        self.max_depth = max_depth
        self.progress_interval = progress_interval  # seconds, None = silent
        self.queue = CrawlerQueue()
        self.visited_urls: set[str] = set()
        self.failed_urls: dict[str, str] = {}  # url -> error
        self.processed_urls: dict[str, dict] = {}  # url -> parsed page
        self.skipped: dict[str, int] = {"filtered": 0, "too_deep": 0}
        self._pages_taken = 0
        self._started_at: float | None = None
        self._finished_at: float | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        # The session is created lazily, inside a running event loop
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=self.max_concurrent,  # connection pool size
                limit_per_host=self.max_concurrent,
                ttl_dns_cache=300,
                # An idle keep-alive connection must leave the pool before
                # sock_read fires: otherwise aiohttp 3.14 hands it out with an
                # already expired timeout and the request fails instantly
                keepalive_timeout=self.timeout.sock_read * 0.8,
            )
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=self.timeout,
                headers={"User-Agent": "AsyncCrawler/0.1"},
                raise_for_status=True,
            )
        return self._session

    async def _fetch(self, url: str) -> FetchResult:
        result = FetchResult(url=url)
        session = await self._get_session()
        async with self.semaphores.acquire(url):
            logger.info("Fetching: %s", url)
            start = time.perf_counter()
            try:
                async with session.get(url) as response:
                    result.status = response.status
                    result.final_url = str(response.url)
                    result.content = await response.text()
            # ClientResponseError subclasses ClientError, so it goes first
            except aiohttp.ClientResponseError as e:
                result.status = e.status
                result.error = f"HTTP {e.status} {e.message}"
            except asyncio.TimeoutError:
                result.error = "Timeout"
            except aiohttp.ClientError as e:
                result.error = f"{type(e).__name__}: {e}"
            # A binary response that is not valid text in its charset
            except UnicodeDecodeError:
                result.error = "UnicodeDecodeError: not a text response"
            result.elapsed = time.perf_counter() - start

        if result.ok:
            logger.info(
                "Success: %s [%s] %d chars in %.2fs",
                url, result.status, len(result.content), result.elapsed,
            )
        else:
            logger.warning("Error: %s — %s (%.2fs)", url, result.error, result.elapsed)
        self.results[url] = result
        return result

    async def fetch_url(self, url: str) -> str:
        """Fetch a single page. Returns an empty string on error."""
        result = await self._fetch(url)
        return result.content if result.ok else ""

    async def fetch_urls(self, urls: list[str]) -> dict[str, str]:
        """Fetch a list of URLs concurrently. Returns successful ones only."""
        results = await asyncio.gather(*(self._fetch(url) for url in urls))
        return {r.url: r.content for r in results if r.ok}

    async def fetch_and_parse(self, url: str) -> dict:
        """Fetch and parse a page: url, title, text, links, metadata and more.

        On a fetch error returns the same keys with empty values and the
        reason in "errors".
        """
        result = await self._fetch(url)
        if not result.ok:
            page = self.parser.empty_result(url)
            page["errors"] = [f"fetch: {result.error}"]
            return page
        # Relative links resolve against the address after redirects
        page = await self.parser.parse_html(result.content, result.final_url or url)
        page["url"] = url
        return page

    async def crawl(
        self,
        start_urls: list[str],
        max_pages: int = 100,
        same_domain_only: bool = False,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_depth: int | None = None,
    ) -> dict[str, dict]:
        """Crawl from start_urls following links, breadth first.

        - max_pages caps the number of fetched pages (failed ones included);
        - max_depth (defaults to the one given to __init__): start URLs have
          depth 0, links found on a page of depth d have depth d + 1;
        - same_domain_only keeps the crawl on the hosts of the start URLs;
        - include_patterns / exclude_patterns are regexes for discovered links.

        Returns {url: parsed page}; the page also has a "depth" key.
        Failed URLs are in self.failed_urls, all fetched ones in self.visited_urls.
        """
        if max_pages < 1:
            raise ValueError("max_pages must be >= 1")
        max_depth = self.max_depth if max_depth is None else max_depth
        if max_depth < 0:
            raise ValueError("max_depth must be >= 0")

        self.queue = queue = CrawlerQueue()
        self.visited_urls = set()
        self.failed_urls = {}
        self.processed_urls = {}
        self.skipped = {"filtered": 0, "too_deep": 0}
        self._pages_taken = 0
        url_filter = URLFilter(same_domain_only, include_patterns, exclude_patterns)

        # Start URLs bypass include/exclude: they are what the user asked for
        for url in start_urls:
            normalized = normalize_url(url, url)
            if normalized is None:
                logger.warning("Invalid start URL skipped: %r", url)
                continue
            normalized = canonicalize(normalized)
            url_filter.allow_domain(normalized)
            queue.add_url(normalized, priority=0, depth=0)

        self._started_at = time.perf_counter()
        self._finished_at = None
        tasks = [
            asyncio.create_task(self._worker(queue, url_filter, max_pages, max_depth))
            for _ in range(self.max_concurrent)
        ]
        workers = list(tasks)
        if self.progress_interval:
            tasks.append(asyncio.create_task(self._report_progress()))
        try:
            await asyncio.gather(*workers)
        finally:
            # On an error or cancellation no task must outlive crawl()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._finished_at = time.perf_counter()

        if self.progress_interval:
            self._print_progress()
        logger.info(
            "Crawl finished: %d processed, %d failed, %d left in queue",
            len(self.processed_urls), len(self.failed_urls), len(queue),
        )
        return self.processed_urls

    async def _worker(
        self, queue: CrawlerQueue, url_filter: URLFilter, max_pages: int, max_depth: int
    ) -> None:
        while (url := await queue.get_next()) is not None:
            self._pages_taken += 1
            if self._pages_taken >= max_pages:
                queue.close()  # the remaining URLs stay queued, unfetched
            self.visited_urls.add(url)
            depth = queue.get_depth(url)
            try:
                page = await self.fetch_and_parse(url)
            except Exception as e:  # a bug on one page must not stop the crawl
                logger.exception("Unexpected error on %s", url)
                error = f"{type(e).__name__}: {e}"
                self.failed_urls[url] = error
                queue.mark_failed(url, error)
                continue

            fetch = self.results[url]
            if not fetch.ok:
                self.failed_urls[url] = fetch.error
                queue.mark_failed(url, fetch.error)
                continue

            page["depth"] = depth
            self.processed_urls[url] = page
            # After a redirect the target page counts as visited too
            if fetch.final_url and (final := canonicalize(fetch.final_url)) != url:
                self.visited_urls.add(final)
                queue.mark_seen(final)

            # Links are queued before mark_processed: while this URL is in
            # progress, idle workers keep waiting instead of quitting
            for link in page["links"]:
                link = canonicalize(link)
                if not url_filter.is_allowed(link):
                    self.skipped["filtered"] += 1
                elif depth >= max_depth:
                    self.skipped["too_deep"] += 1
                else:
                    # Shallower pages first: priority falls with depth
                    queue.add_url(link, priority=-(depth + 1), depth=depth + 1)
            queue.mark_processed(url)

    def get_stats(self) -> dict:
        """Crawl progress: queue counters, speed and concurrency."""
        if self._started_at is None:
            elapsed = 0.0
        else:
            elapsed = (self._finished_at or time.perf_counter()) - self._started_at
        done = len(self.processed_urls) + len(self.failed_urls)
        return {
            **self.queue.get_stats(),
            "visited": len(self.visited_urls),
            "skipped": dict(self.skipped),
            "elapsed": round(elapsed, 2),
            "pages_per_sec": round(done / elapsed, 2) if elapsed else 0.0,
            "concurrency": self.semaphores.get_stats(),
        }

    def _print_progress(self) -> None:
        s = self.get_stats()
        print(
            f"[{s['elapsed']:6.1f}s] processed: {s['processed']:4} | queue: {s['queued']:4} | "
            f"active: {s['concurrency']['active']:3} | errors: {s['failed']:3} | "
            f"{s['pages_per_sec']:.2f} pages/s",
            flush=True,
        )

    async def _report_progress(self) -> None:
        while True:
            await asyncio.sleep(self.progress_interval)
            self._print_progress()

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "AsyncCrawler":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

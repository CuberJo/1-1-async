"""Asynchronous HTTP client built on aiohttp (day 1) with HTML parsing (day 2),
a queue-driven crawl with depth and concurrency control (day 3), polite
crawling: rate limits, robots.txt (day 4), error handling: classified
errors, retry strategies, a circuit breaker, error statistics (day 5) and
saving pages to JSON, CSV or SQLite (day 6)."""

import asyncio
import itertools
import logging
import time

import aiohttp

from circuit_breaker import CircuitBreaker
from crawler_queue import CrawlerQueue
from errors import CircuitOpenError, CrawlerError, NetworkError, TransientError, classify_exception
from html_parser import HTMLParser, normalize_url
from models import FetchResult
from rate_limiter import RateLimiter
from retry_strategy import RetryStrategy
from robots_parser import RobotsParser
from semaphore_manager import SemaphoreManager
from storage import DataStorage, make_record
from url_filter import URLFilter, canonicalize, domain_of

logger = logging.getLogger("crawler")

DEFAULT_USER_AGENT = "AsyncCrawler/0.1"
BLOCKED_ERROR = "Blocked by robots.txt"


class AsyncCrawler:
    """Asynchronous page fetcher and crawler with concurrency and rate limits.

    Day 4 options (the defaults keep the crawler as fast as before):
    - requests_per_second, min_delay, jitter: spacing between requests,
      per domain or, with per_domain_rate=False, for all requests together;
    - respect_robots: check robots.txt before every request, skip disallowed
      URLs, honour Crawl-delay;
    - user_agent: one string or a list to rotate; robots.txt rules are
      checked for the first one;
    - max_retries, backoff_base, backoff_max: retries of temporary errors
      (timeouts, 429, 5xx) after base * 2**attempt seconds.

    Day 5 options:
    - retry_strategy: a RetryStrategy that decides what, how often and after
      what delay to retry; it replaces max_retries / backoff_*. Repeated
      attempts get longer timeouts (RetryStrategy.timeout_growth);
    - circuit_breaker: a CircuitBreaker; requests to a domain that keeps
      failing are refused (CircuitOpenError) until it recovers.

    Day 6 options:
    - storage: a DataStorage; every processed page is saved to it as a
      standard record (make_record). crawl() flushes it at the end, close()
      closes it. A storage error never stops the crawl.
    """

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
        requests_per_second: float | None = None,
        per_domain_rate: bool = True,
        min_delay: float = 0.0,
        jitter: float = 0.0,
        respect_robots: bool = False,
        user_agent: str | list[str] = DEFAULT_USER_AGENT,
        max_retries: int = 0,
        backoff_base: float = 0.5,
        backoff_max: float = 30.0,
        retry_strategy: RetryStrategy | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        storage: DataStorage | None = None,
    ):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if max_depth < 0:
            raise ValueError("max_depth must be >= 0")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        user_agents = [user_agent] if isinstance(user_agent, str) else list(user_agent)
        if not user_agents:
            raise ValueError("user_agent must not be empty")
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

        # Politeness (day 4)
        self.rate_limiter = RateLimiter(requests_per_second, per_domain_rate, min_delay, jitter)
        self.respect_robots = respect_robots
        self.user_agents = user_agents
        self.user_agent = user_agents[0]  # the one robots.txt rules are checked for
        self._user_agent_cycle = itertools.cycle(user_agents)
        self.robots = RobotsParser(self._get_session, user_agent=self.user_agent)
        self.blocked_urls: set[str] = set()  # disallowed by robots.txt

        # Error handling (day 5); without a strategy the day 4 options build one
        self.retry_strategy = retry_strategy or RetryStrategy(
            max_retries=max_retries,
            base_delay=backoff_base,
            max_delay=backoff_max,
            jitter=jitter,
        )
        self.circuit_breaker = circuit_breaker

        # Saving (day 6)
        self.storage = storage
        self.storage_errors = 0  # exceptions a storage raised despite its contract

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
                raise_for_status=True,
            )
        return self._session

    async def _fetch(self, url: str) -> FetchResult:
        if self.respect_robots and not await self._robots_allowed(url):
            result = FetchResult(url=url, error=BLOCKED_ERROR, blocked=True)
            self.results[url] = result
            return result

        result = FetchResult(url=url)

        async def attempt(url: str) -> None:
            result.attempts += 1
            await self._fetch_once(result)

        # Backoff sleeps happen in the strategy, outside the semaphores:
        # a backing-off task holds no slot
        try:
            await self.retry_strategy.execute_with_retry(attempt, url)
        except CrawlerError as e:
            result.content = None
            result.status = e.status  # of the last attempt
            result.error = e.message
            result.error_type = e.kind
            result.retryable = self.retry_strategy.should_retry(e)
            result.retry_after = e.retry_after

        if result.ok:
            logger.info(
                "Success: %s [%s] %d chars in %.2fs, attempts: %d",
                url, result.status, len(result.content), result.elapsed, result.attempts,
            )
        else:
            logger.warning(
                "Error: %s — %s: %s, attempts: %d (%.2fs)",
                url, result.error_type, result.error, result.attempts, result.elapsed,
            )
        self.results[url] = result
        return result

    def _attempt_timeout(self, attempt: int) -> aiohttp.ClientTimeout:
        """Session timeouts, stretched on repeated attempts."""
        k = self.retry_strategy.timeout_multiplier(attempt)
        if k == 1:
            return self.timeout
        t = self.timeout
        return aiohttp.ClientTimeout(
            total=t.total and t.total * k,
            connect=t.connect and t.connect * k,
            sock_read=t.sock_read and t.sock_read * k,
        )

    async def _fetch_once(self, result: FetchResult) -> None:
        """One request; fills result or raises a classified CrawlerError."""
        url = result.url
        domain = domain_of(url)
        breaker = self.circuit_breaker
        session = await self._get_session()
        timeout = self._attempt_timeout(result.attempts)
        async with self.semaphores.acquire(url):
            # Waiting for the rate limit inside the semaphores holds a slot,
            # but nothing can delay the request once its time has come, so
            # requests never bunch up
            await self.rate_limiter.acquire(domain)
            # Checked right before the request: while this task waited for a
            # slot, the others may have found the domain down
            if breaker and not breaker.allow_request(domain):
                raise CircuitOpenError(
                    f"Circuit open for {domain}, retry in {breaker.retry_in(domain):.1f}s", url=url
                )
            if result.attempts > 1:
                logger.info("Fetching (attempt %d, timeout %ss): %s", result.attempts, timeout.total, url)
            else:
                logger.info("Fetching: %s", url)
            result.status = None
            start = time.perf_counter()
            try:
                headers = {"User-Agent": next(self._user_agent_cycle)}
                async with session.get(url, headers=headers, timeout=timeout) as response:
                    result.status = response.status
                    result.final_url = str(response.url)
                    result.content_type = response.content_type
                    result.content = await response.text()
            except Exception as exc:
                error = classify_exception(exc, url)
                self._record_health(domain, error)
                if error is None:  # a bug, not a request failure
                    raise
                raise error
            finally:
                result.elapsed = time.perf_counter() - start
            self._record_health(domain, None)

    def _record_health(self, domain: str, error: CrawlerError | None) -> None:
        # A 404 or a parse error is a healthy server answering; only
        # timeouts, 5xx and network failures count against the domain
        if self.circuit_breaker is None:
            return
        if isinstance(error, (TransientError, NetworkError)):
            self.circuit_breaker.record_failure(domain)
        else:
            self.circuit_breaker.record_success(domain)

    async def _robots_allowed(self, url: str) -> bool:
        """Check url against robots.txt of its site (loaded on first use) and
        pass the site's Crawl-delay on to the rate limiter."""
        await self.robots.fetch_robots(url)
        domain = domain_of(url)
        delay = self.robots.get_crawl_delay(self.user_agent, url)
        if delay and self.rate_limiter.domain_delays.get(domain) != delay:
            logger.info("Crawl-delay %.1fs for %s", delay, domain)
            self.rate_limiter.set_domain_delay(domain, delay)
        if self.robots.can_fetch(url, self.user_agent):
            return True
        if url not in self.blocked_urls:
            self.blocked_urls.add(url)
            logger.warning("Blocked by robots.txt: %s", url)
        return False

    async def fetch_url(self, url: str) -> str:
        """Fetch a single page. Returns an empty string on error."""
        result = await self._fetch(url)
        return result.content if result.ok else ""

    async def fetch_once(self, url: str) -> str:
        """One request without retries or robots.txt checks; raises a
        CrawlerError subclass on failure. Made for a strategy of your own:
        await strategy.execute_with_retry(crawler.fetch_once, url)"""
        result = FetchResult(url=url, attempts=1)
        await self._fetch_once(result)
        return result.content

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
        Failed URLs are in self.failed_urls, all fetched ones in self.visited_urls,
        the ones disallowed by robots.txt in self.blocked_urls (they don't count
        against max_pages).
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
        self.blocked_urls = set()
        self.storage_errors = 0
        self.retry_strategy.stats.reset()
        self.rate_limiter.reset_stats()
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
            # Batches still buffered go to disk now, not only on close()
            if self.storage is not None:
                await self._call_storage(self.storage.flush())

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
            # Checked before counting against max_pages: blocked URLs are free
            if self.respect_robots and not await self._robots_allowed(url):
                queue.mark_blocked(url)
                continue
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
            if self.storage is not None:
                await self._call_storage(self.storage.save(make_record(page, fetch)))

    async def _call_storage(self, operation) -> None:
        # DataStorage logs and counts its own errors and never raises; this
        # guards against a storage of your own that does
        try:
            await operation
        except Exception:
            logger.exception("Storage error")
            self.storage_errors += 1

    def get_stats(self) -> dict:
        """Crawl progress: queue counters, speed, concurrency and request rate."""
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
            "rate": self.rate_limiter.get_stats(),
            "robots_blocked": len(self.blocked_urls),
            "retries": self.retries,
            "errors": self.retry_strategy.stats.to_dict(),
            "circuits": self.circuit_breaker.get_stats() if self.circuit_breaker else {},
            "storage": self.storage.get_stats() if self.storage else {},
            "storage_errors": self.storage_errors,
        }

    @property
    def retries(self) -> int:
        """Repeated requests made since the last crawl() started."""
        return self.retry_strategy.stats.retries

    def get_error_report(self) -> dict:
        """Everything known about errors, ready for json.dump: statistics,
        the final result of every failed URL and the failed attempts log."""
        failed = {
            url: {
                "error_type": r.error_type,
                "error": r.error,
                "status": r.status,
                "attempts": r.attempts,
            }
            for url, r in self.results.items()
            if not r.ok and not r.blocked
        }
        return {
            "stats": self.retry_strategy.stats.to_dict(),
            "circuits": self.circuit_breaker.get_stats() if self.circuit_breaker else {},
            "failed": failed,
            "attempts_log": list(self.retry_strategy.stats.history),
        }

    def _print_progress(self) -> None:
        s = self.get_stats()
        print(
            f"[{s['elapsed']:6.1f}s] processed: {s['processed']:4} | queue: {s['queued']:4} | "
            f"active: {s['concurrency']['active']:3} | errors: {s['failed']:3} | "
            f"blocked: {s['robots_blocked']:3} | {s['pages_per_sec']:.2f} pages/s | "
            f"{s['rate']['current_rps']:.2f} req/s | avg gap: {s['rate']['avg_gap']:.2f}s"
            + (f" | saved: {s['storage']['saved']}" if self.storage else ""),
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
        await self.robots.close()
        if self.storage is not None:
            await self._call_storage(self.storage.close())

    async def __aenter__(self) -> "AsyncCrawler":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

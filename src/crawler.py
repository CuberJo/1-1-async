"""Asynchronous HTTP client built on aiohttp (day 1) with HTML parsing (day 2)."""

import asyncio
import logging
import time

import aiohttp

from html_parser import HTMLParser
from models import FetchResult

logger = logging.getLogger("crawler")


class AsyncCrawler:
    """Asynchronous page fetcher with a concurrency limit."""

    def __init__(
        self,
        max_concurrent: int = 10,
        connect_timeout: float = 5.0,
        read_timeout: float = 10.0,
        total_timeout: float = 30.0,
        parser: HTMLParser | None = None,
    ):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        self.max_concurrent = max_concurrent
        self.timeout = aiohttp.ClientTimeout(
            total=total_timeout,
            connect=connect_timeout,
            sock_read=read_timeout,
        )
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._session: aiohttp.ClientSession | None = None
        self.parser = parser or HTMLParser()
        # Detailed results of the latest requests (status, error, timing)
        self.results: dict[str, FetchResult] = {}

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
        async with self._semaphore:
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

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def __aenter__(self) -> "AsyncCrawler":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

"""Day 7 — sitemap.xml: urlsets, sitemap indexes, gzip, discovery via robots.txt."""

import asyncio
import gzip
import logging
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin

import aiohttp

from errors import CrawlerError
from robots_parser import RobotsParser, origin_of, parse_robots

logger = logging.getLogger("crawler.sitemap")

SITEMAP_MAX_SIZE = 50 * 1024 * 1024  # the protocol's limit for one (uncompressed) file
GZIP_MAGIC = b"\x1f\x8b"


def _local(tag: str) -> str:
    """"{http://www.sitemaps.org/schemas/sitemap/0.9}loc" -> "loc"."""
    return tag.rsplit("}", 1)[-1]


def parse_sitemap(content: bytes) -> tuple[str, list[str]]:
    """Parse one sitemap file. Returns (kind, locations):

    - ("urlset", page URLs) for a regular sitemap;
    - ("index", sitemap URLs) for a sitemap index;
    - ("text", URLs) for a plain text sitemap, one URL per line.

    The namespace is ignored, so sitemaps with a missing or wrong one still
    parse. Raises ValueError on content that is none of these.
    """
    if content.startswith(GZIP_MAGIC):
        content = gzip.decompress(content)
    stripped = content.lstrip()
    if not stripped.startswith(b"<"):
        text = stripped.decode("utf-8", errors="replace")
        urls = [line.strip() for line in text.splitlines()]
        return "text", [u for u in urls if u.startswith(("http://", "https://"))]
    try:
        # expat doesn't load external entities and limits entity expansion
        root = ET.fromstring(stripped)
    except ET.ParseError as e:
        raise ValueError(f"invalid sitemap XML: {e}") from None
    kind = _local(root.tag)
    if kind == "urlset":
        child = "url"
    elif kind == "sitemapindex":
        kind, child = "index", "sitemap"
    else:
        raise ValueError(f"unknown sitemap root <{kind}>")
    locations = []
    for entry in root:
        if _local(entry.tag) != child:
            continue
        for field in entry:
            if _local(field.tag) == "loc" and field.text and field.text.strip():
                locations.append(field.text.strip())
                break
    return kind, locations


class SitemapParser:
    """Loads sitemaps and returns the page URLs they list.

    - fetch_sitemap(url): a urlset gives its URLs; a sitemap index is
      followed recursively (up to max_depth levels of nesting), its children
      loaded concurrently; .xml.gz and gzip-encoded responses are unpacked;
    - within one fetch_sitemap() call every sitemap is loaded once: cycles
      and repeated children are skipped. A later call loads them again;
    - discover(site_url): sitemaps listed in robots.txt, else /sitemap.xml;
    - fetcher(url, max_size) -> bytes, if given, makes every sitemap
      request instead of a plain session.get(): AdvancedCrawler passes its
      fetch_bytes, so sitemaps obey the same robots.txt, rate limits,
      concurrency limits and retries as pages; robots, if given, is the
      RobotsParser whose cached robots.txt find_sitemaps() reads;
    - a sitemap that fails to load or parse is logged and recorded in
      self.errors, the others are still used: the result is never an exception;
      errors and loaded add up over calls until reset().

    URLs keep the sitemap's order, duplicates removed, at most max_urls.
    """

    def __init__(
        self,
        session_factory: Callable[[], Awaitable[aiohttp.ClientSession]] | None = None,
        user_agent: str = "AsyncCrawler/0.1",
        timeout: float = 30.0,
        max_depth: int = 3,
        max_urls: int = 50_000,
        fetcher: Callable[[str, int], Awaitable[bytes]] | None = None,
        robots: RobotsParser | None = None,
    ):
        self._session_factory = session_factory
        self._fetcher = fetcher
        self.robots = robots
        self._own_session: aiohttp.ClientSession | None = None
        self.user_agent = user_agent
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.max_depth = max_depth
        self.max_urls = max_urls
        self.loaded: set[str] = set()  # sitemap URLs requested since reset()
        self.errors: dict[str, str] = {}  # sitemap URL -> error, since reset()

    def reset(self) -> None:
        """Forget loaded sitemaps and errors, e.g. before a new crawl."""
        self.loaded.clear()
        self.errors.clear()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session_factory is not None:
            return await self._session_factory()
        if self._own_session is None or self._own_session.closed:
            self._own_session = aiohttp.ClientSession()
        return self._own_session

    async def fetch_sitemap(self, sitemap_url: str) -> list[str]:
        """All page URLs of a sitemap or a sitemap index (followed recursively)."""
        urls: dict[str, None] = {}  # an ordered set
        # Cycle protection is per call: the same parser can fetch a sitemap again
        await self._collect(sitemap_url, 0, urls, set())
        result = list(urls)[: self.max_urls]
        logger.info("Sitemap %s: %d URLs", sitemap_url, len(result))
        return result

    async def _collect(self, sitemap_url: str, depth: int, urls: dict[str, None], seen: set[str]) -> None:
        if sitemap_url in seen or len(urls) >= self.max_urls:
            return
        # Marked before the request: concurrent branches can't fetch it twice
        seen.add(sitemap_url)
        self.loaded.add(sitemap_url)
        try:
            content = await self._download(sitemap_url)
            kind, locations = parse_sitemap(content)
        except (CrawlerError, aiohttp.ClientError, asyncio.TimeoutError, ValueError, OSError, EOFError) as e:
            error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.warning("Sitemap %s failed: %s", sitemap_url, error)
            self.errors[sitemap_url] = error
            return
        locations = [urljoin(sitemap_url, loc) for loc in locations]
        if kind != "index":
            for loc in locations:
                if len(urls) >= self.max_urls:
                    logger.warning("Sitemap URL limit %d reached at %s", self.max_urls, sitemap_url)
                    break
                urls.setdefault(loc, None)
            return
        if depth >= self.max_depth:
            logger.warning("Sitemap index %s nested deeper than %d, skipped", sitemap_url, self.max_depth)
            self.errors[sitemap_url] = f"nested deeper than {self.max_depth}"
            return
        logger.info("Sitemap index %s: %d sitemaps", sitemap_url, len(locations))
        # Children are loaded together; each one fills its own list, so the
        # final order follows the index, not the order downloads finish in
        parts = [{} for _ in locations]
        await asyncio.gather(*(
            self._collect(loc, depth + 1, part, seen) for loc, part in zip(locations, parts)
        ))
        for part in parts:
            for loc in part:
                if len(urls) >= self.max_urls:
                    return
                urls.setdefault(loc, None)

    async def _download(self, url: str) -> bytes:
        if self._fetcher is not None:
            body = await self._fetcher(url, SITEMAP_MAX_SIZE + 1)
        else:
            body = await self._get(url)
        if len(body) > SITEMAP_MAX_SIZE:
            raise ValueError(f"sitemap larger than {SITEMAP_MAX_SIZE // 2**20} MiB")
        return body

    async def _get(self, url: str) -> bytes:
        session = await self._get_session()
        async with session.get(
            url,
            headers={"User-Agent": self.user_agent},
            timeout=self.timeout,
            raise_for_status=True,
        ) as response:
            # aiohttp undoes Content-Encoding: gzip; a .gz file stays packed
            # and parse_sitemap recognises it by its magic bytes
            return await response.content.read(SITEMAP_MAX_SIZE + 1)

    async def find_sitemaps(self, site_url: str) -> list[str]:
        """Sitemap URLs of a site: the Sitemap: lines of its robots.txt, or
        /sitemap.xml when robots.txt lists none."""
        origin = origin_of(site_url)
        if self.robots is not None:
            await self.robots.fetch_robots(site_url)
            rules = self.robots.get_rules(site_url)
            return (rules.sitemaps if rules else []) or [origin + "/sitemap.xml"]
        try:
            session = await self._get_session()
            async with session.get(
                origin + "/robots.txt",
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
                raise_for_status=True,
            ) as response:
                text = await response.text(errors="replace")
            sitemaps = parse_robots(text).sitemaps
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            sitemaps = []
        return sitemaps or [origin + "/sitemap.xml"]

    async def discover(self, site_url: str) -> list[str]:
        """Page URLs from all sitemaps of the site of site_url."""
        urls: dict[str, None] = {}
        for sitemap in await self.find_sitemaps(site_url):
            for url in await self.fetch_sitemap(sitemap):
                urls.setdefault(url, None)
        return list(urls)[: self.max_urls]

    async def close(self) -> None:
        if self._own_session is not None and not self._own_session.closed:
            await self._own_session.close()
        self._own_session = None

    async def __aenter__(self) -> "SitemapParser":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

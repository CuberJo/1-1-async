"""Day 7 — AdvancedCrawler: every component of days 1-6 plus configuration,
sitemaps, statistics, reports, live progress and logging in one class."""

import logging
import os
from typing import TextIO

from circuit_breaker import CircuitBreaker
from config import CrawlerConfig, StorageSettings
from crawler import AsyncCrawler
from crawler_stats import CrawlerStats
from html_parser import normalize_url
from models import FetchResult
from progress import ProgressMonitor
from sitemap_parser import SitemapParser
from storage import CSVStorage, DataStorage, JSONStorage, MultiStorage, SQLiteStorage
from url_filter import URLFilter, canonicalize
from utils import configure_logging

logger = logging.getLogger("crawler.advanced")


def build_storage(settings: list[StorageSettings]) -> DataStorage | None:
    """Storages from the config: none, one, or a MultiStorage of several."""
    storages = []
    for s in settings:
        options = dict(s.options)
        if s.type == "json":
            storages.append(JSONStorage(s.path, lines=False, **{"indent": 2, **options}))
        elif s.type == "jsonl":
            storages.append(JSONStorage(s.path, lines=True, **options))
        elif s.type == "csv":
            storages.append(CSVStorage(s.path, **options))
        elif s.type == "sqlite":
            storages.append(SQLiteStorage(s.path, **options))
        else:
            raise ValueError(f"unknown storage type {s.type!r}")
    if not storages:
        return None
    return storages[0] if len(storages) == 1 else MultiStorage(*storages)


class AdvancedCrawler(AsyncCrawler):
    """The full crawler, driven by a CrawlerConfig (or a YAML / JSON file).

        crawler = AdvancedCrawler.from_config("config.yaml")
        await crawler.crawl()                    # start URLs + sitemaps from the config
        crawler.get_stats()["total_pages"]
        crawler.export_to_html_report("report.html")
        await crawler.close()

    On top of AsyncCrawler:
    - sitemaps: the pages of config.sitemap.urls (and, with discover, of the
      sitemaps every start site lists in robots.txt) become start URLs, after
      the same filters as discovered links;
    - stats: a CrawlerStats fed by every fetch; get_stats() merges it with
      the queue, concurrency, rate, error and storage counters;
    - progress: a ProgressMonitor line every progress_interval seconds;
    - exports: export_to_json(), export_to_html_report(); run() also writes
      the files named in config.output;
    - storage, logging: built from the config (logging only if asked:
      setup_logging=True or the command line).
    """

    def __init__(
        self,
        config: CrawlerConfig | None = None,
        *,
        storage: DataStorage | None = None,
        setup_logging: bool = False,
        progress_stream: TextIO | None = None,
    ):
        config = config or CrawlerConfig()
        c = config.crawler
        if setup_logging:
            log = config.logging
            configure_logging(log.level, log.console_level, log.file, log.max_bytes, log.backup_count, log.format)
        super().__init__(
            max_concurrent=c.max_concurrent,
            connect_timeout=c.connect_timeout,
            read_timeout=c.read_timeout,
            total_timeout=c.total_timeout,
            max_depth=c.max_depth,
            max_per_domain=c.max_per_domain,
            progress_interval=config.progress_interval,
            requests_per_second=c.rate_limit,
            per_domain_rate=c.per_domain_rate,
            min_delay=c.min_delay,
            jitter=c.jitter,
            respect_robots=c.respect_robots,
            user_agent=c.user_agent,
            max_retries=c.max_retries,
            backoff_base=c.backoff_base,
            backoff_max=c.backoff_max,
            circuit_breaker=CircuitBreaker() if c.circuit_breaker else None,
            storage=storage if storage is not None else build_storage(config.storage),
            proxy=c.proxy,
            headers=c.headers,
            cookies=c.cookies,
            keep_pages=c.keep_pages,
            parse_workers=c.parse_workers,
        )
        self.config = config
        self.stats = CrawlerStats()
        # Sitemap requests share the pages' pipeline: robots.txt, rate and
        # concurrency limits, the circuit breaker and retries
        self.sitemaps = SitemapParser(
            user_agent=self.user_agent,
            max_urls=config.sitemap.max_urls,
            fetcher=self.fetch_bytes,
            robots=self.robots,
        )
        self.sitemap_urls: list[str] = []  # pages found in sitemaps by the last crawl()
        self.start_urls: list[str] = []
        self.progress_stream = progress_stream
        self._monitor: ProgressMonitor | None = None

    @classmethod
    def from_config(cls, path: str | os.PathLike, **kwargs) -> "AdvancedCrawler":
        """A crawler from a .yaml / .yml / .json file (see config.yaml)."""
        return cls(CrawlerConfig.load(path), **kwargs)

    async def crawl(
        self,
        start_urls: list[str] | None = None,
        max_pages: int | None = None,
        same_domain_only: bool | None = None,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
        max_depth: int | None = None,
        sitemaps: list[str] | None = None,
    ) -> dict[str, dict]:
        """Crawl; every argument left as None comes from the config."""
        cfg = self.config
        start_urls = list(cfg.start_urls if start_urls is None else start_urls)
        sitemaps = list(cfg.sitemap.urls if sitemaps is None else sitemaps)
        max_pages = cfg.crawler.max_pages if max_pages is None else max_pages
        max_depth = cfg.crawler.max_depth if max_depth is None else max_depth
        if same_domain_only is None:
            same_domain_only = cfg.filters.same_domain_only
        include_patterns = cfg.filters.include_patterns if include_patterns is None else include_patterns
        exclude_patterns = cfg.filters.exclude_patterns if exclude_patterns is None else exclude_patterns

        self.stats.start()
        self.sitemaps.reset()  # sitemap errors in reports belong to this crawl
        self._monitor = ProgressMonitor(max_pages, self.progress_stream)
        try:
            self.sitemap_urls = await self._urls_from_sitemaps(
                start_urls, sitemaps, same_domain_only, include_patterns, exclude_patterns
            )
            # Sitemap pages go after the given start URLs, duplicates dropped
            urls = list(dict.fromkeys(start_urls + self.sitemap_urls))
            if not urls:
                raise ValueError("nothing to crawl: no start URLs and no pages in the sitemaps")
            self.start_urls = urls
            logger.info(
                "Crawl started: %d start URLs (%d from sitemaps), max_pages=%d, max_depth=%d",
                len(urls), len(self.sitemap_urls), max_pages, max_depth,
            )
            return await super().crawl(
                urls, max_pages, same_domain_only, include_patterns, exclude_patterns, max_depth
            )
        finally:
            self.stats.stop()

    async def _urls_from_sitemaps(
        self,
        start_urls: list[str],
        sitemaps: list[str],
        same_domain_only: bool,
        include_patterns: list[str],
        exclude_patterns: list[str],
    ) -> list[str]:
        if self.config.sitemap.discover:
            for url in start_urls:
                for sitemap in await self.sitemaps.find_sitemaps(url):
                    if sitemap not in sitemaps:
                        sitemaps.append(sitemap)
        if not sitemaps:
            return []
        url_filter = URLFilter(same_domain_only, include_patterns, exclude_patterns)
        # The sitemaps' own sites count as start sites for same_domain_only
        for url in start_urls + sitemaps:
            url_filter.allow_domain(url)
        found: dict[str, None] = {}
        rejected = 0
        for sitemap in sitemaps:
            for url in await self.sitemaps.fetch_sitemap(sitemap):
                normalized = normalize_url(url, sitemap)
                if normalized and url_filter.is_allowed(normalized := canonicalize(normalized)):
                    found.setdefault(normalized, None)
                else:
                    rejected += 1
        if rejected:
            logger.info("Sitemaps: %d URLs rejected by the filters", rejected)
        return list(found)

    async def _fetch(self, url: str) -> FetchResult:
        result = await super()._fetch(url)
        if not result.blocked:  # blocks are counted in _robots_allowed
            self.stats.record(result, depth=self.queue.depths.get(url))
        return result

    async def _robots_allowed(self, url: str) -> bool:
        # crawl() checks robots.txt before _fetch, so blocks are counted here,
        # once per URL
        known = url in self.blocked_urls
        allowed = await super()._robots_allowed(url)
        if not allowed and not known:
            self.stats.record_blocked()
        return allowed

    def get_stats(self) -> dict:
        """Everything AsyncCrawler.get_stats() has, plus CrawlerStats:
        total_pages, successful, failed, blocked, success_rate, uptime,
        avg_speed, avg_response_time, status_codes, top_domains, errors_by_type..."""
        return {**super().get_stats(), **self.stats.to_dict()}

    def _print_progress(self) -> None:
        if self._monitor is None:
            return super()._print_progress()
        self._monitor.update(self.get_stats(), final=self._finished_at is not None)

    def _report_extra(self) -> dict:
        return {
            "start_urls": self.start_urls,
            "sitemap_pages": len(self.sitemap_urls),
            "sitemap_errors": dict(self.sitemaps.errors),
            "failed_urls": dict(self.failed_urls),
            "robots_blocked_urls": sorted(self.blocked_urls),
        }

    def export_to_json(self, filename: str | os.PathLike) -> None:
        """Statistics, crawler counters, failed URLs and the settings as JSON."""
        crawler_stats = super().get_stats()
        extra = {
            **self._report_extra(),
            "crawler": {k: crawler_stats[k] for k in (
                "queued", "processed", "visited", "skipped", "concurrency", "rate",
                "retries", "errors", "circuits", "storage", "storage_errors",
            )},
            "config": self.config.summary(),
        }
        self.stats.export_to_json(filename, extra=extra)
        logger.info("Statistics saved to %s", filename)

    def export_to_html_report(self, filename: str | os.PathLike) -> None:
        """A standalone HTML report with charts and tables."""
        extra = {**self._report_extra(), "config": self.config.summary()}
        self.stats.export_to_html_report(filename, title=self.config.output.report_title, extra=extra)
        logger.info("HTML report saved to %s", filename)

    async def run(self) -> dict:
        """crawl() with the config, then write the files of config.output.
        Returns get_stats()."""
        await self.crawl()
        output = self.config.output
        if output.stats_json:
            self.export_to_json(output.stats_json)
        if output.html_report:
            self.export_to_html_report(output.html_report)
        return self.get_stats()

    async def close(self) -> None:
        await super().close()
        await self.sitemaps.close()

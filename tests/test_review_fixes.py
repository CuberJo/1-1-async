"""Regressions from the review: max_pages with robots.txt, redirect targets
fetched twice, sitemap requests outside the crawler's pipeline."""

import asyncio
from collections import Counter

import pytest
import pytest_asyncio
from aiohttp import web

from advanced_crawler import AdvancedCrawler
from config import CrawlerConfig
from crawler import AsyncCrawler
from crawler_queue import CrawlerQueue

HTML = "<html><head><title>Page</title></head><body></body></html>"


class Site:
    def __init__(self):
        self.hits: Counter[str] = Counter()
        self.base = ""
        self.active = 0
        self.max_active = 0  # most sitemap requests in flight at once


@pytest_asyncio.fixture
async def site():
    s = Site()

    async def handler(request: web.Request) -> web.Response:
        path = request.path
        s.hits[path] += 1
        if path == "/robots.txt":
            await asyncio.sleep(0.1)  # every worker gets a URL before the rules arrive
            return web.Response(text=f"User-agent: *\nDisallow: /private/\nSitemap: {s.base}/index.xml\n")
        if path == "/redirect":
            raise web.HTTPFound(s.base + "/target")
        if path == "/index.xml":
            locs = "".join(f"<sitemap><loc>{s.base}/sm/{i}.xml</loc></sitemap>" for i in range(10))
            locs += f"<sitemap><loc>{s.base}/private/sm.xml</loc></sitemap>"
            return web.Response(text=f"<sitemapindex>{locs}</sitemapindex>", content_type="application/xml")
        if path.startswith("/sm/"):
            s.active += 1
            s.max_active = max(s.max_active, s.active)
            await asyncio.sleep(0.05)
            s.active -= 1
            if path == "/sm/0.xml" and s.hits[path] == 1:
                raise web.HTTPServiceUnavailable()  # retried
            return web.Response(text=f"<urlset><url><loc>{s.base}/p/{path[4:-4]}</loc></url></urlset>",
                                content_type="application/xml")
        return web.Response(text=HTML, content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    s.base = f"http://127.0.0.1:{tcp._server.sockets[0].getsockname()[1]}"
    yield s
    await runner.cleanup()


@pytest.mark.asyncio
async def test_max_pages_holds_while_workers_wait_for_robots(site):
    urls = [f"{site.base}/{i}" for i in range(5)]
    async with AsyncCrawler(max_concurrent=5, respect_robots=True, progress_interval=None) as crawler:
        pages = await crawler.crawl(urls, max_pages=1)
    assert len(pages) == 1
    assert sum(site.hits[f"/{i}"] for i in range(5)) == 1
    assert crawler.queue.get_stats()["queued"] == 4  # left unfetched, not lost


@pytest.mark.asyncio
async def test_redirect_target_is_not_fetched_again(site):
    async with AsyncCrawler(max_concurrent=1, progress_interval=None) as crawler:
        pages = await crawler.crawl([site.base + "/redirect", site.base + "/target"])
    assert site.hits["/target"] == 1
    assert list(pages) == [site.base + "/redirect"]


def test_mark_seen_removes_a_queued_url():
    queue = CrawlerQueue()
    queue.add_url("a", priority=1)
    queue.add_url("b")
    queue.add_url("c")
    queue.mark_seen("b")
    assert len(queue) == 2
    assert not queue.add_url("b")


@pytest.mark.asyncio
async def test_put_back_keeps_the_priority():
    queue = CrawlerQueue()
    queue.add_url("low")
    queue.add_url("high", priority=5)
    assert await queue.get_next() == "high"
    queue.put_back("high")
    assert "high" not in queue.in_progress
    assert await queue.get_next() == "high"


@pytest.mark.asyncio
async def test_sitemaps_go_through_the_crawler_pipeline(site):
    config = CrawlerConfig.from_dict({
        "start_urls": [site.base + "/"],
        "sitemap": {"discover": True},
        "crawler": {"max_pages": 50, "max_depth": 0, "max_concurrent": 10, "max_per_domain": 2,
                    "respect_robots": True, "max_retries": 2, "backoff_base": 0.01},
        "logging": {"file": None},
        "progress_interval": None,
    })
    async with AdvancedCrawler(config) as crawler:
        await crawler.crawl()
    assert site.max_active <= 2  # the per-domain limit holds for sitemaps
    assert site.hits["/sm/0.xml"] == 2  # the 503 was retried
    assert site.hits["/private/sm.xml"] == 0  # robots.txt is respected
    assert site.hits["/robots.txt"] == 1  # loaded once, shared with discovery
    assert {f"{site.base}/p/{i}" for i in range(10)} <= set(crawler.sitemap_urls)
    assert f"{site.base}/private/sm.xml" in crawler.sitemaps.errors

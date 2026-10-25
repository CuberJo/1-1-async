"""AsyncCrawler.crawl tests against a local site (no internet needed)."""

import asyncio
from collections import Counter

import pytest
import pytest_asyncio
from aiohttp import web

from crawler import AsyncCrawler

# path -> links on the page. Depths from "/": a, b, skip/1 = 1; a1, missing = 2; a2 = 3
SITE = {
    "/": ["/a", "/b", "/b", "/a#top", "/skip/1", "/file.pdf", "http://other.invalid/x"],
    "/a": ["/a1", "/", "/b"],  # links back: a cycle
    "/b": ["/a", "/missing"],
    "/a1": ["/a2"],
    "/a2": [],
    "/skip/1": [],
    "/slow": [f"/slow/{i}" for i in range(6)],
}


def page_html(links: list[str]) -> str:
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return f"<html><head><title>Page</title></head><body>{anchors}</body></html>"


@pytest_asyncio.fixture
async def site():
    hits: Counter[str] = Counter()

    async def handler(request):
        path = request.path
        hits[path] += 1
        if path == "/redirect":
            raise web.HTTPFound("/a")
        if path.startswith("/slow/"):
            await asyncio.sleep(0.1)
            return web.Response(text=page_html([]), content_type="text/html")
        if path not in SITE:
            raise web.HTTPNotFound()
        return web.Response(text=page_html(SITE[path]), content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    port = tcp._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", hits
    await runner.cleanup()


async def run_crawl(start: list[str], crawler_kwargs: dict | None = None, **crawl_kwargs):
    kwargs = {"max_concurrent": 4, "read_timeout": 2, "progress_interval": None}
    kwargs.update(crawler_kwargs or {})
    async with AsyncCrawler(**kwargs) as crawler:
        results = await crawler.crawl(start, **crawl_kwargs)
    return crawler, results


def paths(base: str, urls) -> set[str]:
    return {url.removeprefix(base) for url in urls}


@pytest.mark.asyncio
@pytest.mark.parametrize("max_depth, expected", [
    (0, {"/"}),
    (1, {"/", "/a", "/b", "/skip/1"}),
    (2, {"/", "/a", "/b", "/skip/1", "/a1"}),
    (5, {"/", "/a", "/b", "/skip/1", "/a1", "/a2"}),
])
async def test_depth_limit(site, max_depth, expected):
    base, _ = site
    crawler, results = await run_crawl([base], max_depth=max_depth, same_domain_only=True)
    assert paths(base, results) == expected
    for url, page in results.items():
        assert page["depth"] <= max_depth
    if max_depth == 0:
        assert crawler.skipped["too_deep"] > 0


@pytest.mark.asyncio
async def test_depth_is_tracked_per_url(site):
    base, _ = site
    _, results = await run_crawl([base], max_depth=3, same_domain_only=True)
    depths = {url.removeprefix(base): page["depth"] for url, page in results.items()}
    assert depths == {"/": 0, "/a": 1, "/b": 1, "/skip/1": 1, "/a1": 2, "/a2": 3}


@pytest.mark.asyncio
async def test_max_depth_from_constructor(site):
    base, _ = site
    _, results = await run_crawl([base], {"max_depth": 0})
    assert paths(base, results) == {"/"}


@pytest.mark.asyncio
async def test_no_duplicates(site):
    base, hits = site
    # Same start page spelled three ways, plus cycles and duplicate links on pages
    crawler, results = await run_crawl(
        [base, base + "/", base + "/#frag"], max_depth=5, same_domain_only=True,
    )
    assert all(n == 1 for n in hits.values()), hits
    assert crawler.visited_urls == set(results) | set(crawler.failed_urls)
    assert len(crawler.visited_urls) == sum(hits.values())


@pytest.mark.asyncio
async def test_redirect_target_is_not_fetched_again(site):
    base, hits = site
    crawler, results = await run_crawl([base + "/redirect"], max_depth=2, same_domain_only=True)
    assert hits["/a"] == 1  # once via the redirect, never via the links to /a
    assert base + "/a" in crawler.visited_urls
    assert base + "/redirect" in results


@pytest.mark.asyncio
async def test_failed_urls_are_recorded(site):
    base, _ = site
    crawler, results = await run_crawl([base], max_depth=2, same_domain_only=False)
    failed = crawler.failed_urls
    assert failed[base + "/missing"].startswith("HTTP 404")
    assert failed["http://other.invalid/x"].startswith("ClientConnector")
    assert base + "/missing" not in results
    assert crawler.get_stats()["failed"] == 2


@pytest.mark.asyncio
async def test_same_domain_only(site):
    base, _ = site
    crawler, _ = await run_crawl([base], max_depth=1, same_domain_only=True)
    assert "http://other.invalid/x" not in crawler.visited_urls
    assert crawler.skipped["filtered"] > 0


@pytest.mark.asyncio
async def test_exclude_patterns(site):
    base, _ = site
    _, results = await run_crawl(
        [base], max_depth=5, same_domain_only=True, exclude_patterns=[r"/skip/", r"/a\d"],
    )
    assert paths(base, results) == {"/", "/a", "/b"}


@pytest.mark.asyncio
async def test_include_patterns(site):
    base, _ = site
    # The start page doesn't match, but start URLs are always crawled
    _, results = await run_crawl(
        [base], max_depth=5, same_domain_only=True, include_patterns=[r"/a"],
    )
    assert paths(base, results) == {"/", "/a", "/a1", "/a2"}


@pytest.mark.asyncio
async def test_binary_links_are_skipped(site):
    base, hits = site
    await run_crawl([base], max_depth=1, same_domain_only=True)
    assert hits["/file.pdf"] == 0


@pytest.mark.asyncio
async def test_max_pages(site):
    base, hits = site
    crawler, results = await run_crawl([base], max_depth=5, max_pages=3, same_domain_only=True)
    assert len(results) == 3
    assert sum(hits.values()) == 3
    # Breadth first: the start page and depth-1 pages come before deeper ones
    assert all(page["depth"] <= 1 for page in results.values())
    assert crawler.get_stats()["queued"] > 0  # the rest stays in the queue


@pytest.mark.asyncio
async def test_multiple_start_urls(site):
    base, _ = site
    _, results = await run_crawl([base + "/a2", base + "/skip/1"], max_depth=3)
    assert paths(base, results) == {"/a2", "/skip/1"}


@pytest.mark.asyncio
async def test_invalid_start_urls_are_skipped(site):
    base, _ = site
    _, results = await run_crawl(["not a url", "mailto:me@x.com", base + "/a2"], max_depth=1)
    assert paths(base, results) == {"/a2"}


@pytest.mark.asyncio
async def test_crawl_respects_concurrency_limits(site):
    base, _ = site
    crawler, results = await run_crawl(
        [base + "/slow"], {"max_concurrent": 10, "max_per_domain": 2}, max_depth=1,
    )
    assert len(results) == 7
    concurrency = crawler.get_stats()["concurrency"]
    assert concurrency["peak_per_domain"] == {"127.0.0.1": 2}
    assert concurrency["active"] == 0


@pytest.mark.asyncio
async def test_stats_and_progress_output(site, capsys):
    base, _ = site
    crawler, results = await run_crawl(
        [base + "/slow"], {"max_concurrent": 2, "progress_interval": 0.1}, max_depth=1,
    )
    out = capsys.readouterr().out
    assert "processed:" in out and "queue:" in out and "errors:" in out and "pages/s" in out
    assert len(out.strip().splitlines()) >= 2  # periodic lines plus the final one
    stats = crawler.get_stats()
    assert stats["processed"] == len(results) == 7
    assert stats["queued"] == stats["in_progress"] == 0
    assert stats["pages_per_sec"] > 0
    assert stats["elapsed"] > 0


@pytest.mark.asyncio
async def test_crawl_resets_state_between_runs(site):
    base, _ = site
    async with AsyncCrawler(progress_interval=None) as crawler:
        await crawler.crawl([base], max_depth=1, same_domain_only=True)
        results = await crawler.crawl([base + "/a2"], max_depth=1)
    assert paths(base, results) == {"/a2"}
    assert paths(base, crawler.visited_urls) == {"/a2"}


@pytest.mark.asyncio
async def test_crawl_validates_arguments():
    async with AsyncCrawler() as crawler:
        with pytest.raises(ValueError):
            await crawler.crawl(["https://example.com"], max_pages=0)
        with pytest.raises(ValueError):
            await crawler.crawl(["https://example.com"], max_depth=-1)
    with pytest.raises(ValueError):
        AsyncCrawler(max_depth=-1)

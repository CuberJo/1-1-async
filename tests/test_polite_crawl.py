"""AsyncCrawler politeness tests: robots.txt, rate limits, User-Agent, retries."""

import logging
import time
from collections import Counter, defaultdict

import pytest
import pytest_asyncio
from aiohttp import web

from crawler import AsyncCrawler

TOLERANCE = 0.02

ROBOTS = """
User-agent: *
Disallow: /private/

User-agent: MyBot
Disallow: /secret
"""

SITE = {
    "/": ["/private/1", "/private/2", "/a", "/b", "/c", "/secret"],
    "/a": ["/private/3"],
    "/b": [],
    "/c": [],
    "/private/1": [],
    "/private/2": [],
    "/private/3": [],
    "/secret": [],
}


def page_html(links: list[str]) -> str:
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return f"<html><head><title>Page</title></head><body>{anchors}</body></html>"


class Site:
    def __init__(self, base: str):
        self.base = base
        self.hits: Counter[str] = Counter()
        self.times: list[float] = []  # page requests only, robots.txt excluded
        self.user_agents: dict[str, list[str]] = defaultdict(list)

    def gaps(self) -> list[float]:
        return [b - a for a, b in zip(self.times, self.times[1:])]


@pytest_asyncio.fixture
async def make_site():
    runners = []

    async def factory(robots: str | None = ROBOTS, host: str = "127.0.0.1") -> Site:
        site: Site | None = None
        flaky = Counter()

        async def handler(request):
            path = request.path
            site.hits[path] += 1
            if path == "/robots.txt":
                if robots is None:
                    raise web.HTTPNotFound()
                return web.Response(text=robots)
            site.times.append(time.monotonic())
            site.user_agents[path].append(request.headers.get("User-Agent", ""))
            if path == "/flaky":  # fails twice, then works
                flaky[path] += 1
                if flaky[path] <= 2:
                    raise web.HTTPServiceUnavailable()
            elif path == "/limited":  # rate limited once, asks to wait
                flaky[path] += 1
                if flaky[path] == 1:
                    raise web.HTTPTooManyRequests(headers={"Retry-After": "0.3"})
            elif path == "/down":
                raise web.HTTPBadGateway()
            elif path not in SITE:
                raise web.HTTPNotFound()
            return web.Response(text=page_html(SITE.get(path, [])), content_type="text/html")

        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        tcp = web.TCPSite(runner, host, 0)
        await tcp.start()
        runners.append(runner)
        port = tcp._server.sockets[0].getsockname()[1]
        site = Site(f"http://{host}:{port}")
        return site

    yield factory
    for runner in runners:
        await runner.cleanup()


def crawler_for(**kwargs) -> AsyncCrawler:
    options = {"max_concurrent": 5, "read_timeout": 2, "progress_interval": None}
    options.update(kwargs)
    return AsyncCrawler(**options)


def paths(base: str, urls) -> set[str]:
    return {url.removeprefix(base) for url in urls}


# --- robots.txt ---

@pytest.mark.asyncio
async def test_disallowed_urls_are_blocked_and_logged(make_site, caplog):
    site = await make_site()
    caplog.set_level(logging.WARNING, logger="crawler")
    async with crawler_for(respect_robots=True, user_agent="TestBot/1.0") as crawler:
        results = await crawler.crawl([site.base], max_depth=2)
    assert paths(site.base, results) == {"/", "/a", "/b", "/c", "/secret"}
    assert paths(site.base, crawler.blocked_urls) == {"/private/1", "/private/2", "/private/3"}
    assert not any(p.startswith("/private/") for p in site.hits)  # never requested
    assert site.hits["/robots.txt"] == 1  # loaded once, then cached
    stats = crawler.get_stats()
    assert stats["robots_blocked"] == stats["blocked"] == 3
    assert stats["failed"] == 0
    blocked_logs = [r.getMessage() for r in caplog.records if "Blocked by robots.txt" in r.getMessage()]
    assert len(blocked_logs) == 3


@pytest.mark.asyncio
async def test_rules_for_the_configured_user_agent(make_site):
    site = await make_site()
    async with crawler_for(respect_robots=True, user_agent="MyBot/1.0") as crawler:
        results = await crawler.crawl([site.base], max_depth=2)
    # MyBot has its own group: /secret is off limits, /private/ is not
    assert paths(site.base, crawler.blocked_urls) == {"/secret"}
    assert "/private/1" in paths(site.base, results)


@pytest.mark.asyncio
async def test_robots_ignored_when_disabled(make_site):
    site = await make_site()
    async with crawler_for(respect_robots=False) as crawler:
        results = await crawler.crawl([site.base], max_depth=2)
    assert len(results) == len(SITE)
    assert site.hits["/robots.txt"] == 0
    assert not crawler.blocked_urls


@pytest.mark.asyncio
async def test_blocked_urls_do_not_count_against_max_pages(make_site):
    site = await make_site()
    async with crawler_for(respect_robots=True, max_concurrent=1) as crawler:
        results = await crawler.crawl([site.base], max_depth=1, max_pages=4)
    assert len(results) == 4  # "/", a, b, c — the blocked ones were skipped for free
    assert len(crawler.blocked_urls) == 2


@pytest.mark.asyncio
async def test_fetch_url_checks_robots(make_site):
    site = await make_site()
    async with crawler_for(respect_robots=True) as crawler:
        assert await crawler.fetch_url(site.base + "/private/1") == ""
        result = crawler.results[site.base + "/private/1"]
        assert result.blocked and result.error == "Blocked by robots.txt"
        assert await crawler.fetch_url(site.base + "/a")
    assert site.hits["/private/1"] == 0


@pytest.mark.asyncio
async def test_missing_robots_allows_everything(make_site):
    site = await make_site(robots=None)
    async with crawler_for(respect_robots=True) as crawler:
        results = await crawler.crawl([site.base], max_depth=2)
    assert len(results) == len(SITE)


@pytest.mark.asyncio
async def test_crawl_delay_from_robots(make_site):
    site = await make_site("User-agent: *\nCrawl-delay: 0.2\n")
    async with crawler_for(respect_robots=True) as crawler:
        await crawler.crawl([site.base], max_depth=1, max_pages=4)
        assert crawler.rate_limiter.domain_delays == {"127.0.0.1": 0.2}
    assert len(site.times) == 4
    assert all(g >= 0.2 - TOLERANCE for g in site.gaps())


# --- rate limiting ---

@pytest.mark.asyncio
async def test_requests_per_second_in_crawl(make_site):
    site = await make_site()
    async with crawler_for(requests_per_second=10) as crawler:
        await crawler.crawl([site.base], max_depth=2)
    assert len(site.times) == len(SITE)
    assert all(g >= 0.1 - TOLERANCE for g in site.gaps())
    rate = crawler.get_stats()["rate"]
    assert rate["total_requests"] == len(SITE)
    assert rate["min_gap"] >= 0.1 - TOLERANCE
    assert 0.1 - TOLERANCE <= rate["avg_gap"] < 0.2
    assert 0 < rate["current_rps"] <= 10.5


@pytest.mark.asyncio
async def test_min_delay_and_jitter_in_crawl(make_site):
    site = await make_site()
    async with crawler_for(min_delay=0.1, jitter=0.05) as crawler:
        await crawler.crawl([site.base], max_depth=1, max_pages=5)
    g = site.gaps()
    assert len(g) == 4
    assert all(0.1 - TOLERANCE <= x <= 0.15 + 0.05 for x in g)


@pytest.mark.asyncio
async def test_different_domains_are_limited_separately(make_site):
    # Two loopback addresses are two different domains for the limiter
    one, two = await make_site(), await make_site(host="127.0.0.2")
    start = time.monotonic()
    async with crawler_for(requests_per_second=10) as crawler:
        await crawler.fetch_urls([one.base + p for p in ("/a", "/b", "/c")] +
                                 [two.base + p for p in ("/a", "/b", "/c")])
    elapsed = time.monotonic() - start
    assert all(g >= 0.1 - TOLERANCE for g in one.gaps())
    assert all(g >= 0.1 - TOLERANCE for g in two.gaps())
    assert elapsed < 0.4  # in parallel: ~0.2s, one after another it would be 0.5s


@pytest.mark.asyncio
async def test_global_rate_limit(make_site):
    one, two = await make_site(), await make_site(host="127.0.0.2")
    async with crawler_for(requests_per_second=10, per_domain_rate=False) as crawler:
        await crawler.fetch_urls([one.base + "/a", two.base + "/a", one.base + "/b", two.base + "/b"])
    times = sorted(one.times + two.times)
    assert all(b - a >= 0.1 - TOLERANCE for a, b in zip(times, times[1:]))


@pytest.mark.asyncio
async def test_progress_shows_rate_and_blocked(make_site, capsys):
    site = await make_site()
    async with crawler_for(respect_robots=True, requests_per_second=20, progress_interval=0.1) as crawler:
        await crawler.crawl([site.base], max_depth=2)
    out = capsys.readouterr().out
    assert "blocked:   3" in out and "req/s" in out and "avg gap:" in out


# --- User-Agent ---

@pytest.mark.asyncio
async def test_custom_user_agent_is_sent(make_site):
    site = await make_site()
    async with crawler_for(user_agent="MyBot/1.0") as crawler:
        await crawler.fetch_url(site.base + "/a")
    assert site.user_agents["/a"] == ["MyBot/1.0"]


@pytest.mark.asyncio
async def test_user_agent_rotation(make_site):
    site = await make_site()
    agents = ["BotA/1.0", "BotB/1.0", "BotC/1.0"]
    async with crawler_for(user_agent=agents, max_concurrent=1) as crawler:
        for _ in range(2):
            for p in ("/a", "/b", "/c"):
                await crawler.fetch_url(site.base + p)
        assert crawler.user_agent == "BotA/1.0"  # robots.txt is checked for it
    sent = [ua for p in ("/a", "/b", "/c") for ua in site.user_agents[p]]
    assert Counter(sent) == {ua: 2 for ua in agents}


def test_user_agent_validation():
    with pytest.raises(ValueError):
        AsyncCrawler(user_agent=[])
    with pytest.raises(ValueError):
        AsyncCrawler(max_retries=-1)


# --- retries with exponential backoff ---

@pytest.mark.asyncio
async def test_temporary_errors_are_retried_with_backoff(make_site):
    site = await make_site()
    start = time.monotonic()
    async with crawler_for(max_retries=3, backoff_base=0.05) as crawler:
        html = await crawler.fetch_url(site.base + "/flaky")
    assert html
    result = crawler.results[site.base + "/flaky"]
    assert result.attempts == 3 and crawler.retries == 2
    # Waits 0.05s, then 0.1s
    g = site.gaps()
    assert g[0] >= 0.05 - TOLERANCE and g[1] >= 0.1 - TOLERANCE
    assert time.monotonic() - start >= 0.15 - TOLERANCE


@pytest.mark.asyncio
async def test_retry_after_is_honoured(make_site):
    site = await make_site()
    async with crawler_for(max_retries=1, backoff_base=0.01) as crawler:
        assert await crawler.fetch_url(site.base + "/limited")
    assert site.gaps()[0] >= 0.3 - TOLERANCE


@pytest.mark.asyncio
async def test_gives_up_after_max_retries(make_site):
    site = await make_site()
    async with crawler_for(max_retries=2, backoff_base=0.01) as crawler:
        assert await crawler.fetch_url(site.base + "/down") == ""
    result = crawler.results[site.base + "/down"]
    assert result.error.startswith("HTTP 502") and result.attempts == 3
    assert site.hits["/down"] == 3


@pytest.mark.asyncio
async def test_permanent_errors_are_not_retried(make_site):
    site = await make_site()
    async with crawler_for(max_retries=3, backoff_base=0.01) as crawler:
        assert await crawler.fetch_url(site.base + "/missing") == ""
    assert site.hits["/missing"] == 1
    assert crawler.retries == 0

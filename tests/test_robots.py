"""robots.txt parsing, matching and loading tests."""

import asyncio
from collections import Counter

import pytest
import pytest_asyncio
from aiohttp import web

from robots_parser import RobotsParser, parse_robots

ROBOTS = """
# Comments and blank lines are ignored
User-agent: *
Disallow: /private/
Disallow: /tmp      # prefix: /tmp, /tmp/x, /tmpfile
Allow: /private/public-page
Disallow: /*.pdf$
Disallow: /search?
Crawl-delay: 1.5

User-agent: BadBot
User-agent: EvilBot
Disallow: /

User-agent: MyBot
Disallow: /only-mybot/
Allow: /tmp
Crawl-delay: 0.5

Sitemap: https://example.com/sitemap.xml
"""


@pytest.mark.parametrize("path, allowed", [
    ("/", True),
    ("/about", True),
    ("/private/", False),
    ("/private/page", False),
    ("/private/public-page", True),  # longer Allow beats shorter Disallow
    ("/tmp", False),
    ("/tmpfile", False),
    ("/docs/report.pdf", False),
    ("/docs/report.pdf?x=1", True),  # "$" anchors the end
    ("/search?q=1", False),
    ("/search", True),
    ("/only-mybot/", True),  # rules of another group
])
def test_rules_for_any_bot(path, allowed):
    assert parse_robots(ROBOTS).can_fetch(path, "SomeBot/2.0") is allowed


@pytest.mark.parametrize("path, allowed", [
    ("/only-mybot/x", False),
    ("/private/page", True),  # MyBot's group replaces the "*" group
    ("/tmp", True),
])
def test_rules_for_specific_bot(path, allowed):
    assert parse_robots(ROBOTS).can_fetch(path, "MyBot/1.0 (+https://my.bot)") is allowed


def test_grouped_user_agents_share_rules():
    rules = parse_robots(ROBOTS)
    assert not rules.can_fetch("/", "BadBot")
    assert not rules.can_fetch("/anything", "evilbot/3")  # case-insensitive


def test_crawl_delay_per_agent():
    rules = parse_robots(ROBOTS)
    assert rules.crawl_delay("*") == 1.5
    assert rules.crawl_delay("MyBot/1.0") == 0.5
    assert rules.crawl_delay("BadBot") == 0.0


def test_to_dict_and_sitemaps():
    data = parse_robots(ROBOTS).to_dict()
    assert data["sitemaps"] == ["https://example.com/sitemap.xml"]
    assert data["groups"][1]["user_agents"] == ["badbot", "evilbot"]
    assert data["groups"][0]["allow"] == ["/private/public-page"]
    assert "/private/" in data["groups"][0]["disallow"]
    assert data["groups"][2]["crawl_delay"] == 0.5


def test_tie_goes_to_allow_and_empty_disallow():
    rules = parse_robots("User-agent: *\nDisallow: /page\nAllow: /page\n")
    assert rules.can_fetch("/page", "*")
    rules = parse_robots("User-agent: *\nDisallow:\n")
    assert rules.can_fetch("/anything", "*")


def test_broken_input_is_tolerated():
    rules = parse_robots("Disallow: /before-any-agent\ngarbage line\nUser-agent: *\nCrawl-delay: soon\nDisallow: /x\n")
    assert rules.can_fetch("/before-any-agent", "*")
    assert not rules.can_fetch("/x", "*")
    assert rules.crawl_delay("*") == 0.0
    assert parse_robots("").can_fetch("/", "*")


def test_percent_encoding_is_normalized():
    rules = parse_robots("User-agent: *\nDisallow: /caf%C3%A9\n")
    assert not rules.can_fetch("/café", "*")
    assert not rules.can_fetch("/caf%C3%A9/menu", "*")


# --- loading over HTTP ---

@pytest_asyncio.fixture
async def robots_server():
    """robots.txt per host is chosen by a prefix: /ok/, /missing/, /broken/."""
    hits: Counter[str] = Counter()

    async def handler(request):
        hits[request.path] += 1
        await asyncio.sleep(0.05)  # lets concurrent callers overlap
        return web.Response(text=ROBOTS)

    async def missing(request):
        hits[request.path] += 1
        raise web.HTTPNotFound()

    async def broken(request):
        hits[request.path] += 1
        raise web.HTTPServiceUnavailable()

    apps = []
    for h in (handler, missing, broken):
        app = web.Application()
        app.router.add_get("/robots.txt", h)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        apps.append((runner, f"http://127.0.0.1:{port}"))
    yield [base for _, base in apps], hits
    for runner, _ in apps:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_fetch_robots_and_check_urls(robots_server):
    (ok, _, _), hits = robots_server
    parser = RobotsParser()
    try:
        data = await parser.fetch_robots(ok + "/some/page")
        assert data["status"] == 200
        assert len(data["groups"]) == 3
        assert parser.can_fetch(ok + "/about")
        assert not parser.can_fetch(ok + "/private/x")
        assert not parser.can_fetch(ok + "/search?q=1")
        assert parser.can_fetch(ok + "/robots.txt")
        assert not parser.can_fetch(ok + "/", "BadBot")
        assert parser.get_crawl_delay() == 1.5  # site of the last fetch_robots
        assert parser.get_crawl_delay("MyBot", ok + "/") == 0.5
    finally:
        await parser.close()


@pytest.mark.asyncio
async def test_robots_is_cached_per_site(robots_server):
    (ok, missing, _), hits = robots_server
    parser = RobotsParser()
    try:
        # Concurrent calls for one site share a single download
        await asyncio.gather(*(parser.fetch_robots(f"{ok}/p{i}") for i in range(5)))
        await parser.fetch_robots(ok + "/again")
        await parser.fetch_robots(missing + "/")
        assert hits["/robots.txt"] == 2  # one per site
        assert parser.get_rules(ok + "/x") is not parser.get_rules(missing + "/x")
    finally:
        await parser.close()


@pytest.mark.asyncio
async def test_missing_robots_allows_everything(robots_server):
    (_, missing, _), _ = robots_server
    parser = RobotsParser()
    try:
        data = await parser.fetch_robots(missing)
        assert data["status"] == 404
        assert parser.can_fetch(missing + "/private/x")
        assert parser.get_crawl_delay(url=missing) == 0.0
    finally:
        await parser.close()


@pytest.mark.asyncio
async def test_unavailable_robots_disallows_everything(robots_server):
    (_, _, broken), _ = robots_server
    parser = RobotsParser()
    try:
        data = await parser.fetch_robots(broken)
        assert data["status"] == 503 and data["disallow_all"]
        assert not parser.can_fetch(broken + "/")
    finally:
        await parser.close()


@pytest.mark.asyncio
async def test_unreachable_site_allows_everything():
    parser = RobotsParser(timeout=2)
    try:
        data = await parser.fetch_robots("http://robots.invalid/")
        assert data["status"] is None
        assert parser.can_fetch("http://robots.invalid/page")
    finally:
        await parser.close()


def test_not_loaded_site_is_allowed():
    assert RobotsParser().can_fetch("https://never-loaded.example/private/")

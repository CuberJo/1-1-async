"""AdvancedCrawler: config, sitemaps, statistics, exports, sessions, CLI end to end."""

import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter

import pytest
import pytest_asyncio
from aiohttp import web

from advanced_crawler import AdvancedCrawler, build_storage
from cli import main
from config import CrawlerConfig, StorageSettings
from storage import CSVStorage, JSONStorage, MultiStorage, SQLiteStorage

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")

# / -> /a, /b ; /a -> /a/1 ; /b -> /missing, /private/x ; sitemap -> /s/1../s/3, /private/y
LINKS = {
    "/": ["/a", "/b"],
    "/a": ["/a/1", "/"],
    "/b": ["/missing", "/private/x", "/login"],
    "/a/1": [],
    "/private/x": [],
    "/login": [],
}


def page(path: str, links: list[str]) -> str:
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return f"<html><head><title>Page {path}</title></head><body><p>Text of {path}</p>{anchors}</body></html>"


class Site:
    def __init__(self):
        self.hits: Counter[str] = Counter()
        self.headers: list[dict] = []
        self.base = ""


@pytest_asyncio.fixture
async def site():
    s = Site()

    async def handler(request: web.Request) -> web.Response:
        path = request.path
        s.hits[path] += 1
        s.headers.append(dict(request.headers))
        if path == "/robots.txt":
            return web.Response(text=f"User-agent: *\nDisallow: /private/\nSitemap: {s.base}/sitemap.xml\n")
        if path == "/sitemap.xml":
            locs = "".join(f"<url><loc>{s.base}{p}</loc></url>"
                           for p in ("/s/1", "/s/2", "/s/3", "/private/y", "/login"))
            return web.Response(text=f"<urlset>{locs}</urlset>", content_type="application/xml")
        if path == "/set-cookie":
            response = web.Response(text=page(path, ["/needs-cookie"]), content_type="text/html")
            response.set_cookie("session", "abc123")
            return response
        if path == "/needs-cookie":
            if request.cookies.get("session") != "abc123":
                raise web.HTTPForbidden()
            return web.Response(text=page(path, []), content_type="text/html")
        if path.startswith("/s/"):
            return web.Response(text=page(path, []), content_type="text/html")
        if path not in LINKS:
            raise web.HTTPNotFound()
        return web.Response(text=page(path, LINKS[path]), content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    s.base = f"http://127.0.0.1:{tcp._server.sockets[0].getsockname()[1]}"
    yield s
    await runner.cleanup()


def make_config(site: Site, tmp_path, **sections) -> CrawlerConfig:
    data = {
        "start_urls": [site.base + "/"],
        "crawler": {"max_pages": 50, "max_depth": 2, "max_concurrent": 4, "read_timeout": 2,
                    "max_retries": 0, **sections.pop("crawler", {})},
        "filters": {"exclude_patterns": ["/login"], **sections.pop("filters", {})},
        "logging": {"file": None},
        "progress_interval": None,
        **sections,
    }
    return CrawlerConfig.from_dict(data)


def paths(site: Site, urls) -> set[str]:
    return {u.removeprefix(site.base) for u in urls}


@pytest.mark.asyncio
async def test_crawl_from_config_with_robots_and_stats(site, tmp_path):
    crawler = AdvancedCrawler(make_config(site, tmp_path))
    try:
        await crawler.crawl()
    finally:
        await crawler.close()
    assert paths(site, crawler.processed_urls) == {"/", "/a", "/b", "/a/1"}
    assert paths(site, crawler.failed_urls) == {"/missing"}
    assert paths(site, crawler.blocked_urls) == {"/private/x"}
    assert site.hits["/login"] == 0 and site.hits["/private/x"] == 0

    stats = crawler.get_stats()
    assert (stats["total_pages"], stats["successful"], stats["failed"], stats["blocked"]) == (5, 4, 1, 1)
    assert stats["status_codes"] == {"200": 4, "404": 1}
    assert stats["errors_by_type"] == {"PermanentError": 1}
    assert stats["top_domains"][0]["pages"] == 5
    assert stats["depths"] == {"0": 1, "1": 2, "2": 2}
    assert stats["uptime"] > 0 and stats["avg_speed"] > 0
    # AsyncCrawler counters are still there
    assert stats["processed"] == 4 and "concurrency" in stats and "rate" in stats


@pytest.mark.asyncio
async def test_sitemap_pages_are_crawled_through_the_filters(site, tmp_path):
    config = make_config(site, tmp_path, sitemap={"urls": [site_url := "{base}/sitemap.xml"]})
    config.sitemap.urls = [site_url.format(base=site.base)]
    config.crawler.max_depth = 0
    async with AdvancedCrawler(config) as crawler:
        await crawler.crawl()
    # /login is excluded by the filter; /private/y is blocked by robots.txt
    assert paths(site, crawler.sitemap_urls) == {"/s/1", "/s/2", "/s/3", "/private/y"}
    assert paths(site, crawler.processed_urls) == {"/", "/s/1", "/s/2", "/s/3"}
    assert paths(site, crawler.blocked_urls) == {"/private/y"}
    assert site.hits["/login"] == 0


@pytest.mark.asyncio
async def test_sitemap_discovery_from_robots(site, tmp_path):
    config = make_config(site, tmp_path, sitemap={"discover": True}, crawler={"max_depth": 0})
    async with AdvancedCrawler(config) as crawler:
        await crawler.crawl()
    assert {"/s/1", "/s/2", "/s/3"} <= paths(site, crawler.processed_urls)


@pytest.mark.asyncio
async def test_sitemap_only_and_nothing_to_crawl(site, tmp_path):
    config = make_config(site, tmp_path, crawler={"respect_robots": False})
    config.start_urls = []
    async with AdvancedCrawler(config) as crawler:
        await crawler.crawl(sitemaps=[site.base + "/sitemap.xml"], max_depth=0)
        assert paths(site, crawler.processed_urls) == {"/s/1", "/s/2", "/s/3"}
        assert paths(site, crawler.failed_urls) == {"/private/y"}  # robots.txt ignored: a 404
        with pytest.raises(ValueError, match="nothing to crawl"):
            await crawler.crawl(sitemaps=[site.base + "/missing.xml"])


@pytest.mark.asyncio
async def test_arguments_override_config(site, tmp_path):
    async with AdvancedCrawler(make_config(site, tmp_path)) as crawler:
        await crawler.crawl(max_pages=2, max_depth=1, exclude_patterns=[])
    assert len(crawler.visited_urls) == 2 and crawler.get_stats()["total_pages"] == 2


@pytest.mark.asyncio
async def test_example_from_the_task(site, tmp_path, monkeypatch):
    """The usage example: from_config -> crawl -> get_stats -> report -> close."""
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        f"start_urls: ['{site.base}/']\n"
        "crawler: {max_pages: 20, read_timeout: 2, max_retries: 0}\n"
        "progress_interval: null\nlogging: {file: null}\n",
        encoding="utf-8",
    )
    from crawler import AdvancedCrawler as Exported  # the import the task uses

    crawler = Exported.from_config(str(config_file))
    await crawler.crawl()
    stats = crawler.get_stats()
    assert stats["total_pages"] == stats["successful"] + stats["failed"] == 6
    crawler.export_to_html_report(str(tmp_path / "report.html"))
    await crawler.close()
    assert "Failed URLs" in (tmp_path / "report.html").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_run_writes_storage_stats_and_report(site, tmp_path):
    out = tmp_path / "out"
    config = make_config(
        site, tmp_path,
        storage=[str(out / "pages.jsonl"), str(out / "pages.db"), str(out / "pages.csv")],
        output={"stats_json": str(out / "stats.json"), "html_report": str(out / "report.html")},
    )
    async with AdvancedCrawler(config) as crawler:
        stats = await crawler.run()
    assert stats["storage"]["saved"] == 4 * 3
    lines = (out / "pages.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 4 and json.loads(lines[0])["status_code"] == 200
    with sqlite3.connect(out / "pages.db") as db:
        assert db.execute("SELECT COUNT(*) FROM pages").fetchone()[0] == 4

    data = json.loads((out / "stats.json").read_text(encoding="utf-8"))
    assert data["successful"] == 4 and list(data["failed_urls"]) == [site.base + "/missing"]
    assert data["robots_blocked_urls"] == [site.base + "/private/x"]
    assert data["crawler"]["storage"]["saved"] == 12 and data["config"]["crawler.max_pages"] == "50"
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "Crawl report" in html and site.base + "/missing" in html


def test_build_storage(tmp_path):
    assert build_storage([]) is None
    single = build_storage([StorageSettings(path=str(tmp_path / "a.json"), type="json")])
    assert isinstance(single, JSONStorage) and not single.lines and single.indent == 2
    multi = build_storage([
        StorageSettings(path=str(tmp_path / "a.jsonl"), type="jsonl", options={"batch_size": 5}),
        StorageSettings(path=str(tmp_path / "a.csv"), type="csv"),
        StorageSettings(path=str(tmp_path / "a.db"), type="sqlite"),
    ])
    assert isinstance(multi, MultiStorage)
    assert [type(s) for s in multi.storages] == [JSONStorage, CSVStorage, SQLiteStorage]
    assert multi.storages[0].lines and multi.storages[0].batch_size == 5


@pytest.mark.asyncio
async def test_keep_pages_false_keeps_memory_flat(site, tmp_path):
    config = make_config(site, tmp_path, crawler={"keep_pages": False}, storage=[str(tmp_path / "p.jsonl")])
    async with AdvancedCrawler(config) as crawler:
        await crawler.crawl()
    page = crawler.processed_urls[site.base + "/a"]
    assert set(page) == {"url", "title", "depth", "links_count", "errors"}
    assert page["title"] == "Page /a" and page["links_count"] == 2
    assert all(r.content is None for r in crawler.results.values())
    saved = (tmp_path / "p.jsonl").read_text(encoding="utf-8")
    assert saved.count("\n") == 4 and "Text of /a" in saved  # the full page is in the storage


@pytest.mark.asyncio
async def test_headers_cookies_and_session(site, tmp_path):
    config = make_config(site, tmp_path, crawler={
        "headers": {"Accept-Language": "ru"}, "cookies": {"theme": "dark"}, "user_agent": "TestBot/7",
    })
    config.start_urls = [site.base + "/set-cookie"]
    async with AdvancedCrawler(config) as crawler:
        await crawler.crawl(max_depth=1)
    # The cookie set by the first page was sent with the next request
    assert site.base + "/needs-cookie" in crawler.processed_urls
    page_requests = [h for h in site.headers if "Accept-Language" in h]
    assert page_requests and all(h["Accept-Language"] == "ru" for h in page_requests)
    assert all(h["User-Agent"] == "TestBot/7" for h in site.headers)
    assert all("theme=dark" in h.get("Cookie", "") for h in page_requests)


@pytest.mark.asyncio
async def test_requests_go_through_the_proxy(site, tmp_path):
    seen: list[str] = []

    async def proxy(request: web.Request) -> web.Response:
        # A plain HTTP proxy gets the absolute URL in the request line
        seen.append(str(request.url))
        return web.Response(text=page("/", []), content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", proxy)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    proxy_url = f"http://127.0.0.1:{tcp._server.sockets[0].getsockname()[1]}"
    try:
        config = make_config(site, tmp_path, crawler={"proxy": proxy_url})
        config.start_urls = ["http://crawl-target.test/"]
        async with AdvancedCrawler(config) as crawler:
            await crawler.crawl()
    finally:
        await runner.cleanup()
    assert crawler.get_stats()["successful"] == 1
    assert {"http://crawl-target.test/robots.txt", "http://crawl-target.test/"} <= set(seen)
    assert sum(site.hits.values()) == 0  # nothing went around the proxy


# --- command line, end to end -----------------------------------------------


@pytest.mark.asyncio
async def test_cli_main_end_to_end(site, tmp_path, capsys):
    out = tmp_path / "out"
    argv = [
        "--urls", site.base + "/", "--max-pages", "10", "--max-depth", "1",
        "--output", str(out / "results.json"), "--stats", str(out / "stats.json"),
        "--report", str(out / "report.html"), "--log-file", str(tmp_path / "logs" / "c.log"),
        "--rate-limit", "100", "--exclude", "/login",
    ]
    # main() runs its own event loop, so it goes to a thread here
    import asyncio

    code = await asyncio.to_thread(main, argv)
    assert code == 0
    printed = capsys.readouterr().out
    assert "Pages processed: 3" in printed and "successful 3" in printed
    assert "100.0%" in printed  # the final progress line
    results = json.loads((out / "results.json").read_text(encoding="utf-8"))
    assert {r["url"] for r in results} == {site.base + p for p in ("/", "/a", "/b")}
    assert json.loads((out / "stats.json").read_text(encoding="utf-8"))["total_pages"] == 3
    assert (out / "report.html").exists()
    log = (tmp_path / "logs" / "c.log").read_text(encoding="utf-8")
    assert "INFO" in log and "Crawl started" in log and "Success:" in log


def test_crawler_py_is_the_command_line():
    result = subprocess.run(
        [sys.executable, os.path.join(SRC, "crawler.py"), "--help"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0
    for option in ("--urls", "--max-pages", "--max-depth", "--output", "--config",
                   "--respect-robots", "--rate-limit"):
        assert option in result.stdout
    bad = subprocess.run(
        [sys.executable, os.path.join(SRC, "crawler.py"), "--max-pages", "5"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert bad.returncode == 2 and "nothing to crawl" in bad.stderr


@pytest.mark.asyncio
async def test_parse_workers_give_the_same_result(site, tmp_path):
    async with AdvancedCrawler(make_config(site, tmp_path)) as crawler:
        in_thread = await crawler.crawl()
    async with AdvancedCrawler(make_config(site, tmp_path, crawler={"parse_workers": 2})) as crawler:
        in_processes = await crawler.crawl()
        assert crawler._parse_pool is not None
    assert crawler._parse_pool is None  # close() shut it down
    assert in_processes == in_thread


@pytest.mark.asyncio
async def test_broken_parse_executor_falls_back_to_a_thread():
    from concurrent.futures import ProcessPoolExecutor

    from html_parser import HTMLParser

    pool = ProcessPoolExecutor(1)
    pool.shutdown()
    page = await HTMLParser().parse_html("<title>T</title><a href='/x'>x</a>", "https://a.org/", pool)
    assert page["title"] == "T" and page["links"] == ["https://a.org/x"]


@pytest.mark.asyncio
async def test_second_crawl_reads_the_sitemaps_again(site, tmp_path):
    config = make_config(site, tmp_path, crawler={"max_depth": 0})
    async with AdvancedCrawler(config) as crawler:
        await crawler.crawl(sitemaps=[site.base + "/sitemap.xml", site.base + "/missing.xml"])
        assert site.base + "/missing.xml" in crawler.sitemaps.errors
        await crawler.crawl(sitemaps=[site.base + "/sitemap.xml"])
    assert paths(site, crawler.sitemap_urls) == {"/s/1", "/s/2", "/s/3", "/private/y"}
    assert {"/s/1", "/s/2", "/s/3"} <= paths(site, crawler.processed_urls)
    assert crawler.sitemaps.errors == {}  # the first crawl's error is gone
    assert site.hits["/sitemap.xml"] == 2

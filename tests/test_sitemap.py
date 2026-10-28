"""Sitemap tests: parsing, indexes, recursion, gzip, errors, discovery."""

import gzip

import pytest
import pytest_asyncio
from aiohttp import web

from sitemap_parser import SitemapParser, parse_sitemap

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def urlset(urls: list[str], ns: str = NS) -> str:
    entries = "".join(f"<url><loc>{u}</loc><lastmod>2026-10-01</lastmod></url>" for u in urls)
    return f'<?xml version="1.0" encoding="UTF-8"?><urlset {ns}>{entries}</urlset>'


def index(sitemaps: list[str]) -> str:
    entries = "".join(f"<sitemap><loc>{s}</loc></sitemap>" for s in sitemaps)
    return f'<?xml version="1.0" encoding="UTF-8"?><sitemapindex {NS}>{entries}</sitemapindex>'


# --- parse_sitemap -------------------------------------------------------


def test_parse_urlset():
    kind, urls = parse_sitemap(urlset(["https://a.org/1", "https://a.org/2"]).encode())
    assert kind == "urlset" and urls == ["https://a.org/1", "https://a.org/2"]


def test_parse_index():
    kind, urls = parse_sitemap(index(["https://a.org/s1.xml", "https://a.org/s2.xml"]).encode())
    assert kind == "index" and urls == ["https://a.org/s1.xml", "https://a.org/s2.xml"]


def test_parse_without_namespace_and_with_whitespace():
    xml = "\n  <urlset><url>\n<loc>\n  https://a.org/x \n</loc></url><url><loc></loc></url></urlset>"
    assert parse_sitemap(xml.encode()) == ("urlset", ["https://a.org/x"])


def test_parse_ignores_image_extension_locs():
    xml = (f'<urlset {NS} xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
           "<url><loc>https://a.org/p</loc><image:image><image:loc>https://a.org/i.png</image:loc>"
           "</image:image></url></urlset>")
    assert parse_sitemap(xml.encode()) == ("urlset", ["https://a.org/p"])


def test_parse_gzip_and_text():
    assert parse_sitemap(gzip.compress(urlset(["https://a.org/z"]).encode())) == ("urlset", ["https://a.org/z"])
    text = b"https://a.org/1\n\nnot a url\r\nhttps://a.org/2\n"
    assert parse_sitemap(text) == ("text", ["https://a.org/1", "https://a.org/2"])


@pytest.mark.parametrize("bad", [b"<urlset><url>", b"<html><body>Not found</body></html>"])
def test_parse_invalid(bad):
    with pytest.raises(ValueError):
        parse_sitemap(bad)


# --- fetching ------------------------------------------------------------


@pytest_asyncio.fixture
async def sitemap_site():
    """Index -> (urlset, nested index -> urlset.gz, broken, missing, itself)."""
    files: dict[str, bytes | str] = {}

    async def handler(request: web.Request) -> web.Response:
        body = files.get(request.path)
        if body is None:
            raise web.HTTPNotFound()
        if isinstance(body, bytes):
            return web.Response(body=body, content_type="application/gzip")
        return web.Response(text=body, content_type="application/xml")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    base = f"http://127.0.0.1:{tcp._server.sockets[0].getsockname()[1]}"
    files.update({
        "/robots.txt": f"User-agent: *\nDisallow:\nSitemap: {base}/sitemap_index.xml\n",
        "/sitemap_index.xml": index([
            f"{base}/pages.xml", "/nested.xml", f"{base}/broken.xml",
            f"{base}/missing.xml", f"{base}/sitemap_index.xml",
        ]),
        "/pages.xml": urlset([f"{base}/a", f"{base}/b", f"{base}/a"]),
        "/nested.xml": index([f"{base}/deep.xml.gz"]),
        "/deep.xml.gz": gzip.compress(urlset([f"{base}/c", f"{base}/b"]).encode()),
        "/broken.xml": "<urlset><url><loc>",
    })
    yield base, files
    await runner.cleanup()


@pytest.mark.asyncio
async def test_fetch_sitemap_index_recursively(sitemap_site):
    base, _ = sitemap_site
    async with SitemapParser() as parser:
        urls = await parser.fetch_sitemap(base + "/sitemap_index.xml")
    # Index order, duplicates removed, the relative child resolved, gzip unpacked
    assert urls == [f"{base}/a", f"{base}/b", f"{base}/c"]
    # A broken and a missing sitemap are recorded, the rest still used;
    # the index listing itself is not loaded twice
    assert set(parser.errors) == {f"{base}/broken.xml", f"{base}/missing.xml"}
    assert "404" in parser.errors[f"{base}/missing.xml"]


@pytest.mark.asyncio
async def test_max_depth_and_max_urls(sitemap_site):
    base, _ = sitemap_site
    async with SitemapParser(max_depth=1) as parser:
        urls = await parser.fetch_sitemap(base + "/sitemap_index.xml")
    assert f"{base}/c" not in urls and f"{base}/nested.xml" in parser.errors
    async with SitemapParser(max_urls=2) as parser:
        assert len(await parser.fetch_sitemap(base + "/sitemap_index.xml")) == 2


@pytest.mark.asyncio
async def test_unreachable_sitemap_returns_empty():
    async with SitemapParser(timeout=2) as parser:
        assert await parser.fetch_sitemap("http://127.0.0.1:1/sitemap.xml") == []
    assert "http://127.0.0.1:1/sitemap.xml" in parser.errors


@pytest.mark.asyncio
async def test_discover_from_robots_and_fallback(sitemap_site):
    base, files = sitemap_site
    async with SitemapParser() as parser:
        assert await parser.find_sitemaps(base + "/any/page") == [base + "/sitemap_index.xml"]
        assert await parser.discover(base + "/") == [f"{base}/a", f"{base}/b", f"{base}/c"]
    # No Sitemap: line -> /sitemap.xml
    files["/robots.txt"] = "User-agent: *\nDisallow:\n"
    files["/sitemap.xml"] = urlset([f"{base}/only"])
    async with SitemapParser() as parser:
        assert await parser.find_sitemaps(base) == [base + "/sitemap.xml"]
        assert await parser.discover(base) == [f"{base}/only"]


@pytest.mark.asyncio
async def test_same_parser_fetches_again_and_reset(sitemap_site):
    base, _ = sitemap_site
    async with SitemapParser() as parser:
        first = await parser.fetch_sitemap(base + "/sitemap_index.xml")
        second = await parser.fetch_sitemap(base + "/sitemap_index.xml")
        assert first == second == [f"{base}/a", f"{base}/b", f"{base}/c"]
        assert base + "/pages.xml" in parser.loaded and parser.errors
        parser.reset()
        assert not parser.loaded and not parser.errors

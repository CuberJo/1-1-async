"""HTMLParser and fetch_and_parse tests (local server, no internet needed)."""

import logging

import pytest
import pytest_asyncio
from aiohttp import web
from bs4 import BeautifulSoup

from crawler import AsyncCrawler
from html_parser import HTMLParser, normalize_url

BASE = "https://example.com/blog/post.html"

VALID_HTML = """<!doctype html>
<html lang="en">
<head>
  <title>  Test   Page </title>
  <meta name="description" content="A page for tests">
  <meta name="keywords" content="python, async , crawler">
  <meta property="og:title" content="OG title">
  <link rel="canonical" href="https://example.com/canonical">
  <style>body { color: red }</style>
  <script>var hidden = "script text";</script>
</head>
<body>
  <h1>Main heading</h1>
  <p>First paragraph.</p>
  <h2>Section</h2>
  <h3>Subsection</h3>
  <h4>Not collected</h4>
  <a href="/about">About</a>
  <a href="next.html">Next</a>
  <a href="../index.html">Up</a>
  <a href="//cdn.example.com/lib">Protocol-relative</a>
  <a href="https://other.org/page#top">External</a>
  <a href="/about#team">Duplicate with fragment</a>
  <a href="#local">Anchor</a>
  <a href="mailto:me@example.com">Mail</a>
  <a href="javascript:void(0)">JS</a>
  <a href="http://bad:port/">Broken port</a>
  <a>No href</a>
  <img src="/img/logo.png" alt="Logo">
  <img src="photo.jpg">
  <img data-src="lazy.jpg" alt="Lazy">
  <img alt="No src">
  <table>
    <tr><th>Name</th><th>Age</th></tr>
    <tr><td>Alice</td><td>30</td></tr>
    <tr><td>Bob</td><td>25</td></tr>
  </table>
  <ul>
    <li>One</li>
    <li>Two
      <ol><li>Nested A</li><li>Nested B</li></ol>
    </li>
  </ul>
  <noscript>Enable JS</noscript>
</body>
</html>"""

BROKEN_HTML = """<html><head><title>Broken
<body>
<div><p>Unclosed paragraph <b>bold <i>italic</b></i>
<a href="/ok">Link<a href='/second'>Second
<ul><li>item one<li>item two
<table><tr><td>cell<td>cell2
</div></div></span>
<img src=/pic.png alt=Picture
"""


@pytest.fixture
def parser():
    return HTMLParser()


@pytest.fixture
def soup():
    return BeautifulSoup(VALID_HTML, "lxml")


# --- valid HTML ---

@pytest.mark.asyncio
async def test_parse_valid_html(parser):
    page = await parser.parse_html(VALID_HTML, BASE)
    assert page["url"] == BASE
    assert page["title"] == "Test Page"
    assert page["errors"] == []
    assert "First paragraph." in page["text"]
    assert len(page["links"]) == 5
    assert len(page["images"]) == 3
    assert set(page) >= {"url", "title", "text", "links", "metadata"}


def test_extract_metadata(parser, soup):
    meta = parser.extract_metadata(soup)
    assert meta["title"] == "Test Page"
    assert meta["description"] == "A page for tests"
    assert meta["keywords"] == ["python", "async", "crawler"]
    assert meta["og:title"] == "OG title"
    assert meta["lang"] == "en"
    assert meta["canonical"] == "https://example.com/canonical"


def test_extract_metadata_missing_tags(parser):
    meta = parser.extract_metadata(BeautifulSoup("<p>no head</p>", "lxml"))
    assert meta == {"title": "", "description": "", "keywords": []}


def test_extract_text_skips_hidden_content(parser, soup):
    text = parser.extract_text(soup)
    assert "Main heading" in text and "First paragraph." in text
    for hidden in ("script text", "color: red", "Enable JS", "Test Page"):
        assert hidden not in text
    assert "  " not in text  # whitespace is collapsed


def test_extract_text_does_not_modify_soup(parser, soup):
    parser.extract_text(soup)
    assert soup.find("script") is not None
    assert soup.find("title") is not None


def test_extract_text_with_selector(parser, soup):
    assert parser.extract_text(soup, "h1") == "Main heading"
    assert parser.extract_text(soup, "h2, h3") == "Section Subsection"
    assert parser.extract_text(soup, ".missing") == ""


def test_extract_headings(parser, soup):
    assert parser.extract_headings(soup) == [
        {"level": 1, "text": "Main heading"},
        {"level": 2, "text": "Section"},
        {"level": 3, "text": "Subsection"},
    ]


def test_extract_images(parser, soup):
    assert parser.extract_images(soup, BASE) == [
        {"src": "https://example.com/img/logo.png", "alt": "Logo"},
        {"src": "https://example.com/blog/photo.jpg", "alt": ""},
        {"src": "https://example.com/blog/lazy.jpg", "alt": "Lazy"},
    ]


def test_extract_tables(parser, soup):
    assert parser.extract_tables(soup) == [
        {"headers": ["Name", "Age"], "rows": [["Alice", "30"], ["Bob", "25"]]},
    ]


def test_extract_tables_without_header(parser):
    soup = BeautifulSoup("<table><tr><td>a</td><td>b</td></tr></table>", "lxml")
    assert parser.extract_tables(soup) == [{"headers": [], "rows": [["a", "b"]]}]


def test_extract_lists(parser, soup):
    assert parser.extract_lists(soup) == [
        {"type": "ul", "items": ["One", "Two"]},
        {"type": "ol", "items": ["Nested A", "Nested B"]},
    ]


# --- links and relative URLs ---

def test_extract_links(parser, soup):
    assert parser.extract_links(soup, BASE) == [
        "https://example.com/about",
        "https://example.com/blog/next.html",
        "https://example.com/index.html",
        "https://cdn.example.com/lib",
        "https://other.org/page",
    ]


def test_extract_links_same_domain_only(soup):
    links = HTMLParser(same_domain_only=True).extract_links(soup, BASE)
    assert links == [
        "https://example.com/about",
        "https://example.com/blog/next.html",
        "https://example.com/index.html",
    ]


@pytest.mark.parametrize("href, expected", [
    ("/about", "https://example.com/about"),
    ("page.html", "https://example.com/blog/page.html"),
    ("./page.html", "https://example.com/blog/page.html"),
    ("../up.html", "https://example.com/up.html"),
    ("../../../too-far.html", "https://example.com/too-far.html"),
    ("?q=1", "https://example.com/blog/post.html?q=1"),
    ("//cdn.example.com/x.js", "https://cdn.example.com/x.js"),
    ("http://other.org/a#frag", "http://other.org/a"),
    ("  /spaces  ", "https://example.com/spaces"),
    ("#top", None),
    ("", None),
    (None, None),
    ("mailto:a@b.c", None),
    ("JavaScript:alert(1)", None),
    ("tel:+123", None),
    ("ftp://example.com/file", None),
    ("http://", None),
    ("http://host:99999999/", None),
])
def test_normalize_url(href, expected):
    assert normalize_url(href, BASE) == expected


@pytest.mark.asyncio
async def test_base_tag_changes_link_resolution(parser):
    html = '<head><base href="https://static.example.com/v2/"></head><a href="doc.html">d</a>'
    page = await parser.parse_html(html, BASE)
    assert page["links"] == ["https://static.example.com/v2/doc.html"]


# --- broken HTML and error handling ---

@pytest.mark.asyncio
async def test_parse_broken_html(parser):
    page = await parser.parse_html(BROKEN_HTML, BASE)
    assert page["errors"] == []
    assert page["title"].startswith("Broken")
    assert "Unclosed paragraph" in page["text"]
    assert "https://example.com/ok" in page["links"]
    assert "https://example.com/second" in page["links"]
    assert page["tables"][0]["rows"] == [["cell", "cell2"]]
    # Unclosed tags nest like in a browser: the last <li> contains the table
    assert page["lists"][0]["items"] == ["item one", "item two cell cell2"]
    assert page["images"] == [{"src": "https://example.com/pic.png", "alt": "Picture"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("html", ["", "   \n ", "not html at all", "<<<>>>&&&", "\x00\x01binary"])
async def test_garbage_input_does_not_crash(parser, html):
    page = await parser.parse_html(html, BASE)
    assert page["url"] == BASE
    assert isinstance(page["links"], list)


@pytest.mark.asyncio
async def test_empty_html_logs_warning(parser, caplog):
    caplog.set_level(logging.WARNING, logger="crawler.parser")
    page = await parser.parse_html("", BASE)
    assert page["errors"] == ["empty html"]
    assert "Empty HTML" in caplog.text


@pytest.mark.asyncio
async def test_missing_title_logs_warning(parser, caplog):
    caplog.set_level(logging.WARNING, logger="crawler.parser")
    await parser.parse_html("<p>text</p>", BASE)
    assert "No <title>" in caplog.text


@pytest.mark.asyncio
async def test_partial_result_when_extractor_fails(parser, monkeypatch, caplog):
    def boom(*args):
        raise RuntimeError("extractor crashed")

    monkeypatch.setattr(parser, "extract_links", boom)
    caplog.set_level(logging.WARNING, logger="crawler.parser")
    page = await parser.parse_html(VALID_HTML, BASE)
    assert page["links"] == []
    assert page["errors"] == ["links: RuntimeError: extractor crashed"]
    # The other fields are still extracted
    assert page["title"] == "Test Page"
    assert len(page["images"]) == 3
    assert "First paragraph." in page["text"]
    assert "Failed to extract links" in caplog.text


@pytest.mark.asyncio
async def test_falls_back_when_parser_missing(caplog):
    caplog.set_level(logging.WARNING, logger="crawler.parser")
    page = await HTMLParser(features="no-such-parser").parse_html(VALID_HTML, BASE)
    assert page["title"] == "Test Page"
    assert "falling back to html.parser" in caplog.text


# --- integration with AsyncCrawler ---

async def page_handler(request):
    return web.Response(text=VALID_HTML, content_type="text/html")


async def redirect_handler(request):
    raise web.HTTPFound("/blog/post.html")


async def status_handler(request):
    return web.Response(status=int(request.match_info["code"]))


@pytest_asyncio.fixture
async def server():
    app = web.Application()
    app.router.add_get("/blog/post.html", page_handler)
    app.router.add_get("/go", redirect_handler)
    app.router.add_get("/status/{code}", status_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    await runner.cleanup()


@pytest_asyncio.fixture
async def crawler():
    c = AsyncCrawler(max_concurrent=5, connect_timeout=1, read_timeout=1, total_timeout=3)
    yield c
    await c.close()


@pytest.mark.asyncio
async def test_fetch_and_parse(server, crawler):
    url = f"{server}/blog/post.html"
    page = await crawler.fetch_and_parse(url)
    assert page["url"] == url
    assert page["title"] == "Test Page"
    assert page["metadata"]["description"] == "A page for tests"
    assert "First paragraph." in page["text"]
    assert f"{server}/about" in page["links"]
    assert f"{server}/blog/next.html" in page["links"]
    assert page["errors"] == []


@pytest.mark.asyncio
async def test_fetch_and_parse_resolves_links_after_redirect(server, crawler):
    page = await crawler.fetch_and_parse(f"{server}/go")
    assert page["url"] == f"{server}/go"  # the original URL is kept
    # "next.html" resolves against /blog/post.html, not against /go
    assert f"{server}/blog/next.html" in page["links"]


@pytest.mark.asyncio
async def test_fetch_and_parse_http_error(server, crawler):
    url = f"{server}/status/404"
    page = await crawler.fetch_and_parse(url)
    assert page["url"] == url
    assert page["title"] == "" and page["links"] == []
    assert page["errors"][0].startswith("fetch: HTTP 404")


@pytest.mark.asyncio
async def test_fetch_and_parse_same_domain_only(server):
    async with AsyncCrawler(parser=HTMLParser(same_domain_only=True)) as c:
        page = await c.fetch_and_parse(f"{server}/blog/post.html")
    assert page["links"]
    assert all(link.startswith(server) for link in page["links"])

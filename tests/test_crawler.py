"""AsyncCrawler tests against a local aiohttp server (no internet needed)."""

import asyncio
import logging
import socket
import time

import pytest
import pytest_asyncio
from aiohttp import web

from crawler import AsyncCrawler


async def ok_handler(request):
    return web.Response(text="hello")


async def delay_handler(request):
    await asyncio.sleep(float(request.match_info["seconds"]))
    return web.Response(text="done")


async def status_handler(request):
    return web.Response(status=int(request.match_info["code"]))


@pytest_asyncio.fixture
async def server():
    app = web.Application()
    app.router.add_get("/ok", ok_handler)
    app.router.add_get("/delay/{seconds}", delay_handler)
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
async def test_valid_url(server, crawler):
    assert await crawler.fetch_url(f"{server}/ok") == "hello"
    assert crawler.results[f"{server}/ok"].status == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [404, 500])
async def test_http_errors(server, crawler, code):
    url = f"{server}/status/{code}"
    assert await crawler.fetch_url(url) == ""
    r = crawler.results[url]
    assert r.status == code
    assert r.error.startswith(f"HTTP {code}")


@pytest.mark.asyncio
async def test_nonexistent_urls(crawler):
    refused = f"http://127.0.0.1:{free_port()}/"  # nothing listens on this port
    bad_dns = "http://nonexistent-domain-for-test.invalid/"
    results = await crawler.fetch_urls([refused, bad_dns])
    assert results == {}
    # Linux replies with RST immediately, Windows retries SYN until connect timeout
    assert crawler.results[refused].error.startswith(("ClientConnector", "Timeout"))
    assert crawler.results[bad_dns].error.startswith("ClientConnector")


@pytest.mark.asyncio
async def test_timeout(server, crawler):
    url = f"{server}/delay/3"  # longer than read_timeout=1
    assert await crawler.fetch_url(url) == ""
    assert crawler.results[url].error == "Timeout"


@pytest.mark.asyncio
async def test_mixed_batch_does_not_crash(server, crawler):
    urls = [f"{server}/ok", f"{server}/status/404", f"{server}/delay/3"]
    results = await crawler.fetch_urls(urls)
    assert results == {f"{server}/ok": "hello"}
    assert len(crawler.results) == 3


@pytest.mark.asyncio
async def test_parallel_faster_than_sequential(server, crawler):
    urls = [f"{server}/delay/0.5?i={i}" for i in range(5)]

    start = time.perf_counter()
    for url in urls:
        await crawler.fetch_url(url)
    seq = time.perf_counter() - start

    start = time.perf_counter()
    results = await crawler.fetch_urls(urls)
    par = time.perf_counter() - start

    assert len(results) == 5
    assert seq >= 2.5
    assert par < 1.0
    assert par < seq / 3


@pytest.mark.asyncio
async def test_concurrency_limit(server):
    c = AsyncCrawler(max_concurrent=2)
    urls = [f"{server}/delay/0.3?i={i}" for i in range(4)]
    start = time.perf_counter()
    await c.fetch_urls(urls)
    elapsed = time.perf_counter() - start
    await c.close()
    # 4 requests of 0.3s each with a limit of 2 -> at least two "waves"
    assert elapsed >= 0.6


@pytest.mark.asyncio
async def test_logging(server, crawler, caplog):
    caplog.set_level(logging.INFO, logger="crawler")
    await crawler.fetch_url(f"{server}/ok")
    await crawler.fetch_url(f"{server}/status/404")
    text = caplog.text
    assert "Fetching" in text
    assert "Success" in text
    assert "Error" in text and "HTTP 404" in text


@pytest.mark.asyncio
async def test_idle_pooled_connection_is_reusable(server, crawler):
    # The connection sits idle in the pool longer than read_timeout=1
    assert await crawler.fetch_url(f"{server}/ok") == "hello"
    await asyncio.sleep(1.5)
    assert await crawler.fetch_url(f"{server}/ok") == "hello"


@pytest.mark.asyncio
async def test_close_is_idempotent(server, crawler):
    await crawler.fetch_url(f"{server}/ok")
    await crawler.close()
    await crawler.close()
    assert crawler._session is None

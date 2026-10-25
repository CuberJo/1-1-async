"""CrawlerQueue, SemaphoreManager and URL filter tests."""

import asyncio

import pytest

from crawler_queue import CrawlerQueue
from semaphore_manager import SemaphoreManager
from url_filter import URLFilter, canonicalize, domain_of


# --- CrawlerQueue ---

async def drain(queue: CrawlerQueue) -> list[str]:
    urls = []
    while (url := await queue.get_next()) is not None:
        urls.append(url)
        queue.mark_processed(url)
    return urls


@pytest.mark.asyncio
async def test_queue_priority_order():
    q = CrawlerQueue()
    q.add_url("low", priority=-1)
    q.add_url("normal")
    q.add_url("high", priority=10)
    q.add_url("normal2")
    # Higher priority first, equal priorities in insertion order
    assert await drain(q) == ["high", "normal", "normal2", "low"]


@pytest.mark.asyncio
async def test_queue_ignores_duplicates():
    q = CrawlerQueue()
    assert q.add_url("a") is True
    assert q.add_url("a", priority=5) is False  # still queued
    url = await q.get_next()
    assert q.add_url("a") is False  # in progress
    q.mark_processed(url)
    assert q.add_url("a") is False  # processed
    q.add_url("b")
    q.mark_failed(await q.get_next(), "boom")
    assert q.add_url("b") is False  # failed
    q.mark_seen("c")
    assert q.add_url("c") is False
    assert await q.get_next() is None


@pytest.mark.asyncio
async def test_queue_get_next_empty_returns_none():
    assert await CrawlerQueue().get_next() is None


@pytest.mark.asyncio
async def test_queue_waits_for_in_progress_urls():
    q = CrawlerQueue()
    q.add_url("start")
    url = await q.get_next()
    waiter = asyncio.create_task(q.get_next())
    await asyncio.sleep(0.05)
    assert not waiter.done()  # empty, but "start" may still add links

    q.add_url("child", depth=1)
    q.mark_processed(url)
    assert await waiter == "child"
    assert q.get_depth("child") == 1

    waiter = asyncio.create_task(q.get_next())
    await asyncio.sleep(0.05)
    assert not waiter.done()
    q.mark_failed("child", "HTTP 500")
    assert await waiter is None  # nothing queued, nothing in progress


@pytest.mark.asyncio
async def test_queue_close():
    q = CrawlerQueue()
    q.add_url("a")
    q.add_url("b")
    q.close()
    assert await q.get_next() is None
    assert q.add_url("c") is False
    assert len(q) == 2


@pytest.mark.asyncio
async def test_queue_stats():
    q = CrawlerQueue()
    for url in ["a", "b", "c", "d"]:
        q.add_url(url)
    q.mark_processed(await q.get_next())
    q.mark_failed(await q.get_next(), "Timeout")
    await q.get_next()
    assert q.get_stats() == {"queued": 1, "in_progress": 1, "processed": 1, "failed": 1, "seen": 4}
    assert q.failed == {"b": "Timeout"}


# --- SemaphoreManager ---

async def hold(manager: SemaphoreManager, url: str, seconds: float = 0.1):
    async with manager.acquire(url):
        await asyncio.sleep(seconds)


@pytest.mark.asyncio
async def test_semaphore_global_limit():
    m = SemaphoreManager(max_concurrent=3)
    await asyncio.gather(*(hold(m, f"https://site{i}.com/") for i in range(10)))
    assert m.peak_active == 3
    assert m.total == 10
    assert m.active_count == 0


@pytest.mark.asyncio
async def test_semaphore_per_domain_limit():
    m = SemaphoreManager(max_concurrent=10, max_per_domain=2)
    urls = [f"https://a.com/{i}" for i in range(6)] + [f"https://b.com/{i}" for i in range(6)]
    loop = asyncio.get_running_loop()
    start = loop.time()
    await asyncio.gather(*(hold(m, url) for url in urls))
    elapsed = loop.time() - start
    assert m.peak_per_domain == {"a.com": 2, "b.com": 2}
    assert m.peak_active == 4  # both domains run side by side
    # 6 requests per domain, 2 at a time -> 3 waves of 0.1s
    assert 0.3 <= elapsed < 0.5


@pytest.mark.asyncio
async def test_semaphore_busy_domain_does_not_block_others():
    m = SemaphoreManager(max_concurrent=2, max_per_domain=1)
    slow = [asyncio.create_task(hold(m, f"https://slow.com/{i}", 0.3)) for i in range(3)]
    await asyncio.sleep(0.01)
    loop = asyncio.get_running_loop()
    start = loop.time()
    await hold(m, "https://fast.com/", 0.01)
    # Waiting slow.com tasks don't hold the second global slot
    assert loop.time() - start < 0.1
    await asyncio.gather(*slow)


@pytest.mark.asyncio
async def test_semaphore_tracks_active_tasks():
    m = SemaphoreManager(max_concurrent=5)
    tasks = [asyncio.create_task(hold(m, "https://a.com/", 0.2)) for _ in range(2)]
    tasks.append(asyncio.create_task(hold(m, "https://www.b.com/", 0.2)))
    await asyncio.sleep(0.05)
    stats = m.get_stats()
    assert stats["active"] == 3
    assert stats["active_per_domain"] == {"a.com": 2, "b.com": 1}
    await asyncio.gather(*tasks)
    assert m.get_stats()["active"] == 0
    assert m.get_stats()["active_per_domain"] == {}


@pytest.mark.asyncio
async def test_semaphore_releases_on_error():
    m = SemaphoreManager(max_concurrent=1)
    with pytest.raises(RuntimeError):
        async with m.acquire("https://a.com/"):
            raise RuntimeError
    assert m.active_count == 0
    await asyncio.wait_for(hold(m, "https://a.com/", 0), timeout=0.5)


def test_semaphore_validates_limits():
    with pytest.raises(ValueError):
        SemaphoreManager(max_concurrent=0)
    with pytest.raises(ValueError):
        SemaphoreManager(max_per_domain=0)


# --- URL filters ---

@pytest.mark.parametrize("url, expected", [
    ("https://example.com", "https://example.com/"),
    ("HTTPS://Example.COM/Path?q=1#frag", "https://example.com/Path?q=1"),
    ("http://example.com:80/a", "http://example.com/a"),
    ("https://example.com:8443/a", "https://example.com:8443/a"),
])
def test_canonicalize(url, expected):
    assert canonicalize(url) == expected


def test_domain_of_ignores_www():
    assert domain_of("https://www.Example.com/a") == "example.com"


def test_filter_same_domain_only():
    f = URLFilter(same_domain_only=True)
    f.allow_domain("https://example.com/")
    assert f.is_allowed("https://example.com/a")
    assert f.is_allowed("https://www.example.com/a")
    assert not f.is_allowed("https://sub.example.com/a")
    assert not f.is_allowed("https://other.org/")
    assert URLFilter().is_allowed("https://other.org/")


def test_filter_exclude_patterns():
    f = URLFilter(exclude_patterns=[r"/login", r"\?page=\d+"])
    assert f.is_allowed("https://example.com/blog")
    assert not f.is_allowed("https://example.com/login?next=/")
    assert not f.is_allowed("https://example.com/blog?page=2")


def test_filter_include_patterns():
    f = URLFilter(include_patterns=[r"/blog/", r"/news/"], exclude_patterns=[r"/draft"])
    assert f.is_allowed("https://example.com/blog/post")
    assert f.is_allowed("https://example.com/news/1")
    assert not f.is_allowed("https://example.com/about")
    assert not f.is_allowed("https://example.com/blog/draft-1")  # exclude wins


def test_filter_skips_binary_files_and_other_schemes():
    f = URLFilter()
    assert not f.is_allowed("https://example.com/report.PDF")
    assert not f.is_allowed("https://example.com/logo.png")
    assert not f.is_allowed("ftp://example.com/")
    assert f.is_allowed("https://example.com/page.html")

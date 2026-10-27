"""Day 5 tests: error classification, RetryStrategy, CircuitBreaker and their
use in AsyncCrawler (local server, no internet needed)."""

import asyncio
import time
from collections import Counter

import aiohttp
import pytest
import pytest_asyncio
from aiohttp import web

from circuit_breaker import CLOSED, HALF_OPEN, OPEN, CircuitBreaker
from crawler import AsyncCrawler
from errors import (
    CircuitOpenError,
    NetworkError,
    ParseError,
    PermanentError,
    TransientError,
    classify_exception,
    error_for_status,
)
from retry_strategy import ErrorPolicy, RetryStrategy

TOLERANCE = 0.02
URL = "http://example.com/page"


# --- classification ---

@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_temporary_statuses_are_transient(status):
    error = error_for_status(status, "Oops", URL)
    assert isinstance(error, TransientError)
    assert error.status == status and error.message == f"HTTP {status} Oops" and error.url == URL


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
def test_client_errors_are_permanent(status):
    assert isinstance(error_for_status(status), PermanentError)


@pytest.mark.parametrize("exc, cls", [
    (asyncio.TimeoutError(), TransientError),
    (aiohttp.ServerTimeoutError("read"), TransientError),
    (aiohttp.ServerDisconnectedError(), NetworkError),
    (aiohttp.ClientPayloadError("cut"), NetworkError),
    (ConnectionRefusedError(), NetworkError),
    (aiohttp.InvalidURL("nope"), PermanentError),
    (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad"), ParseError),
])
def test_exceptions_are_classified(exc, cls):
    error = classify_exception(exc, URL)
    assert type(error) is cls
    assert error.original is exc and error.__cause__ is exc and error.url == URL


def test_response_error_keeps_status_and_retry_after():
    exc = aiohttp.ClientResponseError(None, (), status=429, message="Too Many Requests",
                                      headers={"Retry-After": "2"})
    error = classify_exception(exc, URL)
    assert isinstance(error, TransientError)
    assert error.status == 429 and error.retry_after == 2.0
    assert error.message == "HTTP 429 Too Many Requests"


def test_bugs_are_not_classified():
    assert classify_exception(ValueError("bug")) is None
    assert classify_exception(KeyError("x")) is None


# --- RetryStrategy ---

class Flaky:
    """Coroutine function that raises the given errors in turn, then returns "ok"."""

    def __init__(self, *errors: BaseException):
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self, url: str) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def strategy(**kwargs) -> RetryStrategy:
    options = {"max_retries": 3, "base_delay": 0.01}
    options.update(kwargs)
    return RetryStrategy(**options)


def delays(s: RetryStrategy) -> list[float]:
    return [r["next_retry_in"] for r in s.stats.history if r["next_retry_in"] is not None]


@pytest.mark.asyncio
async def test_transient_errors_are_retried():
    s = strategy()
    func = Flaky(TransientError("Timeout"), error_for_status(503))
    assert await s.execute_with_retry(func, URL) == "ok"
    assert func.calls == 3
    st = s.stats
    assert (st.calls, st.attempts, st.retries, st.successes, st.successful_retries) == (1, 3, 2, 1, 1)
    assert st.errors_by_type == {"TransientError": 2}
    assert st.errors_by_status == {503: 1}


@pytest.mark.asyncio
async def test_permanent_errors_are_not_retried():
    s = strategy()
    func = Flaky(error_for_status(404, "Not Found"))
    with pytest.raises(PermanentError) as info:
        await s.execute_with_retry(func, URL)
    assert func.calls == 1 and info.value.attempts == 1
    assert s.stats.retries == 0
    assert s.stats.permanent_errors == {URL: "HTTP 404 Not Found"}
    assert s.stats.failed_urls == {URL: "PermanentError: HTTP 404 Not Found"}


@pytest.mark.asyncio
async def test_parse_errors_are_not_retried():
    s = strategy()
    func = Flaky(ParseError("broken"))
    with pytest.raises(ParseError):
        await s.execute_with_retry(func, URL)
    assert func.calls == 1
    assert s.stats.permanent_errors == {}  # failed, but not a permanent HTTP error


@pytest.mark.asyncio
async def test_gives_up_after_max_retries():
    s = strategy(max_retries=2)
    func = Flaky(*[error_for_status(503)] * 5)
    with pytest.raises(TransientError) as info:
        await s.execute_with_retry(func, URL)
    assert func.calls == 3 and info.value.attempts == 3
    assert s.stats.failures == 1 and s.stats.successful_retries == 0


@pytest.mark.asyncio
async def test_backoff_is_exponential():
    s = strategy(base_delay=0.02, backoff_factor=2.0)
    func = Flaky(*[TransientError("Timeout")] * 3)
    start = time.monotonic()
    await s.execute_with_retry(func, URL)
    assert delays(s) == [0.02, 0.04, 0.08]
    assert time.monotonic() - start >= 0.14 - TOLERANCE
    assert s.stats.avg_retry_time >= 0.02


def test_backoff_factor_and_cap():
    s = strategy(base_delay=1.0, backoff_factor=3.0, max_delay=5.0)
    error = TransientError("Timeout")
    assert [s.get_delay(error, n) for n in range(3)] == [1.0, 3.0, 5.0]


@pytest.mark.asyncio
async def test_server_error_500_is_retried_a_limited_number_of_times():
    s = strategy(max_retries=5)
    func = Flaky(*[error_for_status(500)] * 5)
    with pytest.raises(TransientError):
        await s.execute_with_retry(func, URL)
    assert func.calls == 2  # one retry only


@pytest.mark.asyncio
async def test_network_errors_have_their_own_limit_and_backoff():
    s = strategy(max_retries=5)
    func = Flaky(*[NetworkError("refused")] * 5)
    with pytest.raises(NetworkError):
        await s.execute_with_retry(func, URL)
    assert func.calls == 3  # capped at 2 retries
    assert delays(s) == [0.02, 0.04]  # twice the base delay


def test_rate_limited_429_waits_longer_and_honours_retry_after():
    s = strategy(base_delay=0.5)
    assert s.get_delay(error_for_status(429), 0) == 4 * s.get_delay(error_for_status(503), 0)
    limited = error_for_status(429, headers={"Retry-After": "10"})
    assert s.get_delay(limited, 0) == 10


@pytest.mark.asyncio
async def test_custom_policies_and_retry_on():
    s = strategy(policies={TransientError: ErrorPolicy(max_retries=1)}, status_policies={503: ErrorPolicy(max_retries=0)})
    func = Flaky(*[TransientError("Timeout")] * 3)
    with pytest.raises(TransientError):
        await s.execute_with_retry(func, URL)
    assert func.calls == 2
    func = Flaky(error_for_status(503))
    with pytest.raises(TransientError):
        await s.execute_with_retry(func, URL)
    assert func.calls == 1

    only_transient = strategy(retry_on=[TransientError])
    func = Flaky(NetworkError("refused"))
    with pytest.raises(NetworkError):
        await only_transient.execute_with_retry(func, URL)
    assert func.calls == 1


@pytest.mark.asyncio
async def test_raw_exceptions_are_classified_and_bugs_propagate():
    s = strategy()
    func = Flaky(asyncio.TimeoutError(), aiohttp.ServerDisconnectedError())
    assert await s.execute_with_retry(func, URL) == "ok"
    assert s.stats.errors_by_type == {"TransientError": 1, "NetworkError": 1}

    func = Flaky(ValueError("bug"))
    with pytest.raises(ValueError):
        await s.execute_with_retry(func, URL)
    assert func.calls == 1


@pytest.mark.asyncio
async def test_retry_on_raw_exception_type():
    s = strategy(retry_on=[asyncio.TimeoutError])
    func = Flaky(asyncio.TimeoutError())
    assert await s.execute_with_retry(func, URL) == "ok"
    func = Flaky(error_for_status(503))
    with pytest.raises(TransientError):
        await s.execute_with_retry(func, URL)
    assert func.calls == 1


@pytest.mark.asyncio
async def test_coroutine_object_is_rejected():
    with pytest.raises(TypeError):
        await strategy().execute_with_retry(Flaky()(URL))


@pytest.mark.asyncio
async def test_attempts_are_logged(caplog):
    caplog.set_level("INFO", logger="crawler.retry")
    s = strategy(max_retries=1)
    with pytest.raises(TransientError):
        await s.execute_with_retry(Flaky(*[error_for_status(503, "Service Unavailable")] * 2), URL)
    text = caplog.text
    assert f"TransientError on {URL} (attempt 1/2): HTTP 503 Service Unavailable — retry in 0.01s" in text
    assert f"Gave up after 2 attempts: {URL}" in text


def test_timeouts_grow_on_retries():
    s = strategy(timeout_growth=2.0)
    assert [s.timeout_multiplier(n) for n in (1, 2, 3)] == [1, 2, 4]


def test_strategy_validation():
    with pytest.raises(ValueError):
        RetryStrategy(max_retries=-1)
    with pytest.raises(ValueError):
        RetryStrategy(backoff_factor=0.5)


# --- CircuitBreaker ---

class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_circuit_opens_on_high_failure_rate_and_recovers():
    clock = Clock()
    cb = CircuitBreaker(failure_rate=0.5, min_requests=4, window=4, recovery_timeout=10, clock=clock)
    cb.record_success("a.com")
    cb.record_failure("a.com")
    cb.record_failure("a.com")
    assert cb.state("a.com") == CLOSED  # 3 known outcomes < min_requests
    cb.record_failure("a.com")
    assert cb.state("a.com") == OPEN and not cb.allow_request("a.com")
    assert cb.allow_request("b.com")  # other domains are not affected
    assert cb.retry_in("a.com") == 10

    clock.now = 10
    assert cb.state("a.com") == HALF_OPEN
    assert cb.allow_request("a.com")  # one trial request...
    assert not cb.allow_request("a.com")  # ...at a time
    cb.record_success("a.com")
    assert cb.state("a.com") == CLOSED and cb.allow_request("a.com")
    assert cb.get_stats()["a.com"]["times_opened"] == 1


def test_failed_trial_reopens_circuit():
    clock = Clock()
    cb = CircuitBreaker(failure_rate=1.0, min_requests=2, window=2, recovery_timeout=5, clock=clock)
    cb.record_failure("a.com")
    cb.record_failure("a.com")
    clock.now = 5
    assert cb.allow_request("a.com")
    cb.record_failure("a.com")
    assert cb.state("a.com") == OPEN and cb.retry_in("a.com") == 5


# --- AsyncCrawler integration ---

def page_html(links: list[str] = ()) -> str:
    anchors = "".join(f'<a href="{href}">{href}</a>' for href in links)
    return f"<html><head><title>Page</title></head><body>{anchors}</body></html>"


@pytest_asyncio.fixture
async def site():
    hits: Counter[str] = Counter()

    async def handler(request):
        path = request.path
        hits[path] += 1
        n = hits[path]
        if path == "/":
            return web.Response(text=page_html(["/ok", "/missing", "/flaky"]), content_type="text/html")
        if path == "/ok":
            return web.Response(text=page_html(), content_type="text/html")
        if path == "/flaky" and n <= 2:  # 503 twice, then fine
            raise web.HTTPServiceUnavailable()
        if path == "/flaky":
            return web.Response(text=page_html(), content_type="text/html")
        if path == "/slow-once":  # times out once
            await asyncio.sleep(1.0 if n == 1 else 0)
            return web.Response(text="late")
        if path == "/slow":  # always 0.4s: needs a longer timeout
            await asyncio.sleep(0.4)
            return web.Response(text="slow")
        if path == "/limited" and n == 1:
            raise web.HTTPTooManyRequests(headers={"Retry-After": "0.3"})
        if path == "/limited":
            return web.Response(text="ok")
        if path == "/forbidden":
            raise web.HTTPForbidden()
        if path == "/unauthorized":
            raise web.HTTPUnauthorized()
        if path == "/server-error":
            raise web.HTTPInternalServerError()
        if path == "/down":
            raise web.HTTPBadGateway()
        if path == "/binary":
            return web.Response(body=b"\xff\xfe\xfa", content_type="text/html", charset="utf-8")
        raise web.HTTPNotFound()

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    port = tcp._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", hits
    await runner.cleanup()


def crawler_for(**strategy_options) -> AsyncCrawler:
    breaker = strategy_options.pop("circuit_breaker", None)
    read_timeout = strategy_options.pop("read_timeout", 2)
    options = {"max_retries": 3, "base_delay": 0.01, "timeout_growth": 1.0}
    options.update(strategy_options)
    return AsyncCrawler(
        max_concurrent=5, read_timeout=read_timeout, progress_interval=None,
        retry_strategy=RetryStrategy(**options), circuit_breaker=breaker,
    )


@pytest.mark.asyncio
async def test_crawler_retries_timeouts(site):
    base, hits = site
    async with crawler_for(read_timeout=0.3) as crawler:
        assert await crawler.fetch_url(base + "/slow-once") == "late"
    result = crawler.results[base + "/slow-once"]
    assert result.attempts == 2 and hits["/slow-once"] == 2
    assert crawler.get_stats()["errors"]["errors_by_type"] == {"TransientError": 1}


@pytest.mark.asyncio
async def test_crawler_grows_timeouts_on_retries(site):
    base, hits = site
    async with crawler_for(read_timeout=0.25, timeout_growth=2.0) as crawler:
        assert await crawler.fetch_url(base + "/slow") == "slow"  # 0.25s fails, 0.5s works
    assert crawler.results[base + "/slow"].attempts == 2


@pytest.mark.asyncio
async def test_crawler_retries_503(site):
    base, hits = site
    async with crawler_for() as crawler:
        assert await crawler.fetch_url(base + "/flaky")
    assert crawler.results[base + "/flaky"].attempts == 3 and crawler.retries == 2
    assert crawler.retry_strategy.stats.successful_retries == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path, status", [("/missing", 404), ("/forbidden", 403), ("/unauthorized", 401)])
async def test_crawler_does_not_retry_permanent_errors(site, path, status):
    base, hits = site
    async with crawler_for() as crawler:
        assert await crawler.fetch_url(base + path) == ""
    result = crawler.results[base + path]
    assert hits[path] == 1 and result.attempts == 1
    assert result.status == status and result.error_type == "PermanentError" and not result.retryable
    assert list(crawler.get_stats()["errors"]["permanent_errors"]) == [base + path]


@pytest.mark.asyncio
async def test_crawler_retries_500_once_and_429_after_retry_after(site):
    base, hits = site
    async with crawler_for(max_retries=3) as crawler:
        assert await crawler.fetch_url(base + "/server-error") == ""
        start = time.monotonic()
        assert await crawler.fetch_url(base + "/limited") == "ok"
        assert time.monotonic() - start >= 0.3 - TOLERANCE
    assert hits["/server-error"] == 2
    assert crawler.results[base + "/server-error"].error_type == "TransientError"


@pytest.mark.asyncio
async def test_crawler_network_and_parse_errors(site):
    base, hits = site
    bad_dns = "http://nonexistent-domain-for-test.invalid/"
    async with crawler_for(max_retries=3) as crawler:
        assert await crawler.fetch_url(bad_dns) == ""
        assert await crawler.fetch_url(base + "/binary") == ""
    dns = crawler.results[bad_dns]
    assert dns.error_type == "NetworkError" and dns.attempts == 3  # NetworkError limit: 2 retries
    assert crawler.results[base + "/binary"].error_type == "ParseError"
    assert hits["/binary"] == 1


@pytest.mark.asyncio
async def test_external_strategy_with_fetch_once(site):
    base, hits = site
    s = strategy()
    async with AsyncCrawler(progress_interval=None) as crawler:
        assert await s.execute_with_retry(crawler.fetch_once, base + "/flaky")
        with pytest.raises(PermanentError) as info:
            await s.execute_with_retry(crawler.fetch_once, base + "/missing")
    assert hits["/flaky"] == 3 and hits["/missing"] == 1
    assert info.value.status == 404 and isinstance(info.value.original, aiohttp.ClientResponseError)


@pytest.mark.asyncio
async def test_crawl_collects_error_stats_and_report(site):
    base, hits = site
    async with crawler_for() as crawler:
        await crawler.fetch_url(base + "/down")  # before crawl(): reset by it
        pages = await crawler.crawl([base], max_depth=1)
    assert set(pages) == {base + "/", base + "/ok", base + "/flaky"}
    assert crawler.failed_urls == {base + "/missing": "HTTP 404 Not Found"}
    errors = crawler.get_stats()["errors"]
    assert errors["errors_by_type"] == {"TransientError": 2, "PermanentError": 1}
    assert errors["errors_by_status"] == {503: 2, 404: 1}
    assert errors["retries"] == 2 and errors["successful_retries"] == 1
    assert errors["avg_retry_time"] > 0

    report = crawler.get_error_report()
    assert report["failed"][base + "/missing"]["error_type"] == "PermanentError"
    assert [(r["url"], r["attempt"]) for r in report["attempts_log"] if r["url"] == base + "/flaky"] == [
        (base + "/flaky", 1), (base + "/flaky", 2)
    ]


@pytest.mark.asyncio
async def test_circuit_breaker_blocks_failing_domain(site):
    base, hits = site
    breaker = CircuitBreaker(failure_rate=1.0, min_requests=3, window=3, recovery_timeout=0.3)
    async with crawler_for(max_retries=0, circuit_breaker=breaker) as crawler:
        for i in range(5):
            await crawler.fetch_url(f"{base}/down?i={i}")
        assert hits["/down"] == 3  # the last two never reached the server
        blocked = crawler.results[f"{base}/down?i=4"]
        assert blocked.error_type == CircuitOpenError.__name__ and blocked.error.startswith("Circuit open")
        assert crawler.get_stats()["circuits"]["127.0.0.1"]["state"] == OPEN

        await asyncio.sleep(0.35)
        assert await crawler.fetch_url(base + "/ok")  # trial request succeeds
        assert breaker.state("127.0.0.1") == CLOSED

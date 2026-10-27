"""Demo: error handling — error classes, automatic retries with exponential
backoff, a circuit breaker, error statistics and a JSON error report.

    python src/retry_demo.py               # local sites with all kinds of errors (offline):
                                           # 1) a crawl with retries, 2) the circuit breaker
    python src/retry_demo.py https://httpbin.org/status/503 https://httpbin.org/status/404 \\
        https://httpbin.org/delay/3 --read-timeout 2
"""

import argparse
import asyncio
import json
import logging
import socket
import sys
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp import web

from circuit_breaker import CircuitBreaker
from crawler import AsyncCrawler
from retry_strategy import RetryStrategy
from utils import setup_logging

# path -> what the local site does with it
ROUTES = {
    "/ok/1": "a normal page",
    "/ok/2": "a normal page",
    "/flaky": "503 twice, then OK",
    "/rate-limited": "429 with Retry-After: 1 once, then OK",
    "/slow-once": "hangs on the first request, then OK",
    "/slow": "always 0.7s: fits only into a grown timeout",
    "/server-error": "500 every time",
    "/bad-gateway": "502 every time",
    "/not-found": "404",
    "/forbidden": "403",
    "/unauthorized": "401",
    "/binary": "bytes that are not valid UTF-8",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def start_site(handler) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


@asynccontextmanager
async def error_site():
    """A site with every kind of error; its index links to all of them, to
    a closed port and to a domain that does not exist."""
    hits: Counter[str] = Counter()
    links = list(ROUTES) + [
        f"http://127.0.0.1:{free_port()}/",  # connection refused (a connect timeout on Windows)
        "http://nonexistent-domain-for-demo.invalid/",  # DNS failure
    ]

    async def handler(request: web.Request) -> web.Response:
        path = request.path
        hits[path] += 1
        n = hits[path]
        if path == "/":
            anchors = "".join(f'<li><a href="{href}">{href}</a></li>' for href in links)
            return web.Response(text=f"<html><head><title>Errors</title></head><body><ul>{anchors}</ul></body></html>",
                                content_type="text/html")
        if path == "/flaky" and n <= 2:
            raise web.HTTPServiceUnavailable()
        if path == "/rate-limited" and n == 1:
            raise web.HTTPTooManyRequests(headers={"Retry-After": "1"})
        if path == "/slow-once" and n == 1:
            await asyncio.sleep(3)
        if path == "/slow":
            await asyncio.sleep(0.7)
        if path == "/server-error":
            raise web.HTTPInternalServerError()
        if path == "/bad-gateway":
            raise web.HTTPBadGateway()
        if path == "/forbidden":
            raise web.HTTPForbidden()
        if path == "/unauthorized":
            raise web.HTTPUnauthorized()
        if path == "/binary":
            return web.Response(body=b"\xff\xfe\x00\xfa" * 10, content_type="text/html", charset="utf-8")
        if path == "/not-found" or path not in ROUTES:
            raise web.HTTPNotFound()
        return web.Response(text=f"<html><head><title>{path}</title></head><body>{ROUTES[path]}</body></html>",
                            content_type="text/html")

    runner, base = await start_site(handler)
    try:
        yield base + "/"
    finally:
        await runner.cleanup()


@asynccontextmanager
async def outage_site():
    """A site that answers 503 until state["up"] is set; counts requests in state["hits"]."""
    state = {"up": False, "hits": 0}

    async def handler(request: web.Request) -> web.Response:
        state["hits"] += 1
        if not state["up"]:
            raise web.HTTPServiceUnavailable()
        return web.Response(text="<html><head><title>Back</title></head><body>up again</body></html>",
                            content_type="text/html")

    runner, base = await start_site(handler)
    try:
        yield base, state
    finally:
        await runner.cleanup()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="*", help="URLs to fetch (default: the local error sites)")
    p.add_argument("--retries", type=int, default=3)
    p.add_argument("--backoff", type=float, default=2.0, help="backoff factor")
    p.add_argument("--base-delay", type=float, default=0.2, help="seconds before the first retry")
    p.add_argument("--read-timeout", type=float, default=0.5)
    p.add_argument("--timeout-growth", type=float, default=2.0, help="timeout multiplier per retry")
    p.add_argument("--report", default="output/error_report.json")
    return p.parse_args()


def print_results(crawler: AsyncCrawler) -> None:
    print(f"\n{'URL':<50} {'Status':>6} {'Tries':>5}  Result")
    print("-" * 100)
    for url, r in crawler.results.items():
        status = str(r.status) if r.status else "-"
        outcome = "OK" if r.ok else f"{r.error_type}: {r.error}"
        short = url if len(url) <= 50 else url[:47] + "..."
        print(f"{short:<50} {status:>6} {r.attempts:>5}  {outcome[:70]}")


def print_error_stats(crawler: AsyncCrawler) -> None:
    stats = crawler.get_stats()["errors"]
    print("\n--- Error statistics ---")
    print(f"Fetches: {stats['calls']}, requests: {stats['attempts']}, "
          f"succeeded: {stats['successes']}, failed: {stats['failures']}")
    print(f"Retries: {stats['retries']}, fetches saved by a retry: {stats['successful_retries']}, "
          f"average time per retry: {stats['avg_retry_time']:.2f}s")
    print("Errors by type (every failed attempt):")
    for kind, count in sorted(stats["errors_by_type"].items(), key=lambda kv: -kv[1]):
        print(f"  {kind:<18} {count}")
    print("Errors by HTTP status:", dict(sorted(stats["errors_by_status"].items())))
    print("URLs with permanent errors:")
    for url, error in stats["permanent_errors"].items():
        print(f"  {url} — {error}")
    for domain, circuit in crawler.get_stats()["circuits"].items():
        print(f"Circuit {domain}: {circuit['state']}, opened {circuit['times_opened']} time(s), "
              f"{circuit['recent_failures']}/{circuit['recent_requests']} recent requests failed")


def make_strategy(args: argparse.Namespace, **overrides) -> RetryStrategy:
    options = dict(
        max_retries=args.retries,
        backoff_factor=args.backoff,
        base_delay=args.base_delay,
        jitter=0.05,
        timeout_growth=args.timeout_growth,
    )
    options.update(overrides)
    return RetryStrategy(**options)


def make_crawler(args: argparse.Namespace, strategy: RetryStrategy,
                 breaker: CircuitBreaker | None = None) -> AsyncCrawler:
    return AsyncCrawler(
        max_concurrent=8,
        max_per_domain=2,
        connect_timeout=1.0,
        read_timeout=args.read_timeout,
        total_timeout=10.0,
        progress_interval=None,
        retry_strategy=strategy,
        circuit_breaker=breaker,
    )


async def retry_demo(args: argparse.Namespace, urls: list[str], crawl: bool) -> dict:
    strategy = make_strategy(args)
    print(f"\nRetries: up to {strategy.max_retries}, delay {strategy.base_delay}s * {strategy.backoff_factor}^n, "
          f"timeouts x{strategy.timeout_growth} per retry; retried: "
          f"{', '.join(cls.__name__ for cls in strategy.retry_on)}")
    # The local error site fails on purpose and would trip a breaker; a list
    # of real URLs gets one, to stop hammering dead sites
    breaker = None if crawl else CircuitBreaker()
    async with make_crawler(args, strategy, breaker) as crawler:
        print("\n=== Fetching (retry log) ===", flush=True)
        if crawl:
            await crawler.crawl(urls, max_pages=50, max_depth=1)
        else:
            await crawler.fetch_urls(urls)
    print_results(crawler)
    print_error_stats(crawler)
    return crawler.get_error_report()


async def circuit_breaker_demo(args: argparse.Namespace) -> dict:
    """10 pages of a site that is down: the breaker opens after a few
    failures and refuses the rest; once the site is back, a trial request
    closes it again."""
    print("\n\n=== Circuit breaker: a site that is down ===", flush=True)
    breaker = CircuitBreaker(failure_rate=0.6, min_requests=4, window=6, recovery_timeout=2.0)
    strategy = make_strategy(args, max_retries=1)
    async with outage_site() as (base, state), make_crawler(args, strategy, breaker) as crawler:
        urls = [f"{base}/page/{i}" for i in range(1, 11)]
        await crawler.fetch_urls(urls)
        attempts = sum(crawler.results[u].attempts for u in urls)
        refused = sum(crawler.retry_strategy.stats.errors_by_type.values()) - state["hits"]
        domain = next(iter(breaker.get_stats()))
        print(f"\nCircuit for {domain}: {breaker.state(domain)}; of {attempts} attempts "
              f"{state['hits']} reached the site, {refused} were refused without a request", flush=True)

        state["up"] = True
        wait = breaker.retry_in(domain) + 0.1
        print(f"The site is back up; waiting {wait:.1f}s for the circuit to let a trial request through", flush=True)
        await asyncio.sleep(wait)
        print(f"Circuit: {breaker.state(domain)}", flush=True)
        ok = await crawler.fetch_url(urls[0])
        print(f"Trial request: {'OK' if ok else 'failed'} -> circuit {breaker.state(domain)}", flush=True)
        results = await crawler.fetch_urls(urls[1:])
        print(f"The other pages: {len(results)}/{len(urls) - 1} fetched", flush=True)
    print_error_stats(crawler)
    return crawler.get_error_report()


def save_report(path: str, report: dict) -> None:
    report_path = Path(path)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nError report saved to {report_path}")


async def main(args: argparse.Namespace) -> None:
    if args.urls:
        save_report(args.report, {"fetch": await retry_demo(args, args.urls, crawl=False)})
        return
    async with error_site() as url:
        print(f"Local error site: {url}")
        for path, behaviour in ROUTES.items():
            print(f"  {path:<15} {behaviour}")
        crawl_report = await retry_demo(args, [url], crawl=True)
    breaker_report = await circuit_breaker_demo(args)
    save_report(args.report, {"crawl": crawl_report, "circuit_breaker": breaker_report})


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")  # the log
    setup_logging()
    # Show every retry and circuit decision, hide per-request lines
    logging.getLogger("crawler").setLevel(logging.WARNING)
    logging.getLogger("crawler.retry").setLevel(logging.INFO)
    logging.getLogger("crawler.circuit").setLevel(logging.INFO)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    asyncio.run(main(parse_args()))

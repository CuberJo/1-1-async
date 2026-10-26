"""Demo: polite crawling — robots.txt, rate limiting, delay statistics.

    python src/polite_demo.py                 # local demo site with robots.txt (offline)
    python src/polite_demo.py https://docs.python.org/3/ --max-pages 15
    python src/polite_demo.py https://httpbin.org/ --rps 1 --user-agent "MyBot/1.0"
"""

import argparse
import asyncio
import logging
import sys
from contextlib import asynccontextmanager

from aiohttp import web

from crawler import AsyncCrawler
from utils import setup_logging

LOCAL_ROBOTS = """\
User-agent: *
Disallow: /admin/
Disallow: /private/
Disallow: /*?sort=
Allow: /private/press-kit
Crawl-delay: 0.3

User-agent: BadBot
Disallow: /
"""


def local_page(path: str) -> str:
    """Every catalog page links to more pages, some of them disallowed."""
    links = ["/admin/", "/private/notes", "/private/press-kit", "/catalog?sort=price"]
    links += [f"/catalog/{i}" for i in range(1, 9)]
    if path.startswith("/catalog/"):
        n = int(path.rsplit("/", 1)[1])
        links = [f"/catalog/{n}/item/{i}" for i in range(1, 4)] + ["/admin/stats"]
    anchors = "".join(f'<li><a href="{href}">{href}</a></li>' for href in links)
    return f"<html><head><title>Demo {path}</title></head><body><ul>{anchors}</ul></body></html>"


@asynccontextmanager
async def local_site():
    async def handler(request: web.Request) -> web.Response:
        if request.path == "/robots.txt":
            return web.Response(text=LOCAL_ROBOTS)
        await asyncio.sleep(0.05)  # pretend to do some work
        return web.Response(text=local_page(request.path), content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/"
    finally:
        await runner.cleanup()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="*", help="start URLs (default: a local demo site)")
    p.add_argument("--max-pages", type=int, default=20)
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--rps", type=float, default=2.0, help="requests per second per domain")
    p.add_argument("--min-delay", type=float, default=0.5, help="minimum seconds between requests")
    p.add_argument("--jitter", type=float, default=0.2, help="random extra delay, seconds")
    p.add_argument("--global-rate", action="store_true", help="one limit for all domains")
    p.add_argument("--user-agent", default="MyBot/1.0")
    p.add_argument("--retries", type=int, default=2)
    p.add_argument("--ignore-robots", action="store_true")
    return p.parse_args()


async def show_robots(crawler: AsyncCrawler, url: str) -> None:
    """Print the rules that apply to our bot and check a few URLs against them."""
    data = await crawler.robots.fetch_robots(url)
    rules = crawler.robots.get_rules(url)
    print(f"\n=== robots.txt of {url} (HTTP {data['status']}) ===")
    if data["disallow_all"]:
        print("Unavailable: everything is disallowed")
    groups = rules.groups_for(crawler.user_agent)
    if not groups:
        print(f"No rules for {crawler.user_agent}: everything is allowed")
    for group in groups:
        print(f"Group for {crawler.user_agent}: User-agent: {', '.join(group.agents)}")
        for allow, pattern, _ in group.rules:
            print(f"  {'Allow:   ' if allow else 'Disallow:'} {pattern}")
    print(f"Crawl-delay: {crawler.robots.get_crawl_delay(crawler.user_agent, url) or 'not set'}")

    # Sample URLs: the start page and paths taken from the rules themselves
    base = url.split("/", 3)
    origin = f"{base[0]}//{base[2]}"
    samples = [url] + [
        origin + pattern.rstrip("$").replace("*", "x")
        for group in groups for _, pattern, _ in group.rules[:4]
    ]
    print("\nChecks:")
    for sample in dict.fromkeys(samples):
        verdict = "allowed" if crawler.robots.can_fetch(sample, crawler.user_agent) else "BLOCKED"
        print(f"  {verdict:<8} {sample}")


def print_delay_stats(crawler: AsyncCrawler, limit: int = 15) -> None:
    history = list(crawler.rate_limiter.history)
    if not history:
        return
    t0 = history[0][0]
    print(f"\n{'#':>3} {'Start':>7} {'Domain':<24} {'Waited':>7} {'Gap':>7}")
    print("-" * 52)
    for i, (t, domain, wait, gap) in enumerate(history[:limit], 1):
        gap_text = f"{gap:.2f}s" if gap is not None else "-"
        print(f"{i:>3} {t - t0:>6.2f}s {domain[:24]:<24} {wait:>6.2f}s {gap_text:>7}")
    if len(history) > limit:
        print(f"... and {len(history) - limit} more")


async def run(args: argparse.Namespace, start_urls: list[str]) -> None:
    async with AsyncCrawler(
        max_concurrent=args.concurrency,
        max_depth=args.max_depth,
        requests_per_second=args.rps,
        per_domain_rate=not args.global_rate,
        min_delay=args.min_delay,
        jitter=args.jitter,
        respect_robots=not args.ignore_robots,
        user_agent=args.user_agent,
        max_retries=args.retries,
    ) as crawler:
        limiter = crawler.rate_limiter
        print(f"User-Agent: {crawler.user_agent}")
        print(f"Limit: {args.rps} req/s, min delay {args.min_delay}s, jitter up to {args.jitter}s "
              f"-> interval {limiter.interval:.2f}..{limiter.interval + limiter.jitter:.2f}s "
              f"{'for all domains' if args.global_rate else 'per domain'}")
        if crawler.respect_robots:
            for url in start_urls:
                await show_robots(crawler, url)

        print("\n=== Crawling ===")
        results = await crawler.crawl(start_urls, max_pages=args.max_pages, same_domain_only=True)
        stats = crawler.get_stats()

    print_delay_stats(crawler)
    rate = stats["rate"]
    print(f"\nProcessed:    {len(results)} pages in {stats['elapsed']:.1f}s, failed: {stats['failed']}, "
          f"retries: {stats['retries']}")
    print(f"Requests:     {rate['total_requests']} "
          f"({rate['total_requests'] / stats['elapsed'] if stats['elapsed'] else 0:.2f} req/s on average, "
          f"{rate['current_rps']:.2f} req/s at the end)")
    print(f"Gap between requests to one domain: avg {rate['avg_gap']:.2f}s, min {rate['min_gap']:.2f}s")
    print(f"Average wait for the rate limiter: {rate['avg_wait']:.2f}s")
    if rate["domain_delays"]:
        print(f"Crawl-delay applied: {rate['domain_delays']}")
    print(f"Blocked by robots.txt: {stats['robots_blocked']}")
    for url in sorted(crawler.blocked_urls):
        print(f"  {url}")
    for url, error in crawler.failed_urls.items():
        print(f"Failed: {url} — {error}")


async def main(args: argparse.Namespace) -> None:
    if args.urls:
        await run(args, args.urls)
        return
    async with local_site() as url:
        print(f"Local demo site: {url}")
        await run(args, [url])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    setup_logging()
    # Keep robots.txt decisions visible, hide per-request lines
    logging.getLogger("crawler").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)  # the local site's log
    asyncio.run(main(parse_args()))

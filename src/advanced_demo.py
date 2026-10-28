"""Demo: AdvancedCrawler from a config file, with sitemaps, statistics and reports.

    python src/advanced_demo.py                    # local demo site (offline)
    python src/advanced_demo.py --config config.yaml   # your config, real sites

The local site has robots.txt, a sitemap index, slow pages, 404s and a
flaky 503 page, so every part of the report has something to show.
Writes output/demo_config.yaml, output/demo_pages.jsonl, output/demo.db,
output/demo_stats.json, output/report.html and logs/demo.log.
"""

import argparse
import asyncio
import html
import random
import sys
from collections import Counter
from contextlib import asynccontextmanager

from aiohttp import web

from crawler import AdvancedCrawler

SECTIONS = ("news", "docs", "shop", "blog")


def demo_page(path: str) -> str:
    parts = path.strip("/").split("/")
    section = parts[0] if parts[0] in SECTIONS else None
    if section and len(parts) == 2 and parts[1].isdigit():
        n = int(parts[1])
        links = [f"/{section}/{n + 1}", f"/{section}/{n + 7}", f"/{section}/", "/"]
    elif section:
        links = [f"/{section}/{i}" for i in range(1, 9)] + ["/"]
    else:
        links = [f"/{s}/" for s in SECTIONS] + ["/private/admin", "/old-page", "/flaky"]
    title = html.escape(path.strip("/").replace("/", " · ") or "Home")
    anchors = "".join(f'<li><a href="{href}">{href}</a></li>' for href in links)
    return (f"<html><head><title>{title}</title></head><body><h1>{title}</h1>"
            f"<p>Demo page {html.escape(path)}.</p><ul>{anchors}</ul></body></html>")


@asynccontextmanager
async def local_site():
    hits: Counter[str] = Counter()
    rng = random.Random(7)

    async def handler(request: web.Request) -> web.Response:
        path = request.path
        hits[path] += 1
        base = f"{request.scheme}://{request.host}"
        if path == "/robots.txt":
            return web.Response(text=f"User-agent: *\nDisallow: /private/\nSitemap: {base}/sitemap_index.xml\n")
        if path == "/sitemap_index.xml":
            maps = "".join(f"<sitemap><loc>{base}/sitemap-{s}.xml</loc></sitemap>" for s in ("blog", "shop"))
            return web.Response(text=f"<sitemapindex>{maps}</sitemapindex>", content_type="application/xml")
        if path.startswith("/sitemap-"):
            section = path[len("/sitemap-"):-len(".xml")]
            locs = "".join(f"<url><loc>{base}/{section}/{i}</loc></url>" for i in range(20, 26))
            return web.Response(text=f"<urlset>{locs}</urlset>", content_type="application/xml")
        if path == "/flaky" and hits[path] % 3:
            raise web.HTTPServiceUnavailable()  # recovers on the third attempt
        if path == "/old-page" or path.endswith(("/13", "/29")):
            raise web.HTTPNotFound()
        parts = path.strip("/").split("/")
        if path not in ("/", "/flaky") and parts[0] not in SECTIONS:
            raise web.HTTPNotFound()
        # Real servers are uneven: most pages are quick, some are slow
        await asyncio.sleep(rng.choice((0.01, 0.02, 0.05, 0.05, 0.1, 0.3)))
        return web.Response(text=demo_page(path), content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


def write_demo_config(base: str, path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"""# Written by advanced_demo.py for the local demo site
start_urls: ["{base}/"]
crawler:
  max_pages: 60
  max_depth: 3
  max_concurrent: 8
  rate_limit: 40
  respect_robots: true
  max_retries: 2
  backoff_base: 0.1
  user_agent: "DemoBot/1.0"
sitemap:
  discover: true
storage:
  - output/demo_pages.jsonl
  - output/demo.db
output:
  stats_json: output/demo_stats.json
  report_title: Demo crawl report
logging:
  file: logs/demo.log
  console_level: ERROR
progress_interval: 0.25
""")


async def main(config_path: str) -> None:
    # The example from the task, as is
    crawler = AdvancedCrawler.from_config(config_path, setup_logging=True)

    await crawler.crawl()

    stats = crawler.get_stats()
    print(f"Обработано: {stats['total_pages']} страниц")
    print(f"Успешно: {stats['successful']}")
    print(f"Ошибок: {stats['failed']}")

    crawler.export_to_html_report("output/report.html")
    await crawler.close()

    # A bit more of what the statistics hold
    print(f"\nBlocked by robots.txt: {stats['blocked']}, pages from sitemaps: {len(crawler.sitemap_urls)}")
    print(f"Run time {stats['uptime']:.2f}s, {stats['avg_speed']:.1f} pages/s, "
          f"avg response {stats['avg_response_time'] * 1000:.0f} ms, retries {stats['retries']}")
    print("Status codes:", stats["status_codes"])
    print("Top domains:", [(d["domain"], d["pages"]) for d in stats["top_domains"]])
    print("Pages by depth:", stats["depths"])
    if crawler.config.output.stats_json:
        crawler.export_to_json(crawler.config.output.stats_json)
        print(f"\nStatistics: {crawler.config.output.stats_json}")
    print("Report: output/report.html")
    if crawler.config.logging.file:
        print(f"Log: {crawler.config.logging.file}")


async def demo() -> None:
    async with local_site() as base:
        print(f"Local demo site: {base}\n")
        import os

        os.makedirs("output", exist_ok=True)
        write_demo_config(base, "output/demo_config.yaml")
        await main("output/demo_config.yaml")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", help="a config file of your own instead of the local demo site")
    args = parser.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")  # Cyrillic output in any console
    asyncio.run(main(args.config) if args.config else demo())

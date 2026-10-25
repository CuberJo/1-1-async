"""Demo: crawl a site with a depth limit, show live progress, save the data.

    python src/crawl_demo.py
    python src/crawl_demo.py https://books.toscrape.com --max-pages 30 --max-depth 1
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

import aiofiles

from crawler import AsyncCrawler
from utils import print_parse_stats, setup_logging

DEFAULT_START_URLS = ["https://quotes.toscrape.com/"]
# Login form and per-tag listings add little; tag pages alone are ~150 URLs
DEFAULT_EXCLUDE = [r"/login", r"/tag/.+/page/"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("urls", nargs="*", default=DEFAULT_START_URLS, help="start URLs")
    p.add_argument("--max-pages", type=int, default=50)
    p.add_argument("--max-depth", type=int, default=2)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--per-domain", type=int, default=5, help="concurrent requests per domain")
    p.add_argument("--all-domains", action="store_true", help="follow links to other sites")
    p.add_argument("--include", action="append", default=[], help="regex, may repeat")
    p.add_argument("--exclude", action="append", default=None, help="regex, may repeat")
    p.add_argument("--output", type=Path, default=Path("output/crawl_results.json"))
    return p.parse_args()


async def save_results(path: Path, crawler: AsyncCrawler, start_urls: list[str]) -> None:
    data = {
        "start_urls": start_urls,
        "stats": crawler.get_stats(),
        "pages": crawler.processed_urls,
        "failed": crawler.failed_urls,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(data, ensure_ascii=False, indent=2))


async def main(args: argparse.Namespace) -> None:
    async with AsyncCrawler(
        max_concurrent=args.concurrency,
        max_per_domain=args.per_domain,
        max_depth=args.max_depth,
    ) as crawler:
        results = await crawler.crawl(
            start_urls=args.urls,
            max_pages=args.max_pages,
            same_domain_only=not args.all_domains,
            include_patterns=args.include,
            exclude_patterns=DEFAULT_EXCLUDE if args.exclude is None else args.exclude,
        )

    pages = list(results.values())
    if pages:
        print_parse_stats(pages)

    stats = crawler.get_stats()
    by_depth: dict[int, int] = {}
    for page in pages:
        by_depth[page["depth"]] = by_depth.get(page["depth"], 0) + 1

    print(f"\nProcessed:  {len(results)} pages in {stats['elapsed']:.1f}s ({stats['pages_per_sec']:.2f} pages/s)")
    print(f"Failed:     {len(crawler.failed_urls)}")
    for url, error in crawler.failed_urls.items():
        print(f"  {url} — {error}")
    print(f"Left in queue: {stats['queued']}")
    print(f"By depth:   {dict(sorted(by_depth.items()))}")
    print(f"Skipped links: {stats['skipped']}")
    print(f"Peak concurrency: {stats['concurrency']['peak_active']} "
          f"(per domain: {stats['concurrency']['peak_per_domain']})")

    await save_results(args.output, crawler, args.urls)
    print(f"Saved to {args.output}")


if __name__ == "__main__":
    # Page titles contain non-ASCII characters; a Windows pipe defaults to cp1252
    sys.stdout.reconfigure(encoding="utf-8")
    setup_logging()
    # Per-request logs would drown the progress lines
    logging.getLogger("crawler").setLevel(logging.WARNING)
    asyncio.run(main(parse_args()))

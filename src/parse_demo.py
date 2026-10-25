"""Demo: fetch and parse several pages, print summaries and statistics."""

import asyncio
import json
import sys

from crawler import AsyncCrawler
from utils import print_parse_stats, setup_logging, summarize_page

URLS = [
    "https://example.com",
    "https://httpbin.org/html",
    "https://www.python.org",
    "https://docs.python.org/3/library/asyncio.html",
    "https://en.wikipedia.org/wiki/Web_crawler",
    "https://httpbin.org/status/404",  # fetch error -> empty result, no crash
]


async def main():
    async with AsyncCrawler(max_concurrent=5) as crawler:
        pages = await asyncio.gather(*(crawler.fetch_and_parse(url) for url in URLS))

    for page in pages:
        print(f"\n=== {page['url']} ===")
        print(json.dumps(summarize_page(page), ensure_ascii=False, indent=4))

    print_parse_stats(pages)
    parsed = sum(1 for p in pages if not p["errors"])
    print(f"\nParsed without errors: {parsed} of {len(pages)}")
    print(f"Total links: {sum(len(p['links']) for p in pages)}")


if __name__ == "__main__":
    # Page titles contain non-ASCII characters; a Windows pipe defaults to cp1252
    sys.stdout.reconfigure(encoding="utf-8")
    setup_logging()
    asyncio.run(main())

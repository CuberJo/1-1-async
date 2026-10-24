"""Demo: sequential vs concurrent fetching."""

import asyncio
import time

from crawler import AsyncCrawler
from utils import print_report, setup_logging

URLS = [
    "https://example.com",
    "https://httpbin.org/delay/1",
    "https://httpbin.org/delay/2",
    "https://httpbin.org/get",
    "https://httpbin.org/html",
    "https://www.python.org",
    "https://httpbin.org/status/404",               # HTTP error
    "https://httpbin.org/status/500",               # HTTP error
    "https://nonexistent-domain-for-test.invalid",  # network error
    "https://httpbin.org/delay/10",                 # timeout
]


async def sequential(crawler: AsyncCrawler) -> float:
    start = time.perf_counter()
    for url in URLS:
        await crawler.fetch_url(url)
    return time.perf_counter() - start


async def parallel(crawler: AsyncCrawler) -> tuple[float, dict[str, str]]:
    start = time.perf_counter()
    results = await crawler.fetch_urls(URLS)
    return time.perf_counter() - start, results


async def main():
    # read_timeout=5 so that /delay/10 is guaranteed to time out
    async with AsyncCrawler(max_concurrent=5, read_timeout=5) as crawler:
        print("=== Sequential fetching ===")
        seq_time = await sequential(crawler)
        print_report([crawler.results[url] for url in URLS])

        print("\n=== Concurrent fetching ===")
        par_time, results = await parallel(crawler)
        print_report([crawler.results[url] for url in URLS])

    print(f"\nFetched {len(results)} of {len(URLS)} pages")
    print(f"Sequential: {seq_time:.2f}s")
    print(f"Concurrent: {par_time:.2f}s")
    print(f"Speedup:    x{seq_time / par_time:.1f}")


if __name__ == "__main__":
    setup_logging()
    asyncio.run(main())

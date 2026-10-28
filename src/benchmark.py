"""Day 7 — performance tests: async vs sync crawler, memory, scalability.

    python src/benchmark.py                       # 100 / 500 / 1000 pages, 50 ms latency
    python src/benchmark.py --pages 100 1000 5000 --latency 0.1 --concurrency 50
    python src/benchmark.py --sync-pages 100 500  # sync runs are slow: few by default
    python src/benchmark.py --parse-workers 0     # skip the process pool runs

A local site runs in a separate process, so its CPU doesn't compete with
the crawler. Every response waits --latency seconds, like a real network.
Each run is made twice: once for time, once under tracemalloc for memory
(tracemalloc slows Python down, so it never touches the timing run).
tracemalloc sees the main process only: with parse_workers the parsing
memory lives in the worker processes.
Results: a table on screen and output/benchmark.json.
"""

import argparse
import asyncio
import json
import logging
import multiprocessing
import os
import socket
import time
import tracemalloc
import urllib.error
import urllib.request
from collections import deque

from aiohttp import web

from crawler import AsyncCrawler
from html_parser import HTMLParser
from storage import JSONStorage
from url_filter import canonicalize

FANOUT = 10  # links to child pages: /p/0 -> /p/1../p/10, /p/1 -> /p/11../p/20, ...
TOTAL_PAGES = 20_000  # pages that exist on the benchmark site


def site_page(n: int) -> str:
    """A ~10 KB page: text, a table, a list, child links and a few back links."""
    children = [f"/p/{FANOUT * n + k}" for k in range(1, FANOUT + 1) if FANOUT * n + k < TOTAL_PAGES]
    back = ["/p/0", f"/p/{max(n - 1, 0)}", f"/p/{(n - 1) // FANOUT if n else 0}"]
    links = "".join(f'<li><a href="{href}">Page {href[3:]}</a></li>' for href in children + back)
    text = "".join(
        f"<p>Paragraph {i} of page {n}. Asynchronous crawlers overlap network waits, "
        f"so a slow server costs time once per batch, not once per page.</p>"
        for i in range(25)
    )
    rows = "".join(f"<tr><td>{n}</td><td>{i}</td><td>{n * i}</td></tr>" for i in range(20))
    return (
        f"<html><head><title>Page {n}</title><meta name='description' content='Benchmark page {n}'>"
        f"</head><body><h1>Page {n}</h1>{text}<table><tr><th>n</th><th>i</th><th>n*i</th></tr>{rows}"
        f"</table><ul>{links}</ul></body></html>"
    )


def run_site(port: int, latency: float) -> None:
    """The benchmark site; runs in a child process until terminated."""

    async def handler(request: web.Request) -> web.Response:
        await asyncio.sleep(latency)
        name = request.match_info["n"]
        if not name.isdigit() or int(name) >= TOTAL_PAGES:
            raise web.HTTPNotFound()
        return web.Response(text=site_page(int(name)), content_type="text/html")

    app = web.Application()
    app.router.add_get("/p/{n}", handler)
    web.run_app(app, host="127.0.0.1", port=port, print=None, access_log=None, backlog=1024)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(base: str, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            urllib.request.urlopen(base + "/p/0", timeout=1).read()
            return
        except (urllib.error.URLError, OSError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)


class SyncCrawler:
    """The baseline: the same BFS and the same parser, one page at a time."""

    def __init__(self, timeout: float = 10.0):
        self.parser = HTMLParser()
        self.timeout = timeout

    def crawl(self, start_url: str, max_pages: int) -> int:
        queue, seen, done = deque([start_url]), {start_url}, 0
        while queue and done < max_pages:
            url = queue.popleft()
            done += 1
            try:
                with urllib.request.urlopen(url, timeout=self.timeout) as response:
                    html = response.read().decode("utf-8", errors="replace")
            except (urllib.error.URLError, OSError):
                continue
            page = self.parser._parse_sync(html, url)
            for link in page["links"]:
                link = canonicalize(link)
                if link not in seen:
                    seen.add(link)
                    queue.append(link)
        return done


async def crawl_async(
    start_url: str, pages: int, concurrency: int,
    keep_pages: bool = True, out: str | None = None, parse_workers: int = 0,
) -> int:
    storage = JSONStorage(out, batch_size=100) if out else None
    async with AsyncCrawler(
        max_concurrent=concurrency, max_depth=10, progress_interval=None,
        keep_pages=keep_pages, storage=storage, parse_workers=parse_workers,
    ) as crawler:
        await crawler.crawl([start_url], max_pages=pages)
    return len(crawler.visited_urls)


def measure(fn, memory: bool) -> dict:
    """Run fn() once; with memory=True under tracemalloc (peak of Python allocations)."""
    if memory:
        tracemalloc.start()
    start = time.perf_counter()
    pages = fn()
    elapsed = time.perf_counter() - start
    result = {"pages": pages, "seconds": round(elapsed, 3)}
    if memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        result = {"pages": pages, "peak_mb": round(peak / 2**20, 1)}
    return result


def run_case(name: str, fn) -> dict:
    timing = measure(fn, memory=False)
    mem = measure(fn, memory=True)
    pages = timing["pages"]
    row = {
        "name": name,
        "pages": pages,
        "seconds": timing["seconds"],
        "pages_per_sec": round(pages / timing["seconds"], 1) if timing["seconds"] else 0.0,
        "peak_mb": mem["peak_mb"],
        "kb_per_page": round(mem["peak_mb"] * 1024 / pages, 1) if pages else 0.0,
    }
    print(f"{name:<40} {pages:>6} {row['seconds']:>9.2f} {row['pages_per_sec']:>9.1f} "
          f"{row['peak_mb']:>9.1f} {row['kb_per_page']:>8.1f}", flush=True)
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pages", type=int, nargs="+", default=[100, 500, 1000])
    parser.add_argument("--sync-pages", type=int, nargs="*", default=[100],
                        help="sizes for the sync crawler (it takes pages x latency seconds)")
    parser.add_argument("--latency", type=float, default=0.05, help="server delay per page, seconds")
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--parse-workers", type=int, default=max((os.cpu_count() or 2) - 1, 1),
                        help="processes for the parse_workers runs, 0 to skip them (default: CPUs - 1)")
    parser.add_argument("--output", default="output/benchmark.json")
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)

    port = free_port()
    server = multiprocessing.Process(target=run_site, args=(port, args.latency), daemon=True)
    server.start()
    base = f"http://127.0.0.1:{port}"
    start_url = base + "/p/0"
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    tmp_out = os.path.join(os.path.dirname(args.output) or ".", "benchmark_pages.jsonl")
    try:
        wait_for(base)
        print(f"Site: {base}, latency {args.latency * 1000:.0f} ms, async concurrency {args.concurrency}\n")
        print(f"{'run':<40} {'pages':>6} {'seconds':>9} {'pages/s':>9} {'peak MB':>9} {'KB/page':>8}")
        print("-" * 86)
        rows = []
        for n in args.sync_pages:
            rows.append(run_case(f"sync, {n} pages", lambda n=n: SyncCrawler().crawl(start_url, n)))
        for n in args.pages:
            rows.append(run_case(
                f"async, {n} pages",
                lambda n=n: asyncio.run(crawl_async(start_url, n, args.concurrency)),
            ))
        if args.parse_workers:
            for n in args.pages:
                rows.append(run_case(
                    f"async, parse_workers={args.parse_workers}, {n} pages",
                    lambda n=n: asyncio.run(crawl_async(
                        start_url, n, args.concurrency, parse_workers=args.parse_workers)),
                ))
        for n in args.pages:
            rows.append(run_case(
                f"async + storage, keep_pages=False, {n}",
                lambda n=n: asyncio.run(crawl_async(start_url, n, args.concurrency, False, tmp_out)),
            ))
    finally:
        server.terminate()
        server.join()
        if os.path.exists(tmp_out):
            os.remove(tmp_out)

    sync = {r["pages"]: r for r in rows if r["name"].startswith("sync")}
    speedups = {}
    for prefix in ("async, ", f"async, parse_workers={args.parse_workers}, "):
        runs = {r["pages"]: r for r in rows if r["name"] == f"{prefix}{r['pages']} pages"}
        for n in sorted(sync.keys() & runs.keys()):
            x = round(sync[n]["seconds"] / runs[n]["seconds"], 1)
            speedups[f"{prefix}{n} pages"] = x
            print(f"\n{prefix.strip(', ')} is {x}x faster than sync on {n} pages", end="")
    print()
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump({"latency": args.latency, "concurrency": args.concurrency, "runs": rows,
                   "speedup": speedups}, f, indent=2)
    print(f"Saved to {args.output}")


if __name__ == "__main__":
    main()

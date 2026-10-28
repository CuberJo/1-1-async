"""Demo: saving crawled pages to JSON, CSV and SQLite, reading them back.

    python src/storage_demo.py                    # local demo site (offline)
    python src/storage_demo.py https://quotes.toscrape.com/ --max-pages 20
    python src/storage_demo.py --benchmark 0      # skip the batch size benchmark

Writes output/pages.jsonl, pages.json, pages.csv and crawler.db.
"""

import argparse
import asyncio
import html
import logging
import os
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from aiohttp import web

from crawler import AsyncCrawler
from storage import CSVStorage, DataStorage, JSONStorage, MultiStorage, SQLiteStorage
from utils import setup_logging

# Titles and texts that break naive CSV and JSON writers
LOCAL_PAGES = {
    "/": ("Home, sweet \"home\"", "Welcome!\nThis line has a comma, a; semicolon and a tab\there."),
    "/ru": ("Новости; обзор", "Текст на русском — с «кавычками» и переносом\nстроки."),
    "/emoji": ("Emoji 🚀 page", "Unicode: ü ß ñ 中文 🎉 — and a backslash \\ too."),
    "/quotes": ('He said "hi", then \'bye\'', 'Cell with "double" quotes, commas, and\r\nCRLF.'),
}


def local_page(path: str) -> str:
    n = int(path.rsplit("/", 1)[1]) if path.startswith("/item/") else None
    if n is not None:
        title, text = f"Item {n}", f"Description of item {n}, price {n * 10}$"
        links = [f"/item/{n + 1}", "/"]
    else:
        title, text = LOCAL_PAGES[path]
        links = [p for p in LOCAL_PAGES if p != path] + [f"/item/{i}" for i in range(1, 6)]
    anchors = "".join(f'<li><a href="{href}">{href}</a></li>' for href in links)
    paragraphs = "".join(f"<p>{html.escape(line)}</p>" for line in text.splitlines())
    title = html.escape(title)
    return (
        f"<html lang='en'><head><title>{title}</title>"
        f'<meta name="description" content="{title} — demo page"></head>'
        f"<body><h1>{title}</h1>{paragraphs}<ul>{anchors}</ul></body></html>"
    )


@asynccontextmanager
async def local_site():
    async def handler(request: web.Request) -> web.Response:
        path = request.path
        if path not in LOCAL_PAGES and not (path.startswith("/item/") and path[6:].isdigit()):
            raise web.HTTPNotFound()
        if path == "/item/7":
            raise web.HTTPServiceUnavailable()  # a failed page is not saved
        return web.Response(text=local_page(path), content_type="text/html")

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
    p.add_argument("--max-pages", type=int, default=30)
    p.add_argument("--max-depth", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=5)
    p.add_argument("--out", default="output", help="output directory")
    p.add_argument("--csv-encoding", default="utf-8-sig", help="utf-8-sig opens correctly in Excel")
    p.add_argument("--benchmark", type=int, default=1000, help="records per benchmark run, 0 = skip")
    return p.parse_args()


def remove(*paths: str) -> None:
    for path in paths:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(path + suffix):
                os.remove(path + suffix)


def size(path: str) -> str:
    n = os.path.getsize(path) if os.path.exists(path) else 0
    return f"{n / 1024:.1f} KB"


def print_storage_stats(storages: list[DataStorage]) -> None:
    print(f"\n{'Storage':<12} {'File':<28} {'Size':>10} {'Saved':>6} {'Failed':>7} "
          f"{'Batches':>8} {'Retries':>8} {'Write time':>11}")
    print("-" * 96)
    for s in storages:
        st = s.get_stats()
        kind = type(s).__name__.removesuffix("Storage")
        if isinstance(s, JSONStorage):
            kind += " lines" if s.lines else " array"
        print(f"{kind:<12} {s.path:<28} {size(s.path):>10} {st['saved']:>6} {st['failed']:>7} "
              f"{st['batches']:>8} {st['retries']:>8} {st['write_time'] * 1000:>9.1f}ms")


async def read_back(storages: list[DataStorage], results: dict[str, dict]) -> None:
    print("\n=== Reading the saved data back ===")
    loaded = {}
    for s in storages:
        start = time.perf_counter()
        loaded[s.path] = {r["url"]: r for r in await s.load()}
        print(f"{s.path:<28} {len(loaded[s.path]):>4} records in {(time.perf_counter() - start) * 1000:.1f}ms")

    # Every format must give back the same records as the others
    reference = next(iter(loaded.values()))
    problems = []
    if set(reference) != set(results):
        problems.append("saved URLs differ from the crawled ones")
    for path, records in loaded.items():
        for url, record in reference.items():
            if records.get(url) != record:
                problems.append(f"{path}: {url} differs")
    print("Integrity: " + ("OK, all formats hold identical records" if not problems else "; ".join(problems[:5])))

    sample = reference.get(next((u for u in reference if u.endswith("/quotes")), next(iter(reference), "")))
    if sample:
        print("\nSample record (CSV round trip of tricky characters):")
        csv_record = loaded[storages[2].path][sample["url"]]
        for key in ("url", "title", "text", "status_code", "content_type", "crawled_at"):
            print(f"  {key:<13} {csv_record[key]!r}")
        print(f"  {'links':<13} {len(csv_record['links'])} links, e.g. {csv_record['links'][:2]}")
        print(f"  {'metadata':<13} {csv_record['metadata']}")


async def query_db(db: SQLiteStorage) -> None:
    print("\n=== SQLite queries ===")
    s = await db.summary()
    print(f"Pages: {s['pages']}, links: {s['links']}, average text: {s['avg_text_length']} chars")
    print(f"Crawled from {s['first_crawled']} to {s['last_crawled']}")
    print(f"By domain:       {s['by_domain']}")
    print(f"By status:       {s['by_status']}")
    print(f"By content type: {s['by_content_type']}")
    print("Most links:      " + ", ".join(f"{url} ({n})" for url, n in s["most_links"][:3]))
    since = datetime.now(timezone.utc) - timedelta(minutes=5)
    recent = await db.find(since=since, limit=3)
    print(f"Crawled in the last 5 minutes (3 newest): {[r['url'] for r in recent]}")
    for word in ("Item", "Новости"):
        found = await db.find(title_contains=word, limit=50)
        print(f"Title contains {word!r}: {len(found)} pages, e.g. {[r['title'] for r in found[:3]]}")


async def show_error_handling(out: str) -> None:
    print("\n=== Write errors don't stop anything ===")
    # A directory where a file should be: every write fails
    storage = JSONStorage(out, max_retries=2, retry_delay=0.05)
    accepted = await storage.save({"url": "https://example.com/", "title": "lost"})
    rejected = await storage.save({"title": "no url"})
    await storage.close()
    st = storage.get_stats()
    print(f"save() returned {accepted} (accepted) and {rejected} (invalid record), no exception")
    print(f"failed: {st['failed']}, retries: {st['retries']}, last error: {st['last_error']}")


async def benchmark(out: str, n: int) -> None:
    print(f"\n=== Batch size benchmark: {n} records ===")
    text = "Lorem ipsum, dolor \"sit\" amet. " * 60
    records = [
        {"url": f"https://example.com/p/{i}", "title": f"Page {i}", "text": text,
         "links": [f"https://example.com/p/{i + k}" for k in range(1, 11)],
         "metadata": {"description": f"page {i}"}, "status_code": 200, "content_type": "text/html"}
        for i in range(n)
    ]
    makers = {
        "JSON lines": lambda path, b: JSONStorage(path + ".jsonl", batch_size=b),
        "CSV": lambda path, b: CSVStorage(path + ".csv", batch_size=b),
        "SQLite": lambda path, b: SQLiteStorage(path + ".db", batch_size=b),
    }
    print(f"{'Storage':<12} {'batch=1':>14} {'batch=100':>14} {'speed-up':>9}")
    for name, make in makers.items():
        speeds = []
        for batch in (1, 100):
            path = os.path.join(out, f"bench_{batch}")
            remove(path + ".jsonl", path + ".csv", path + ".db")
            start = time.perf_counter()
            async with make(path, batch) as storage:
                for r in records:
                    await storage.save(r)
            speeds.append(n / (time.perf_counter() - start))
            remove(path + ".jsonl", path + ".csv", path + ".db")
        print(f"{name:<12} {speeds[0]:>10.0f} r/s {speeds[1]:>10.0f} r/s {speeds[1] / speeds[0]:>8.1f}x")


async def run(args: argparse.Namespace, start_urls: list[str]) -> None:
    out = args.out
    paths = [os.path.join(out, name) for name in ("pages.jsonl", "pages.json", "pages.csv", "crawler.db")]
    remove(*paths)
    storages: list[DataStorage] = [
        JSONStorage(paths[0], batch_size=10),
        JSONStorage(paths[1], indent=2, batch_size=10),
        CSVStorage(paths[2], encoding=args.csv_encoding, batch_size=10),
        SQLiteStorage(paths[3], batch_size=10),
    ]

    print("=== Crawling, saving every page to 4 storages ===")
    crawler = AsyncCrawler(
        max_concurrent=args.concurrency,
        max_depth=args.max_depth,
        progress_interval=0.5,
        storage=MultiStorage(*storages),
    )
    try:
        results = await crawler.crawl(start_urls, max_pages=args.max_pages, same_domain_only=True)
    finally:
        await crawler.close()  # closes the storages too
    print(f"Processed: {len(results)}, failed (not saved): {len(crawler.failed_urls)}")
    print_storage_stats(storages)

    # Fresh instances, as another program would read the files
    readers = [
        JSONStorage(paths[0]),
        JSONStorage(paths[1]),
        CSVStorage(paths[2], encoding=args.csv_encoding),
        SQLiteStorage(paths[3]),
    ]
    try:
        await read_back(readers, results)
        await query_db(readers[3])
    finally:
        for r in readers:
            await r.close()

    await show_error_handling(out)
    if args.benchmark:
        await benchmark(out, args.benchmark)


async def main(args: argparse.Namespace) -> None:
    if args.urls:
        await run(args, args.urls)
        return
    async with local_site() as url:
        print(f"Local demo site: {url}")
        await run(args, [url])


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    setup_logging(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    asyncio.run(main(parse_args()))

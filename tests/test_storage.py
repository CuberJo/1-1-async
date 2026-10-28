"""Storage tests: JSON, CSV, SQLite, batching, write errors, crawler integration."""

import asyncio
import csv
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from aiohttp import web

from crawler import AsyncCrawler
from storage import (
    FIELDS,
    CSVStorage,
    DataStorage,
    JSONStorage,
    MultiStorage,
    SQLiteStorage,
    make_record,
    normalize_record,
)

T0 = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)

# Everything that tends to break a file format
TRICKY_TEXT = 'Comma, "quotes", semi;colon\nnew line\r\ntab\t Кириллица ü 🚀 \\ back\\slash'


def record(i: int, **overrides) -> dict:
    data = {
        "url": f"https://example.com/page/{i}",
        "title": f"Page {i}, \"quoted\"",
        "text": f"{TRICKY_TEXT} #{i}",
        "links": [f"https://example.com/page/{i + 1}", "https://other.org/?a=1&b=2"],
        "metadata": {"description": f"Описание {i}", "keywords": ["a", "b"], "og": {"type": "article"}},
        "crawled_at": T0 + timedelta(seconds=i),
        "status_code": 200,
        "content_type": "text/html",
    }
    data.update(overrides)
    return data


async def save_all(storage: DataStorage, records: list[dict]) -> None:
    for r in records:
        assert await storage.save(r)


def by_url(records: list[dict]) -> dict[str, dict]:
    return {r["url"]: r for r in records}


# --- the standard record -------------------------------------------------


def test_normalize_fills_defaults_and_keeps_extras():
    r = normalize_record({"url": "https://a.org/", "status_code": "404", "depth": 2})
    assert list(r)[: len(FIELDS)] == list(FIELDS)
    assert r["title"] == "" and r["links"] == [] and r["metadata"] == {}
    assert r["status_code"] == 404 and r["content_type"] is None
    assert isinstance(r["crawled_at"], datetime) and r["depth"] == 2


@pytest.mark.parametrize("bad", [{}, {"url": ""}, {"url": 5}, {"url": "u", "crawled_at": "yesterday"}])
def test_normalize_rejects_invalid(bad):
    with pytest.raises((ValueError, TypeError)):
        normalize_record(bad)


def test_make_record_from_page_and_fetch():
    from models import FetchResult

    page = {"url": "https://a.org/", "title": "T", "text": "x", "links": ["https://a.org/b"],
            "metadata": {"title": "T"}, "images": [], "depth": 1}
    fetch = FetchResult(url="https://a.org/", status=200, content_type="text/html")
    r = make_record(page, fetch, crawled_at=T0)
    assert set(r) == set(FIELDS)
    assert r["status_code"] == 200 and r["content_type"] == "text/html" and r["crawled_at"] == T0


# --- JSON ------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name, indent", [("out.jsonl", None), ("out.json", None), ("out.json", 2)])
async def test_json_roundtrip(tmp_path, name, indent):
    records = [record(i) for i in range(45)]
    path = tmp_path / name
    async with JSONStorage(path, indent=indent, batch_size=10) as storage:
        await save_all(storage, records)
    assert storage.get_stats()["saved"] == 45

    loaded = await JSONStorage(path).load()
    assert loaded == [normalize_record(r) for r in records]  # dates, unicode, nesting intact
    raw = path.read_text(encoding="utf-8")
    assert "Кириллица ü 🚀" in raw  # not \u-escaped
    if name.endswith(".jsonl"):
        assert len(raw.splitlines()) == 45
    else:
        assert json.loads(raw)[0]["url"] == records[0]["url"]
        assert ('\n    "url"' in raw) == bool(indent)


@pytest.mark.asyncio
async def test_json_array_is_valid_after_every_batch(tmp_path):
    path = tmp_path / "out.json"
    storage = JSONStorage(path, indent=2, batch_size=3)
    for i in range(10):
        await storage.save(record(i))
        if i >= 2:  # the first batch is on disk
            assert len(json.loads(path.read_text(encoding="utf-8"))) == (i + 1) // 3 * 3
    await storage.close()
    assert len(json.loads(path.read_text(encoding="utf-8"))) == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["out.jsonl", "out.json"])
async def test_json_append(tmp_path, name):
    path = tmp_path / name
    async with JSONStorage(path, indent=2) as storage:
        await save_all(storage, [record(i) for i in range(3)])
    async with JSONStorage(path, indent=2, append=True) as storage:
        await save_all(storage, [record(i) for i in range(3, 5)])
    async with JSONStorage(path) as storage:  # append=False replaces the file
        loaded = await storage.load()
    assert [r["url"] for r in loaded] == [record(i)["url"] for i in range(5)]


@pytest.mark.asyncio
async def test_json_append_to_empty_array(tmp_path):
    path = tmp_path / "out.json"
    path.write_text("[]", encoding="utf-8")
    async with JSONStorage(path, append=True) as storage:
        await storage.save(record(1))
    assert [r["url"] for r in json.loads(path.read_text(encoding="utf-8"))] == [record(1)["url"]]


@pytest.mark.asyncio
async def test_json_append_to_non_array_fails_without_raising(tmp_path):
    path = tmp_path / "out.json"
    path.write_text('{"not": "an array"}', encoding="utf-8")
    async with JSONStorage(path, append=True, retry_delay=0) as storage:
        assert await storage.save(record(1))  # accepted, then the write fails
    assert storage.stats["failed"] == 1
    assert path.read_text(encoding="utf-8") == '{"not": "an array"}'


@pytest.mark.asyncio
async def test_json_large_volume_streams(tmp_path):
    path = tmp_path / "big.jsonl"
    async with JSONStorage(path, batch_size=200) as storage:
        await save_all(storage, [record(i, text="x" * 2000) for i in range(2000)])
    assert storage.stats["batches"] == 10
    count = 0
    async for r in JSONStorage(path).iter_records():
        count += 1
    assert count == 2000


# --- CSV -------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "cp1251"])
async def test_csv_roundtrip_encodings(tmp_path, encoding):
    path = tmp_path / "out.csv"
    records = [record(i, title="Заголовок; \"кавычки\", запятая") for i in range(12)]
    async with CSVStorage(path, encoding=encoding, batch_size=5) as storage:
        await save_all(storage, records)

    loaded = await CSVStorage(path, encoding=encoding).load()
    expected = [normalize_record(r) for r in records]
    if encoding == "cp1251":  # no ü and emoji in cp1251: replaced, the rest intact
        for r in expected:
            r["text"] = r["text"].replace("ü", "?").replace("🚀", "?")
    assert loaded == expected

    # A standard reader sees one header and intact cells
    with open(path, encoding=encoding, newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == list(FIELDS)
    assert len(rows) == 13
    assert json.loads(rows[1][3]) == records[0]["links"]
    if bom := "".encode(encoding):
        assert path.read_bytes().count(bom) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16"])
async def test_csv_append_keeps_one_header_and_bom(tmp_path, encoding):
    path = tmp_path / "out.csv"
    async with CSVStorage(path, encoding=encoding) as storage:
        await save_all(storage, [record(1), record(2)])
    async with CSVStorage(path, encoding=encoding, append=True) as storage:
        await save_all(storage, [record(3)])
    data = path.read_bytes()
    assert data.count("".encode(encoding)) == 1
    loaded = await CSVStorage(path, encoding=encoding).load()
    assert [r["url"] for r in loaded] == [record(i)["url"] for i in (1, 2, 3)]


@pytest.mark.asyncio
async def test_csv_header_from_first_record_and_extra_fields(tmp_path, caplog):
    path = tmp_path / "out.csv"
    async with CSVStorage(path, delimiter=";") as storage:
        await storage.save(record(1, depth=0))
        await storage.save(record(2, depth=1, lang="ru"))  # lang is not in the header
    header = path.read_text(encoding="utf-8").splitlines()[0]
    assert header.split(";") == [*FIELDS, "depth"]
    assert "dropped: ['lang']" in caplog.text
    loaded = await CSVStorage(path, delimiter=";").load()
    assert [r["depth"] for r in loaded] == ["0", "1"]  # CSV keeps extras as text


@pytest.mark.asyncio
async def test_csv_strips_nul(tmp_path):
    path = tmp_path / "out.csv"
    async with CSVStorage(path) as storage:
        await storage.save(record(1, text="a\x00b"))
    assert (await CSVStorage(path).load())[0]["text"] == "ab"


def test_csv_unknown_encoding():
    with pytest.raises(LookupError):
        CSVStorage("x.csv", encoding="no-such-codec")


# --- SQLite ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_sqlite_init_db_creates_table_and_indexes(tmp_path):
    path = tmp_path / "db" / "crawler.db"
    async with SQLiteStorage(path) as storage:
        await storage.init_db()
        await storage.init_db()  # idempotent
    with sqlite3.connect(path) as db:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        plan = " ".join(str(r) for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM pages WHERE domain = 'example.com'"
        ))
    assert {"pages", "idx_pages_domain", "idx_pages_status", "idx_pages_crawled_at", "idx_pages_title"} <= names
    assert "idx_pages_domain" in plan


@pytest.mark.asyncio
async def test_sqlite_batches_roundtrip_and_upsert(tmp_path):
    path = tmp_path / "crawler.db"
    records = [record(i) for i in range(250)]
    async with SQLiteStorage(path, batch_size=100) as storage:
        await save_all(storage, records[:150])
        stats = storage.get_stats()
        assert (stats["batches"], stats["buffered"]) == (1, 50)
        await save_all(storage, records[150:])
        await storage.save(record(0, title="Updated"))  # same url
        assert await storage.count() == 250
        assert storage.stats["batches"] == 3

        loaded = await storage.load()
        expected = [normalize_record(r) for r in records]
        expected[0]["title"] = "Updated"
        assert loaded == expected
        assert (await storage.get(records[5]["url"])) == normalize_record(records[5])
        assert await storage.get("https://nowhere/") is None


@pytest.mark.asyncio
async def test_sqlite_find_and_summary(tmp_path):
    async with SQLiteStorage(tmp_path / "crawler.db", batch_size=10) as storage:
        await save_all(storage, [record(i) for i in range(5)])
        await storage.save(record(9, url="https://other.org/x", status_code=203, title="100%_done"))
        assert len(await storage.find(domain="example.com")) == 5
        assert [r["url"] for r in await storage.find(status_code=203)] == ["https://other.org/x"]
        assert [r["url"] for r in await storage.find(title_contains="%_")] == ["https://other.org/x"]
        assert len(await storage.find(since=T0 + timedelta(seconds=3))) == 3
        assert len(await storage.find(limit=2)) == 2
        s = await storage.summary()
    assert s["pages"] == 6 and s["links"] == 12
    assert s["by_domain"] == {"example.com": 5, "other.org": 1}
    assert s["by_status"] == {200: 5, 203: 1}
    assert s["first_crawled"] < s["last_crawled"]


@pytest.mark.asyncio
async def test_sqlite_stores_dates_in_utc(tmp_path):
    moscow = timezone(timedelta(hours=3))
    async with SQLiteStorage(tmp_path / "crawler.db") as storage:
        await storage.save(record(1, crawled_at=datetime(2026, 1, 1, 15, 0, tzinfo=moscow)))
        stored = (await storage.load())[0]["crawled_at"]
    assert stored == datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert stored.utcoffset() == timedelta(0)


# --- batching and concurrency ----------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_saves_lose_nothing(tmp_path):
    storages = [
        JSONStorage(tmp_path / "a.jsonl", batch_size=7),
        JSONStorage(tmp_path / "a.json", indent=2, batch_size=7),
        CSVStorage(tmp_path / "a.csv", batch_size=7),
        SQLiteStorage(tmp_path / "a.db", batch_size=7),
    ]
    multi = MultiStorage(*storages)
    records = [record(i) for i in range(200)]
    assert all(await asyncio.gather(*(multi.save(r) for r in records)))
    await multi.close()
    assert multi.get_stats()["saved"] == 800
    for storage in storages:
        assert by_url(await storage.load()) == by_url(normalize_record(r) for r in records), storage.name


@pytest.mark.asyncio
async def test_flush_writes_partial_batch(tmp_path):
    path = tmp_path / "out.jsonl"
    storage = JSONStorage(path, batch_size=10)
    await save_all(storage, [record(i) for i in range(4)])
    assert storage.get_stats()["buffered"] == 4 and not path.exists()
    await storage.flush()
    assert len(path.read_text(encoding="utf-8").splitlines()) == 4
    await storage.close()
    await storage.close()  # twice is fine


# --- write errors ----------------------------------------------------------


class FlakyStorage(JSONStorage):
    """Fails the first `failures` writes after writing half of the batch."""

    def __init__(self, *args, failures: int, **kwargs):
        super().__init__(*args, retry_delay=0.01, **kwargs)
        self.failures = failures

    async def _write_at_end(self, data: bytes, tail: bytes = b"") -> None:
        if self.failures:
            self.failures -= 1
            f = await self._open()
            await f.seek(self._end)
            await f.write(data[: len(data) // 2])
            await f.flush()
            raise OSError(28, "No space left on device")
        await super()._write_at_end(data, tail)


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["out.json", "out.jsonl"])
async def test_retry_after_partial_write_leaves_no_garbage(tmp_path, caplog, name):
    path = tmp_path / name
    async with FlakyStorage(path, batch_size=5, failures=2) as storage:
        await save_all(storage, [record(i) for i in range(10)])
    assert storage.stats["saved"] == 10 and storage.stats["retries"] == 2
    assert storage.stats["failed"] == 0
    assert len(await JSONStorage(path).load()) == 10
    assert "attempt 1/4, retry in" in caplog.text


@pytest.mark.asyncio
async def test_persistent_write_error_is_logged_not_raised(tmp_path, caplog):
    storage = FlakyStorage(tmp_path / "out.json", batch_size=2, failures=100, max_retries=2)
    await save_all(storage, [record(i) for i in range(5)])  # no exception
    await storage.close()
    assert storage.stats["failed"] == 5 and storage.stats["saved"] == 0
    assert storage.stats["retries"] == 6  # 2 per batch, 3 batches
    assert "No space left on device" in storage.stats["last_error"]
    assert caplog.text.count("records not saved") == 3


@pytest.mark.asyncio
async def test_unwritable_paths(tmp_path):
    directory = tmp_path / "dir"
    directory.mkdir()
    for storage in (JSONStorage(directory, retry_delay=0), CSVStorage(directory, retry_delay=0),
                    SQLiteStorage(directory, retry_delay=0)):
        async with storage:
            await storage.save(record(1))
        assert storage.stats["failed"] == 1, storage.name


@pytest.mark.asyncio
async def test_invalid_record_and_closed_storage(tmp_path, caplog):
    storage = JSONStorage(tmp_path / "out.jsonl")
    assert not await storage.save({"title": "no url"})
    assert await storage.save(record(1))
    await storage.close()
    assert not await storage.save(record(2))
    assert storage.stats["failed"] == 2 and storage.stats["saved"] == 1
    assert "invalid record" in caplog.text and "storage is closed" in caplog.text


@pytest.mark.asyncio
async def test_sqlite_batch_is_atomic(tmp_path, monkeypatch):
    """A failing batch leaves no rows behind; the retry inserts it once."""
    storage = SQLiteStorage(tmp_path / "crawler.db", batch_size=3, retry_delay=0)
    calls = 0
    original = storage._row

    def row(r):
        nonlocal calls
        calls += 1
        if calls == 2:  # the second row of the first attempt
            raise sqlite3.OperationalError("database is locked")
        return original(r)

    monkeypatch.setattr(storage, "_row", row)
    await save_all(storage, [record(i) for i in range(3)])
    assert await storage.count() == 3
    assert storage.stats["retries"] == 1
    await storage.close()


# --- crawler integration ---------------------------------------------------

SITE = {
    "/": ["/a", "/b", "/missing"],
    "/a": ["/b", "/c"],
    "/b": ["/"],
    "/c": [],
}


@pytest_asyncio.fixture
async def site():
    async def handler(request):
        if request.path not in SITE:
            raise web.HTTPNotFound()
        anchors = "".join(f'<a href="{href}">{href}</a>' for href in SITE[request.path])
        html = (f"<html><head><title>Page {request.path}, \"x\"</title>"
                f'<meta name="description" content="Описание {request.path}"></head>'
                f"<body><p>Text\nwith, commas</p>{anchors}</body></html>")
        return web.Response(text=html, content_type="text/html")

    app = web.Application()
    app.router.add_get("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    port = tcp._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}/"
    await runner.cleanup()


def make_crawler(storage) -> AsyncCrawler:
    return AsyncCrawler(max_concurrent=3, read_timeout=2, progress_interval=None, storage=storage)


@pytest.mark.asyncio
async def test_crawler_saves_every_processed_page(site, tmp_path):
    json_storage = JSONStorage(tmp_path / "results.json", batch_size=100)
    db_storage = SQLiteStorage(tmp_path / "crawler.db")
    crawler = make_crawler(MultiStorage(json_storage, db_storage))
    results = await crawler.crawl(start_urls=[site])
    # Flushed by crawl(): readable before close()
    saved = by_url(await JSONStorage(tmp_path / "results.json").load())
    await crawler.close()
    assert json_storage.closed and db_storage.closed

    assert set(saved) == set(results) and len(saved) == 4
    assert site + "missing" in crawler.failed_urls and site + "missing" not in saved
    page = saved[site]
    assert page["title"] == 'Page /, "x"'
    assert page["metadata"]["description"] == "Описание /"
    assert page["status_code"] == 200 and page["content_type"] == "text/html"
    assert sorted(page["links"]) == sorted(site + p.lstrip("/") for p in SITE["/"])
    assert isinstance(page["crawled_at"], datetime) and page["crawled_at"].tzinfo
    assert by_url(await SQLiteStorage(tmp_path / "crawler.db").load()) == saved
    assert crawler.get_stats()["storage"]["saved"] == 8


@pytest.mark.asyncio
async def test_crawl_survives_storage_errors(site, tmp_path):
    class Broken(DataStorage):
        async def save(self, data):
            raise RuntimeError("storage bug")

        async def _write_batch(self, records): ...

        async def _close(self): ...

        def iter_records(self): ...

    async with make_crawler(Broken()) as crawler:
        results = await crawler.crawl([site])
    assert len(results) == 4 and crawler.storage_errors == 4

    directory = tmp_path / "dir"
    directory.mkdir()
    storage = CSVStorage(directory, batch_size=1, max_retries=1, retry_delay=0)
    async with make_crawler(storage) as crawler:
        results = await crawler.crawl([site])
    assert len(results) == 4 and crawler.storage_errors == 0
    assert storage.stats["failed"] == 4


@pytest.mark.asyncio
async def test_fetch_result_has_content_type(site):
    async with AsyncCrawler(progress_interval=None) as crawler:
        await crawler.fetch_url(site)
    assert crawler.results[site].content_type == "text/html"

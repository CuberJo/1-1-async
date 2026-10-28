"""Day 6 — saving crawled pages: JSON, CSV and SQLite storages.

Every storage is a DataStorage: save() buffers records, a full buffer is
written as one batch, a failed write is retried with backoff and then logged
and dropped. A storage never raises into the crawl."""

import asyncio
import csv
import io
import json
import logging
import os
import sys
import textwrap
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiofiles
import aiofiles.os
import aiosqlite

from models import FetchResult

logger = logging.getLogger("crawler.storage")

# The standard record, in column order
FIELDS = ("url", "title", "text", "links", "metadata", "crawled_at", "status_code", "content_type")


def make_record(page: dict, fetch: FetchResult | None = None, crawled_at: datetime | None = None) -> dict:
    """Standard record from a parsed page (fetch_and_parse) and its fetch result."""
    return {
        "url": page["url"],
        "title": page.get("title") or "",
        "text": page.get("text") or "",
        "links": list(page.get("links") or []),
        "metadata": dict(page.get("metadata") or {}),
        "crawled_at": crawled_at or datetime.now(timezone.utc),
        "status_code": fetch.status if fetch else None,
        "content_type": fetch.content_type if fetch else None,
    }


def normalize_record(data: dict) -> dict:
    """All standard fields with their types, then any extra keys as they are.
    Raises ValueError (no url, bad date) or TypeError (wrong field types)."""
    url = data.get("url")
    if not url or not isinstance(url, str):
        raise ValueError("record has no url")
    crawled_at = data.get("crawled_at") or datetime.now(timezone.utc)
    if isinstance(crawled_at, str):
        crawled_at = datetime.fromisoformat(crawled_at)
    if not isinstance(crawled_at, datetime):
        raise TypeError(f"crawled_at must be a datetime, got {type(crawled_at).__name__}")
    status = data.get("status_code")
    record = {
        "url": url,
        "title": str(data.get("title") or ""),
        "text": str(data.get("text") or ""),
        "links": [str(link) for link in data.get("links") or []],
        "metadata": dict(data.get("metadata") or {}),
        "crawled_at": crawled_at,
        "status_code": int(status) if status not in (None, "") else None,
        "content_type": str(data.get("content_type") or "") or None,
    }
    record.update((k, v) for k, v in data.items() if k not in record)
    return record


def decode_record(stored: dict) -> dict:
    """Stored form back to a standard record: JSON strings to lists and
    dicts, ISO dates to datetime, empty CSV cells to None."""
    record = dict(stored)
    for key in ("links", "metadata"):
        if isinstance(record.get(key), str):
            record[key] = json.loads(record[key]) if record[key] else None
    return normalize_record(record)


def _json_default(obj):
    if isinstance(obj, datetime):
        return obj.isoformat()
    return str(obj)


def _dumps(obj, indent: int | None = None) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=indent, default=_json_default)


class DataStorage(ABC):
    """Base of all storages: buffering, batch writes, retries, statistics.

    - batch_size: records kept in memory before they are written together;
      flush() and close() write the rest;
    - max_retries, retry_delay: a failed batch is retried after
      retry_delay * 2**attempt seconds, then dropped with an error in the log.

    Subclasses implement _write_batch(records), _close() and iter_records().
    save() and flush() never raise: invalid records and write errors are
    logged and counted in stats["failed"].
    """

    def __init__(self, batch_size: int = 1, max_retries: int = 3, retry_delay: float = 0.1):
        if batch_size < 1:
            raise ValueError("batch_size must be >= 1")
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        self.batch_size = batch_size
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.closed = False
        self._buffer: list[dict] = []
        self._lock = asyncio.Lock()  # one batch is written at a time
        self.stats = {
            "saved": 0,  # records written
            "failed": 0,  # records rejected or lost after all retries
            "batches": 0,
            "retries": 0,
            "write_time": 0.0,  # seconds spent in writes, failed ones included
            "last_error": None,
        }

    async def save(self, data: dict) -> bool:
        """Queue one record; a full buffer is written as one batch.
        Returns False if the record was rejected (invalid or storage closed)."""
        if self.closed:
            self._fail([data], "storage is closed")
            return False
        try:
            record = normalize_record(data)
        except (ValueError, TypeError) as e:
            self._fail([data], f"invalid record: {e}")
            return False
        self._buffer.append(record)
        if len(self._buffer) >= self.batch_size:
            await self.flush()
        return True

    async def flush(self) -> None:
        """Write everything buffered."""
        async with self._lock:
            # Records saved while a batch is being written join the next one
            while self._buffer:
                batch, self._buffer = self._buffer, []
                await self._write_with_retry(batch)

    async def _write_with_retry(self, batch: list[dict]) -> None:
        for attempt in range(self.max_retries + 1):
            start = time.perf_counter()
            try:
                await self._write_batch(batch)
            except Exception as e:
                self.stats["write_time"] += time.perf_counter() - start
                error = f"{type(e).__name__}: {e}"
                if attempt == self.max_retries:
                    self._fail(batch, f"write failed after {attempt + 1} attempts: {error}")
                    return
                delay = self.retry_delay * 2**attempt
                self.stats["retries"] += 1
                logger.warning(
                    "%s: writing %d records failed (%s), attempt %d/%d, retry in %.2fs",
                    self.name, len(batch), error, attempt + 1, self.max_retries + 1, delay,
                )
                await asyncio.sleep(delay)
            else:
                self.stats["write_time"] += time.perf_counter() - start
                self.stats["saved"] += len(batch)
                self.stats["batches"] += 1
                return

    def _fail(self, records: list[dict], error: str) -> None:
        self.stats["failed"] += len(records)
        self.stats["last_error"] = error
        urls = [r.get("url") if isinstance(r, dict) else repr(r) for r in records[:3]]
        more = f" and {len(records) - 3} more" if len(records) > 3 else ""
        logger.error("%s: %d records not saved (%s): %s%s", self.name, len(records), error, urls, more)

    async def close(self) -> None:
        """Write the buffer and release the file or connection. Safe to call twice."""
        if self.closed:
            return
        try:
            await self.flush()
        finally:
            self.closed = True
            try:
                await self._close()
            except Exception as e:
                logger.error("%s: close failed: %s: %s", self.name, type(e).__name__, e)

    async def load(self) -> list[dict]:
        """Everything saved so far, as standard records (buffer flushed first)."""
        return [record async for record in self.iter_records()]

    @property
    def name(self) -> str:
        return type(self).__name__

    def get_stats(self) -> dict:
        return {**self.stats, "buffered": len(self._buffer)}

    @abstractmethod
    async def _write_batch(self, records: list[dict]) -> None:
        """Write the records or raise; a retry gets the same records again."""

    @abstractmethod
    async def _close(self) -> None: ...

    @abstractmethod
    def iter_records(self) -> AsyncIterator[dict]: ...

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


class FileStorage(DataStorage):
    """One binary handle kept open between batches. Every batch is written at
    a known end offset and the file is cut right after it, so a retried batch
    overwrites the remains of a failed attempt instead of doubling them.

    append=False replaces the file on the first write; append=True continues it.
    A storage made only to load() never touches the file.
    """

    def __init__(self, path: str | os.PathLike, append: bool = False, **kwargs):
        super().__init__(**kwargs)
        self.path = os.fspath(path)
        self.append = append
        self._file = None
        self._end = 0  # where the next batch starts

    @property
    def name(self) -> str:
        return f"{type(self).__name__}({self.path})"

    async def _open(self):
        if self._file is None:
            directory = os.path.dirname(self.path)
            if directory:
                await aiofiles.os.makedirs(directory, exist_ok=True)
            existing = self.append and await aiofiles.os.path.exists(self.path)
            f = await aiofiles.open(self.path, "r+b" if existing else "w+b")
            try:
                size = await f.seek(0, os.SEEK_END)
                await self._start(f, size)
            except BaseException:
                await f.close()
                raise
            self._file = f
        return self._file

    async def _start(self, f, size: int) -> None:
        """Called once the file is open: set self._end, check what's there."""
        self._end = size

    async def _write_at_end(self, data: bytes, tail: bytes = b"") -> None:
        """Write data at the end offset, then tail, which the next batch will
        overwrite (the closing bracket of a JSON array)."""
        f = await self._open()
        await f.seek(self._end)
        await f.write(data + tail)
        await f.truncate()
        await f.flush()
        self._end += len(data)

    async def _close(self) -> None:
        if self._file is not None:
            f, self._file = self._file, None
            await f.close()


class JSONStorage(FileStorage):
    """Records in a JSON file, written batch by batch, never held in memory.

    - lines=True: JSON Lines, one record per line; the default for .jsonl
      and .ndjson. Can be read back line by line, any size;
    - lines=False: a JSON array, the default for other names. indent=2 gives
      pretty output. The file is a valid array after every batch, not only
      after close().

    Dates are written as ISO 8601. The file is always UTF-8, as JSON requires.
    """

    def __init__(
        self,
        path: str | os.PathLike,
        indent: int | None = None,
        lines: bool | None = None,
        append: bool = False,
        batch_size: int = 20,
        **kwargs,
    ):
        super().__init__(path, append=append, batch_size=batch_size, **kwargs)
        if lines is None:
            lines = self.path.endswith((".jsonl", ".ndjson"))
        self.lines = lines
        self.indent = indent  # ignored in JSON Lines: a record must fit one line
        self._empty = True  # the array has no elements yet

    async def _start(self, f, size: int) -> None:
        if self.lines:
            self._end = size
            if size:
                await f.seek(size - 1)
                if await f.read(1) != b"\n":  # a record cut short by a crash
                    await f.write(b"\n")
                    self._end += 1
            return
        if not size:
            await f.write(b"[\n]\n")
            self._end = 1
            return
        # Appending to an array: find its closing bracket near the end
        base = max(0, size - 4096)
        await f.seek(base)
        tail = (await f.read()).rstrip()
        if not tail.endswith(b"]"):
            raise ValueError(f"{self.path} is not a JSON array")
        before = tail[:-1].rstrip()
        self._end = base + len(before)
        self._empty = before.endswith(b"[")

    async def _write_batch(self, records: list[dict]) -> None:
        await self._open()  # finds out whether an existing array is empty
        if self.lines:
            data = "".join(_dumps(r) + "\n" for r in records)
            await self._write_at_end(data.encode("utf-8"))
            return
        items = (_dumps(r, self.indent) for r in records)
        if self.indent:
            items = (textwrap.indent(item, " " * self.indent) for item in items)
        data = ("\n" if self._empty else ",\n") + ",\n".join(items)
        await self._write_at_end(data.encode("utf-8"), b"\n]\n")
        self._empty = False

    async def iter_records(self) -> AsyncIterator[dict]:
        await self.flush()
        async with aiofiles.open(self.path, encoding="utf-8") as f:
            if self.lines:
                async for line in f:
                    if line.strip():
                        yield decode_record(json.loads(line))
            else:
                for record in json.loads(await f.read()):
                    yield decode_record(record)


class CSVStorage(FileStorage):
    """Records as CSV rows.

    - Header: fieldnames if given, else the header of the file being
      appended to, else the keys of the first record. Keys missing from the
      header are dropped with a warning (once per key);
    - links and metadata are stored as JSON, dates as ISO 8601;
    - encoding: any Python codec ("utf-8-sig" for Excel, "cp1251", "utf-16"...).
      The BOM is written once, at the start of the file. Characters the
      codec can't encode follow `errors` ("replace" by default);
    - commas, quotes, line breaks and the delimiter are quoted by the csv
      module; NUL characters, which CSV readers choke on, are removed.
    """

    def __init__(
        self,
        path: str | os.PathLike,
        encoding: str = "utf-8",
        errors: str = "replace",
        delimiter: str = ",",
        fieldnames: list[str] | None = None,
        append: bool = False,
        batch_size: int = 50,
        **kwargs,
    ):
        super().__init__(path, append=append, batch_size=batch_size, **kwargs)
        self.encoding = encoding
        self.errors = errors
        self.delimiter = delimiter
        self.fieldnames = list(fieldnames) if fieldnames else None
        self._bom = "".encode(encoding)  # b"" for codecs without one; checks the name too
        self._dropped: set[str] = set()

    async def _start(self, f, size: int) -> None:
        self._end = size
        if size and self.fieldnames is None:
            await f.seek(0)
            head = (await f.read(65536)).decode(self.encoding, "replace")
            self.fieldnames = next(csv.reader(io.StringIO(head), delimiter=self.delimiter), None)

    def _encode(self, text: str) -> bytes:
        data = text.encode(self.encoding, self.errors)
        # encode() adds a BOM every time; only the start of the file gets one
        if self._end and self._bom and data.startswith(self._bom):
            data = data[len(self._bom):]
        return data

    @staticmethod
    def _cell(value) -> str:
        if value is None:
            return ""
        if isinstance(value, (list, dict)):
            value = _dumps(value)
        elif isinstance(value, datetime):
            value = value.isoformat()
        return str(value).replace("\x00", "")

    async def _write_batch(self, records: list[dict]) -> None:
        await self._open()  # reads the header of an existing file
        if self.fieldnames is None:
            self.fieldnames = list(records[0])
        header = set(self.fieldnames)
        buf = io.StringIO(newline="")
        writer = csv.DictWriter(
            buf, self.fieldnames, delimiter=self.delimiter, restval="", extrasaction="ignore"
        )
        if self._end == 0:
            writer.writeheader()
        for record in records:
            extra = record.keys() - header - self._dropped
            if extra:
                self._dropped |= extra
                logger.warning("%s: fields not in the header are dropped: %s", self.name, sorted(extra))
            writer.writerow({k: self._cell(v) for k, v in record.items()})
        await self._write_at_end(self._encode(buf.getvalue()))

    async def iter_records(self) -> AsyncIterator[dict]:
        await self.flush()
        csv.field_size_limit(min(sys.maxsize, 2**31 - 1))  # page texts are long
        # Quoted cells may span lines, so the file is parsed as a whole
        async with aiofiles.open(self.path, encoding=self.encoding, newline="") as f:
            text = await f.read()
        if text.startswith("﻿"):  # utf-8 read back without -sig
            text = text[1:]
        for row in csv.DictReader(io.StringIO(text, newline=""), delimiter=self.delimiter):
            yield decode_record(row)


SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id           INTEGER PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    domain       TEXT NOT NULL,
    title        TEXT NOT NULL DEFAULT '',
    text         TEXT NOT NULL DEFAULT '',
    links        TEXT NOT NULL DEFAULT '[]',  -- JSON array
    metadata     TEXT NOT NULL DEFAULT '{}',  -- JSON object
    crawled_at   TEXT NOT NULL,               -- ISO 8601, UTC
    status_code  INTEGER,
    content_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_pages_domain ON pages(domain);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status_code);
CREATE INDEX IF NOT EXISTS idx_pages_crawled_at ON pages(crawled_at);
CREATE INDEX IF NOT EXISTS idx_pages_title ON pages(title);
"""

COLUMNS = ("url", "domain", "title", "text", "links", "metadata", "crawled_at", "status_code", "content_type")

# A page crawled again replaces its row
UPSERT = (
    f"INSERT INTO pages ({', '.join(COLUMNS)}) VALUES ({', '.join('?' * len(COLUMNS))}) "
    "ON CONFLICT(url) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in COLUMNS[1:])
)


class SQLiteStorage(DataStorage):
    """Records in an SQLite table `pages` through aiosqlite (queries run in
    its own thread, the event loop is never blocked).

    - a batch is one executemany() in one transaction: all rows or none,
      so a retry never duplicates anything;
    - url is unique: a page crawled again updates its row;
    - indexes on domain, status_code, crawled_at and title; find() and
      summary() use them;
    - WAL journal: readers don't wait for the writer.

    Keys beyond the standard ones are not stored (logged once).
    """

    def __init__(self, path: str | os.PathLike, batch_size: int = 100, **kwargs):
        super().__init__(batch_size=batch_size, **kwargs)
        self.path = os.fspath(path)
        self._db: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()
        self._dropped: set[str] = set()

    @property
    def name(self) -> str:
        return f"SQLiteStorage({self.path})"

    async def init_db(self) -> aiosqlite.Connection:
        """Connect and create the table and indexes, once."""
        async with self._init_lock:
            if self._db is None:
                directory = os.path.dirname(self.path)
                if directory and self.path != ":memory:":
                    await aiofiles.os.makedirs(directory, exist_ok=True)
                db = await aiosqlite.connect(self.path)
                try:
                    db.row_factory = aiosqlite.Row
                    await db.execute("PRAGMA journal_mode = WAL")
                    await db.execute("PRAGMA synchronous = NORMAL")
                    await db.executescript(SCHEMA)
                    await db.commit()
                except BaseException:
                    await db.close()
                    raise
                self._db = db
        return self._db

    def _row(self, record: dict) -> tuple:
        extra = record.keys() - set(FIELDS) - self._dropped
        if extra:
            self._dropped |= extra
            logger.warning("%s: fields without a column are dropped: %s", self.name, sorted(extra))
        crawled_at = record["crawled_at"]
        if crawled_at.tzinfo:  # one zone, so the text sorts by time
            crawled_at = crawled_at.astimezone(timezone.utc)
        return (
            record["url"],
            urlparse(record["url"]).hostname or "",
            record["title"],
            record["text"],
            _dumps(record["links"]),
            _dumps(record["metadata"]),
            crawled_at.isoformat(),
            record["status_code"],
            record["content_type"],
        )

    async def _write_batch(self, records: list[dict]) -> None:
        db = await self.init_db()
        rows = [self._row(r) for r in records]
        try:
            await db.executemany(UPSERT, rows)
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                pass  # the original error matters more
            raise

    async def _close(self) -> None:
        if self._db is not None:
            db, self._db = self._db, None
            await db.close()

    @staticmethod
    def _decode_row(row) -> dict:
        record = dict(row)
        record.pop("id", None)
        record.pop("domain", None)
        return decode_record(record)

    async def iter_records(self, chunk: int = 500) -> AsyncIterator[dict]:
        await self.flush()
        db = await self.init_db()
        async with db.execute("SELECT * FROM pages ORDER BY id") as cursor:
            while rows := await cursor.fetchmany(chunk):
                for row in rows:
                    yield self._decode_row(row)

    async def get(self, url: str) -> dict | None:
        await self.flush()
        db = await self.init_db()
        async with db.execute("SELECT * FROM pages WHERE url = ?", (url,)) as cursor:
            row = await cursor.fetchone()
        return self._decode_row(row) if row else None

    async def count(self) -> int:
        await self.flush()
        db = await self.init_db()
        async with db.execute("SELECT COUNT(*) FROM pages") as cursor:
            return (await cursor.fetchone())[0]

    async def find(
        self,
        domain: str | None = None,
        status_code: int | None = None,
        title_contains: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """Pages matching all the given conditions, newest first."""
        conditions, params = [], []
        if domain is not None:
            conditions.append("domain = ?")
            params.append(domain)
        if status_code is not None:
            conditions.append("status_code = ?")
            params.append(status_code)
        if title_contains:
            conditions.append("title LIKE ? ESCAPE '\\'")
            escaped = title_contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            params.append(f"%{escaped}%")
        if since is not None:
            if since.tzinfo:
                since = since.astimezone(timezone.utc)
            conditions.append("crawled_at >= ?")
            params.append(since.isoformat())
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        await self.flush()
        db = await self.init_db()
        sql = f"SELECT * FROM pages {where} ORDER BY crawled_at DESC LIMIT ?"
        async with db.execute(sql, (*params, limit)) as cursor:
            return [self._decode_row(row) for row in await cursor.fetchall()]

    async def summary(self) -> dict:
        """Aggregates over the stored pages."""
        await self.flush()
        db = await self.init_db()

        async def rows(sql: str) -> list:
            async with db.execute(sql) as cursor:
                return await cursor.fetchall()

        total, avg_text, links, first, last = (await rows(
            "SELECT COUNT(*), AVG(LENGTH(text)), SUM(json_array_length(links)), "
            "MIN(crawled_at), MAX(crawled_at) FROM pages"
        ))[0]
        by = {}
        for column in ("domain", "status_code", "content_type"):
            by[column] = {
                row[0]: row[1]
                for row in await rows(
                    f"SELECT {column}, COUNT(*) FROM pages GROUP BY {column} ORDER BY COUNT(*) DESC"
                )
            }
        most_linked = [
            (row[0], row[1])
            for row in await rows(
                "SELECT url, json_array_length(links) AS n FROM pages ORDER BY n DESC LIMIT 5"
            )
        ]
        return {
            "pages": total,
            "avg_text_length": round(avg_text or 0),
            "links": links or 0,
            "first_crawled": first,
            "last_crawled": last,
            "by_domain": by["domain"],
            "by_status": by["status_code"],
            "by_content_type": by["content_type"],
            "most_links": most_linked,
        }


class MultiStorage(DataStorage):
    """Sends every record to several storages: one crawl, several formats.
    Each storage keeps its own buffer, retries and statistics; get_stats()
    sums them (4 storages x 10 pages = 40 saved) and lists each one."""

    def __init__(self, *storages: DataStorage):
        super().__init__()
        if not storages:
            raise ValueError("MultiStorage needs at least one storage")
        self.storages = list(storages)

    async def save(self, data: dict) -> bool:
        results = await asyncio.gather(*(s.save(data) for s in self.storages))
        return all(results)

    async def flush(self) -> None:
        await asyncio.gather(*(s.flush() for s in self.storages))

    async def close(self) -> None:
        self.closed = True
        await asyncio.gather(*(s.close() for s in self.storages))

    async def _write_batch(self, records: list[dict]) -> None:
        for record in records:
            await self.save(record)

    async def _close(self) -> None:
        await self.close()

    def iter_records(self) -> AsyncIterator[dict]:
        return self.storages[0].iter_records()

    def get_stats(self) -> dict:
        totals = {key: 0 for key in ("saved", "failed", "batches", "retries", "buffered")}
        for storage in self.storages:
            stats = storage.get_stats()
            for key in totals:
                totals[key] += stats[key]
        return {**totals, "storages": {s.name: s.get_stats() for s in self.storages}}

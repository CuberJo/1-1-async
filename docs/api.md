# API reference

All modules live in `src/`. Put it on `sys.path` before importing: run scripts from `src/`, or use `pythonpath = src` as `pytest.ini` does.

## AdvancedCrawler — `advanced_crawler.py` (also `from crawler import AdvancedCrawler`)

A subclass of `AsyncCrawler` that is driven by a `CrawlerConfig`.

| Member | Description |
|---|---|
| `AdvancedCrawler(config=None, *, storage=None, setup_logging=False, progress_stream=None)` | `config` defaults to `CrawlerConfig()`. `storage` replaces the storages from the config. `setup_logging=True` applies `config.logging`. `progress_stream` defaults to stdout. |
| `AdvancedCrawler.from_config(path, **kwargs)` | builds the crawler from a `.yaml` / `.yml` / `.json` file |
| `await crawl(start_urls=None, max_pages=None, same_domain_only=None, include_patterns=None, exclude_patterns=None, max_depth=None, sitemaps=None)` | Crawls and returns `{url: page}`. An argument left as `None` comes from the config. Sitemap pages are added after the start URLs. Raises `ValueError` when there is nothing to crawl. |
| `await run()` | `crawl()`, then writes `config.output` files; returns `get_stats()` |
| `get_stats() -> dict` | `AsyncCrawler.get_stats()` combined with `CrawlerStats.to_dict()` (see below) |
| `export_to_json(filename)` | statistics, crawler counters, failed and blocked URLs, sitemap errors and settings |
| `export_to_html_report(filename)` | a self-contained HTML report |
| `await close()` / `async with` | closes the session, the parse processes, the storages and the sitemap parser |
| `stats` | the `CrawlerStats` of the last crawl |
| `sitemaps` | the `SitemapParser`, with `errors` from the last crawl |
| `sitemap_urls`, `start_urls` | the pages taken from sitemaps / every start URL of the last crawl |
| `config` | the `CrawlerConfig` |

Everything from `AsyncCrawler` is still there: `processed_urls`, `failed_urls`, `blocked_urls`, `visited_urls`, `results`, `fetch_url`, `fetch_and_parse`, `get_error_report()`, and so on.

## AsyncCrawler — `crawler.py`

The crawler from days 1–6, see the README sections for each day. Day 7 added these constructor options:

| Option | Default | Description |
|---|---|---|
| `proxy` | `None` | proxy URL for every request of the session |
| `headers` | `None` | default headers for every request |
| `cookies` | `None` | starting cookies; the session's cookie jar keeps the ones sites set, IP hosts included |
| `keep_pages` | `True` | `False`: `processed_urls[url]` keeps only `url`, `title`, `depth`, `links_count`, `errors`, and `results[url].content` is dropped once the page is parsed and saved |
| `parse_workers` | `0` | `N > 0` parses pages in a `ProcessPoolExecutor(N)`, created on first use and shut down by `close()` |

`python src/crawler.py [options]` runs the command line (`cli.main`).

## SitemapParser — `sitemap_parser.py`

| Member | Description |
|---|---|
| `SitemapParser(session_factory=None, user_agent="AsyncCrawler/0.1", timeout=30.0, max_depth=3, max_urls=50000)` | `session_factory`: an async function that returns an `aiohttp.ClientSession` to share. Without one, the parser opens its own session. |
| `await fetch_sitemap(sitemap_url) -> list[str]` | page URLs of a sitemap, or of a sitemap index followed recursively. Keeps the order, removes duplicates, returns at most `max_urls`. |
| `await find_sitemaps(site_url) -> list[str]` | `Sitemap:` lines of the site's robots.txt, or `[origin + "/sitemap.xml"]` |
| `await discover(site_url) -> list[str]` | `fetch_sitemap` for every sitemap `find_sitemaps` returns |
| `errors: dict[str, str]` | sitemap URL → why it was skipped (HTTP error, invalid XML, nested too deep) |
| `loaded: set[str]` | sitemaps requested since `reset()`, a record only. Cycle protection is per `fetch_sitemap` call, so the same parser can fetch a sitemap again. |
| `reset()` | clears `loaded` and `errors`; `AdvancedCrawler.crawl()` calls it at the start of every crawl |
| `await close()` / `async with` | closes its own session |
| `parse_sitemap(content: bytes) -> (kind, urls)` | a module function that parses one file without the network. `kind` is `"urlset"`, `"index"` or `"text"`. gzip is detected by its magic bytes. Raises `ValueError` on invalid content. |

## CrawlerStats — `crawler_stats.py`

| Member | Description |
|---|---|
| `CrawlerStats(top_n=10, slowest_n=10)` | |
| `start()`, `stop()`, `reset()` | `start()` resets the counters and starts the clock; `record()` starts it if needed |
| `record(result: FetchResult, depth=None)` | counts one fetch |
| `record_blocked()` | counts a URL that robots.txt disallowed |
| `total_pages`, `successful`, `failed`, `blocked`, `uptime`, `pages_per_sec`, `success_rate`, `avg_response_time`, `running` | live values |
| `top_domains(n=None) -> list[dict]` | `[{"domain", "pages", "failed"}]`, largest first |
| `to_dict() -> dict` | everything below, JSON-ready |
| `export_to_json(filename, extra=None)` | `to_dict()` plus `extra`, pretty-printed; creates the folder |
| `export_to_html_report(filename, title="Crawl report", extra=None)` | the HTML report. `extra` may hold `failed_urls`, `config` (a flat dict) and `start_urls`. |

`to_dict()` keys:

| Key | Meaning |
|---|---|
| `total_pages` | pages requested (`successful + failed`); robots.txt blocks are not requests |
| `successful`, `failed`, `blocked` | counts |
| `success_rate` | `successful / total_pages`, 0..1 |
| `attempts` | HTTP requests, retries included |
| `uptime`, `started_at` | seconds since `start()`, and the UTC start time in ISO format |
| `avg_speed` | pages per second over the whole run |
| `avg_response_time` | seconds per page, all attempts included |
| `bytes_downloaded` | UTF-8 size of the successful pages |
| `status_codes` | `{"200": n, "404": n, "none": n}`; `none` means no HTTP response |
| `errors_by_type` | `{"TransientError": n, "PermanentError": n, "NetworkError": n, ...}` |
| `top_domains`, `domains_count` | see `top_domains()` |
| `depths` | `{"0": n, "1": n, ...}` |
| `content_types` | `{"text/html": n}` |
| `timeline` | pages finished in each second of the run |
| `slowest` | `[{"url", "elapsed"}]`, slowest first |

## CrawlerConfig — `config.py`

| Member | Description |
|---|---|
| `CrawlerConfig.load(path)` | reads YAML or JSON; raises `ConfigError` (a `ValueError`) with the file name and key |
| `CrawlerConfig.from_dict(data)` | builds and validates; `storage` may be a string, a dict or a list |
| `validate()` | checks ranges, regexes and log levels, and infers storage types; call it again after changing fields |
| `to_dict()` | every setting as plain data |
| `summary() -> dict[str, str]` | flat `"section.key": value` for reports, with secrets hidden |
| sections | `crawler: CrawlerSettings`, `filters: FilterSettings`, `sitemap: SitemapSettings`, `storage: list[StorageSettings]`, `output: OutputSettings`, `logging: LoggingSettings` (dataclasses; fields are listed in [configuration.md](configuration.md)) |

`advanced_crawler.build_storage(settings) -> DataStorage | None` turns `storage` settings into one storage, or a `MultiStorage` when there are several.

## ProgressMonitor — `progress.py`

| Member | Description |
|---|---|
| `ProgressMonitor(max_pages, stream=None, width=24, smoothing=0.3)` | |
| `update(stats, final=False)` | draws one line from `get_stats()`: redraws in place (`\r`) on a terminal, prints a new line in a pipe or file. Falls back to `#`/`-` when the stream can't encode `█`. |
| `render(stats, final=False) -> str` | the line without printing it |
| `snapshot(done, queued, in_progress, now=None) -> dict` | `done`, `target`, `percent`, `speed` (smoothed pages/s), `eta` (seconds or `None`) |

## Logging — `utils.py`

`configure_logging(level="INFO", console_level="WARNING", log_file=None, max_bytes=5 MiB, backup_count=3, fmt="text")` sets up a stderr handler and a `RotatingFileHandler`. Each has its own level, and the root logger's level is the lower of the two. `fmt="json"` uses `JSONFormatter`, one object per line. Calling it again replaces the handlers instead of adding more.

Logger names: `crawler` (requests), `crawler.parser`, `crawler.robots`, `crawler.retry`, `crawler.circuit`, `crawler.storage`, `crawler.sitemap`, `crawler.advanced`.

## Command line — `cli.py`

`main(argv=None) -> int` parses the options, builds the config (`config_from_args`), runs `AdvancedCrawler.run()` and prints a summary. `build_parser()` returns the `argparse` parser. Exit codes: 0 on success, 1 when no page could be fetched, 2 for invalid options or config, 130 on Ctrl+C.

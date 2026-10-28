# Configuration guide

`AdvancedCrawler` and the command line read their settings from a YAML (`.yaml`, `.yml`) or JSON (`.json`) file. [config.yaml](../config.yaml) at the project root is a commented example.

```bash
python src/crawler.py --config config.yaml                  # everything from the file
python src/crawler.py --config config.yaml --max-pages 20   # the command line wins
```

```python
crawler = AdvancedCrawler.from_config("config.yaml")
config = CrawlerConfig.load("config.json")           # or build one in code:
config = CrawlerConfig.from_dict({"start_urls": ["https://example.com/"]})
```

Every key is optional. A missing key takes the default from the tables below, and an empty file is a valid config. The file is checked strictly: a misspelled key, a wrong type or an out-of-range value stops the program with a message that names the key, for example `config.yaml: unknown option crawler.max_page` or `crawler.rate_limit must be > 0 or null, got 0`. Relative paths are resolved against the current directory, the same as on the command line.

## Top level

| Key | Type | Default | Meaning |
|---|---|---|---|
| `start_urls` | list of URLs | `[]` | where the crawl starts (depth 0) |
| `crawler` | section | | how to crawl, see below |
| `filters` | section | | which links to follow |
| `sitemap` | section | | sitemaps as a source of URLs |
| `storage` | list | `[]` | where to save pages |
| `output` | section | | statistics and report files |
| `logging` | section | | log file and console |
| `progress_interval` | seconds or `null` | `1.0` | how often the progress line updates, `null` turns it off |

The crawl needs at least one start URL or one sitemap page.

## `crawler`

| Key | Default | Meaning |
|---|---|---|
| `max_pages` | `100` | the most pages to fetch; failed ones count, robots.txt blocks don't |
| `max_depth` | `2` | link depth; start URLs and sitemap pages are 0 |
| `max_concurrent` | `10` | requests in flight at once |
| `max_per_domain` | `null` | ... to one domain; `null` uses `max_concurrent` |
| `rate_limit` | `null` | requests per second, per domain; `null` means no limit |
| `per_domain_rate` | `true` | `false`: one limit shared by all domains |
| `min_delay` | `0.0` | the shortest gap between requests to a domain, in seconds |
| `jitter` | `0.0` | adds a random 0..jitter seconds to every gap and every retry delay |
| `respect_robots` | `true` | obey robots.txt `Disallow` / `Allow` and `Crawl-delay` |
| `user_agent` | `"AsyncCrawler/0.1"` | one string, or a list that rotates; robots.txt rules are checked for the first one |
| `connect_timeout`, `read_timeout`, `total_timeout` | `5`, `10`, `30` | seconds; they grow ×1.5 on each retry |
| `max_retries` | `2` | retries of timeouts, 429, 5xx and network errors |
| `backoff_base`, `backoff_max` | `0.5`, `30` | the retry delay is `base * 2**n` seconds, capped at the max, and never shorter than `Retry-After` |
| `circuit_breaker` | `false` | stops requests to a domain that keeps failing (≥50% of the last 10), then tries again after 30 s |
| `keep_pages` | `true` | `false` keeps only a short summary of each page in memory. Use it on big crawls with a storage. |
| `parse_workers` | `0` | `N > 0`: parse pages in N processes. ~3–4× faster when parsing is the limit, costs ~1 s at start-up. |
| `proxy` | `null` | `http://user:pass@host:port`; used for every request, robots.txt and sitemaps included |
| `headers` | `{}` | extra headers for every request (`User-Agent` comes from `user_agent`) |
| `cookies` | `{}` | starting cookies; cookies the sites set are kept for the session |

The password in a proxy URL and the values of cookies never appear in reports or statistics. Only the cookie names are shown.

## `filters`

| Key | Default | Meaning |
|---|---|---|
| `same_domain_only` | `true` | follow links only on the sites of the start URLs and sitemaps (`www.` is ignored) |
| `include_patterns` | `[]` | regexes; when any are given, a link must match at least one |
| `exclude_patterns` | `[]` | regexes; a link that matches any of them is skipped |

The patterns are searched anywhere in the URL (`re.search`). In YAML, put them in single quotes: `'\?sort='`. Links to images, archives, PDFs, scripts and styles are always skipped. The start URLs bypass the filters, but sitemap pages go through them.

## `sitemap`

| Key | Default | Meaning |
|---|---|---|
| `urls` | `[]` | sitemap or sitemap-index URLs. Their pages are crawled as start URLs. |
| `discover` | `false` | also read the `Sitemap:` lines of each start site's robots.txt, or `/sitemap.xml` if there are none |
| `max_urls` | `10000` | the most URLs to take from sitemaps |

Indexes are followed up to 3 levels deep. `.xml.gz` and plain-text sitemaps work. A sitemap that fails to load is logged and skipped. Set `crawler.max_depth: 0` to crawl only the sitemap pages without following their links.

## `storage`

A list of storages, each one gets every successfully crawled page:

```yaml
storage:
  - output/pages.jsonl                       # short form: the type from the extension
  - path: output/pages.csv
    options: {encoding: utf-8-sig}           # passed to CSVStorage
  - {path: output/crawl, type: sqlite, options: {batch_size: 200}}
```

| Type | Extensions | Class and useful options |
|---|---|---|
| `json` | `.json` | `JSONStorage`, a JSON array, pretty-printed (`indent`, `append`, `batch_size`) |
| `jsonl` | `.jsonl`, `.ndjson` | `JSONStorage`, one record per line (`append`, `batch_size`) |
| `csv` | `.csv` | `CSVStorage` (`encoding`, `delimiter`, `fieldnames`, `append`, `batch_size`) |
| `sqlite` | `.db`, `.sqlite`, `.sqlite3` | `SQLiteStorage` (`batch_size`) |

On the command line, `--output FILE` replaces the whole list with one file.

## `output`

| Key | Default | Meaning |
|---|---|---|
| `stats_json` | `null` | statistics, failed URLs and settings as JSON (`--stats`) |
| `html_report` | `null` | the HTML report (`--report`) |
| `report_title` | `"Crawl report"` | the report's heading |

`AdvancedCrawler.run()` and the command line write these files after the crawl. `crawl()` alone doesn't write them; call `export_to_json()` and `export_to_html_report()` yourself.

## `logging`

| Key | Default | Meaning |
|---|---|---|
| `level` | `INFO` | log file level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `console_level` | `WARNING` | console level. `-v` sets INFO, `-vv` DEBUG, `-q` ERROR. |
| `file` | `logs/crawler.log` | `null` means no file |
| `max_bytes` | `5242880` | rotate at this size... |
| `backup_count` | `3` | ...and keep this many old files (`crawler.log.1` ...) |
| `format` | `text` | `json`: one JSON object per line (`time`, `level`, `logger`, `message`, `exception`) |

The command line always sets up logging. In code, pass `AdvancedCrawler(config, setup_logging=True)`, or configure `logging` yourself.

## Command line ↔ config

| Option | Config key |
|---|---|
| `--urls URL...` | `start_urls` |
| `--sitemap URL...` | `sitemap.urls` |
| `--discover-sitemaps` / `--no-discover-sitemaps` | `sitemap.discover` |
| `--max-pages N` | `crawler.max_pages` |
| `--max-depth N` | `crawler.max_depth` |
| `--same-domain` / `--no-same-domain` | `filters.same_domain_only` |
| `--include RE...`, `--exclude RE...` | `filters.include_patterns`, `filters.exclude_patterns` |
| `--respect-robots` / `--no-respect-robots` | `crawler.respect_robots` |
| `--rate-limit RPS` | `crawler.rate_limit` |
| `--max-concurrent N` | `crawler.max_concurrent` |
| `--parse-workers N` | `crawler.parse_workers` |
| `--max-retries N` | `crawler.max_retries` |
| `--timeout SEC` | `crawler.total_timeout` |
| `--user-agent UA` | `crawler.user_agent` |
| `--proxy URL` | `crawler.proxy` |
| `--output FILE` | `storage` (replaced by one file) |
| `--stats FILE`, `--report FILE` | `output.stats_json`, `output.html_report` |
| `--log-file FILE`, `--log-level LEVEL` | `logging.file`, `logging.level` |
| `-v`, `-vv`, `-q` | `logging.console_level`; `-q` also turns off the progress line |

## Recipes

A polite crawl of one site, with the results in SQLite:

```yaml
start_urls: [https://example.com/]
crawler: {max_pages: 500, rate_limit: 1, max_per_domain: 2, respect_robots: true}
storage: [output/example.db]
output: {html_report: output/example.html}
```

Only the pages listed in a sitemap, fast and with flat memory:

```yaml
sitemap: {urls: [https://example.com/sitemap_index.xml]}
crawler: {max_pages: 5000, max_depth: 0, max_concurrent: 30, rate_limit: 10,
          keep_pages: false, parse_workers: 4}
storage: [output/pages.jsonl]
```

Blog posts only, through a proxy, with a session cookie:

```yaml
start_urls: [https://example.com/blog/]
filters:
  include_patterns: ['/blog/']
  exclude_patterns: ['/tag/', '\?page=']
crawler:
  proxy: http://user:secret@proxy.local:3128
  cookies: {sessionid: abc123}
  headers: {Accept-Language: ru}
```

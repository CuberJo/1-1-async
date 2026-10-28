"""Day 7 — command line for AdvancedCrawler.

    python src/crawler.py --urls https://example.com --max-pages 100 --output results.json
    python src/crawler.py --config config.yaml
    python src/crawler.py --config config.yaml --max-pages 20 --report output/report.html

Options given on the command line override the config file; everything else
comes from the file, or from the defaults without one.
"""

import argparse
import asyncio
import sys

from config import LOG_LEVELS, ConfigError, CrawlerConfig, StorageSettings


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError(f"must be >= 0, got {value}")
    return value


def positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be > 0, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crawler.py",
        description="Asynchronous web crawler: robots.txt, rate limits, retries, sitemaps, "
                    "JSON/CSV/SQLite output, statistics and HTML reports.",
        epilog="Example: python src/crawler.py --urls https://quotes.toscrape.com/ "
               "--max-pages 50 --output output/results.json --report output/report.html",
    )
    what = parser.add_argument_group("what to crawl")
    what.add_argument("--urls", nargs="+", metavar="URL", help="start URLs")
    what.add_argument("--sitemap", nargs="+", metavar="URL", help="sitemap.xml URLs whose pages are crawled too")
    what.add_argument("--discover-sitemaps", action=argparse.BooleanOptionalAction, default=None,
                      help="also read the sitemaps the start sites list in robots.txt")
    what.add_argument("--max-pages", type=positive_int, help="pages to fetch at most (default 100)")
    what.add_argument("--max-depth", type=non_negative_int, help="link depth, start URLs are 0 (default 2)")
    what.add_argument("--same-domain", action=argparse.BooleanOptionalAction, default=None,
                      help="stay on the start sites (default: yes)")
    what.add_argument("--include", nargs="+", metavar="REGEX", help="crawl only links matching one of these")
    what.add_argument("--exclude", nargs="+", metavar="REGEX", help="skip links matching any of these")

    how = parser.add_argument_group("how to crawl")
    how.add_argument("--config", metavar="FILE", help="YAML or JSON config file")
    how.add_argument("--respect-robots", action=argparse.BooleanOptionalAction, default=None,
                     help="obey robots.txt (default: yes)")
    how.add_argument("--rate-limit", type=positive_float, metavar="RPS",
                     help="requests per second per domain (default: unlimited)")
    how.add_argument("--max-concurrent", type=positive_int, help="requests in flight at once (default 10)")
    how.add_argument("--parse-workers", type=non_negative_int, metavar="N",
                     help="parse pages in N processes, faster on big crawls (default 0: a thread)")
    how.add_argument("--max-retries", type=non_negative_int, help="retries of temporary errors (default 2)")
    how.add_argument("--timeout", type=positive_float, metavar="SEC", help="total timeout of one request")
    how.add_argument("--user-agent", help="User-Agent header")
    how.add_argument("--proxy", metavar="URL", help="proxy for all requests, http://user:pass@host:port")

    out = parser.add_argument_group("output")
    out.add_argument("--output", "-o", metavar="FILE",
                     help="save pages here; the format follows the extension: .json .jsonl .csv .db")
    out.add_argument("--stats", metavar="FILE", help="save statistics as JSON")
    out.add_argument("--report", metavar="FILE", help="save an HTML report with charts")
    out.add_argument("--log-file", metavar="FILE", help="log file (default logs/crawler.log)")
    out.add_argument("--log-level", type=str.upper, choices=LOG_LEVELS, help="log file level (default INFO)")
    out.add_argument("-v", "--verbose", action="count", default=0,
                     help="more console logging: -v INFO, -vv DEBUG (default WARNING)")
    out.add_argument("-q", "--quiet", action="store_true", help="no progress line, console errors only")
    return parser


def config_from_args(args: argparse.Namespace) -> CrawlerConfig:
    """The config file (or defaults) with the command line options on top."""
    config = CrawlerConfig.load(args.config) if args.config else CrawlerConfig()
    c, f = config.crawler, config.filters
    overrides = [
        (c, "max_pages", args.max_pages),
        (c, "max_depth", args.max_depth),
        (c, "respect_robots", args.respect_robots),
        (c, "rate_limit", args.rate_limit),
        (c, "max_concurrent", args.max_concurrent),
        (c, "max_retries", args.max_retries),
        (c, "parse_workers", args.parse_workers),
        (c, "total_timeout", args.timeout),
        (c, "user_agent", args.user_agent),
        (c, "proxy", args.proxy),
        (f, "same_domain_only", args.same_domain),
        (f, "include_patterns", args.include),
        (f, "exclude_patterns", args.exclude),
        (config.sitemap, "discover", args.discover_sitemaps),
        (config.output, "stats_json", args.stats),
        (config.output, "html_report", args.report),
        (config.logging, "file", args.log_file),
        (config.logging, "level", args.log_level),
    ]
    for section, name, value in overrides:
        if value is not None:
            setattr(section, name, value)
    if args.urls:
        config.start_urls = args.urls
    if args.sitemap:
        config.sitemap.urls = args.sitemap
    if args.output:
        config.storage = [StorageSettings(path=args.output)]
    if args.verbose:
        config.logging.console_level = "DEBUG" if args.verbose > 1 else "INFO"
    if args.quiet:
        config.logging.console_level = "ERROR"
        config.progress_interval = None
    config.validate()
    if not config.start_urls and not config.sitemap.urls:
        raise ConfigError("nothing to crawl: give --urls, --sitemap or start_urls in the config")
    return config


def print_summary(stats: dict, config: CrawlerConfig) -> None:
    print()
    print(f"Pages processed: {stats['total_pages']}  (successful {stats['successful']}, "
          f"failed {stats['failed']}, blocked by robots.txt {stats['blocked']})")
    print(f"Time: {stats['uptime']:.1f}s, {stats['avg_speed']:.2f} pages/s, "
          f"avg response {stats['avg_response_time'] * 1000:.0f} ms")
    if stats["status_codes"]:
        print("Status codes: " + ", ".join(f"{k}: {v}" for k, v in stats["status_codes"].items()))
    if stats["top_domains"]:
        print("Top domains: " + ", ".join(f"{d['domain']} ({d['pages']})" for d in stats["top_domains"][:5]))
    if stats["errors_by_type"]:
        print("Errors: " + ", ".join(f"{k}: {v}" for k, v in stats["errors_by_type"].items()))
    files = [s.path for s in config.storage] + [p for p in (config.output.stats_json, config.output.html_report) if p]
    if stats.get("storage"):
        print(f"Saved pages: {stats['storage']['saved']}")
    if files:
        print("Files: " + ", ".join(files))
    if config.logging.file:
        print(f"Log: {config.logging.file}")


async def run(config: CrawlerConfig) -> dict:
    from advanced_crawler import AdvancedCrawler

    crawler = AdvancedCrawler(config, setup_logging=True)
    try:
        return await crawler.run()
    finally:
        await crawler.close()


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = config_from_args(args)
    except (ConfigError, OSError) as e:
        parser.error(str(e))  # exits with code 2
    try:
        stats = asyncio.run(run(config))
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        return 130
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    print_summary(stats, config)
    # Exit code 1 when nothing at all could be fetched
    return 0 if stats["successful"] or not stats["total_pages"] else 1


if __name__ == "__main__":
    sys.exit(main())

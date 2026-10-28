"""Configuration files and the command line."""

import json
import os

import pytest

from cli import build_parser, config_from_args
from config import ConfigError, CrawlerConfig

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_defaults():
    config = CrawlerConfig.from_dict({})
    assert config.crawler.max_pages == 100 and config.crawler.respect_robots is True
    assert config.filters.same_domain_only and config.storage == []
    assert config.logging.file == "logs/crawler.log"


def test_load_yaml(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text(
        "start_urls: [https://a.org/]\n"
        "crawler:\n  max_pages: 5\n  rate_limit: 2\n  user_agent: [Bot/1, Bot/2]\n"
        "  headers: {X-Test: yes-header}\n"
        "filters:\n  exclude_patterns: ['/login', '\\?sort=']\n"
        "storage:\n  - out/p.jsonl\n  - {path: out/p.csv, options: {encoding: utf-8-sig}}\n"
        "  - {path: out/db, type: sqlite}\n"
        "logging: {level: debug}\n"
        "progress_interval: null\n",
        encoding="utf-8",
    )
    config = CrawlerConfig.load(path)
    assert config.start_urls == ["https://a.org/"]
    assert config.crawler.max_pages == 5 and config.crawler.rate_limit == 2.0
    assert isinstance(config.crawler.rate_limit, float)
    assert config.crawler.user_agent == ["Bot/1", "Bot/2"]
    assert config.crawler.headers == {"X-Test": "yes-header"}
    assert config.filters.exclude_patterns == ["/login", "\\?sort="]
    assert [(s.type, s.path) for s in config.storage] == [
        ("jsonl", "out/p.jsonl"), ("csv", "out/p.csv"), ("sqlite", "out/db")]
    assert config.storage[1].options == {"encoding": "utf-8-sig"}
    assert config.logging.level == "DEBUG" and config.progress_interval is None


def test_load_json_and_storage_shortcut(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"sitemap": {"urls": ["https://a.org/sitemap.xml"]}, "storage": "x.db"}))
    config = CrawlerConfig.load(path)
    assert config.sitemap.urls == ["https://a.org/sitemap.xml"]
    assert [(s.type, s.path) for s in config.storage] == [("sqlite", "x.db")]


def test_repo_config_is_valid():
    config = CrawlerConfig.load(os.path.join(ROOT, "config.yaml"))
    assert config.start_urls and config.output.html_report


@pytest.mark.parametrize("data, message", [
    ({"crawler": {"max_page": 5}}, "unknown option crawler.max_page"),
    ({"startt_urls": []}, "unknown option startt_urls"),
    ({"crawler": {"max_pages": "5"}}, "crawler.max_pages must be an integer"),
    ({"crawler": {"max_pages": True}}, "crawler.max_pages must be an integer"),
    ({"crawler": {"respect_robots": "yes"}}, "crawler.respect_robots must be true or false"),
    ({"crawler": {"max_pages": 0}}, "crawler.max_pages must be >= 1"),
    ({"crawler": {"rate_limit": 0}}, "crawler.rate_limit must be > 0"),
    ({"crawler": {"user_agent": 5}}, "crawler.user_agent has the wrong type"),
    ({"start_urls": "https://a.org"}, "start_urls must be a list"),
    ({"start_urls": [1]}, "start_urls[0] must be a string"),
    ({"filters": {"exclude_patterns": ["("]}}, "invalid regex"),
    ({"storage": [{"path": "out.txt"}]}, "storage[0].type must be one of"),
    ({"storage": [{"type": "json"}]}, "storage[0].path is required"),
    ({"logging": {"level": "LOUD"}}, "logging.level must be one of"),
    ({"crawler": []}, "crawler must be a mapping"),
])
def test_invalid_configs(data, message):
    with pytest.raises(ConfigError, match=None) as info:
        CrawlerConfig.from_dict(data)
    assert message in str(info.value)


def test_invalid_files(tmp_path):
    bad_yaml = tmp_path / "bad.yaml"
    bad_yaml.write_text("crawler: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        CrawlerConfig.load(bad_yaml)
    wrong_ext = tmp_path / "c.toml"
    wrong_ext.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown config format"):
        CrawlerConfig.load(wrong_ext)
    not_a_map = tmp_path / "list.yaml"
    not_a_map.write_text("- a\n- b\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        CrawlerConfig.load(not_a_map)
    with pytest.raises(ConfigError, match=r"c\.json: unknown option nope"):
        (tmp_path / "c.json").write_text('{"nope": 1}')
        CrawlerConfig.load(tmp_path / "c.json")


def test_summary_hides_secrets():
    config = CrawlerConfig.from_dict({
        "crawler": {"proxy": "http://user:secret@proxy.local:3128", "cookies": {"session": "s3cr3t"}},
        "storage": ["a.jsonl"],
    })
    summary = config.summary()
    assert summary["crawler.proxy"] == "http://user:***@proxy.local:3128"
    assert summary["crawler.cookies"] == "session"
    assert "secret" not in json.dumps(summary) and "s3cr3t" not in json.dumps(summary)
    assert summary["storage"] == "jsonl: a.jsonl"


# --- command line ------------------------------------------------------------


def parse(*argv: str) -> CrawlerConfig:
    return config_from_args(build_parser().parse_args(list(argv)))


def test_cli_example_from_the_task():
    config = parse("--urls", "https://example.com", "--max-pages", "100", "--output", "results.json")
    assert config.start_urls == ["https://example.com"]
    assert config.crawler.max_pages == 100
    assert [(s.type, s.path) for s in config.storage] == [("json", "results.json")]


def test_cli_overrides_config(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("start_urls: [https://a.org/]\ncrawler: {max_pages: 5, max_depth: 3, rate_limit: 1}\n"
                    "storage: [a.jsonl]\n", encoding="utf-8")
    config = parse("--config", str(path), "--max-depth", "0", "--no-respect-robots",
                   "--rate-limit", "2.5", "--exclude", "/x", "/y", "--report", "r.html", "-v")
    assert config.start_urls == ["https://a.org/"]  # from the file
    assert config.crawler.max_pages == 5  # from the file
    assert config.crawler.max_depth == 0 and config.crawler.respect_robots is False
    assert config.crawler.rate_limit == 2.5
    assert config.filters.exclude_patterns == ["/x", "/y"]
    assert config.storage[0].path == "a.jsonl"  # no --output: the file's storage stays
    assert config.output.html_report == "r.html"
    assert config.logging.console_level == "INFO"


def test_cli_quiet_and_sitemap_only():
    config = parse("--sitemap", "https://a.org/sitemap.xml", "-q", "--respect-robots")
    assert config.start_urls == [] and config.sitemap.urls == ["https://a.org/sitemap.xml"]
    assert config.progress_interval is None and config.logging.console_level == "ERROR"


@pytest.mark.parametrize("argv", [
    ["--max-pages", "0", "--urls", "https://a.org"],
    ["--max-depth", "-1", "--urls", "https://a.org"],
    ["--rate-limit", "0", "--urls", "https://a.org"],
    ["--log-level", "loud", "--urls", "https://a.org"],
])
def test_cli_rejects_bad_values(argv, capsys):
    with pytest.raises(SystemExit) as info:
        build_parser().parse_args(argv)
    assert info.value.code == 2


def test_cli_needs_something_to_crawl():
    with pytest.raises(ConfigError, match="nothing to crawl"):
        parse("--max-pages", "5")
    with pytest.raises(ConfigError, match="storage\\[0\\].type"):
        parse("--urls", "https://a.org", "--output", "results.txt")

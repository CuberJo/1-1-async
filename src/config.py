"""Day 7 — crawler configuration: YAML or JSON file -> typed, validated settings."""

import json
import os
import re
import types
import typing
from dataclasses import MISSING, asdict, dataclass, field, fields, is_dataclass
from typing import Any

DEFAULT_USER_AGENT = "AsyncCrawler/0.1"
STORAGE_TYPES = ("json", "jsonl", "csv", "sqlite")
STORAGE_EXTENSIONS = {
    ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl", ".csv": "csv",
    ".db": "sqlite", ".sqlite": "sqlite", ".sqlite3": "sqlite",
}
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class ConfigError(ValueError):
    """Invalid configuration; the message names the offending key."""


@dataclass
class CrawlerSettings:
    max_pages: int = 100
    max_depth: int = 2
    max_concurrent: int = 10
    max_per_domain: int | None = None
    rate_limit: float | None = None  # requests per second (per domain), None = unlimited
    per_domain_rate: bool = True
    min_delay: float = 0.0
    jitter: float = 0.0
    respect_robots: bool = True
    user_agent: str | list[str] = DEFAULT_USER_AGENT
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    total_timeout: float = 30.0
    max_retries: int = 2
    backoff_base: float = 0.5
    backoff_max: float = 30.0
    circuit_breaker: bool = False
    keep_pages: bool = True  # keep parsed pages in memory (crawl() result)
    parse_workers: int = 0  # > 0: parse in that many processes (CPU-bound crawls)
    proxy: str | None = None  # "http://user:pass@host:port"
    headers: dict[str, str] = field(default_factory=dict)
    cookies: dict[str, str] = field(default_factory=dict)


@dataclass
class FilterSettings:
    same_domain_only: bool = True
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)


@dataclass
class SitemapSettings:
    urls: list[str] = field(default_factory=list)  # sitemap.xml URLs to read
    discover: bool = False  # also look up the sitemaps of every start URL's site
    max_urls: int = 10_000


@dataclass
class StorageSettings:
    path: str
    type: str | None = None  # json | jsonl | csv | sqlite, by default from the extension
    options: dict[str, Any] = field(default_factory=dict)  # passed to the storage class


@dataclass
class OutputSettings:
    stats_json: str | None = None
    html_report: str | None = None
    report_title: str = "Crawl report"


@dataclass
class LoggingSettings:
    level: str = "INFO"  # for the log file
    console_level: str = "WARNING"
    file: str | None = "logs/crawler.log"
    max_bytes: int = 5 * 1024 * 1024
    backup_count: int = 3
    format: str = "text"  # text | json (one JSON object per line)


@dataclass
class CrawlerConfig:
    start_urls: list[str] = field(default_factory=list)
    crawler: CrawlerSettings = field(default_factory=CrawlerSettings)
    filters: FilterSettings = field(default_factory=FilterSettings)
    sitemap: SitemapSettings = field(default_factory=SitemapSettings)
    storage: list[StorageSettings] = field(default_factory=list)
    output: OutputSettings = field(default_factory=OutputSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    progress_interval: float | None = 1.0  # seconds between progress lines, None = off

    @classmethod
    def from_dict(cls, data: dict) -> "CrawlerConfig":
        data = dict(data or {})
        # Shortcuts: "storage: out.jsonl" or one storage as a dict
        storage = data.get("storage")
        if isinstance(storage, (str, dict)):
            storage = [storage]
        if isinstance(storage, list):
            data["storage"] = [{"path": s} if isinstance(s, str) else s for s in storage]
        config = _build(cls, data, "")
        config.validate()
        return config

    @classmethod
    def load(cls, path: str | os.PathLike) -> "CrawlerConfig":
        """Read a .yaml / .yml or .json file. Relative paths inside stay
        relative to the current directory, as on the command line."""
        path = os.fspath(path)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        if path.lower().endswith((".yaml", ".yml")):
            import yaml  # only needed for YAML configs

            try:
                data = yaml.safe_load(text)
            except yaml.YAMLError as e:
                raise ConfigError(f"{path}: invalid YAML: {e}") from None
        elif path.lower().endswith(".json"):
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                raise ConfigError(f"{path}: invalid JSON: {e}") from None
        else:
            raise ConfigError(f"{path}: unknown config format, use .yaml, .yml or .json")
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: the top level must be a mapping")
        try:
            return cls.from_dict(data)
        except ConfigError as e:
            raise ConfigError(f"{path}: {e}") from None

    def validate(self) -> None:
        c = self.crawler
        positive = {"crawler.max_pages": c.max_pages, "crawler.max_concurrent": c.max_concurrent,
                    "sitemap.max_urls": self.sitemap.max_urls}
        for key, value in positive.items():
            if value < 1:
                raise ConfigError(f"{key} must be >= 1, got {value}")
        non_negative = {"crawler.max_depth": c.max_depth, "crawler.max_retries": c.max_retries,
                        "crawler.min_delay": c.min_delay, "crawler.jitter": c.jitter,
                        "crawler.parse_workers": c.parse_workers}
        for key, value in non_negative.items():
            if value < 0:
                raise ConfigError(f"{key} must be >= 0, got {value}")
        if c.max_per_domain is not None and c.max_per_domain < 1:
            raise ConfigError(f"crawler.max_per_domain must be >= 1, got {c.max_per_domain}")
        if c.rate_limit is not None and c.rate_limit <= 0:
            raise ConfigError(f"crawler.rate_limit must be > 0 or null, got {c.rate_limit}")
        if self.progress_interval is not None and self.progress_interval <= 0:
            raise ConfigError("progress_interval must be > 0 or null")
        for i, s in enumerate(self.storage):
            s.type = s.type or STORAGE_EXTENSIONS.get(os.path.splitext(s.path)[1].lower())
            if s.type not in STORAGE_TYPES:
                raise ConfigError(
                    f"storage[{i}].type must be one of {', '.join(STORAGE_TYPES)} "
                    f"(or a path ending in {', '.join(STORAGE_EXTENSIONS)}), got {s.type!r} for {s.path!r}"
                )
        log = self.logging
        for key in ("level", "console_level"):
            value = getattr(log, key).upper()
            if value not in LOG_LEVELS:
                raise ConfigError(f"logging.{key} must be one of {', '.join(LOG_LEVELS)}, got {value!r}")
            setattr(log, key, value)
        if log.format not in ("text", "json"):
            raise ConfigError(f"logging.format must be text or json, got {log.format!r}")
        for key in ("include_patterns", "exclude_patterns"):
            for pattern in getattr(self.filters, key):
                try:
                    re.compile(pattern)
                except re.error as e:
                    raise ConfigError(f"filters.{key}: invalid regex {pattern!r}: {e}") from None

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> dict[str, str]:
        """Flat "section.key": value view of the settings worth showing in a report."""
        flat = {}
        for section in ("crawler", "filters", "sitemap"):
            for key, value in asdict(getattr(self, section)).items():
                if key in ("headers", "cookies"):
                    value = ", ".join(value) or "-"  # names only, values may be secret
                elif key == "proxy" and value:
                    value = _hide_password(value)
                flat[f"{section}.{key}"] = "-" if value in (None, [], "") else str(value)
        flat["storage"] = ", ".join(f"{s.type}: {s.path}" for s in self.storage) or "-"
        return flat


def _hide_password(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    if parts.password is None:
        return url
    netloc = f"{parts.username}:***@{parts.hostname}" + (f":{parts.port}" if parts.port else "")
    return urlunsplit(parts._replace(netloc=netloc))


def _build(cls, data: Any, where: str):
    """data (a dict from YAML/JSON) -> an instance of the dataclass cls.
    Unknown keys and wrong types raise ConfigError naming the key."""
    if not isinstance(data, dict):
        raise ConfigError(f"{where or 'config'} must be a mapping, got {type(data).__name__}")
    hints = typing.get_type_hints(cls)
    known = {f.name: f for f in fields(cls)}
    unknown = sorted(set(data) - set(known))
    if unknown:
        prefix = f"{where}." if where else ""
        raise ConfigError(f"unknown option {prefix}{unknown[0]} (known: {', '.join(known)})")
    kwargs = {}
    for name, f in known.items():
        key = f"{where}.{name}" if where else name
        if name in data:
            kwargs[name] = _convert(data[name], hints[name], key)
        elif f.default is MISSING and f.default_factory is MISSING:
            raise ConfigError(f"{key} is required")
    return cls(**kwargs)


def _convert(value: Any, tp, key: str):
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        options = typing.get_args(tp)
        if value is None and type(None) in options:
            return None
        errors = []
        for option in options:
            if option is type(None):
                continue
            try:
                return _convert(value, option, key)
            except ConfigError as e:
                errors.append(str(e))
        if len(errors) == 1:
            raise ConfigError(errors[0])
        raise ConfigError(f"{key} has the wrong type: {type(value).__name__} ({value!r})")
    if is_dataclass(tp):
        return _build(tp, value, key)
    if origin is list:
        if not isinstance(value, list):
            raise ConfigError(f"{key} must be a list, got {type(value).__name__}")
        (item,) = typing.get_args(tp)
        return [_convert(v, item, f"{key}[{i}]") for i, v in enumerate(value)]
    if origin is dict:
        if not isinstance(value, dict):
            raise ConfigError(f"{key} must be a mapping, got {type(value).__name__}")
        _, item = typing.get_args(tp)
        return {str(k): _convert(v, item, f"{key}.{k}") for k, v in value.items()}
    if tp is Any:
        return value
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{key} must be true or false, got {value!r}")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"{key} must be a number, got {value!r}")
        return float(value)
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{key} must be an integer, got {value!r}")
        return value
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{key} must be a string, got {value!r}")
        return value
    raise ConfigError(f"{key}: unsupported type {tp}")  # a bug in the dataclasses above

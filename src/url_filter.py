"""Day 3 — URL canonicalization and crawl filters."""

import re
from urllib.parse import urlparse, urlunparse

DEFAULT_PORTS = {"http": 80, "https": 443}
# Links to files that are not HTML pages
SKIP_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp",
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".exe", ".dmg", ".iso",
    ".mp3", ".mp4", ".avi", ".mov", ".webm", ".wav",
    ".css", ".js", ".json", ".xml", ".woff", ".woff2", ".ttf",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)


def canonicalize(url: str) -> str:
    """One spelling per page: lowercase scheme and host, no default port,
    no fragment, "/" instead of an empty path.

    "HTTPS://Example.com:443#top" and "https://example.com/" give the same result.
    """
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if parsed.port and parsed.port != DEFAULT_PORTS.get(scheme):
        host = f"{host}:{parsed.port}"
    return urlunparse((scheme, host, parsed.path or "/", parsed.params, parsed.query, ""))


def domain_of(url: str) -> str:
    """Host without "www.": www.example.com and example.com are the same site."""
    host = (urlparse(url).hostname or "").lower()
    return host.removeprefix("www.")


class URLFilter:
    """Decides whether a discovered link should be crawled.

    - same_domain_only: only hosts of the start URLs (see allow_domain);
    - exclude_patterns: regexes, a match anywhere in the URL rejects it;
    - include_patterns: regexes, if given the URL must match at least one;
    - links to binary files (images, archives, PDF...) are always rejected.
    """

    def __init__(
        self,
        same_domain_only: bool = False,
        include_patterns: list[str] | None = None,
        exclude_patterns: list[str] | None = None,
    ):
        self.same_domain_only = same_domain_only
        self.include = [re.compile(p) for p in include_patterns or []]
        self.exclude = [re.compile(p) for p in exclude_patterns or []]
        self.allowed_domains: set[str] = set()

    def allow_domain(self, url: str) -> None:
        self.allowed_domains.add(domain_of(url))

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return False
        if parsed.path.lower().endswith(SKIP_EXTENSIONS):
            return False
        if self.same_domain_only and domain_of(url) not in self.allowed_domains:
            return False
        if any(p.search(url) for p in self.exclude):
            return False
        if self.include and not any(p.search(url) for p in self.include):
            return False
        return True

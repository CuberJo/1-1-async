"""Crawler data models."""

from dataclasses import dataclass


@dataclass
class FetchResult:
    """Result of fetching a single URL."""

    url: str
    content: str | None = None
    status: int | None = None
    error: str | None = None
    error_type: str | None = None  # TransientError, PermanentError, NetworkError...
    elapsed: float = 0.0
    final_url: str | None = None  # address after redirects
    attempts: int = 0  # requests made, retries included
    blocked: bool = False  # disallowed by robots.txt, never requested
    retryable: bool = False  # the error is temporary (timeout, 429, 5xx...)
    retry_after: float | None = None  # seconds, from the Retry-After header
    content_type: str | None = None  # "text/html", without the charset

    @property
    def ok(self) -> bool:
        return self.error is None

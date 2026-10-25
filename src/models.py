"""Crawler data models."""

from dataclasses import dataclass


@dataclass
class FetchResult:
    """Result of fetching a single URL."""

    url: str
    content: str | None = None
    status: int | None = None
    error: str | None = None
    elapsed: float = 0.0
    final_url: str | None = None  # address after redirects

    @property
    def ok(self) -> bool:
        return self.error is None

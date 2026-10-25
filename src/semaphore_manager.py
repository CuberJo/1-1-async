"""Day 3 — global and per-domain concurrency limits."""

import asyncio
from collections import Counter
from contextlib import asynccontextmanager

from url_filter import domain_of


class SemaphoreManager:
    """Caps concurrent requests in total and per domain, tracks active tasks.

    Usage:
        async with manager.acquire(url):
            ...  # at most max_concurrent requests overall
                 # and max_per_domain to the domain of url
    """

    def __init__(self, max_concurrent: int = 10, max_per_domain: int | None = None):
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be >= 1")
        if max_per_domain is not None and max_per_domain < 1:
            raise ValueError("max_per_domain must be >= 1")
        self.max_concurrent = max_concurrent
        self.max_per_domain = max_per_domain or max_concurrent
        self._global = asyncio.Semaphore(max_concurrent)
        self._domains: dict[str, asyncio.Semaphore] = {}
        self.active: Counter[str] = Counter()  # domain -> requests in flight
        self.peak_active = 0
        self.peak_per_domain: Counter[str] = Counter()
        self.total = 0

    def _domain_semaphore(self, domain: str) -> asyncio.Semaphore:
        if domain not in self._domains:
            self._domains[domain] = asyncio.Semaphore(self.max_per_domain)
        return self._domains[domain]

    @asynccontextmanager
    async def acquire(self, url: str):
        domain = domain_of(url)
        # Domain slot first: a task waiting for a busy domain must not hold
        # a global slot that a request to another domain could use
        async with self._domain_semaphore(domain), self._global:
            self.active[domain] += 1
            self.total += 1
            self.peak_active = max(self.peak_active, self.active_count)
            self.peak_per_domain[domain] = max(self.peak_per_domain[domain], self.active[domain])
            try:
                yield
            finally:
                self.active[domain] -= 1
                if not self.active[domain]:
                    del self.active[domain]

    @property
    def active_count(self) -> int:
        return sum(self.active.values())

    def get_stats(self) -> dict:
        return {
            "active": self.active_count,
            "active_per_domain": dict(self.active),
            "peak_active": self.peak_active,
            "peak_per_domain": dict(self.peak_per_domain),
            "total_requests": self.total,
        }

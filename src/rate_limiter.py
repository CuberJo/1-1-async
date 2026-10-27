"""Day 4 — request rate limiting: requests per second, minimum delay, jitter,
per-domain Crawl-delay, exponential backoff and rate statistics."""

import asyncio
import random
import time
from collections import deque

GLOBAL_KEY = "*"  # slot shared by all domains when per_domain=False


def backoff_delay(
    attempt: int,
    base: float = 0.5,
    max_delay: float = 30.0,
    jitter: float = 0.0,
    retry_after: float | None = None,
    factor: float = 2.0,
) -> float:
    """Delay before retry number attempt + 1: base * factor**attempt plus up
    to jitter seconds, at least the server's Retry-After, at most max_delay."""
    delay = base * factor**attempt
    if jitter:
        delay += random.uniform(0, jitter)
    if retry_after:
        delay = max(delay, retry_after)
    return min(delay, max_delay)


class RateLimiter:
    """Spaces requests out in time.

    Every request reserves the earliest free start time ("slot") and sleeps
    until it, so concurrent callers are served in order, one interval apart:

    - interval = max(1 / requests_per_second, min_delay) plus a random
      0..jitter seconds, so requests don't arrive on a perfect beat;
    - per_domain=True: every domain has its own slots; False: one shared
      sequence for all requests;
    - set_domain_delay() (robots.txt Crawl-delay) adds a per-domain minimum
      interval on top, in both modes.

    Usage:
        await limiter.acquire("example.com")  # returns the seconds waited
    """

    def __init__(
        self,
        requests_per_second: float | None = 1.0,
        per_domain: bool = True,
        min_delay: float = 0.0,
        jitter: float = 0.0,
        window: float = 5.0,
    ):
        if requests_per_second is not None and requests_per_second <= 0:
            raise ValueError("requests_per_second must be > 0 or None")
        if min_delay < 0 or jitter < 0:
            raise ValueError("min_delay and jitter must be >= 0")
        self.requests_per_second = requests_per_second
        self.per_domain = per_domain
        self.min_delay = min_delay
        self.jitter = jitter
        self.interval = max(1 / requests_per_second if requests_per_second else 0.0, min_delay)
        self.domain_delays: dict[str, float] = {}  # domain -> Crawl-delay
        self._next: dict[str, float] = {}  # key -> earliest start of the next request
        self.window = window  # seconds of history behind current_rps
        self.reset_stats()

    def reset_stats(self) -> None:
        """Clear the counters; reserved slots stay, so politeness carries over."""
        self.total = 0
        self.total_wait = 0.0
        self._recent: deque[float] = deque()  # start times within the window
        self._last: dict[str, float] = {}  # domain -> start of its latest request
        self._gap_sum = 0.0
        self._gap_count = 0
        self.min_gap: float | None = None
        # Latest requests: (start time, domain, seconds waited, gap or None)
        self.history: deque[tuple[float, str, float, float | None]] = deque(maxlen=1000)

    def set_domain_delay(self, domain: str, delay: float) -> None:
        if delay > 0:
            self.domain_delays[domain] = delay
        else:
            self.domain_delays.pop(domain, None)

    def _rate_interval(self) -> float:
        if self.jitter:
            return self.interval + random.uniform(0, self.jitter)
        return self.interval

    async def acquire(self, domain: str | None = None) -> float:
        """Wait until a request to domain may start. Returns the seconds waited."""
        domain = domain or ""
        now = time.monotonic()
        key = domain if self.per_domain else GLOBAL_KEY
        # key -> interval this request imposes on the next one with that key
        slots = {key: self._rate_interval()}
        if crawl_delay := self.domain_delays.get(domain):
            slots[domain] = max(slots.get(domain, 0.0), crawl_delay)
        # No await between reading and updating _next: reservations never race
        start = max(now, *(self._next.get(k, now) for k in slots))
        for k, interval in slots.items():
            self._next[k] = start + interval

        wait = start - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._record(domain, time.monotonic(), wait)
        return wait

    def _record(self, domain: str, now: float, wait: float) -> None:
        self.total += 1
        self.total_wait += wait
        self._recent.append(now)
        gap = None
        if domain in self._last:
            gap = now - self._last[domain]
            self._gap_sum += gap
            self._gap_count += 1
            self.min_gap = gap if self.min_gap is None else min(self.min_gap, gap)
        self._last[domain] = now
        self.history.append((now, domain, wait, gap))

    def current_rps(self) -> float:
        """Requests per second over the last `window` seconds."""
        now = time.monotonic()
        while self._recent and now - self._recent[0] > self.window:
            self._recent.popleft()
        if len(self._recent) < 2:
            return 0.0
        span = self._recent[-1] - self._recent[0]
        return (len(self._recent) - 1) / span if span > 0 else 0.0

    def get_stats(self) -> dict:
        """Gaps are measured between consecutive requests to the same domain."""
        return {
            "total_requests": self.total,
            "current_rps": round(self.current_rps(), 2),
            "avg_gap": round(self._gap_sum / self._gap_count, 3) if self._gap_count else 0.0,
            "min_gap": round(self.min_gap, 3) if self.min_gap is not None else 0.0,
            "avg_wait": round(self.total_wait / self.total, 3) if self.total else 0.0,
            "domain_delays": dict(self.domain_delays),
        }

"""Day 5 — automatic retries: which errors to retry, how many times and how
long to wait, with error statistics."""

import asyncio
import inspect
import logging
import time
from collections import Counter, deque
from dataclasses import dataclass

from errors import CrawlerError, NetworkError, PermanentError, TransientError, classify_exception
from rate_limiter import backoff_delay

logger = logging.getLogger("crawler.retry")


@dataclass(frozen=True)
class ErrorPolicy:
    """Retry settings for one error type or HTTP status.

    max_retries caps the strategy's max_retries (None = no extra cap);
    delay_multiplier scales the base delay; backoff_factor replaces the
    strategy's one (None = keep it).
    """

    max_retries: int | None = None
    delay_multiplier: float = 1.0
    backoff_factor: float | None = None


# A network failure needs more time to clear up and rarely does after a few tries
DEFAULT_POLICIES = {
    TransientError: ErrorPolicy(),
    NetworkError: ErrorPolicy(max_retries=2, delay_multiplier=2.0),
}
# Rate limited: back off harder. Server error: may be a bug, don't insist
DEFAULT_STATUS_POLICIES = {
    429: ErrorPolicy(delay_multiplier=4.0),
    500: ErrorPolicy(max_retries=1),
}


class RetryStats:
    """Error counters. errors_by_type / errors_by_status count every failed
    attempt; successes, failures, permanent_errors and failed_urls count
    calls (one URL fetch with all its retries)."""

    def __init__(self, history_size: int = 1000):
        self.history_size = history_size
        self.reset()

    def reset(self) -> None:
        self.calls = 0
        self.attempts = 0
        self.successes = 0
        self.failures = 0
        self.retries = 0
        self.successful_retries = 0  # calls that succeeded after retrying
        self.retry_time = 0.0  # backoff sleep + the repeated attempt
        self.errors_by_type: Counter[str] = Counter()
        self.errors_by_status: Counter[int] = Counter()
        self.permanent_errors: dict[str, str] = {}  # url -> error
        self.failed_urls: dict[str, str] = {}  # url -> "Kind: error", all final failures
        self.history: deque[dict] = deque(maxlen=self.history_size)  # failed attempts

    def record_attempt_error(self, url: str | None, attempt: int, error: CrawlerError,
                             elapsed: float, next_retry_in: float | None) -> None:
        self.errors_by_type[error.kind] += 1
        if error.status is not None:
            self.errors_by_status[error.status] += 1
        self.history.append({
            "url": url,
            "attempt": attempt,
            "error_type": error.kind,
            "status": error.status,
            "message": error.message,
            "elapsed": round(elapsed, 3),
            "next_retry_in": None if next_retry_in is None else round(next_retry_in, 3),
            "at": time.time(),
        })

    @property
    def avg_retry_time(self) -> float:
        return self.retry_time / self.retries if self.retries else 0.0

    def to_dict(self) -> dict:
        return {
            "calls": self.calls,
            "attempts": self.attempts,
            "successes": self.successes,
            "failures": self.failures,
            "retries": self.retries,
            "successful_retries": self.successful_retries,
            "avg_retry_time": round(self.avg_retry_time, 3),
            "errors_by_type": dict(self.errors_by_type),
            "errors_by_status": dict(self.errors_by_status),
            "permanent_errors": dict(self.permanent_errors),
            "failed_urls": dict(self.failed_urls),
        }


class RetryStrategy:
    """Runs a coroutine function and repeats it on temporary errors.

    - Exceptions are classified (errors.classify_exception); only those of
      the retry_on types are retried, PermanentError and ParseError by
      default are not. Exceptions that are not request failures (bugs)
      propagate at once, unchanged.
    - The delay before retry n (from 0) is
      base_delay * multiplier * factor**n + uniform(0, jitter), at least
      Retry-After and at most max_delay. multiplier, factor and an extra cap
      on the number of retries come from the ErrorPolicy of the HTTP status
      or, failing that, of the error type (see DEFAULT_*_POLICIES).
    - timeout_multiplier(attempt) tells the caller how much to stretch its
      timeouts on a repeated attempt.

    Usage:
        strategy = RetryStrategy(max_retries=3, backoff_factor=2.0)
        html = await strategy.execute_with_retry(crawler.request, url)
    """

    def __init__(
        self,
        max_retries: int = 3,
        backoff_factor: float = 2.0,
        retry_on: list[type[BaseException]] | None = None,
        base_delay: float = 0.5,
        max_delay: float = 30.0,
        jitter: float = 0.0,
        policies: dict[type[CrawlerError], ErrorPolicy] | None = None,
        status_policies: dict[int, ErrorPolicy] | None = None,
        timeout_growth: float = 1.5,
    ):
        if max_retries < 0:
            raise ValueError("max_retries must be >= 0")
        if backoff_factor < 1 or timeout_growth < 1:
            raise ValueError("backoff_factor and timeout_growth must be >= 1")
        if base_delay < 0 or jitter < 0:
            raise ValueError("base_delay and jitter must be >= 0")
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self.retry_on = tuple(retry_on) if retry_on is not None else (TransientError, NetworkError)
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.jitter = jitter
        self.policies = {**DEFAULT_POLICIES, **(policies or {})}
        self.status_policies = {**DEFAULT_STATUS_POLICIES, **(status_policies or {})}
        self.timeout_growth = timeout_growth
        self.stats = RetryStats()

    def policy_for(self, error: CrawlerError) -> ErrorPolicy:
        if error.status in self.status_policies:
            return self.status_policies[error.status]
        for cls in type(error).__mro__:
            if cls in self.policies:
                return self.policies[cls]
        return ErrorPolicy()

    def should_retry(self, error: CrawlerError) -> bool:
        # retry_on may name raw exceptions too, e.g. asyncio.TimeoutError
        return isinstance(error, self.retry_on) or isinstance(error.original, self.retry_on)

    def max_retries_for(self, error: CrawlerError) -> int:
        cap = self.policy_for(error).max_retries
        return self.max_retries if cap is None else min(cap, self.max_retries)

    def get_delay(self, error: CrawlerError, retry: int) -> float:
        """Seconds to wait before retry number retry + 1."""
        policy = self.policy_for(error)
        return backoff_delay(
            retry,
            self.base_delay * policy.delay_multiplier,
            self.max_delay,
            self.jitter,
            error.retry_after,
            factor=policy.backoff_factor or self.backoff_factor,
        )

    def timeout_multiplier(self, attempt: int) -> float:
        """Timeouts of attempt number `attempt` (from 1) are this many times longer."""
        return self.timeout_growth ** (attempt - 1)

    async def execute_with_retry(self, coro, *args, **kwargs):
        """Await coro(*args, **kwargs), repeating it on retryable errors.

        Returns its result or raises the classified CrawlerError of the last
        attempt (with .attempts set). The URL for logs is kwargs["url"] or
        the first string argument.
        """
        if inspect.iscoroutine(coro):
            coro.close()
            raise TypeError("pass a coroutine function and its arguments: a coroutine can be awaited only once")
        url = kwargs.get("url") or next((a for a in args if isinstance(a, str)), None)
        stats = self.stats
        stats.calls += 1
        attempt = 0
        retry_started: float | None = None
        while True:
            attempt += 1
            stats.attempts += 1
            start = time.perf_counter()
            try:
                result = await coro(*args, **kwargs)
            except Exception as exc:
                error = classify_exception(exc, url)
                if error is None:
                    raise
            else:
                if retry_started is not None:
                    stats.retry_time += time.perf_counter() - retry_started
                    stats.successful_retries += 1
                    logger.info("Success after %d attempts: %s", attempt, url)
                stats.successes += 1
                return result

            now = time.perf_counter()
            if retry_started is not None:
                stats.retry_time += now - retry_started
            limit = self.max_retries_for(error)
            retryable = self.should_retry(error)
            if retryable and attempt <= limit:
                delay = self.get_delay(error, attempt - 1)
                stats.record_attempt_error(url, attempt, error, now - start, delay)
                stats.retries += 1
                logger.warning(
                    "%s on %s (attempt %d/%d): %s — retry in %.2fs",
                    error.kind, url, attempt, limit + 1, error.message, delay,
                )
                retry_started = time.perf_counter()
                await asyncio.sleep(delay)
                continue

            stats.record_attempt_error(url, attempt, error, now - start, None)
            stats.failures += 1
            stats.failed_urls[url] = f"{error.kind}: {error.message}"
            if isinstance(error, PermanentError):
                stats.permanent_errors[url] = error.message
            # INFO: the caller gets the error and reports the final result itself
            if retryable:
                logger.info("Gave up after %d attempts: %s — %s: %s", attempt, url, error.kind, error.message)
            else:
                logger.info("%s on %s: %s — not retried", error.kind, url, error.message)
            error.attempts = attempt
            raise error

"""Day 5 — error classification: every failure of a request becomes one of
TransientError, PermanentError, NetworkError or ParseError."""

import asyncio

import aiohttp

# Statuses worth asking again: the server is busy, overloaded or restarting
TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}


class CrawlerError(Exception):
    """Base class: a failed request with its URL, HTTP status and the
    original exception (in .original and __cause__)."""

    def __init__(
        self,
        message: str,
        url: str | None = None,
        status: int | None = None,
        retry_after: float | None = None,
        original: BaseException | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.url = url
        self.status = status
        self.retry_after = retry_after  # seconds, from the Retry-After header
        self.original = original
        self.attempts = 0  # set by RetryStrategy when it gives up

    @property
    def kind(self) -> str:
        return type(self).__name__


class TransientError(CrawlerError):
    """Temporary: timeouts, 429, 5xx. The same request may work later."""


class PermanentError(CrawlerError):
    """Final: 404, 403, 401, an invalid URL. Repeating won't help."""


class NetworkError(CrawlerError):
    """Connection refused or reset, DNS failure, broken transfer."""


class ParseError(CrawlerError):
    """The response came, but can't be read (not text, broken encoding...)."""


class CircuitOpenError(CrawlerError):
    """The domain is temporarily blocked by the circuit breaker; no request was made."""


def parse_retry_after(headers) -> float | None:
    """Retry-After in seconds; the HTTP-date form is ignored."""
    try:
        return max(float(headers["Retry-After"]), 0.0)
    except (TypeError, KeyError, ValueError):
        return None


def error_for_status(status: int, message: str = "", url: str | None = None, headers=None) -> CrawlerError:
    """HTTP error status -> TransientError (429, 5xx...) or PermanentError (other 4xx)."""
    cls = TransientError if status in TRANSIENT_STATUSES or status >= 500 else PermanentError
    text = f"HTTP {status} {message}".rstrip()
    return cls(text, url=url, status=status, retry_after=parse_retry_after(headers))


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def classify_exception(exc: BaseException, url: str | None = None) -> CrawlerError | None:
    """Turn an exception into a CrawlerError subclass; None if it is not a
    request failure (a bug, a cancellation) and must propagate as is."""
    if isinstance(exc, CrawlerError):
        if exc.url is None:
            exc.url = url
        return exc
    if isinstance(exc, asyncio.CancelledError):
        return None

    error: CrawlerError
    # TooManyRedirects subclasses ClientResponseError, so it goes first
    if isinstance(exc, aiohttp.TooManyRedirects):
        error = PermanentError(_describe(exc), url=url)
    elif isinstance(exc, aiohttp.ClientResponseError):
        error = error_for_status(exc.status, exc.message, url, exc.headers)
    # Before ClientError: ServerTimeoutError is both
    elif isinstance(exc, asyncio.TimeoutError):
        error = TransientError("Timeout", url=url)
    # A bad certificate or a malformed URL won't fix itself
    elif isinstance(exc, (aiohttp.ClientSSLError, aiohttp.InvalidURL)):
        error = PermanentError(_describe(exc), url=url)
    elif isinstance(exc, (aiohttp.ClientError, ConnectionError, OSError)):
        error = NetworkError(_describe(exc), url=url)
    elif isinstance(exc, UnicodeDecodeError):
        error = ParseError("UnicodeDecodeError: not a text response", url=url)
    else:
        return None
    error.original = exc
    error.__cause__ = exc
    return error

"""Day 5 — circuit breaker: stop sending requests to a domain that keeps
failing and try it again after a pause."""

import logging
import time
from collections import deque
from dataclasses import dataclass, field

logger = logging.getLogger("crawler.circuit")

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


@dataclass
class _Circuit:
    state: str = CLOSED
    outcomes: deque = field(default_factory=deque)  # True = failure
    opened_at: float = 0.0
    probing: bool = False  # a half-open trial request is in flight
    times_opened: int = 0


class CircuitBreaker:
    """Per-domain circuit breaker.

    - closed: requests pass, the last `window` outcomes are tracked;
    - when at least `min_requests` are known and the share of failures
      reaches `failure_rate`, the circuit opens: requests are refused;
    - after `recovery_timeout` seconds it is half-open: one trial request
      passes; success closes the circuit, failure opens it again.

    Only failures that say the site is unhealthy (timeouts, 5xx, network
    errors) should be recorded as failures; a 404 is a healthy answer.
    """

    def __init__(
        self,
        failure_rate: float = 0.5,
        min_requests: int = 5,
        window: int = 10,
        recovery_timeout: float = 30.0,
        clock=time.monotonic,
    ):
        if not 0 < failure_rate <= 1:
            raise ValueError("failure_rate must be in (0, 1]")
        if min_requests < 1 or window < min_requests:
            raise ValueError("need 1 <= min_requests <= window")
        self.failure_rate = failure_rate
        self.min_requests = min_requests
        self.window = window
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._circuits: dict[str, _Circuit] = {}

    def _circuit(self, domain: str) -> _Circuit:
        if domain not in self._circuits:
            self._circuits[domain] = _Circuit(outcomes=deque(maxlen=self.window))
        return self._circuits[domain]

    def state(self, domain: str) -> str:
        c = self._circuit(domain)
        if c.state == OPEN and self._clock() - c.opened_at >= self.recovery_timeout:
            c.state = HALF_OPEN
            c.probing = False
            logger.info("Circuit half-open for %s: trying one request", domain)
        return c.state

    def allow_request(self, domain: str) -> bool:
        """True if a request to domain may go now. In the half-open state
        only one trial request passes until its outcome is recorded."""
        state = self.state(domain)
        if state == CLOSED:
            return True
        c = self._circuit(domain)
        if state == HALF_OPEN and not c.probing:
            c.probing = True
            return True
        return False

    def retry_in(self, domain: str) -> float:
        """Seconds until an open circuit lets a trial request through."""
        c = self._circuit(domain)
        if c.state != OPEN:
            return 0.0
        return max(self.recovery_timeout - (self._clock() - c.opened_at), 0.0)

    def record_success(self, domain: str) -> None:
        c = self._circuit(domain)
        if c.state == HALF_OPEN:
            logger.info("Circuit closed for %s: recovered", domain)
            c.state = CLOSED
            c.outcomes.clear()
            c.probing = False
        c.outcomes.append(False)

    def record_failure(self, domain: str) -> None:
        c = self._circuit(domain)
        if c.state == HALF_OPEN:
            self._open(domain, c, "trial request failed")
            return
        c.outcomes.append(True)
        failures = sum(c.outcomes)
        if (
            c.state == CLOSED
            and len(c.outcomes) >= self.min_requests
            and failures / len(c.outcomes) >= self.failure_rate
        ):
            self._open(domain, c, f"{failures}/{len(c.outcomes)} recent requests failed")

    def _open(self, domain: str, c: _Circuit, reason: str) -> None:
        c.state = OPEN
        c.opened_at = self._clock()
        c.probing = False
        c.times_opened += 1
        logger.warning("Circuit open for %s (%s): blocked for %.1fs", domain, reason, self.recovery_timeout)

    def get_stats(self) -> dict:
        return {
            domain: {
                "state": self.state(domain),
                "recent_failures": sum(c.outcomes),
                "recent_requests": len(c.outcomes),
                "times_opened": c.times_opened,
            }
            for domain, c in self._circuits.items()
        }

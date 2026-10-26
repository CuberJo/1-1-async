"""RateLimiter and backoff tests."""

import asyncio
import time

import pytest

from rate_limiter import RateLimiter, backoff_delay

# asyncio may wake a sleeper up to one clock tick early
TOLERANCE = 0.02


async def request_times(limiter: RateLimiter, domains: list[str]) -> dict[str, list[float]]:
    """Fire one acquire per item concurrently, return start times per domain."""
    times: dict[str, list[float]] = {}

    async def one(domain: str):
        await limiter.acquire(domain)
        times.setdefault(domain, []).append(time.monotonic())

    await asyncio.gather(*(one(d) for d in domains))
    return {d: sorted(t) for d, t in times.items()}


def gaps(times: list[float]) -> list[float]:
    return [b - a for a, b in zip(times, times[1:])]


@pytest.mark.asyncio
async def test_single_domain_is_spaced():
    limiter = RateLimiter(requests_per_second=10)  # 0.1s apart
    start = time.monotonic()
    times = await request_times(limiter, ["a.com"] * 5)
    assert all(g >= 0.1 - TOLERANCE for g in gaps(times["a.com"]))
    assert times["a.com"][0] - start < 0.05  # the first one goes at once
    assert 0.4 - TOLERANCE <= times["a.com"][-1] - start < 0.55


@pytest.mark.asyncio
async def test_domains_have_separate_limits():
    limiter = RateLimiter(requests_per_second=10, per_domain=True)
    start = time.monotonic()
    times = await request_times(limiter, ["a.com", "b.com"] * 4)
    for domain in ("a.com", "b.com"):
        assert all(g >= 0.1 - TOLERANCE for g in gaps(times[domain]))
    # Both domains run side by side: 4 requests each take ~0.3s, not 0.7s
    assert time.monotonic() - start < 0.45


@pytest.mark.asyncio
async def test_global_limit_shared_by_domains():
    limiter = RateLimiter(requests_per_second=10, per_domain=False)
    start = time.monotonic()
    times = await request_times(limiter, ["a.com", "b.com"] * 3)
    all_times = sorted(t for ts in times.values() for t in ts)
    assert all(g >= 0.1 - TOLERANCE for g in gaps(all_times))
    assert time.monotonic() - start >= 0.5 - TOLERANCE


@pytest.mark.asyncio
async def test_min_delay_overrides_faster_rate():
    limiter = RateLimiter(requests_per_second=100, min_delay=0.1)
    assert limiter.interval == 0.1
    times = await request_times(limiter, ["a.com"] * 4)
    assert all(g >= 0.1 - TOLERANCE for g in gaps(times["a.com"]))


@pytest.mark.asyncio
async def test_no_rate_means_no_waiting():
    limiter = RateLimiter(requests_per_second=None)
    start = time.monotonic()
    await request_times(limiter, ["a.com"] * 20)
    assert time.monotonic() - start < 0.05
    assert limiter.get_stats()["total_requests"] == 20


@pytest.mark.asyncio
async def test_jitter_adds_random_delay():
    limiter = RateLimiter(requests_per_second=20, jitter=0.05)  # 0.05..0.1s apart
    times = await request_times(limiter, ["a.com"] * 8)
    g = gaps(times["a.com"])
    assert all(0.05 - TOLERANCE <= x <= 0.1 + TOLERANCE for x in g)
    assert max(g) - min(g) > 0.005  # not a perfect beat


@pytest.mark.asyncio
async def test_domain_delay_applies_on_top():
    limiter = RateLimiter(requests_per_second=100)
    limiter.set_domain_delay("slow.com", 0.15)
    times = await request_times(limiter, ["slow.com"] * 3 + ["fast.com"] * 3)
    assert all(g >= 0.15 - TOLERANCE for g in gaps(times["slow.com"]))
    assert times["fast.com"][-1] - times["fast.com"][0] < 0.1


@pytest.mark.asyncio
async def test_domain_delay_in_global_mode():
    limiter = RateLimiter(requests_per_second=100, per_domain=False)
    limiter.set_domain_delay("slow.com", 0.15)
    times = await request_times(limiter, ["slow.com"] * 3 + ["fast.com"] * 3)
    assert all(g >= 0.15 - TOLERANCE for g in gaps(times["slow.com"]))
    # fast.com only follows the shared 0.01s interval
    assert times["fast.com"][-1] - times["fast.com"][0] < 0.1


@pytest.mark.asyncio
async def test_acquire_returns_wait_and_stats():
    limiter = RateLimiter(requests_per_second=10)
    assert await limiter.acquire("a.com") == 0
    waited = await limiter.acquire("a.com")
    assert 0.08 <= waited <= 0.1
    await limiter.acquire("a.com")
    stats = limiter.get_stats()
    assert stats["total_requests"] == 3
    assert stats["min_gap"] >= 0.1 - TOLERANCE
    assert 0.1 - TOLERANCE <= stats["avg_gap"] < 0.13
    assert 7 < stats["current_rps"] <= 10.5
    assert stats["avg_wait"] > 0
    limiter.reset_stats()
    assert limiter.get_stats()["total_requests"] == 0


def test_validates_arguments():
    with pytest.raises(ValueError):
        RateLimiter(requests_per_second=0)
    with pytest.raises(ValueError):
        RateLimiter(min_delay=-1)
    with pytest.raises(ValueError):
        RateLimiter(jitter=-0.1)


def test_backoff_grows_exponentially():
    assert [backoff_delay(n, base=0.5) for n in range(4)] == [0.5, 1.0, 2.0, 4.0]
    assert backoff_delay(10, base=1, max_delay=30) == 30
    assert backoff_delay(0, base=0.5, retry_after=3) == 3
    assert backoff_delay(0, base=0.5, retry_after=100, max_delay=10) == 10
    assert 1.0 <= backoff_delay(1, base=0.5, jitter=0.5) <= 1.5

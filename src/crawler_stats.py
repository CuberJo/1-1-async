"""Day 7 — crawl statistics: counters, distributions, timeline, export."""

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone

from models import FetchResult
from url_filter import domain_of


class CrawlerStats:
    """Collects what happened during a crawl, one fetch result at a time.

    - record(result, depth=None) after every fetch: successes, failures,
      URLs blocked by robots.txt, response times, bytes, status codes,
      error types, domains, depths and a per-second timeline;
    - start() / stop() mark the run; uptime counts until stop() or now;
    - to_dict() returns everything as plain JSON-ready values;
    - export_to_json(filename), export_to_html_report(filename) save it.

    Memory does not grow with the number of pages beyond the counters
    (one entry per domain, status, depth and second of the run).
    """

    def __init__(self, top_n: int = 10, slowest_n: int = 10):
        self.top_n = top_n
        self.slowest_n = slowest_n
        self.reset()

    def reset(self) -> None:
        self.started_at: float | None = None
        self.finished_at: float | None = None
        self.started_wall: datetime | None = None
        self.successful = 0
        self.failed = 0
        self.blocked = 0
        self.attempts = 0
        self.bytes_downloaded = 0
        self.total_response_time = 0.0
        self.status_codes: Counter[str] = Counter()  # "200", "404", "none" for no response
        self.errors_by_type: Counter[str] = Counter()
        self.domains: Counter[str] = Counter()
        self.domain_failures: Counter[str] = Counter()
        self.depths: Counter[int] = Counter()
        self.content_types: Counter[str] = Counter()
        self.timeline: Counter[int] = Counter()  # second of the run -> pages done
        self.slowest: list[tuple[float, str]] = []  # (elapsed, url), slowest first

    def start(self) -> None:
        self.reset()
        self.started_at = time.perf_counter()
        self.started_wall = datetime.now(timezone.utc)

    def stop(self) -> None:
        if self.started_at is not None and self.finished_at is None:
            self.finished_at = time.perf_counter()

    @property
    def running(self) -> bool:
        return self.started_at is not None and self.finished_at is None

    @property
    def total_pages(self) -> int:
        """Pages requested: successful + failed (robots.txt blocks are not requests)."""
        return self.successful + self.failed

    @property
    def uptime(self) -> float:
        if self.started_at is None:
            return 0.0
        return (self.finished_at or time.perf_counter()) - self.started_at

    @property
    def pages_per_sec(self) -> float:
        uptime = self.uptime
        return self.total_pages / uptime if uptime > 0 else 0.0

    @property
    def success_rate(self) -> float:
        return self.successful / self.total_pages if self.total_pages else 0.0

    @property
    def avg_response_time(self) -> float:
        return self.total_response_time / self.total_pages if self.total_pages else 0.0

    def record(self, result: FetchResult, depth: int | None = None) -> None:
        """Count one fetch result. Starts the clock if start() was not called."""
        if self.started_at is None:
            self.start()
        if result.blocked:
            self.record_blocked()
            return
        domain = domain_of(result.url) or "?"
        self.domains[domain] += 1
        self.attempts += result.attempts
        self.total_response_time += result.elapsed
        self.status_codes[str(result.status) if result.status else "none"] += 1
        if depth is not None:
            self.depths[depth] += 1
        if result.ok:
            self.successful += 1
            self.bytes_downloaded += len((result.content or "").encode("utf-8", errors="replace"))
            if result.content_type:
                self.content_types[result.content_type] += 1
        else:
            self.failed += 1
            self.domain_failures[domain] += 1
            self.errors_by_type[result.error_type or "Error"] += 1
        self.timeline[int(self.uptime)] += 1
        self._track_slowest(result.elapsed, result.url)

    def record_blocked(self) -> None:
        """A URL disallowed by robots.txt: never requested, counted apart."""
        if self.started_at is None:
            self.start()
        self.blocked += 1

    def _track_slowest(self, elapsed: float, url: str) -> None:
        if len(self.slowest) < self.slowest_n or elapsed > self.slowest[-1][0]:
            self.slowest.append((elapsed, url))
            self.slowest.sort(key=lambda item: item[0], reverse=True)  # stable: ties keep order
            del self.slowest[self.slowest_n:]

    def top_domains(self, n: int | None = None) -> list[dict]:
        return [
            {"domain": d, "pages": c, "failed": self.domain_failures[d]}
            for d, c in self.domains.most_common(n or self.top_n)
        ]

    def to_dict(self) -> dict:
        seconds = int(self.uptime) + 1 if self.started_at is not None else 0
        return {
            "total_pages": self.total_pages,
            "successful": self.successful,
            "failed": self.failed,
            "blocked": self.blocked,
            "success_rate": round(self.success_rate, 4),
            "attempts": self.attempts,
            "uptime": round(self.uptime, 3),
            "started_at": self.started_wall.isoformat() if self.started_wall else None,
            "avg_speed": round(self.pages_per_sec, 2),  # pages per second
            "avg_response_time": round(self.avg_response_time, 4),
            "bytes_downloaded": self.bytes_downloaded,
            "status_codes": dict(sorted(self.status_codes.items())),
            "errors_by_type": dict(self.errors_by_type.most_common()),
            "top_domains": self.top_domains(),
            "domains_count": len(self.domains),
            "depths": {str(d): c for d, c in sorted(self.depths.items())},
            "content_types": dict(self.content_types.most_common()),
            "timeline": [self.timeline.get(s, 0) for s in range(seconds)],
            "slowest": [{"url": u, "elapsed": round(e, 3)} for e, u in self.slowest],
        }

    def export_to_json(self, filename: str | os.PathLike, extra: dict | None = None) -> None:
        """Statistics (plus any extra sections) as pretty-printed JSON."""
        data = {**self.to_dict(), **(extra or {})}
        _ensure_dir(filename)
        with open(filename, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, default=str)

    def export_to_html_report(
        self, filename: str | os.PathLike, title: str = "Crawl report", extra: dict | None = None
    ) -> None:
        """A standalone HTML page with charts and tables (no external files)."""
        from report import render_html_report

        html = render_html_report(self.to_dict(), title=title, extra=extra or {})
        _ensure_dir(filename)
        with open(filename, "w", encoding="utf-8") as f:
            f.write(html)


def _ensure_dir(filename: str | os.PathLike) -> None:
    folder = os.path.dirname(os.fspath(filename))
    if folder:
        os.makedirs(folder, exist_ok=True)

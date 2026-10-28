"""Day 7 — live crawl progress: bar, percent, current speed, ETA, active tasks."""

import sys
import time
from typing import TextIO


def format_eta(seconds: float | None) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds + 0.5)
    minutes, s = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{s:02d}" if hours else f"{minutes:02d}:{s:02d}"


class ProgressMonitor:
    """Renders one progress line from crawler counters.

    - percent: done / target, where target = min(max_pages, done + queued +
      in progress): the crawl can't fetch more than it has found;
    - speed: pages per second over the last update (smoothed), not the
      average of the whole run, so it reacts to slowdowns;
    - ETA: remaining pages / current speed;
    - active: requests in flight right now.

    On a terminal the line is redrawn in place (\\r); in a file or a pipe
    every update is a new line.
    """

    def __init__(self, max_pages: int, stream: TextIO | None = None, width: int = 24, smoothing: float = 0.3):
        self.max_pages = max_pages
        self.stream = stream or sys.stdout
        self.width = width
        self.smoothing = smoothing  # weight of the newest speed sample
        self.speed = 0.0
        self._last: tuple[float, int] | None = None  # (time, done)
        self._inline = bool(getattr(self.stream, "isatty", lambda: False)())
        self._width_drawn = 0
        # A cp1251 / cp866 console or pipe can't print block characters
        try:
            "█░".encode(getattr(self.stream, "encoding", None) or "ascii")
            self.chars = "█░"
        except (UnicodeEncodeError, LookupError):
            self.chars = "#-"

    def snapshot(self, done: int, queued: int, in_progress: int, now: float | None = None) -> dict:
        """Compute the numbers for one update (separate from drawing, for tests)."""
        now = time.perf_counter() if now is None else now
        if self._last is not None:
            dt = now - self._last[0]
            if dt > 0:
                sample = (done - self._last[1]) / dt
                self.speed = sample if self.speed == 0 else (
                    self.smoothing * sample + (1 - self.smoothing) * self.speed
                )
        self._last = (now, done)
        target = max(min(self.max_pages, done + queued + in_progress), done, 1)
        remaining = target - done
        eta = remaining / self.speed if self.speed > 0 else (0.0 if remaining == 0 else None)
        return {
            "done": done,
            "target": target,
            "percent": 100.0 * done / target,
            "speed": self.speed,
            "eta": eta,
        }

    def render(self, stats: dict, final: bool = False) -> str:
        """stats: AdvancedCrawler.get_stats() (or any dict with the same keys)."""
        done = stats["processed"] + stats["failed"]
        snap = self.snapshot(done, stats["queued"], stats["in_progress"])
        filled = int(self.width * snap["percent"] / 100)
        bar = self.chars[0] * filled + self.chars[1] * (self.width - filled)
        speed = stats.get("avg_speed", 0.0) if final else snap["speed"]
        return (
            f"[{bar}] {snap['percent']:5.1f}% {done}/{snap['target']} | "
            f"{speed:5.2f} p/s{' avg' if final else ''} | "
            f"ETA {format_eta(0 if final else snap['eta'])} | "
            f"active {stats['concurrency']['active']:2} | queue {stats['queued']:4} | "
            f"errors {stats['failed']} | blocked {stats['robots_blocked']} | "
            f"{format_eta(stats['elapsed'])} elapsed"
        )

    def update(self, stats: dict, final: bool = False) -> None:
        line = self.render(stats, final)
        if self._inline:
            # Pad to wipe a longer previous line
            self.stream.write("\r" + line.ljust(self._width_drawn))
            self._width_drawn = len(line)
            if final:
                self.stream.write("\n")
        else:
            self.stream.write(line + "\n")
        self.stream.flush()

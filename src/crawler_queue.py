"""Day 3 — URL queue with priorities, deduplication and per-URL state."""

import asyncio
import heapq
import itertools


class CrawlerQueue:
    """Priority queue of URLs to crawl.

    - higher priority comes out first, equal priorities keep FIFO order;
    - a URL is accepted only once: re-adding a queued, in-progress, processed
      or failed URL is ignored;
    - every URL goes queued -> in progress -> processed | failed | blocked;
    - get_next() waits while the queue is empty but some URLs are still in
      progress (they may add new links) and returns None once the crawl is over.
    """

    def __init__(self):
        self._heap: list[tuple[int, int, str]] = []
        self._counter = itertools.count()  # tie-breaker keeps FIFO order
        self._seen: set[str] = set()
        self._changed = asyncio.Event()
        self._closed = False
        self.depths: dict[str, int] = {}
        self.priorities: dict[str, int] = {}
        self.in_progress: set[str] = set()
        self.processed: set[str] = set()
        self.failed: dict[str, str] = {}
        self.blocked: set[str] = set()  # disallowed by robots.txt

    def add_url(self, url: str, priority: int = 0, depth: int = 0) -> bool:
        """Add a URL. Returns False if it was seen before or the queue is closed."""
        if self._closed or url in self._seen:
            return False
        self._seen.add(url)
        self.depths[url] = depth
        self.priorities[url] = priority
        self._push(url)
        return True

    def _push(self, url: str) -> None:
        # heapq pops the smallest item, so the priority is negated
        heapq.heappush(self._heap, (-self.priorities[url], next(self._counter), url))
        self._changed.set()

    def mark_seen(self, url: str) -> None:
        """Never hand out this URL (e.g. the target of a redirect that was
        already fetched): it is not queued later and, if queued now, removed."""
        self._seen.add(url)
        heap = [entry for entry in self._heap if entry[2] != url]
        if len(heap) != len(self._heap):
            heapq.heapify(heap)
            self._heap = heap

    def put_back(self, url: str) -> None:
        """Return an in-progress URL to the queue unprocessed, with its priority.
        Works on a closed queue too: the URL then stays queued, unfetched."""
        if url in self.in_progress:
            self.in_progress.discard(url)
            self._push(url)

    async def get_next(self) -> str | None:
        """Next URL to process, or None when there is nothing left to do."""
        while True:
            if self._closed:
                return None
            if self._heap:
                _, _, url = heapq.heappop(self._heap)
                self.in_progress.add(url)
                return url
            if not self.in_progress:
                return None
            # Wait until a URL is added or an in-progress one is finished
            self._changed.clear()
            await self._changed.wait()

    def mark_processed(self, url: str) -> None:
        self.in_progress.discard(url)
        self.processed.add(url)
        self._changed.set()

    def mark_failed(self, url: str, error: str) -> None:
        self.in_progress.discard(url)
        self.failed[url] = error
        self._changed.set()

    def mark_blocked(self, url: str) -> None:
        self.in_progress.discard(url)
        self.blocked.add(url)
        self._changed.set()

    def close(self) -> None:
        """Stop handing out URLs: get_next() returns None from now on."""
        self._closed = True
        self._changed.set()

    def get_depth(self, url: str) -> int:
        return self.depths.get(url, 0)

    def __len__(self) -> int:
        return len(self._heap)

    def get_stats(self) -> dict:
        return {
            "queued": len(self._heap),
            "in_progress": len(self.in_progress),
            "processed": len(self.processed),
            "failed": len(self.failed),
            "blocked": len(self.blocked),
            "seen": len(self._seen),
        }

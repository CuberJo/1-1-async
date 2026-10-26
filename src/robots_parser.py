"""Day 4 — robots.txt: loading, parsing, per-site cache and URL checks."""

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import unquote, urlparse

import aiohttp

logger = logging.getLogger("crawler.robots")

ROBOTS_MAX_SIZE = 500 * 1024  # RFC 9309: parse at least 500 KiB, ignore the rest


def _pattern_regex(pattern: str) -> re.Pattern:
    """robots.txt path pattern -> regex: "*" is any sequence, a trailing "$"
    anchors the end, everything else is literal and matches as a prefix."""
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


def product_token(user_agent: str) -> str:
    """"MyBot/1.0 (+https://example.com)" -> "mybot"."""
    return user_agent.split("/")[0].strip().lower()


@dataclass
class RobotsGroup:
    """Rules for one or more User-agent lines in a row."""

    agents: list[str] = field(default_factory=list)  # lowercase
    rules: list[tuple[bool, str, re.Pattern]] = field(default_factory=list)  # (allow, pattern, regex)
    crawl_delay: float | None = None


@dataclass
class RobotsRules:
    """Parsed robots.txt of one site."""

    groups: list[RobotsGroup] = field(default_factory=list)
    sitemaps: list[str] = field(default_factory=list)
    status: int | None = None  # HTTP status of robots.txt, None on a network error
    disallow_all: bool = False  # robots.txt was unavailable (5xx)

    def groups_for(self, user_agent: str) -> list[RobotsGroup]:
        """Groups that apply to user_agent: the most specific User-agent name
        contained in its product token wins, otherwise the "*" groups."""
        token = product_token(user_agent)
        best = ""
        for group in self.groups:
            for agent in group.agents:
                if agent != "*" and agent in token and len(agent) > len(best):
                    best = agent
        name = best or "*"
        # Groups with the same User-agent are combined (RFC 9309)
        return [g for g in self.groups if name in g.agents]

    def can_fetch(self, path: str, user_agent: str = "*") -> bool:
        """The longest matching pattern decides; on a tie Allow wins.
        No matching rule means the path is allowed."""
        if self.disallow_all:
            return False
        path = unquote(path)
        best: tuple[int, bool] | None = None  # (pattern length, allow)
        for group in self.groups_for(user_agent):
            for allow, pattern, regex in group.rules:
                if regex.match(path):
                    candidate = (len(pattern), allow)
                    if best is None or candidate > best:
                        best = candidate
        return True if best is None else best[1]

    def crawl_delay(self, user_agent: str = "*") -> float:
        delays = [g.crawl_delay for g in self.groups_for(user_agent) if g.crawl_delay is not None]
        return max(delays, default=0.0)

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "disallow_all": self.disallow_all,
            "groups": [
                {
                    "user_agents": g.agents,
                    "allow": [p for allow, p, _ in g.rules if allow],
                    "disallow": [p for allow, p, _ in g.rules if not allow],
                    "crawl_delay": g.crawl_delay,
                }
                for g in self.groups
            ],
            "sitemaps": self.sitemaps,
        }


def parse_robots(text: str) -> RobotsRules:
    """Parse robots.txt text. Unknown fields and broken lines are ignored."""
    rules = RobotsRules()
    group: RobotsGroup | None = None
    collecting_agents = False  # User-agent lines in a row form one group
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        name, value = (part.strip() for part in line.split(":", 1))
        name = name.lower()
        if name == "user-agent":
            if not collecting_agents:
                group = RobotsGroup()
                rules.groups.append(group)
                collecting_agents = True
            group.agents.append(value.lower())
        elif name == "sitemap":  # not tied to a group
            rules.sitemaps.append(value)
        elif group is not None:  # rules before the first User-agent are ignored
            collecting_agents = False
            if name in ("allow", "disallow"):
                # An empty Disallow allows everything: it's simply no rule
                if value:
                    group.rules.append((name == "allow", value, _pattern_regex(unquote(value))))
            elif name == "crawl-delay":
                try:
                    group.crawl_delay = max(float(value), 0.0)
                except ValueError:
                    logger.debug("Bad Crawl-delay: %r", value)
    return rules


def origin_of(url: str) -> str:
    """robots.txt is per scheme + host + port: "https://example.com:8443"."""
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


class RobotsParser:
    """Loads robots.txt once per site and answers whether a URL may be fetched.

    - fetch_robots(url) downloads and caches the rules of the URL's site;
    - can_fetch / get_crawl_delay use the cache only (call fetch_robots first);
    - 2xx: rules from the file; 4xx (no robots.txt): everything allowed;
      429 and 5xx (site unavailable): everything disallowed; network error:
      everything allowed (the page request itself will report the error).
    """

    def __init__(
        self,
        session_factory: Callable[[], Awaitable[aiohttp.ClientSession]] | None = None,
        user_agent: str = "AsyncCrawler/0.1",
        timeout: float = 10.0,
    ):
        self._session_factory = session_factory
        self._own_session: aiohttp.ClientSession | None = None
        self.user_agent = user_agent  # sent when downloading robots.txt
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._cache: dict[str, RobotsRules] = {}  # origin -> rules
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_origin: str | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session_factory is not None:
            return await self._session_factory()
        if self._own_session is None or self._own_session.closed:
            self._own_session = aiohttp.ClientSession()
        return self._own_session

    async def fetch_robots(self, base_url: str) -> dict:
        """Rules of the site of base_url (any URL of the site will do), as a dict.
        Downloaded once: later calls, concurrent ones included, use the cache."""
        origin = origin_of(base_url)
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            if origin not in self._cache:
                self._cache[origin] = await self._download(origin)
        self._last_origin = origin
        return self._cache[origin].to_dict()

    async def _download(self, origin: str) -> RobotsRules:
        url = origin + "/robots.txt"
        try:
            session = await self._get_session()
            async with session.get(
                url,
                headers={"User-Agent": self.user_agent},
                timeout=self.timeout,
                raise_for_status=False,
            ) as response:
                status = response.status
                if status == 429 or status >= 500:
                    logger.warning("robots.txt unavailable (HTTP %d), disallowing %s", status, origin)
                    return RobotsRules(status=status, disallow_all=True)
                if status >= 400:
                    logger.info("No robots.txt (HTTP %d) at %s: everything allowed", status, origin)
                    return RobotsRules(status=status)
                body = await response.content.read(ROBOTS_MAX_SIZE)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.warning("Could not load %s (%s): everything allowed", url, type(e).__name__)
            return RobotsRules()
        rules = parse_robots(body.decode("utf-8", errors="replace"))
        rules.status = status
        logger.info("Loaded %s: %d groups", url, len(rules.groups))
        return rules

    def get_rules(self, url: str) -> RobotsRules | None:
        return self._cache.get(origin_of(url))

    def can_fetch(self, url: str, user_agent: str = "*") -> bool:
        """Whether user_agent may fetch url. A site whose robots.txt was not
        loaded yet counts as allowed."""
        rules = self.get_rules(url)
        if rules is None:
            return True
        parsed = urlparse(url)
        path = parsed.path or "/"
        if path == "/robots.txt":
            return True
        if parsed.query:
            path += "?" + parsed.query
        return rules.can_fetch(path, user_agent)

    def get_crawl_delay(self, user_agent: str = "*", url: str | None = None) -> float:
        """Crawl-delay in seconds for user_agent (0.0 if not set) on the site of
        url, or on the site passed to fetch_robots last."""
        origin = origin_of(url) if url else self._last_origin
        rules = self._cache.get(origin) if origin else None
        return rules.crawl_delay(user_agent) if rules else 0.0

    async def close(self) -> None:
        if self._own_session is not None and not self._own_session.closed:
            await self._own_session.close()
        self._own_session = None

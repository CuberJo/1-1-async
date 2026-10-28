"""Day 2 — HTML parsing and structured data extraction built on BeautifulSoup."""

import asyncio
import copy
import logging
import pickle
import re
from concurrent.futures import Executor
from concurrent.futures.process import BrokenProcessPool
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup, FeatureNotFound, Tag

logger = logging.getLogger("crawler.parser")

# Tags whose content is never visible text
NON_TEXT_TAGS = ("script", "style", "noscript", "template", "svg", "head")
# Schemes that don't point to a fetchable page
SKIP_SCHEMES = ("javascript:", "mailto:", "tel:", "data:", "ftp:", "file:")
ALLOWED_SCHEMES = ("http", "https")


def _clean(text: str) -> str:
    """Collapse runs of whitespace into a single space."""
    return re.sub(r"\s+", " ", text).strip()


def _own_text(tag: Tag, exclude) -> str:
    """Text of tag without the given descendants. Works on a copy, soup stays intact."""
    tag = copy.copy(tag)
    for child in tag.find_all(exclude):
        child.decompose()
    return _clean(tag.get_text(" "))


def normalize_url(href: str | None, base_url: str) -> str | None:
    """Turn href into an absolute http(s) URL without a fragment, or None if invalid."""
    if not href:
        return None
    href = href.strip()
    if not href or href.startswith("#") or href.lower().startswith(SKIP_SCHEMES):
        return None
    try:
        absolute, _fragment = urldefrag(urljoin(base_url, href))
        parsed = urlparse(absolute)
        parsed.port  # raises ValueError on a malformed port
    except ValueError:
        return None
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        return None
    return absolute


class HTMLParser:
    """Extracts text, links, metadata and structured blocks from HTML."""

    def __init__(self, same_domain_only: bool = False, features: str = "lxml"):
        # same_domain_only=True drops links leading to other hosts
        self.same_domain_only = same_domain_only
        self.features = features

    def _make_soup(self, html: str) -> BeautifulSoup:
        try:
            return BeautifulSoup(html, self.features)
        except FeatureNotFound:
            logger.warning("Parser %r is not installed, falling back to html.parser", self.features)
            return BeautifulSoup(html, "html.parser")

    async def parse_html(self, html: str, url: str, executor: Executor | None = None) -> dict:
        """Parse a page. Never raises: failed parts are listed in result["errors"].

        executor: e.g. a ProcessPoolExecutor to parse on other CPU cores; if
        it breaks, parsing falls back to a thread.
        """
        # Parsing is CPU-bound, so it runs in a thread and doesn't block the event loop
        if executor is not None:
            try:
                return await asyncio.get_running_loop().run_in_executor(executor, self._parse_sync, html, url)
            except (BrokenProcessPool, pickle.PicklingError, RuntimeError) as e:
                # RuntimeError: the executor was shut down
                logger.warning("Parse executor failed (%s), parsing in a thread: %s", type(e).__name__, url)
        return await asyncio.to_thread(self._parse_sync, html, url)

    @staticmethod
    def empty_result(url: str) -> dict:
        """Result skeleton: every field present, all empty."""
        return {
            "url": url,
            "title": "",
            "text": "",
            "links": [],
            "metadata": {},
            "images": [],
            "headings": [],
            "tables": [],
            "lists": [],
            "errors": [],
        }

    def _parse_sync(self, html: str, url: str) -> dict:
        result = self.empty_result(url)
        if not html or not html.strip():
            logger.warning("Empty HTML: %s", url)
            result["errors"].append("empty html")
            return result

        try:
            soup = self._make_soup(html)
        except Exception as e:
            logger.warning("Failed to parse HTML of %s: %s", url, e)
            result["errors"].append(f"parse: {type(e).__name__}: {e}")
            return result

        # <base href> changes how relative links resolve
        base_tag = soup.find("base", href=True)
        base_url = urljoin(url, base_tag["href"]) if base_tag else url

        # Each step is isolated: one failure doesn't lose the other results
        steps = {
            "metadata": lambda: self.extract_metadata(soup),
            "links": lambda: self.extract_links(soup, base_url),
            "images": lambda: self.extract_images(soup, base_url),
            "headings": lambda: self.extract_headings(soup),
            "tables": lambda: self.extract_tables(soup),
            "lists": lambda: self.extract_lists(soup),
            "text": lambda: self.extract_text(soup),
        }
        for field, step in steps.items():
            try:
                result[field] = step()
            except Exception as e:
                logger.warning("Failed to extract %s from %s: %s", field, url, e)
                result["errors"].append(f"{field}: {type(e).__name__}: {e}")

        result["title"] = result["metadata"].get("title", "")
        if not result["title"]:
            logger.warning("No <title> on page: %s", url)
        return result

    def extract_links(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        """All unique absolute links from <a href>, in document order."""
        base_host = urlparse(base_url).hostname
        links: dict[str, None] = {}  # dict keeps insertion order and dedupes
        for a in soup.find_all("a", href=True):
            link = normalize_url(a["href"], base_url)
            if link is None:
                continue
            if self.same_domain_only and urlparse(link).hostname != base_host:
                continue
            links[link] = None
        return list(links)

    def extract_text(self, soup: BeautifulSoup, selector: str | None = None) -> str:
        """Visible text of the page, or of the elements matching a CSS selector."""
        elements = soup.select(selector) if selector else [soup.body or soup]
        return " ".join(t for el in elements if (t := _own_text(el, NON_TEXT_TAGS)))

    def extract_metadata(self, soup: BeautifulSoup) -> dict:
        """title, description, keywords, plus lang, canonical and Open Graph tags."""
        title_tag = soup.find("title")
        metadata = {
            "title": _clean(title_tag.get_text()) if title_tag else "",
            "description": "",
            "keywords": [],
        }
        for meta in soup.find_all("meta"):
            key = (meta.get("name") or meta.get("property") or "").strip().lower()
            content = _clean(meta.get("content") or "")
            if not key or not content:
                continue
            if key == "keywords":
                metadata["keywords"] = [k.strip() for k in content.split(",") if k.strip()]
            elif key == "description" or key.startswith("og:"):
                metadata[key] = content

        html_tag = soup.find("html")
        if isinstance(html_tag, Tag) and html_tag.get("lang"):
            metadata["lang"] = html_tag["lang"]
        canonical = soup.find("link", rel="canonical", href=True)
        if canonical:
            metadata["canonical"] = canonical["href"]
        return metadata

    def extract_images(self, soup: BeautifulSoup, base_url: str) -> list[dict]:
        """All <img> with absolute src and alt text."""
        images = []
        for img in soup.find_all("img"):
            # Lazy-loaded images keep the real address in data-src
            src = normalize_url(img.get("src") or img.get("data-src"), base_url)
            if src is None:
                continue
            images.append({"src": src, "alt": _clean(img.get("alt") or "")})
        return images

    def extract_headings(self, soup: BeautifulSoup) -> list[dict]:
        """h1–h3 in document order: [{"level": 1, "text": "..."}]."""
        headings = []
        for h in soup.find_all(["h1", "h2", "h3"]):
            text = _clean(h.get_text(" "))
            if text:
                headings.append({"level": int(h.name[1]), "text": text})
        return headings

    def extract_tables(self, soup: BeautifulSoup) -> list[dict]:
        """Tables as {"headers": [...], "rows": [[...], ...]}."""
        tables = []
        for table in soup.find_all("table"):
            # Skip rows of nested tables: they are collected separately
            rows = [tr for tr in table.find_all("tr") if tr.find_parent("table") is table]
            headers: list[str] = []
            data = []
            for tr in rows:
                cells = tr.find_all(["th", "td"], recursive=False)
                values = [_clean(c.get_text(" ")) for c in cells]
                if not values:
                    continue
                # The first row made of <th> only is the header
                if not headers and not data and all(c.name == "th" for c in cells):
                    headers = values
                else:
                    data.append(values)
            if headers or data:
                tables.append({"headers": headers, "rows": data})
        return tables

    def extract_lists(self, soup: BeautifulSoup) -> list[dict]:
        """<ul>/<ol> as {"type": "ul", "items": [...]}; nested lists come as separate entries."""
        lists = []
        for lst in soup.find_all(["ul", "ol"]):
            items = []
            for li in lst.find_all("li", recursive=False):
                text = _own_text(li, ["ul", "ol"])
                if text:
                    items.append(text)
            if items:
                lists.append({"type": lst.name, "items": items})
        return lists

"""Helpers: logging setup and report output."""

import logging

from models import FetchResult


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def print_report(results: list[FetchResult]) -> None:
    print(f"\n{'URL':<48} {'Status':<8} {'Time':>7}  Result")
    print("-" * 90)
    for r in results:
        status = str(r.status) if r.status else "-"
        outcome = f"OK, {len(r.content)} chars" if r.ok else r.error
        print(f"{r.url:<48} {status:<8} {r.elapsed:>6.2f}s  {outcome}")


def summarize_page(page: dict, max_links: int = 5) -> dict:
    """Short page summary: key fields and counters instead of full data."""
    return {
        "url": page["url"],
        "title": page["title"],
        "description": page["metadata"].get("description", ""),
        "text_length": len(page["text"]),
        "links_count": len(page["links"]),
        "links": page["links"][:max_links],
        "images_count": len(page["images"]),
        "headings": [f"h{h['level']}: {h['text']}" for h in page["headings"][:max_links]],
        "tables_count": len(page["tables"]),
        "lists_count": len(page["lists"]),
        "errors": page["errors"],
    }


def print_parse_stats(pages: list[dict]) -> None:
    print(f"\n{'URL':<40} {'Text':>7} {'Links':>6} {'Imgs':>5} {'Heads':>6} {'Tables':>7} {'Lists':>6}  Errors")
    print("-" * 100)
    for p in pages:
        url = p["url"] if len(p["url"]) <= 40 else p["url"][:37] + "..."
        print(
            f"{url:<40} {len(p['text']):>7} {len(p['links']):>6} {len(p['images']):>5} "
            f"{len(p['headings']):>6} {len(p['tables']):>7} {len(p['lists']):>6}  {len(p['errors'])}"
        )

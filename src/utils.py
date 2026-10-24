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

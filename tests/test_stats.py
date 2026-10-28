"""Statistics, JSON/HTML export, progress line and logging setup."""

import io
import json
import logging

import pytest

from crawler_stats import CrawlerStats
from models import FetchResult
from progress import ProgressMonitor, format_eta
from report import compact, nice_max, render_html_report
from utils import configure_logging


def ok(url: str, elapsed: float = 0.1, status: int = 200, content: str = "<html>é</html>") -> FetchResult:
    return FetchResult(url=url, content=content, status=status, elapsed=elapsed, attempts=1,
                       content_type="text/html")


def failed(url: str, status: int | None, error_type: str, attempts: int = 3) -> FetchResult:
    return FetchResult(url=url, status=status, error="boom", error_type=error_type, elapsed=0.5, attempts=attempts)


@pytest.fixture
def stats() -> CrawlerStats:
    s = CrawlerStats(top_n=2, slowest_n=2)
    s.start()
    for i in range(5):
        s.record(ok(f"https://a.org/{i}", elapsed=0.09 * (i + 1)), depth=i % 2)
    s.record(ok("https://www.b.org/", elapsed=0.05), depth=0)
    s.record(failed("https://b.org/x", 404, "PermanentError", attempts=1), depth=1)
    s.record(failed("https://c.org/", None, "NetworkError"), depth=1)
    s.record(FetchResult(url="https://a.org/private", error="Blocked by robots.txt", blocked=True))
    s.stop()
    return s


def test_counters(stats):
    d = stats.to_dict()
    assert (d["total_pages"], d["successful"], d["failed"], d["blocked"]) == (8, 6, 2, 1)
    assert d["success_rate"] == 0.75
    assert d["attempts"] == 6 + 1 + 3
    assert d["status_codes"] == {"200": 6, "404": 1, "none": 1}
    assert d["errors_by_type"] == {"PermanentError": 1, "NetworkError": 1}
    assert d["depths"] == {"0": 4, "1": 4}
    assert d["content_types"] == {"text/html": 6}
    assert d["bytes_downloaded"] == 6 * len("<html>é</html>".encode())
    assert d["avg_response_time"] == pytest.approx((0.09 * 15 + 0.05 + 0.5 + 0.5) / 8, abs=1e-4)


def test_top_domains_slowest_and_timeline(stats):
    d = stats.to_dict()
    # www.b.org and b.org are one site; top_n=2 keeps the two largest
    assert d["top_domains"] == [{"domain": "a.org", "pages": 5, "failed": 0},
                                {"domain": "b.org", "pages": 2, "failed": 1}]
    assert d["domains_count"] == 3
    assert [s["url"] for s in d["slowest"]] == ["https://b.org/x", "https://c.org/"]  # 0.5s each, earliest first
    assert sum(d["timeline"]) == 8 and len(d["timeline"]) >= 1


def test_uptime_and_speed_freeze_after_stop(stats):
    uptime = stats.uptime
    assert uptime > 0 and not stats.running
    assert stats.uptime == uptime
    assert stats.pages_per_sec == pytest.approx(8 / uptime)


def test_record_starts_the_clock_and_start_resets():
    s = CrawlerStats()
    assert s.to_dict()["total_pages"] == 0 and s.uptime == 0
    s.record(ok("https://a.org/"))
    assert s.running and s.total_pages == 1
    s.start()
    assert s.total_pages == 0 and s.to_dict()["status_codes"] == {}


def test_export_to_json(stats, tmp_path):
    path = tmp_path / "sub" / "stats.json"
    stats.export_to_json(path, extra={"failed_urls": {"https://c.org/": "boom"}})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["total_pages"] == 8 and data["failed_urls"] == {"https://c.org/": "boom"}


def test_export_to_html_report(stats, tmp_path):
    path = tmp_path / "report.html"
    stats.export_to_html_report(path, title="Test <report>", extra={
        "failed_urls": {"https://c.org/<script>": "NetworkError"},
        "config": {"crawler.max_pages": "10"},
        "start_urls": ["https://a.org/"],
    })
    html = path.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>") and "<title>Test &lt;report&gt;</title>" in html
    for section in ("Status codes", "Top domains", "Throughput over time", "Errors by type",
                    "Pages by depth", "Slowest pages", "Failed URLs", "Settings"):
        assert section in html
    assert html.count("<svg") == 1 and html.count('class="bars"') == 4  # timeline + 4 bar charts
    assert "<script>\"" not in html and "https://c.org/&lt;script&gt;" in html  # escaped
    assert "4xx client error" in html and "no response" in html


def test_html_report_without_data():
    html = render_html_report(CrawlerStats().to_dict())
    assert "No data" in html and "<svg" not in html


def test_formatting_helpers():
    assert [compact(n) for n in (0, 999, 1284, 12900, 4_200_000)] == ["0", "999", "1,284", "12.9K", "4.2M"]
    assert [nice_max(v) for v in (0, 3, 7, 12, 99, 101)] == [1, 5, 10, 20, 100, 200]
    assert [format_eta(s) for s in (None, 5, 65.4, 3725)] == ["--:--", "00:05", "01:05", "1:02:05"]


# --- progress ----------------------------------------------------------------


def test_progress_snapshot_percent_speed_eta():
    m = ProgressMonitor(max_pages=100)
    first = m.snapshot(done=0, queued=10, in_progress=2, now=0.0)
    assert first["target"] == 12 and first["eta"] is None
    snap = m.snapshot(done=10, queued=50, in_progress=5, now=2.0)
    assert snap["speed"] == 5.0  # first sample is taken as is
    assert snap["target"] == 65 and snap["percent"] == pytest.approx(100 * 10 / 65)
    assert snap["eta"] == pytest.approx(55 / 5)
    # max_pages caps the target; speed is smoothed
    snap = m.snapshot(done=20, queued=500, in_progress=10, now=3.0)
    assert snap["target"] == 100
    assert snap["speed"] == pytest.approx(0.3 * 10 + 0.7 * 5)


def crawler_stats(processed: int, failed: int = 0) -> dict:
    return {"processed": processed, "failed": failed, "queued": 3, "in_progress": 2,
            "concurrency": {"active": 2}, "robots_blocked": 1, "elapsed": 4.0, "avg_speed": 2.5}


def test_progress_line_in_a_pipe_and_ascii_fallback():
    stream = io.StringIO()
    m = ProgressMonitor(max_pages=10, stream=stream, width=10)
    m.update(crawler_stats(2, 1))
    m.update(crawler_stats(5), final=True)
    lines = stream.getvalue().splitlines()
    assert len(lines) == 2  # not a terminal: one line per update
    assert "3/8" in lines[0] and "active  2" in lines[0] and "errors 1" in lines[0]
    assert "2.50 p/s avg" in lines[1] and "ETA 00:00" in lines[1]

    class Cp1251(io.StringIO):
        encoding = "cp1251"

    m = ProgressMonitor(max_pages=10, stream=Cp1251(), width=10)
    assert "#" in m.render(crawler_stats(5)) and "█" not in m.render(crawler_stats(5))


def test_progress_redraws_in_place_on_a_terminal():
    class Terminal(io.StringIO):
        encoding = "utf-8"

        def isatty(self):
            return True

    stream = Terminal()
    m = ProgressMonitor(max_pages=10, stream=stream)
    m.update(crawler_stats(1))
    m.update(crawler_stats(10), final=True)
    out = stream.getvalue()
    assert out.count("\r") == 2 and out.endswith("\n") and out.count("\n") == 1


# --- logging -----------------------------------------------------------------


@pytest.fixture
def restore_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def test_configure_logging_levels_and_rotation(tmp_path, restore_logging, capsys):
    log_file = tmp_path / "logs" / "crawler.log"
    configure_logging("DEBUG", "WARNING", str(log_file), max_bytes=2000, backup_count=2)
    log = logging.getLogger("crawler.test")
    log.debug("debug line")
    log.warning("warning line")
    for i in range(100):
        log.info("filler %d %s", i, "x" * 50)
    err = capsys.readouterr().err
    assert "warning line" in err and "debug line" not in err and "filler" not in err
    files = sorted(p.name for p in log_file.parent.iterdir())
    assert files == ["crawler.log", "crawler.log.1", "crawler.log.2"]  # rotated, 2 kept
    assert all(p.stat().st_size <= 2000 for p in log_file.parent.iterdir())
    last = log_file.read_text(encoding="utf-8").splitlines()[-1]
    assert " INFO    crawler.test: filler 99" in last and last[:4].isdigit()  # 2026-...

    # Calling it again replaces the handlers instead of adding more
    configure_logging("INFO", "ERROR", None)
    assert len(logging.getLogger().handlers) == 1


def test_configure_logging_json(tmp_path, restore_logging):
    log_file = tmp_path / "crawler.jsonl"
    configure_logging("INFO", "CRITICAL", str(log_file), fmt="json")
    try:
        raise RuntimeError("bad")
    except RuntimeError:
        logging.getLogger("crawler").exception("Ошибка %s", 42)
    entry = json.loads(log_file.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["level"] == "ERROR" and entry["logger"] == "crawler"
    assert entry["message"] == "Ошибка 42" and "RuntimeError: bad" in entry["exception"]
    assert entry["time"].endswith("+00:00")

"""Day 7 — standalone HTML crawl report: stat tiles, SVG charts, tables.

Everything is inline (CSS, SVG, a few lines of JS for tooltips), so the
file opens anywhere without a network. Charts follow the system light or
dark theme.
"""

from datetime import datetime, timezone
from html import escape

CHART_W = 640  # viewBox width of the column chart

# Status code class -> (status color role, legend text)
STATUS_CLASSES = {
    "2": ("good", "2xx success"),
    "3": ("warning", "3xx redirect"),
    "4": ("serious", "4xx client error"),
    "5": ("critical", "5xx server error"),
}

CSS = """
:root {
  color-scheme: light;
  --page: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --series-1: #2a78d6;
  --good: #0ca30c; --warning: #fab219; --serious: #ec835a; --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    color-scheme: dark;
    --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --series-1: #3987e5;
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --page: #0d0d0d; --surface: #1a1a19; --ink: #f0efec; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --series-1: #3987e5;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--page); color: var(--ink);
  font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
main { max-width: 1080px; margin: 0 auto; padding: 32px 16px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; }
h2 { font-size: 17px; margin: 0 0 2px; }
.sub { color: var(--ink-2); margin: 0 0 24px; overflow-wrap: anywhere; }
.note { color: var(--muted); font-size: 13px; margin: 0 0 12px; }
.tiles { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; margin-bottom: 24px; }
.tile, .card { background: var(--surface); border: 1px solid var(--ring); border-radius: 10px; }
.tile { padding: 14px 16px; }
.tile .label { color: var(--ink-2); font-size: 13px; }
.tile .value { font-size: 26px; font-weight: 600; }
.tile .value.hero { font-size: 48px; line-height: 1.1; }
.tile .meta { color: var(--muted); font-size: 12px; }
.status { display: inline-flex; align-items: center; gap: 6px; }
.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex: none; }
.grid2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 480px), 1fr)); gap: 16px; }
.card { padding: 18px 20px; margin-bottom: 16px; overflow: hidden; }
svg { display: block; width: 100%; height: auto; font: 12px system-ui, -apple-system, "Segoe UI", sans-serif; }
svg text { fill: var(--ink-2); }
svg .value { fill: var(--ink); font-weight: 600; }
svg .tick { fill: var(--muted); font-variant-numeric: tabular-nums; }
svg .gridline { stroke: var(--grid); stroke-width: 1; }
svg .baseline { stroke: var(--axis); stroke-width: 1; }
svg .hit { fill: transparent; }
.bars { display: grid; gap: 6px; margin-top: 8px; }
.bar-row { display: grid; grid-template-columns: minmax(80px, 38%) 1fr; align-items: center; gap: 10px; min-height: 24px; }
.bar-row:hover .bar { opacity: .8; }
.bar-label { color: var(--ink-2); font-size: 13px; text-align: right; overflow-wrap: anywhere; }
.bar-track { display: flex; align-items: center; gap: 6px; min-width: 0; padding-right: 48px; }
.bar { display: block; height: 18px; border-radius: 0 4px 4px 0; flex: none; max-width: 100%; }
.bar-value { font-weight: 600; font-size: 13px; font-variant-numeric: tabular-nums; white-space: nowrap; margin-right: -48px; }
svg .mark { transition: opacity .1s; }
svg g.item:hover .mark { opacity: .8; }
.legend { display: flex; flex-wrap: wrap; gap: 14px; color: var(--ink-2); font-size: 13px; margin: 6px 0 10px; }
.table-wrap { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; font-variant-numeric: tabular-nums; }
th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--grid); vertical-align: top; }
th { color: var(--ink-2); font-weight: 600; font-size: 13px; }
td.num, th.num { text-align: right; }
td.url { word-break: break-all; }
details summary { cursor: pointer; color: var(--ink-2); margin-top: 8px; }
.empty { color: var(--muted); padding: 12px 0; }
#tip { position: fixed; pointer-events: none; z-index: 10; display: none; padding: 6px 10px; border-radius: 6px;
  background: var(--surface); color: var(--ink); border: 1px solid var(--ring);
  box-shadow: 0 4px 16px rgba(0,0,0,.18); font-size: 13px; max-width: 360px; }
"""

# Tooltips: every chart item carries data-tip; one floating box follows the pointer
JS = """
const tip = document.getElementById('tip');
document.querySelectorAll('[data-tip]').forEach(el => {
  el.addEventListener('pointermove', e => {
    tip.textContent = el.dataset.tip; tip.style.display = 'block';
    const x = Math.min(e.clientX + 14, innerWidth - tip.offsetWidth - 8);
    tip.style.left = x + 'px'; tip.style.top = (e.clientY + 14) + 'px';
  });
  el.addEventListener('pointerleave', () => { tip.style.display = 'none'; });
});
"""


def compact(n: float) -> str:
    """1284 -> "1,284", 12900 -> "12.9K", 4200000 -> "4.2M"."""
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if abs(n) >= 10_000:
        return f"{n / 1000:.1f}K".replace(".0K", "K")
    if isinstance(n, float) and not n.is_integer():
        return f"{n:,.2f}"
    return f"{int(n):,}"


def human_bytes(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def human_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, s = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {s:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def nice_max(value: float) -> float:
    """Round an axis maximum up to 1, 2 or 5 x 10^k."""
    if value <= 0:
        return 1
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 2, 5, 10):
        if value <= step * magnitude:
            return step * magnitude
    return 10 * magnitude


def _truncate(text: str, limit: int = 30) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _column(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Path of a column with a rounded top and a square base at y + h."""
    r = min(r, w / 2, h)
    if r <= 0 or w <= 0 or h <= 0:
        return f"M{x},{y}h{max(w, 0)}v{max(h, 0)}h{-max(w, 0)}Z"
    return (f"M{x},{y + h}v{-(h - r)}a{r},{r} 0 0 1 {r},{-r}h{w - 2 * r}"
            f"a{r},{r} 0 0 1 {r},{r}v{h - r}Z")


def bar_chart(items: list[tuple[str, float, str, str]], unit: str = "") -> str:
    """Horizontal bars: items are (label, value, color css var, tooltip).
    HTML rows rather than SVG, so the text keeps its size in narrow cards."""
    if not items:
        return '<p class="empty">No data</p>'
    top = max(v for _, v, _, _ in items) or 1
    rows = []
    for label, value, color, tip in items:
        width = max(100 * value / top, 0.5 if value else 0)
        rows.append(
            f'<div class="bar-row" data-tip="{escape(tip)}">'
            f'<span class="bar-label" title="{escape(label)}">{escape(_truncate(label))}</span>'
            f'<span class="bar-track"><span class="bar" style="width:{width:.2f}%;background:var({color})"></span>'
            f'<span class="bar-value">{escape(compact(value))}{unit}</span></span></div>'
        )
    return f'<div class="bars">{"".join(rows)}</div>'


def column_chart(values: list[int], x_label: str, tip_fmt: str) -> str:
    """Vertical columns over time (one per second or per bucket of seconds)."""
    if not values or not any(values):
        return '<p class="empty">No data</p>'
    # At most ~60 columns: longer runs are grouped into buckets of seconds
    bucket = max(1, -(-len(values) // 60))
    cols = [sum(values[i:i + bucket]) / bucket for i in range(0, len(values), bucket)]
    left, right, top_pad, bottom = 44, 12, 12, 30
    h = 200
    plot_w = CHART_W - left - right
    plot_h = h - top_pad - bottom
    top = nice_max(max(cols))
    slot = plot_w / len(cols)
    col_w = max(min(24, slot - 2), 1)  # 2px surface gap between neighbours
    parts = [f'<svg viewBox="0 0 {CHART_W} {h}" role="img">']
    for k in range(5):  # gridlines and ticks
        v = top * k / 4
        y = top_pad + plot_h - plot_h * k / 4
        cls = "baseline" if k == 0 else "gridline"
        parts.append(f'<line class="{cls}" x1="{left}" y1="{y}" x2="{CHART_W - right}" y2="{y}"/>')
        parts.append(f'<text class="tick" x="{left - 6}" y="{y + 4}" text-anchor="end">{v:g}</text>')
    for i, value in enumerate(cols):
        x = left + i * slot + (slot - col_w) / 2
        ch = plot_h * value / top
        y = top_pad + plot_h - ch
        start, end = i * bucket, min((i + 1) * bucket, len(values))
        span = f"{start}s" if bucket == 1 else f"{start}–{end}s"
        tip = tip_fmt.format(span=span, value=compact(round(value, 2)))
        parts.append(f'<g class="item" data-tip="{escape(tip)}">')
        parts.append(f'<rect class="hit" x="{left + i * slot}" y="{top_pad}" width="{slot}" height="{plot_h}"/>')
        if value:
            parts.append(f'<path class="mark" fill="var(--series-1)" '
                         f'd="{_column(x, y, col_w, ch)}"/>')
        parts.append("</g>")
    # A handful of x ticks: start, middle, end
    for i in sorted({0, len(cols) // 2, len(cols) - 1}):
        x = left + i * slot + slot / 2
        parts.append(f'<text class="tick" x="{x}" y="{h - 10}" text-anchor="middle">{i * bucket}s</text>')
    parts.append("</svg>")
    note = f'<p class="note">{escape(x_label)}' + (f", averaged over {bucket}s buckets" if bucket > 1 else "") + "</p>"
    return note + "".join(parts)


def _table(headers: list[str], rows: list[list], numeric: set[int] = frozenset(), url_cols: set[int] = frozenset()) -> str:
    if not rows:
        return '<p class="empty">Nothing to show</p>'
    head = "".join(f'<th class="{"num" if i in numeric else ""}">{escape(h)}</th>' for i, h in enumerate(headers))
    body = []
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            cls = "num" if i in numeric else "url" if i in url_cols else ""
            cells.append(f'<td class="{cls}">{escape(str(cell))}</td>')
        body.append("<tr>" + "".join(cells) + "</tr>")
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>'


def _tile(label: str, value: str, meta: str = "", hero: bool = False) -> str:
    return (f'<div class="tile"><div class="label">{escape(label)}</div>'
            f'<div class="value{" hero" if hero else ""}">{escape(value)}</div>'
            + (f'<div class="meta">{meta}</div>' if meta else "") + "</div>")


def _status_role(code: str) -> tuple[str, str]:
    return STATUS_CLASSES.get(code[:1], ("critical", "no response"))


def _card(title: str, body: str, note: str = "") -> str:
    return f'<section class="card"><h2>{escape(title)}</h2>' + (f'<p class="note">{escape(note)}</p>' if note else "") + body + "</section>"


def render_html_report(stats: dict, title: str = "Crawl report", extra: dict | None = None) -> str:
    """The report page for CrawlerStats.to_dict() output.

    extra may hold "failed_urls" ({url: error}), "config" (a flat dict of
    settings shown as a table) and "start_urls".
    """
    extra = extra or {}
    total = stats["total_pages"]
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    success_pct = f"{stats['success_rate'] * 100:.1f}% of requests"
    tiles = "".join([
        _tile("Pages processed", compact(total), f"{compact(stats['blocked'])} blocked by robots.txt", hero=True),
        _tile("Successful", compact(stats["successful"]),
              f'<span class="status"><span class="dot" style="background:var(--good)"></span>{success_pct}</span>'),
        _tile("Failed", compact(stats["failed"]),
              f'<span class="status"><span class="dot" style="background:var(--critical)"></span>'
              f'{len(stats["errors_by_type"])} error type{"" if len(stats["errors_by_type"]) == 1 else "s"}</span>'),
        _tile("Average speed", f"{stats['avg_speed']:.2f}", "pages per second"),
        _tile("Run time", human_duration(stats["uptime"]), escape(stats.get("started_at") or "")[:19].replace("T", " ")),
        _tile("Avg response", f"{stats['avg_response_time'] * 1000:.0f} ms", f"{compact(stats['attempts'])} requests incl. retries"),
        _tile("Downloaded", human_bytes(stats["bytes_downloaded"]), f"{compact(stats['domains_count'])} domain{'' if stats['domains_count'] == 1 else 's'}"),
    ])

    # Status codes: colored by class, the code itself is the label
    codes = stats["status_codes"]
    code_items = []
    for code, count in sorted(codes.items(), key=lambda kv: (-kv[1], kv[0])):
        role, text = _status_role(code)
        name = "no response" if code == "none" else code
        code_items.append((name, count, f"--{role}", f"{name} ({text}): {count} pages, {count / total:.1%}" if total else name))
    used_roles = {_status_role(c) for c in codes}
    legend = "".join(
        f'<span class="status"><span class="dot" style="background:var(--{role})"></span>{escape(text)}</span>'
        for role, text in [*STATUS_CLASSES.values(), ("critical", "no response")]
        if (role, text) in used_roles
    )
    status_card = _card(
        "Status codes", f'<div class="legend">{legend}</div>' + bar_chart(code_items)
    )

    domains = stats["top_domains"]
    domain_items = [
        (d["domain"], d["pages"], "--series-1", f"{d['domain']}: {d['pages']} pages, {d['failed']} failed")
        for d in domains
    ]
    domains_card = _card(
        "Top domains", bar_chart(domain_items),
        f"by pages requested, {len(domains)} of {stats['domains_count']}",
    )

    timeline_card = _card(
        "Throughput over time",
        column_chart(stats["timeline"], "Pages finished per second of the run", "{span}: {value} pages/s"),
    )

    errors = stats["errors_by_type"]
    error_items = [(name, n, "--series-1", f"{name}: {n} pages") for name, n in errors.items()]
    errors_card = _card("Errors by type", bar_chart(error_items) if errors else '<p class="empty">No errors</p>')

    depths = stats["depths"]
    depth_items = [(f"depth {d}", n, "--series-1", f"depth {d}: {n} pages") for d, n in depths.items()]
    depth_card = _card("Pages by depth", bar_chart(depth_items))

    tables = [
        _card("Domains", _table(
            ["Domain", "Pages", "Failed", "Share"],
            [[d["domain"], d["pages"], d["failed"], f"{d['pages'] / total:.1%}" if total else "-"] for d in domains],
            numeric={1, 2, 3},
        )),
        _card("Status codes (table)", _table(
            ["Status", "Pages", "Share"],
            [[c, n, f"{n / total:.1%}" if total else "-"] for c, n in codes.items()],
            numeric={1, 2},
        )),
        _card("Slowest pages", _table(
            ["URL", "Time, s"], [[s["url"], f"{s['elapsed']:.3f}"] for s in stats["slowest"]],
            numeric={1}, url_cols={0},
        )),
    ]
    failed = extra.get("failed_urls") or {}
    if failed:
        rows = [[url, error] for url, error in list(failed.items())[:200]]
        more = f" (first 200 of {len(failed)})" if len(failed) > 200 else ""
        tables.append(_card(f"Failed URLs{more}", _table(["URL", "Error"], rows, url_cols={0})))
    config = extra.get("config") or {}
    if config:
        tables.append(_card("Settings", _table(["Option", "Value"], [[k, v] for k, v in config.items()])))

    start_urls = extra.get("start_urls") or []
    subtitle = f"Generated {generated}"
    if start_urls:
        shown = ", ".join(start_urls[:3]) + (f" and {len(start_urls) - 3} more" if len(start_urls) > 3 else "")
        subtitle += f" · start: {shown}"

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<main>
<h1>{escape(title)}</h1>
<p class="sub">{escape(subtitle)}</p>
<div class="tiles">{tiles}</div>
{timeline_card}
<div class="grid2">{status_card}{domains_card}{errors_card}{depth_card}</div>
{"".join(tables)}
</main>
<div id="tip"></div>
<script>{JS}</script>
</body>
</html>
"""

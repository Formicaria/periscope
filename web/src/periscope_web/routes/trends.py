"""/trends: what the event log remembers — uptime per service, how often things alerted, the numbers as
sparklines, and every event in a table you can filter and download.

Everything on this page is read back out of `runtime.history` (`periscope.history`). When the log is not
available the runtime hands out the no-op one instead, so the page still renders — it just says there is
nothing to show yet, which is also what a fresh install looks like.

The charts are drawn here rather than in the browser: `sparkline()` turns a series into the two path strings
the template puts inside an `<svg>`, so there is no chart library and nothing to load.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from periscope.history import ALERT_KINDS, DOWN_KINDS, UP_KINDS
from periscope.hooks import NullHistory

from ..render import partial, render

log = logging.getLogger(__name__)
router = APIRouter()

DAY = 86400
WINDOWS = [("24h", 1.0), ("7d", 7.0), ("30d", 30.0)]      # the three spans every tile is measured over
DEFAULT_DAYS = 1.0
TABLE_LIMIT = 200                                          # rows in the table; the CSV takes the lot
CSV_LIMIT = 5000
MAX_KEYS = 8                                               # things listed per service card before the rest
MAX_SPARKS = 4                                             # metrics charted per service card
SEVERITY_BADGE = {"ok": "badge-success", "info": "badge-info", "warning": "badge-warning",
                  "critical": "badge-error"}
MARK_KINDS = (*UP_KINDS, *DOWN_KINDS)


def history_of(request: Request) -> Any:
    """The runtime's event log, or the no-op one when this runtime has none (a bare install, a test)."""
    return getattr(request.app.state.runtime, "history", None) or NullHistory()


def service_names(request: Request) -> list[str]:
    """Every service periscope knows about: installed first, then anything only the config mentions."""
    runtime = request.app.state.runtime
    extra = [n for n in runtime.store.services if n not in runtime.specs]
    return list(runtime.specs) + extra


def clock(ts: float | None) -> str:
    if not ts:
        return "—"
    return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def days_of(raw: Any, fallback: float = DEFAULT_DAYS) -> float:
    """The window a query asked for, clamped to something a chart can actually draw."""
    try:
        days = float(raw)
    except (TypeError, ValueError):
        return fallback
    return min(400.0, max(0.04, days))


def bucket_for(days: float) -> float:
    """How wide one point on a sparkline is: about sixty of them across whatever window was asked for."""
    return max(300.0, round(days * DAY / 60 / 300) * 300)


def sparkline(points: list[tuple[float, float]], *, width: int = 240, height: int = 40,
              pad: float = 3.0) -> dict[str, Any]:
    """A series as two SVG paths — the line and the area under it — plus the numbers worth printing.

    The box is `width` × `height` in its own coordinates and the `<svg>` scales it to fit, so nothing here
    depends on how wide the card ends up being."""
    values = [v for _, v in points]
    if not values:
        return {"empty": True, "n": 0}
    low, high = min(values), max(values)
    inner_w, inner_h = width - 2 * pad, height - 2 * pad
    step = inner_w / (len(values) - 1) if len(values) > 1 else 0.0
    if high == low:                                        # every reading the same: draw it down the middle
        coords = [(pad + i * step, pad + inner_h / 2) for i in range(len(values))]
    else:
        coords = [(pad + i * step, pad + inner_h - (v - low) / (high - low) * inner_h)
                  for i, v in enumerate(values)]
    if len(coords) == 1:                                   # one reading: a flat line across the box, so it shows
        coords = [(pad, pad + inner_h / 2), (width - pad, pad + inner_h / 2)]
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area = f"{line} L{coords[-1][0]:.1f},{height - pad:.1f} L{coords[0][0]:.1f},{height - pad:.1f} Z"
    return {"empty": False, "n": len(values), "line": line, "area": area, "width": width, "height": height,
            "min": low, "max": high, "last": values[-1], "avg": sum(values) / len(values),
            "dot": {"x": coords[-1][0], "y": coords[-1][1]},
            "from": clock(points[0][0]), "to": clock(points[-1][0])}


def uptime_rows(history: Any, service: str, since: float, until: float) -> list[dict[str, Any]]:
    """One row per thing this service reports up and down, newest events first, with its uptime for the window."""
    keys = history.counts(since=since, until=until, service=service, kind=MARK_KINDS, by="key")
    rows = []
    for key in list(keys)[:MAX_KEYS]:
        percent = history.uptime(service=service, key=key, since=since, until=until)
        if percent is None:
            continue
        rows.append({"key": key or "—", "uptime": percent, "changes": keys[key],
                     "badge": "badge-success" if percent >= 99 else
                              "badge-warning" if percent >= 95 else "badge-error"})
    return sorted(rows, key=lambda r: r["uptime"])


def spark_cards(history: Any, service: str, since: float, until: float, days: float) -> list[dict[str, Any]]:
    """The numbers this service has actually written, each as a small chart."""
    metrics = history.counts(of="samples", since=since, until=until, service=service, by="metric")
    out = []
    for metric in list(metrics)[:MAX_SPARKS]:
        points = history.series(service=service, metric=metric, since=since, until=until,
                                bucket=bucket_for(days))
        if not points:
            continue
        out.append({"metric": metric, "chart": sparkline(points)})
    return out


def service_tiles(request: Request, since: float, until: float, days: float) -> list[dict[str, Any]]:
    """One tile per service that has anything in the log: uptime, alert counts, and its charts."""
    history = history_of(request)
    runtime = request.app.state.runtime
    alerts = {label: history.counts(since=until - span * DAY, until=until, kind=ALERT_KINDS, by="service")
              for label, span in WINDOWS}
    events = history.counts(since=since, until=until, by="service")
    tiles = []
    for name in service_names(request):
        spec = runtime.specs.get(name)
        rows = uptime_rows(history, name, since, until)
        charts = spark_cards(history, name, since, until, days)
        total = events.get(name, 0)
        if not rows and not charts and not total:
            continue                                       # nothing has been written about this one yet
        overall = round(sum(r["uptime"] for r in rows) / len(rows), 2) if rows else None
        tiles.append({
            "name": name, "title": spec.title if spec else name, "events": total,
            "uptime": overall, "rows": rows, "charts": charts,
            "alerts": [{"label": label, "n": alerts[label].get(name, 0)} for label, _ in WINDOWS],
            "badge": "badge-ghost" if overall is None else
                     "badge-success" if overall >= 99 else "badge-warning" if overall >= 95 else "badge-error",
        })
    return tiles


def filters_from(request: Request) -> dict[str, Any]:
    """The table's filters as the query string left them — the same shape the CSV link rebuilds."""
    q = request.query_params
    return {"service": (q.get("service") or "").strip(), "kind": (q.get("kind") or "").strip(),
            "severity": (q.get("severity") or "").strip(), "search": (q.get("q") or "").strip(),
            "days": days_of(q.get("days"))}


def event_rows(history: Any, picked: dict[str, Any], *, limit: int) -> list[dict[str, Any]]:
    until = time.time()
    rows = history.events(since=until - picked["days"] * DAY, until=until, service=picked["service"] or None,
                          kind=picked["kind"] or None, severity=picked["severity"] or None,
                          search=picked["search"], limit=limit)
    for row in rows:
        row["when"] = clock(row.get("ts"))
        row["badge"] = SEVERITY_BADGE.get(str(row.get("severity") or ""), "badge-ghost")
    return rows


def table_ctx(request: Request) -> dict[str, Any]:
    """Everything the events table needs: the rows, the pick-lists, and the link that downloads the same set."""
    history = history_of(request)
    picked = filters_from(request)
    rows = event_rows(history, picked, limit=TABLE_LIMIT)
    until = time.time()
    since = until - picked["days"] * DAY
    query = [(name, picked[name]) for name in ("service", "kind", "severity") if picked[name]]
    query += [("q", picked["search"])] if picked["search"] else []
    query += [("days", f"{picked['days']:g}")]
    return {
        "rows": rows, "picked": picked, "windows": WINDOWS, "truncated": len(rows) >= TABLE_LIMIT,
        "services": sorted(history.counts(since=since, until=until, by="service")),
        "kinds": sorted(history.counts(since=since, until=until, by="kind")),
        "severities": sorted(history.counts(since=since, until=until, by="severity")),
        "csv_href": "/trends/events.csv?" + "&".join(f"{k}={v}" for k, v in query),
    }


@router.get("/trends")
async def trends_page(request: Request):
    """The whole page: the tiles for the window that was picked, and the table below them."""
    days = days_of(request.query_params.get("days"))
    until = time.time()
    since = until - days * DAY
    history = history_of(request)
    tiles = service_tiles(request, since, until, days)
    ctx = table_ctx(request)
    ctx.update({
        "tiles": tiles, "days": days, "windows": WINDOWS,
        "enabled": bool(getattr(history, "enabled", False)),
        "total": sum(history.counts(since=since, until=until).values()),
        "since_text": clock(since), "until_text": clock(until),
    })
    return render(request, "trends.html", ctx)


@router.get("/trends/events")
async def trends_events(request: Request):
    """Just the table — what the filters swap in through HTMX."""
    return partial(request, "partials/trend_events.html", table_ctx(request))


@router.get("/trends/events.csv")
async def trends_csv(request: Request):
    """The filtered events as a spreadsheet, up to `CSV_LIMIT` rows."""
    picked = filters_from(request)
    rows = event_rows(history_of(request), picked, limit=CSV_LIMIT)
    buf = io.StringIO()
    out = csv.writer(buf)
    out.writerow(["when", "server", "service", "kind", "key", "severity", "title", "detail", "value"])
    for row in rows:
        out.writerow([row["when"], row["server"], row["service"], row["kind"], row["key"], row["severity"],
                      row["title"], row["detail"], "" if row["value"] is None else row["value"]])
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d")
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="periscope-events-{stamp}.csv"'})

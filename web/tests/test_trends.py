"""/trends: the page reads the event log back — uptime per service, alert counts, hand-drawn sparklines, and a
filterable table you can download. It has to render on a runtime with no log at all (a bare install) as
happily as on one with a night's worth of events in it."""

from __future__ import annotations

import time

import pytest
from periscope.history import History
from periscope_web.routes import trends
from periscope_web.routes.trends import bucket_for, days_of, sparkline

HX = {"HX-Request": "true"}
HOUR = 3600
DAY = 86400


@pytest.fixture
def app(make_app):
    """The web app with the Trends router mounted, whether or not it is registered by default yet."""
    application = make_app()
    if not any(getattr(route, "path", "") == "/trends" for route in application.routes):
        application.include_router(trends.router)
    return application


@pytest.fixture
def log(runtime, tmp_path):
    """A real event log hung on the runtime, the way `periscope.hooks.history_for` hangs one on the real one."""
    h = History(tmp_path / "data" / "history.db", retention_days=30)
    runtime.history = h
    yield h
    h.close()


def seed(h: History, now: float) -> None:
    """A container that fell over for an hour, a proxmox alert nobody closed, and a night of numbers."""
    h.record(service="github", kind="down", key="anthill", severity="critical", title="anthill is stopped",
             detail="exit code 1", server="testlab", at=now - 6 * HOUR)
    h.record(service="github", kind="up", key="anthill", severity="ok", title="anthill is running again",
             server="testlab", at=now - 5 * HOUR)
    h.record(service="pve", kind="alert", key="pve1:cpu", severity="warning", title="High CPU on pve1",
             at=now - 4 * HOUR)
    h.record(service="pve", kind="feed", key="pve1", severity="info", title="a note about pve1",
             at=now - 3 * HOUR)
    for i in range(12):
        h.sample(service="pve", metric="cpu", value=40 + i * 3, key="pve1", at=now - (12 - i) * HOUR)
        h.sample(service="pve", metric="mem", value=60.0, key="pve1", at=now - (12 - i) * HOUR)


# ----- the drawing is arithmetic, so it can be checked on its own ---------------------------------------
def test_a_sparkline_is_two_paths_and_the_numbers_worth_printing():
    chart = sparkline([(0.0, 10.0), (60.0, 20.0), (120.0, 0.0)], width=100, height=20, pad=0)
    assert chart["empty"] is False and chart["n"] == 3
    assert chart["min"] == 0.0 and chart["max"] == 20.0 and chart["last"] == 0.0
    assert chart["avg"] == pytest.approx(10.0)
    assert chart["line"] == "M0.0,10.0 L50.0,0.0 L100.0,20.0"       # highest reading sits at the top
    assert chart["area"].startswith(chart["line"]) and chart["area"].endswith("Z")
    assert (chart["dot"]["x"], chart["dot"]["y"]) == (100.0, 20.0)


def test_a_sparkline_of_one_reading_is_drawn_flat_and_of_none_is_empty():
    flat = sparkline([(0.0, 7.0)], width=100, height=20, pad=0)
    assert flat["line"] == "M0.0,10.0 L100.0,10.0" and flat["min"] == flat["max"] == 7.0
    assert sparkline([])["empty"] is True


def test_a_flat_series_does_not_divide_by_zero():
    chart = sparkline([(0.0, 5.0), (1.0, 5.0), (2.0, 5.0)], width=100, height=20, pad=0)
    assert chart["line"] == "M0.0,10.0 L50.0,10.0 L100.0,10.0"      # down the middle, not pinned to an edge


def test_the_window_is_clamped_and_the_buckets_follow_it():
    assert days_of("7") == 7.0 and days_of(None) == 1.0 and days_of("nonsense") == 1.0
    assert days_of("0") == 0.04 and days_of("9999") == 400.0
    assert bucket_for(1.0) == 1500.0 and bucket_for(30.0) == 43200.0
    assert bucket_for(0.04) == 300.0                                # never finer than five minutes


# ----- the page ------------------------------------------------------------------------------------------
async def test_the_page_renders_when_nothing_has_been_recorded(client):
    r = await client.get("/trends")
    assert r.status_code == 200
    assert "Nothing is being recorded yet" in r.text
    assert "nothing matches" in r.text                               # the table says so rather than breaking
    assert "Download as CSV" in r.text


async def test_the_page_says_so_when_the_log_is_on_but_the_window_is_empty(client, log):
    r = await client.get("/trends?days=1")
    assert r.status_code == 200
    assert "Nothing is being recorded yet" not in r.text
    assert "Nothing happened in this window" in r.text


async def test_the_page_shows_uptime_alert_counts_and_charts(client, log):
    seed(log, time.time())
    r = await client.get("/trends?days=1")
    assert r.status_code == 200
    html = r.text
    assert "GitHub" in html and "Proxmox VE" in html                 # a tile per service that wrote something
    assert "95.83% up" in html                                       # one hour down out of twenty-four
    assert "anthill" in html and "badge-warning" in html
    assert "alerts · last 24h" in html and "alerts · last 30d" in html
    assert "<svg" in html and 'preserveAspectRatio="none"' in html   # the sparklines, drawn here not loaded
    assert html.count("<svg") >= 2 and ">cpu<" in html and ">mem<" in html
    assert "High CPU on pve1" in html                                # and the events table below


async def test_the_window_buttons_change_what_is_measured(client, log):
    now = time.time()
    log.record(service="pve", kind="alert", key="old", severity="warning", title="last week", at=now - 5 * DAY)
    day = await client.get("/trends?days=1")
    week = await client.get("/trends?days=7")
    assert "last week" not in day.text and "last week" in week.text
    assert 'href="/trends?days=7.0"' in day.text


async def test_a_service_with_nothing_in_the_log_gets_no_tile(client, log):
    seed(log, time.time())
    html = (await client.get("/trends?days=1")).text
    assert "Sonarr" not in html                                      # configured, but it has written nothing


# ----- the table -----------------------------------------------------------------------------------------
async def test_the_table_filters_on_service_kind_severity_and_text(client, log):
    seed(log, time.time())
    async def rows(query: str) -> str:
        r = await client.get(f"/trends/events?{query}", headers=HX)
        assert r.status_code == 200
        return r.text

    assert "High CPU on pve1" in await rows("service=pve")
    assert "anthill is stopped" not in await rows("service=pve")
    assert "anthill is stopped" in await rows("kind=down")
    assert "High CPU on pve1" not in await rows("kind=down")
    assert "anthill is stopped" in await rows("severity=critical")
    assert "a note about pve1" in await rows("q=note")
    assert "High CPU on pve1" not in await rows("q=note")
    assert "nothing matches" in await rows("service=nobody")


async def test_the_table_partial_keeps_the_filters_it_was_given(client, log):
    seed(log, time.time())
    html = await (await client.get("/trends/events?service=pve&kind=alert&days=7", headers=HX)).aread()
    text = html.decode()
    assert '<option value="pve" selected>' in text and '<option value="alert" selected>' in text
    assert '<option value="7.0" selected>' in text
    assert "/trends/events.csv?service=pve&amp;kind=alert&amp;days=7" in text


async def test_the_events_download_is_a_csv_of_what_the_filters_picked(client, log):
    seed(log, time.time())
    r = await client.get("/trends/events.csv?service=github&days=1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment; filename=" in r.headers["content-disposition"]
    lines = [line for line in r.text.splitlines() if line]
    assert lines[0] == "when,server,service,kind,key,severity,title,detail,value"
    assert len(lines) == 3                                           # the header plus github's two events
    assert all(",github," in line for line in lines[1:])
    assert "anthill is stopped" in r.text and "exit code 1" in r.text
    assert "High CPU on pve1" not in r.text


async def test_the_download_works_with_no_log_at_all(client):
    r = await client.get("/trends/events.csv")
    assert r.status_code == 200 and r.text.strip() == "when,server,service,kind,key,severity,title,detail,value"


async def test_the_page_needs_a_signed_in_user(anon):
    r = await anon.get("/trends")
    assert r.status_code in (302, 303, 307, 401)
    assert "/login" in r.headers.get("location", "/login")

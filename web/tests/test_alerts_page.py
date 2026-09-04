"""/alerts: the open alerts and their buttons, and the quiet-times editor that writes config/maintenance.yaml.

The alert routers are stand-ins — the page only ever asks them for `snapshot()` and calls `ack`/`snooze`/
`resolve` — but the maintenance windows are the real thing, reading and writing a real file in tmp_path.
"""

from __future__ import annotations

import time

import pytest
import yaml
from periscope.maintenance import Windows
from periscope_web.routes import alerts as alerts_routes

HX = {"HX-Request": "true"}


# ----- doubles ----------------------------------------------------------------------------------------------
def row(fingerprint, title, severity="warning", **kw):
    base = {"fingerprint": fingerprint, "title": title, "severity": severity, "count": 1,
            "since": time.time() - 600, "acked_by": "", "acked_ts": None, "snoozed_until": None,
            "snoozed_by": "", "escalated": False, "suppressed": False, "suppressed_reason": "",
            "state": "firing", "lines": ["🔔 Firing — nobody has acked it yet"], "service": ""}
    base.update(kw)
    return base


class FakeRouter:
    """What `bot.alerts` looks like from the web UI: something that can list and be told what to do."""

    def __init__(self, *rows):
        self.rows = list(rows)
        self.calls: list[tuple] = []

    def snapshot(self):
        return [dict(r) for r in self.rows]

    def _find(self, fingerprint):
        return next((r for r in self.rows if r["fingerprint"] == fingerprint), None)

    async def ack(self, fingerprint, who=""):
        self.calls.append(("ack", fingerprint, who))
        found = self._find(fingerprint)
        if found is None:
            return False
        found.update({"acked_by": who, "acked_ts": time.time(), "state": "acked",
                      "lines": [f"✅ Acked by {who} — no more pings"]})
        return True

    async def snooze(self, fingerprint, hours, who=""):
        self.calls.append(("snooze", fingerprint, hours, who))
        found = self._find(fingerprint)
        if found is None:
            return False
        found.update({"snoozed_until": time.time() + hours * 3600, "snoozed_by": who, "state": "snoozed"})
        return True

    async def resolve(self, fingerprint, note=None, by=""):
        self.calls.append(("resolve", fingerprint, note, by))
        found = self._find(fingerprint)
        if found is None:
            return False
        self.rows.remove(found)
        return True


class Exploding:
    def snapshot(self):
        raise RuntimeError("this service's state is unreadable")


@pytest.fixture(autouse=True)
def mounted(app):
    """Use the page's router whether or not it is wired into routes/register() yet."""
    if not any(getattr(r, "path", "") == "/alerts" for r in app.routes):
        app.include_router(alerts_routes.router)
    return app


@pytest.fixture
def live(runtime):
    """Two services with something open: one critical, one already acked."""
    pve = FakeRouter(row("pve:node:pve1:cpu", "High CPU on pve1", "critical", count=4),
                     row("pve:node:pve2:disk", "Disk nearly full on pve2", acked_by="Bob", state="acked",
                         lines=["✅ Acked by Bob at 09:12 — no more pings"]))
    github = FakeRouter(row("github:rate_limit", "GitHub rate limit is low", "info"))
    runtime.services = {"pve": type("Bot", (), {"alerts": pve})(), "github": type("Bot", (), {"alerts": github})()}
    return {"pve": pve, "github": github}


def window_file(store):
    return store.path.parent / "maintenance.yaml"


def written(store) -> dict:
    return yaml.safe_load(window_file(store).read_text()) or {}


# ----- what is firing ---------------------------------------------------------------------------------------
async def test_the_page_lists_every_open_alert_with_its_state(client, live):
    r = await client.get("/alerts")
    assert r.status_code == 200
    html = r.text
    assert "High CPU on pve1" in html and "Disk nearly full on pve2" in html and "GitHub rate limit is low" in html
    assert 'id="alert-pve-pve-node-pve1-cpu"' in html                      # one card per fingerprint
    assert "seen 4 times" in html and "open for" in html
    assert ">critical<" in html and ">acked<" in html and ">firing<" in html
    assert "acked by Bob" in html
    assert html.index("High CPU on pve1") < html.index("GitHub rate limit is low")   # worst first
    assert "Ack" in html and "Snooze" in html and "Resolve" in html


async def test_with_nothing_running_the_page_still_works(client):
    r = await client.get("/alerts")
    assert r.status_code == 200
    assert "No services are running yet" in r.text and "Add a quiet time" in r.text


async def test_a_service_that_cannot_be_read_does_not_empty_the_page(client, runtime, live):
    runtime.services["broken"] = type("Bot", (), {"alerts": Exploding()})()
    r = await client.get("/alerts")
    assert r.status_code == 200 and "High CPU on pve1" in r.text


# ----- the buttons ------------------------------------------------------------------------------------------
async def test_ack_snooze_and_resolve_from_the_browser(client, live):
    form = {"service": "pve", "fingerprint": "pve:node:pve1:cpu"}
    r = await client.post("/alerts/ack", data=form, headers=HX)
    assert r.status_code == 200 and live["pve"].calls[0] == ("ack", "pve:node:pve1:cpu", "Alice")
    assert "Acked by Alice" in r.text and "the pings stop" in r.text          # card + toast

    r = await client.post("/alerts/snooze", data={**form, "hours": "8"}, headers=HX)
    assert r.status_code == 200 and live["pve"].calls[1] == ("snooze", "pve:node:pve1:cpu", 8, "Alice")
    assert "snoozed for 8h" in r.text

    r = await client.post("/alerts/resolve", data=form, headers=HX)
    assert r.status_code == 200 and live["pve"].calls[2][0] == "resolve"
    assert live["pve"].calls[2][3] == "Alice" and "Closed by hand by Alice" in live["pve"].calls[2][2]
    assert "High CPU on pve1" not in r.text and "went green" in r.text        # gone from the list


async def test_acting_on_something_that_is_not_there(client, live):
    r = await client.post("/alerts/ack", data={"service": "pve", "fingerprint": "pve:gone"}, headers=HX)
    assert r.status_code == 200 and "already closed" in r.text
    r = await client.post("/alerts/ack", data={"service": "nope", "fingerprint": "x"}, headers=HX)
    assert r.status_code == 404 and "not running" in r.text
    r = await client.post("/alerts/ack", data={"service": "pve"}, headers=HX)
    assert r.status_code == 422 and "which alert" in r.text
    r = await client.post("/alerts/snooze", data={"service": "pve", "fingerprint": "pve:node:pve1:cpu",
                                                  "hours": "not a number"}, headers=HX)
    assert r.status_code == 200 and live["pve"].calls[-1][2] == 1             # unreadable length → one hour


# ----- the quiet times --------------------------------------------------------------------------------------
async def test_adding_a_weekly_quiet_time_writes_the_file(client, store):
    form = {"kind": "repeat", "reason": "Nightly backups", "days": ["mon", "tue"], "start": "01:00",
            "end": "04:30", "services": ["pve"], "servers": ["testlab"], "keys": "pve:node:*"}
    r = await client.post("/alerts/windows", data=form, headers=HX)
    assert r.status_code == 200 and "quiet time added" in r.text
    assert "Nightly backups" in r.text and "Mon Tue, 01:00–04:30" in r.text
    assert "pve on testlab" in r.text

    data = written(store)
    assert data["version"] == 1
    (win,) = data["windows"]
    assert win == {"id": "nightly-backups", "reason": "Nightly backups", "days": ["mon", "tue"],
                   "start": "01:00", "end": "04:30", "servers": ["testlab"], "services": ["pve"],
                   "keys": ["pve:node:*"]}
    # what the bots will read back out of it
    assert Windows(window_file(store)).errors == []

    r = await client.get("/alerts")
    assert "Nightly backups" in r.text and 'id="window-nightly-backups"' in r.text


async def test_a_one_off_quiet_time_names_two_moments(client, store):
    form = {"kind": "once", "reason": "Rebuilding pve1", "start_at": "2099-09-06T20:00",
            "end_at": "2099-09-07T02:00"}
    r = await client.post("/alerts/windows", data=form, headers=HX)
    assert r.status_code == 200 and "once:" in r.text
    (win,) = written(store)["windows"]
    assert win["start"] == "2099-09-06T20:00" and win["end"] == "2099-09-07T02:00" and "days" not in win
    assert win["reason"] == "Rebuilding pve1"


async def test_a_quiet_time_can_be_switched_off_and_removed(client, store):
    await client.post("/alerts/windows", data={"reason": "Nightly backups", "start": "01:00", "end": "04:30"},
                      headers=HX)
    r = await client.post("/alerts/windows/nightly-backups/toggle", headers=HX)
    assert r.status_code == 200 and written(store)["windows"][0]["enabled"] is False
    r = await client.post("/alerts/windows/nightly-backups/toggle", headers=HX)
    assert "enabled" not in written(store)["windows"][0]                      # on is the default; not written

    r = await client.post("/alerts/windows/nightly-backups/delete", headers=HX)
    assert r.status_code == 200 and written(store)["windows"] == []
    assert "No quiet times yet" in r.text
    r = await client.post("/alerts/windows/nothing-like-this/delete", headers=HX)
    assert r.status_code == 404


async def test_a_window_that_cannot_work_is_refused_in_plain_language(client, store):
    r = await client.post("/alerts/windows", data={"reason": "Nonsense", "start": "tea time", "end": "later"},
                          headers=HX)
    assert r.status_code == 422 and "clock times like 01:00" in r.text
    r = await client.post("/alerts/windows", data={"reason": "", "start": "01:00", "end": "02:00"}, headers=HX)
    assert r.status_code == 422 and "give the window a reason" in r.text
    assert not window_file(store).exists()


async def test_the_global_quiet_switch(client, store):
    r = await client.post("/alerts/quiet", data={"until": "2099-01-01T02:00", "reason": "Rack power work"},
                          headers=HX)
    assert r.status_code == 200 and "everything is quiet until" in r.text
    assert written(store)["quiet_until"] == "2099-01-01T02:00"
    assert Windows(window_file(store)).quiet("anything", server="anywhere", key="any:fp") is True
    assert "quiet now" in r.text and "Rack power work" in r.text and "Turn alerts back on" in r.text

    r = await client.post("/alerts/quiet", data={"until": "", "reason": ""}, headers=HX)
    assert r.status_code == 200 and "quiet switch is off again" in r.text
    assert written(store)["quiet_until"] == ""
    assert Windows(window_file(store)).quiet("anything") is False

    r = await client.post("/alerts/quiet", data={"until": "some time next week"}, headers=HX)
    assert r.status_code == 422 and "could not be read" in r.text


async def test_a_broken_quiet_times_file_is_reported_on_the_page(client, store):
    window_file(store).write_text("windows: [ this is not: valid: yaml\n  - nope\n")
    r = await client.get("/alerts")
    assert r.status_code == 200
    assert "The quiet-times file has a problem" in r.text and "nothing is being kept quiet" in r.text


async def test_the_form_offers_this_installs_servers_and_services(client):
    r = await client.get("/alerts")
    html = r.text
    assert 'name="servers" value="testlab"' in html and 'name="servers" value="plex land"' in html
    for service in ("pve", "sonarr", "github"):
        assert f'name="services" value="{service}"' in html
    assert 'name="days" value="mon"' in html and 'name="days" value="sun"' in html and "> Sun" in html


async def test_the_page_needs_a_sign_in(anon):
    r = await anon.get("/alerts")
    assert r.status_code in (302, 401)

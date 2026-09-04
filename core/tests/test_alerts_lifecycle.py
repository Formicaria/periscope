"""The life of one alert: posted once, counted when it repeats, acked, snoozed, escalated, held back, closed.

No Discord and no network — a fake channel remembers what was sent and edited, and the router's state lives in
a real JsonState on disk so "does an ack survive a restart?" is answered by building a second router over the
same file. The maintenance windows are the real `periscope.maintenance.Windows` reading a real YAML file.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import discord
import pytest
from periscope.alerts import Alert, AlertRouter, escalate_minutes, status_lines
from periscope.embeds import Severity
from periscope.hooks import NullWindows, windows_for
from periscope.maintenance import Windows, normalise_days, parse_clock
from periscope.messages import Messages
from periscope.state import JsonState
from periscope.views import AlertActionView, alert_custom_id

ROLE = 4242
CHANNEL = 1002


# ----- doubles ----------------------------------------------------------------------------------------------
def not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


class FakeMessage:
    def __init__(self, channel, mid, content, embed, view):
        self.channel, self.id, self.content = channel, mid, content
        self.embeds = [embed] if embed is not None else []
        self.view = view
        self.edits: list[dict] = []
        self.deleted = False

    async def edit(self, **kw):
        self.edits.append(kw)
        if "content" in kw:
            self.content = kw["content"]
        if kw.get("embed") is not None:
            self.embeds = [kw["embed"]]
        if "view" in kw:
            self.view = kw["view"]
        return self

    def field(self, name):
        for e in self.embeds:
            for f in e.fields:
                if f.name == name:
                    return f.value
        return ""

    @property
    def status(self):
        return self.field("Status")


class FakeChannel:
    def __init__(self, cid=CHANNEL):
        self.id = cid
        self.sent: list[FakeMessage] = []
        self.gone = False           # the card was deleted in Discord
        self._next = 5000

    async def send(self, content=None, embed=None, view=None, allowed_mentions=None, **kw):
        self._next += 1
        msg = FakeMessage(self, self._next, content, embed, view)
        self.sent.append(msg)
        return msg

    async def fetch_message(self, mid):
        if self.gone:
            raise not_found()
        for msg in self.sent:
            if msg.id == mid:
                return msg
        raise not_found()

    @property
    def cards(self):
        """Only the alert cards — the escalation re-ping is a plain message with no embed."""
        return [m for m in self.sent if m.embeds]

    @property
    def pings(self):
        return [m.content for m in self.sent if m.content]


class Recorder:
    """Stands in for `bot.history` until the real event log lands."""

    enabled = True

    def __init__(self):
        self.events: list[dict] = []

    def record(self, **kw):
        self.events.append(kw)

    def details(self):
        return [e["detail"] for e in self.events]


class Boom:
    """A `bot.windows` that is broken in the worst way: every question raises."""

    enabled = True

    def quiet(self, *a, **kw):
        raise RuntimeError("the window config exploded")

    def reason(self, *a, **kw):
        raise RuntimeError("the window config exploded")

    def active(self):
        raise RuntimeError("the window config exploded")


class FakeBot:
    def __init__(self, tmp_path, *, windows=None, role=ROLE, admin=True, name="pve"):
        self.name = name
        self.lab_name = "testlab"
        self.state = JsonState(tmp_path / "state.json")
        self.messages = Messages(None, service=name, lab="testlab")
        self.history = Recorder()
        self.windows = windows or NullWindows()
        self.channel = FakeChannel()
        self.settings = SimpleNamespace(alert_channel_id=CHANNEL, alert_role_id=role, lab_name="testlab",
                                        admin_role_ids=[], status_channel_id=None, status_interval_s=60)
        self.env: dict[str, str] = {}
        self.admin = admin
        self.views: list = []
        self.listeners: list = []

    def add_view(self, view):
        self.views.append(view)

    def add_listener(self, coro, name):
        self.listeners.append(name)

    def is_admin(self, user):
        return self.admin

    async def get_channel_safe(self, cid):
        return self.channel if int(cid) == CHANNEL else None


def router_for(tmp_path, *, cooldown=0, **kw) -> AlertRouter:
    return AlertRouter(FakeBot(tmp_path, **kw), cooldown_s=cooldown)


def cpu(severity=Severity.WARNING, title="High CPU on pve1") -> Alert:
    return Alert(fingerprint="pve:node:pve1:cpu", title=title, description="CPU at **93%** for 3 polls.",
                 severity=severity, fields={"Node": "pve1"})


def yaml_file(tmp_path, text: str):
    path = tmp_path / "config" / "maintenance.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


# ----- dedupe and grouping ----------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_repeat_edits_the_open_card_and_counts_it(tmp_path):
    r = router_for(tmp_path)
    ch = r.bot.channel
    first = await r.fire(cpu())
    assert len(ch.cards) == 1 and first.status.startswith("🔔 Firing")

    again = await r.fire(cpu())
    assert again is first and len(ch.cards) == 1                 # edited, never posted twice
    assert "Seen 2 times" in first.status
    for _ in range(3):
        await r.fire(cpu())
    assert len(ch.cards) == 1 and "Seen 5 times" in first.status
    assert r.bot.state.get("alerts:pve:node:pve1:cpu")["count"] == 5
    assert r.active() == ["pve:node:pve1:cpu"]
    assert [row["state"] for row in r.snapshot()] == ["firing"]
    r.close()


@pytest.mark.asyncio
async def test_two_problems_are_two_cards_and_close_independently(tmp_path):
    r = router_for(tmp_path)
    ch = r.bot.channel
    await r.fire(cpu())
    await r.fire(Alert(fingerprint="pve:node:pve2:disk", title="Disk nearly full on pve2"))
    assert len(ch.cards) == 2 and sorted(r.active()) == ["pve:node:pve1:cpu", "pve:node:pve2:disk"]

    assert await r.resolve("pve:node:pve1:cpu", "CPU back to 41%") is True
    assert r.active() == ["pve:node:pve2:disk"]
    assert "RESOLVED" in ch.cards[0].embeds[0].title and ch.cards[0].view is None
    assert ch.cards[0].field("Resolution") == "CPU back to 41%"
    assert await r.resolve("pve:node:pve1:cpu") is False         # already closed: nothing to write
    r.close()


@pytest.mark.asyncio
async def test_a_card_someone_deleted_is_posted_again(tmp_path):
    r = router_for(tmp_path)
    ch = r.bot.channel
    await r.fire(cpu())
    ch.gone = True
    await r.fire(cpu())                                          # the card is gone: post a fresh one
    ch.gone = False
    assert len(ch.cards) == 2 and "Seen 2 times" in ch.cards[1].status
    r.close()


# ----- ack --------------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ack_stops_the_pings_and_survives_a_restart(tmp_path):
    r = router_for(tmp_path)
    ch = r.bot.channel
    card = await r.fire(cpu(Severity.CRITICAL))
    assert card.content == f"<@&{ROLE}>"                         # a critical alert pings the alert role

    assert await r.ack("pve:node:pve1:cpu", who="Alice", user_id=555) is True
    assert card.content is None and "Acked by Alice" in card.status
    assert "acked" in r.bot.history.details()[0]
    assert r.snapshot()[0]["acked_by"] == "Alice" and r.snapshot()[0]["state"] == "acked"

    # a restart: a brand-new router over the same state file still knows who acked it
    after = AlertRouter(FakeBot(tmp_path), cooldown_s=0)
    after.bot.channel = ch
    assert after.snapshot()[0]["acked_by"] == "Alice"
    ch.gone = True                                               # force a fresh post rather than an edit
    await after.fire(cpu(Severity.CRITICAL))
    ch.gone = False
    assert ch.cards[-1].content is None                          # acked: still no ping after the restart
    assert "Acked by Alice" in ch.cards[-1].status
    r.close()
    after.close()


@pytest.mark.asyncio
async def test_acking_something_already_closed_says_so(tmp_path):
    r = router_for(tmp_path)
    assert await r.ack("pve:nothing:here") is False
    assert await r.snooze("pve:nothing:here", 8) is False
    r.close()


# ----- snooze -----------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_snooze_holds_the_pings_and_expiry_re_arms_the_alert(tmp_path):
    r = router_for(tmp_path)
    ch = r.bot.channel
    card = await r.fire(cpu(Severity.CRITICAL))
    assert card.content == f"<@&{ROLE}>"

    assert await r.snooze("pve:node:pve1:cpu", 8, who="Alice") is True
    assert card.content is None and "Snoozed by Alice" in card.status
    row = r.snapshot()[0]
    assert row["state"] == "snoozed" and row["snoozed_until"] > time.time() + 7 * 3600

    ch.gone = True                                               # a repeat while snoozed still does not ping
    await r.fire(cpu(Severity.CRITICAL))
    assert ch.cards[-1].content is None

    record = r.bot.state.get("alerts:pve:node:pve1:cpu")         # wind the snooze back into the past
    record["snooze_until"] = time.time() - 1
    r.bot.state.set("alerts:pve:node:pve1:cpu", record)
    await r.fire(cpu(Severity.CRITICAL))
    ch.gone = False
    assert ch.cards[-1].content == f"<@&{ROLE}>"                 # re-armed: it pings again
    assert "Snoozed" not in ch.cards[-1].status
    r.close()


# ----- escalation -------------------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_an_unacked_critical_escalates_exactly_once(tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_ESCALATE_MIN", "15")
    r = router_for(tmp_path)
    ch = r.bot.channel
    assert escalate_minutes(r.bot) == 15
    card = await r.fire(cpu(Severity.CRITICAL))
    record = r.bot.state.get("alerts:pve:node:pve1:cpu")
    assert record["escalate_min"] == 15 and record["escalate_at"] == pytest.approx(record["ts"] + 900, abs=2)

    assert await r.tick(now=time.time() + 100) == []             # not due yet
    assert await r.tick(now=time.time() + 901) == ["pve:node:pve1:cpu"]
    assert ch.pings[-1].startswith(f"<@&{ROLE}>") and "nobody acked it in 15 minutes" in ch.pings[-1]
    assert "the alert role was pinged again" in card.status
    assert "escalated" in " ".join(r.bot.history.details())

    assert await r.tick(now=time.time() + 5000) == []            # once, and only once
    assert len([m for m in ch.sent if not m.embeds]) == 1
    r.close()


@pytest.mark.asyncio
async def test_a_restart_does_not_push_the_escalation_deadline_out(tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_ESCALATE_MIN", "15")
    r = router_for(tmp_path)
    await r.fire(cpu(Severity.CRITICAL))
    due = r.bot.state.get("alerts:pve:node:pve1:cpu")["escalate_at"]
    r.close()

    after = AlertRouter(FakeBot(tmp_path), cooldown_s=0)          # the process came back up
    after.bot.channel = r.bot.channel
    after.bot.channel.gone = True                                 # the card is refetched, not reposted
    after.bot.channel.gone = False
    assert after.bot.state.get("alerts:pve:node:pve1:cpu")["escalate_at"] == due
    assert await after._on_ready() is None                        # coming up catches up on what fell due
    assert await after.tick(now=due + 1) == ["pve:node:pve1:cpu"]
    after.close()


@pytest.mark.asyncio
async def test_escalation_is_off_by_default_and_never_touches_a_warning(tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_ESCALATE_MIN", raising=False)
    r = router_for(tmp_path)
    await r.fire(cpu(Severity.CRITICAL))
    assert "escalate_at" not in r.bot.state.get("alerts:pve:node:pve1:cpu")
    assert await r.tick(now=time.time() + 99999) == []
    r.close()

    monkeypatch.setenv("ALERT_ESCALATE_MIN", "15")
    warn = router_for(tmp_path / "warn")
    await warn.fire(cpu())                                       # WARNING: nothing to escalate to
    assert "escalate_at" not in warn.bot.state.get("alerts:pve:node:pve1:cpu")
    warn.close()

    monkeypatch.setenv("ALERT_ESCALATE_MIN", "nonsense")
    assert escalate_minutes(warn.bot) == 0


@pytest.mark.asyncio
async def test_acking_before_the_timer_calls_off_the_escalation(tmp_path, monkeypatch):
    monkeypatch.setenv("ALERT_ESCALATE_MIN", "15")
    r = router_for(tmp_path)
    await r.fire(cpu(Severity.CRITICAL))
    await r.ack("pve:node:pve1:cpu", who="Alice")
    assert await r.tick(now=time.time() + 901) == []
    assert [m for m in r.bot.channel.sent if not m.embeds] == []
    r.close()


# ----- maintenance windows ----------------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_window_suppresses_the_alert_and_then_lets_it_through(tmp_path):
    now = time.time()
    path = yaml_file(tmp_path, "version: 1\nwindows:\n"
                               "  - id: nightly-backups\n"
                               '    reason: "Nightly backups — the disks are meant to be busy"\n'
                               "    days: [mon, tue, wed, thu, fri, sat, sun]\n"
                               '    start: "00:00"\n    end: "24:00"\n'
                               "    services: [pve]\n")
    clock = SimpleNamespace(t=now)
    windows = Windows(path, clock=lambda: clock.t)
    assert windows.errors == [] and windows.quiet("pve", server="testlab") is True

    r = router_for(tmp_path, windows=windows)
    assert await r.fire(cpu()) is None
    assert r.bot.channel.sent == [] and r.active() == []          # nothing posted, nothing left behind
    (event,) = r.bot.history.events
    assert event["kind"] == "alert" and event["key"] == "pve:node:pve1:cpu"
    assert "suppressed" in event["detail"] and "Nightly backups" in event["detail"]

    yaml_file(tmp_path, "version: 1\nwindows: []\n")              # the window is taken away
    windows.reload(force=True)
    card = await r.fire(cpu())
    assert card is not None and len(r.bot.channel.cards) == 1
    r.close()


@pytest.mark.asyncio
async def test_an_open_card_says_the_alert_fired_during_a_quiet_time(tmp_path):
    path = yaml_file(tmp_path, "version: 1\nwindows: []\n")
    windows = Windows(path)
    r = router_for(tmp_path, windows=windows)
    card = await r.fire(cpu())

    yaml_file(tmp_path, 'version: 1\nquiet_until: "2099-01-01T00:00"\nquiet_reason: "Rack power work"\n')
    windows.reload(force=True)
    assert await r.fire(cpu()) is card                             # the card is refreshed, nothing is sent
    assert len(r.bot.channel.cards) == 1
    assert "fired again during a quiet time" in card.status and "Rack power work" in card.status
    assert r.snapshot()[0]["state"] == "held back"
    r.close()


@pytest.mark.asyncio
async def test_a_window_only_covers_what_it_names(tmp_path):
    path = yaml_file(tmp_path, "version: 1\nwindows:\n"
                               "  - id: docker-only\n"
                               '    reason: "Container host rebuild"\n'
                               '    start: "00:00"\n    end: "24:00"\n'
                               "    services: [docker]\n    servers: [testlab]\n"
                               '    keys: ["docker:container:*"]\n')
    windows = Windows(path)
    assert windows.quiet("docker", server="testlab", key="docker:container:radarr:exited") is True
    assert windows.quiet("docker", server="testlab", key="docker:daemon:unreachable") is False
    assert windows.quiet("docker", server="plexland", key="docker:container:radarr:exited") is False
    assert windows.quiet("pve", server="testlab", key="docker:container:radarr:exited") is False
    assert windows.quiet() is False                                # an alert we cannot place is never silenced

    r = router_for(tmp_path, windows=windows, name="pve")
    assert await r.fire(cpu()) is not None                         # this service is not in the window
    r.close()


@pytest.mark.asyncio
async def test_a_malformed_window_file_fails_open(tmp_path):
    path = yaml_file(tmp_path, "windows: [ this is not: valid: yaml\n  - nope\n")
    windows = windows_for(path)
    assert isinstance(windows, Windows) and windows.errors and windows.quiet("pve") is False
    assert windows.active() == [] and windows.reason("pve") == ""

    r = router_for(tmp_path, windows=windows)
    assert await r.fire(cpu()) is not None                         # a file we cannot read keeps nobody quiet
    r.close()

    half = yaml_file(tmp_path / "half", "version: 1\nwindows:\n"
                                        "  - id: fine\n    start: \"00:00\"\n    end: \"24:00\"\n"
                                        "  - id: broken\n    start: \"tea time\"\n    end: \"later\"\n")
    w2 = Windows(half)
    assert any("broken" in e for e in w2.errors)                   # one bad window, the good one still works
    assert w2.quiet("anything") is True and "fine" in [a["id"] for a in w2.active()]


@pytest.mark.asyncio
async def test_nothing_a_window_does_can_stop_an_alert(tmp_path):
    r = router_for(tmp_path, windows=Boom())
    assert r.quiet_reason(cpu()) == ""
    assert await r.fire(cpu()) is not None
    r.close()


# ----- the file format --------------------------------------------------------------------------------------
def test_weekday_windows_read_the_clock(tmp_path):
    # Wednesday 2 September 2026, 02:00 local — inside a 01:00→04:30 window, outside it at 05:00
    inside = time.mktime((2026, 9, 2, 2, 0, 0, 0, 0, -1))
    path = yaml_file(tmp_path, "version: 1\nwindows:\n"
                               "  - id: nightly\n    reason: Backups\n    days: [weekdays]\n"
                               '    start: "01:00"\n    end: "04:30"\n')
    clock = SimpleNamespace(t=inside)
    w = Windows(path, clock=lambda: clock.t)
    assert w.quiet("pve") is True and w.reason("pve") == "Backups"
    assert [a["id"] for a in w.active()] == ["nightly"]

    clock.t = inside + 4 * 3600                                    # 06:00 the same Wednesday
    assert w.quiet("pve") is False and w.active() == []

    clock.t = time.mktime((2026, 9, 6, 2, 0, 0, 0, 0, -1))         # a Sunday: not a weekday
    assert w.quiet("pve") is False


def test_a_window_can_run_past_midnight(tmp_path):
    path = yaml_file(tmp_path, "version: 1\nwindows:\n"
                               "  - id: overnight\n    days: [fri]\n"
                               '    start: "22:00"\n    end: "02:00"\n')
    friday_late = time.mktime((2026, 9, 4, 23, 0, 0, 0, 0, -1))
    clock = SimpleNamespace(t=friday_late)
    w = Windows(path, clock=lambda: clock.t)
    assert w.quiet() is True
    clock.t = friday_late + 2 * 3600                               # 01:00 Saturday, still the Friday window
    assert w.quiet() is True
    clock.t = friday_late + 4 * 3600                               # 03:00 Saturday: over
    assert w.quiet() is False
    clock.t = friday_late - 24 * 3600                              # 23:00 Thursday: wrong day
    assert w.quiet() is False


def test_a_one_off_window_expires_by_itself(tmp_path):
    path = yaml_file(tmp_path, "version: 1\nwindows:\n"
                               "  - id: pve1-rebuild\n    reason: Rebuilding pve1\n"
                               '    start: "2026-09-06T20:00:00"\n    end: "2026-09-07T02:00:00"\n'
                               "    services: [pve]\n")
    during = time.mktime((2026, 9, 6, 22, 0, 0, 0, 0, -1))
    clock = SimpleNamespace(t=during)
    w = Windows(path, clock=lambda: clock.t)
    assert w.quiet("pve") is True and w.reason("pve") == "Rebuilding pve1"
    (open_now,) = w.active()
    assert open_now["one_off"] is True and open_now["until"] is not None
    clock.t = during + 5 * 3600
    assert w.quiet("pve") is False
    assert w.rows()[0]["when"].startswith("once:") and w.rows()[0]["open"] is False


def test_the_global_switch_covers_everything(tmp_path):
    path = yaml_file(tmp_path, 'version: 1\nquiet_until: "2099-01-01T00:00"\nquiet_reason: "Rack power work"\n'
                               "windows: []\n")
    w = Windows(path)
    assert w.quiet("anything", server="anywhere", key="any:fingerprint") is True
    assert w.reason("anything") == "Rack power work"
    assert [a["id"] for a in w.active()] == ["quiet-everything"]
    raw, why, until = w.quiet_until()
    assert raw == "2099-01-01T00:00" and why == "Rack power work" and until > time.time()

    w.set_quiet_until("")                                          # switched off again
    assert w.quiet("anything") is False and w.active() == []
    with pytest.raises(ValueError):
        w.set_quiet_until("some time next week")


def test_the_editor_round_trips_the_file(tmp_path):
    path = tmp_path / "config" / "maintenance.yaml"
    w = Windows(path)
    assert w.rows() == [] and w.quiet("pve") is False              # a file that does not exist yet is fine
    added = w.add(reason="Nightly backups", days="mon,tue,wed,thu,fri", start="01:00", end="04:30",
                  services="pve, docker", servers="testlab")
    assert added.id == "nightly-backups" and path.exists()
    w.add(reason="Rebuilding pve1", start="2026-09-06T20:00", end="2026-09-07T02:00")

    fresh = Windows(path)                                          # what a restart (or another process) reads
    ids = [row["id"] for row in fresh.rows()]
    assert ids == ["nightly-backups", "rebuilding-pve1"]
    first = fresh.rows()[0]
    assert first["when"] == "Mon–Fri, 01:00–04:30" and first["services"] == ["pve", "docker"]
    assert first["scope"] == "pve, docker on testlab"

    assert fresh.set_enabled("nightly-backups", False) is True
    assert Windows(path).rows()[0]["enabled"] is False
    assert fresh.remove("nightly-backups") is True
    assert [r["id"] for r in Windows(path).rows()] == ["rebuilding-pve1"]
    assert fresh.remove("nightly-backups") is False
    assert fresh.set_enabled("nothing-like-this", True) is False

    with pytest.raises(ValueError):
        fresh.add(reason="Nonsense", start="tea time", end="later")


def test_the_small_parsers():
    assert normalise_days("Mon, tuesday, WEDS") == ["mon", "tue", "wed"]
    assert normalise_days("weekends") == ["sat", "sun"] and normalise_days(None) == []
    assert normalise_days([1, 7, "nope"]) == ["mon", "sun"]
    assert parse_clock("01:00") == 60 and parse_clock("24:00") == 1440 and parse_clock("9") == 540
    assert parse_clock("25:00") is None and parse_clock("") is None and parse_clock("tea time") is None


# ----- the card's controls ----------------------------------------------------------------------------------
def test_the_custom_ids_never_change():
    assert alert_custom_id("ack", "pve") == "periscope:alert:ack:pve"
    assert alert_custom_id("resolve") == "periscope:alert:resolve:core"
    view = AlertActionView(SimpleNamespace(scope="docker"), scope="docker")
    ids = sorted(c.custom_id for c in view.children)
    assert ids == ["periscope:alert:ack:docker", "periscope:alert:resolve:docker", "periscope:alert:snooze:docker"]
    assert view.timeout is None and view.is_persistent()          # what makes it survive a restart
    assert [o.value for o in view.snooze.options] == ["1", "8", "24"]
    assert [c.row for c in view.children] == [0, 0, 1]            # a menu needs a row of its own


class FakeResponse:
    def __init__(self):
        self.messages: list[tuple[str, bool]] = []
        self.deferred = False

    async def send_message(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))

    async def send(self, content, ephemeral=False):
        self.messages.append((content, ephemeral))

    async def defer(self):
        self.deferred = True


class FakeInteraction:
    def __init__(self, message=None, name="Alice", uid=555):
        self.message = message
        self.user = SimpleNamespace(id=uid, display_name=name, name=name)
        self.response = FakeResponse()
        self.followup = FakeResponse()

    def said(self):
        return [text for text, _ in self.response.messages + self.followup.messages]


@pytest.mark.asyncio
async def test_the_buttons_are_admin_only(tmp_path):
    r = router_for(tmp_path, admin=False)
    view = r.persistent_view()
    interaction = FakeInteraction()
    assert await view.interaction_check(interaction) is False
    assert interaction.said() == ["🚫 Admin only."]

    r.bot.admin = True
    assert await view.interaction_check(FakeInteraction()) is True
    r.close()


@pytest.mark.asyncio
async def test_clicking_ack_snooze_and_resolve(tmp_path):
    r = router_for(tmp_path)
    card = await r.fire(cpu(Severity.CRITICAL))
    assert r.bot.views and r.bot.listeners == ["on_ready"]         # the persistent view was handed over once
    assert r.by_message(card.id) == "pve:node:pve1:cpu" and r.by_message(999999) == ""

    await r.on_ack(FakeInteraction(card))
    assert card.content is None and "Acked by Alice" in card.status
    await r.on_snooze(FakeInteraction(card), 8)
    assert "Snoozed by Alice" in card.status
    await r.on_resolve(FakeInteraction(card))
    assert r.active() == [] and "RESOLVED" in card.embeds[0].title
    assert card.field("Closed by") == "Alice"

    stale = FakeInteraction(card)                                  # the same card, now closed
    await r.on_ack(stale)
    assert stale.said() == ["That alert is already closed — nothing to do."]
    r.close()


def test_the_status_field_says_what_happened():
    now = time.time()
    assert status_lines({}) == ["🔔 Firing — nobody has acked it yet"]
    lines = " ".join(status_lines({"count": 4, "first_ts": now, "acked_ts": now, "acked_name": "Alice",
                                   "escalated_ts": now, "escalate_min": 15, "suppressed": True,
                                   "suppressed_reason": "Backups"}))
    assert "Seen 4 times" in lines and "Acked by Alice" in lines
    assert "after 15 minutes" in lines and "Backups" in lines

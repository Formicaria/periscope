"""v2: eight media services on a shared presence, one MediaHub per presence, per-app slash groups + webhooks."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from discord import app_commands
from periscope import Settings, Store
from periscope.http import HttpClient, HttpError
from periscope.runtime import Runtime

from periscope_arr.__main__ import ArrBot
from periscope_arr.client import ArrClient
from periscope_arr.config import ArrSettings
from periscope_arr.hub import COGS
from periscope_arr.service import SERVICES

NAMES = ["sonarr", "radarr", "lidarr", "prowlarr", "qbittorrent", "sabnzbd", "plex", "jellyfin"]
SONARR = {"SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": "sk", "MEDIA_CHANNEL_ID": "7", "ARR_QUEUE_STALL_MIN": "45"}
RADARR = {"RADARR_URL": "http://radarr:7878", "RADARR_API_KEY": "rk", "ALERT_CHANNEL_ID": "9"}
PLEX = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "pt"}
GRAB = {"eventType": "Grab", "series": {"title": "The Expanse", "year": 2015}, "movie": {"title": "Dune", "year": 2021},
        "episodes": [{"seasonNumber": 6, "episodeNumber": 1}], "release": {"quality": "WEBDL-1080p"}}


# ----- helpers ---------------------------------------------------------------------------------------

def make_runtime(tmp_path, services: dict[str, dict], **lab) -> Runtime:
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.lab.update({"alert_channel_id": "2", "status_channel_id": "1", **lab})
    s.webhook["secret"] = "s3cret"
    for name, env in services.items():
        s.services[name] = {"enabled": True, "presence": "default", "env": env}
    rt = Runtime(s, tmp_path)
    rt.assemble()
    assert not rt.skipped, rt.skipped
    return rt


async def build_all(rt: Runtime, *names: str):
    pres = rt.presences["default"]

    async def never_ready():  # the presence never logs in; loops stay parked in before_loop until unload
        await asyncio.Event().wait()

    pres.wait_until_ready = never_ready
    for n in names:
        await rt.services[n].spec.build(rt.services[n])
    return pres


async def teardown(rt: Runtime, *names: str):
    for n in names:
        await rt.services[n].unload()
    hub = getattr(rt.presences["default"], "media_hub", None)
    if hub is not None:
        await hub.close()


def routes(rt: Runtime) -> set[tuple[str, str]]:
    return {(r.method, r.resource.canonical) for r in rt.webhook.app.router.routes()}


class FakeInteraction:
    def __init__(self, user_id: int = 1):
        self.user = SimpleNamespace(id=user_id)
        self.sent: list[tuple] = []
        self._done = False
        self.response = SimpleNamespace(defer=self._defer, send_message=self._send, is_done=lambda: self._done)
        self.followup = SimpleNamespace(send=self._send)

    async def _defer(self, **kw):
        self._done = True

    async def _send(self, content=None, **kw):
        self._done = True
        self.sent.append((content, kw))


class FakeChannel:
    def __init__(self, cid: int):
        self.id = cid
        self.embeds: list = []

    async def send(self, content=None, *, embed=None, **kw):
        self.embeds.append(embed)
        return SimpleNamespace(id=100 + len(self.embeds), channel=self)


# ----- specs ------------------------------------------------------------------------------------------

def test_eight_specs_nothing_named_arr():
    specs = {s.name: s for s in SERVICES}
    assert list(specs) == NAMES and "arr" not in specs
    assert all(s.group == "media" and s.slash == f"/{s.name}" and s.check is not None for s in SERVICES)
    keys = {n: [x.key for x in specs[n].settings] for n in NAMES}
    assert keys["sonarr"] == ["SONARR_URL", "SONARR_API_KEY", "VERIFY_SSL", "MEDIA_CHANNEL_ID", "ARR_QUEUE_STALL_MIN"]
    assert keys["prowlarr"] == ["PROWLARR_URL", "PROWLARR_API_KEY", "VERIFY_SSL", "MEDIA_CHANNEL_ID"]
    assert keys["qbittorrent"] == ["QBIT_URL", "QBIT_API_KEY", "QBIT_USER", "QBIT_PASS", "VERIFY_SSL"]
    assert keys["sabnzbd"] == ["SABNZBD_URL", "SABNZBD_API_KEY", "VERIFY_SSL"]
    assert keys["plex"] == ["PLEX_URL", "PLEX_TOKEN", "VERIFY_SSL"]
    assert keys["jellyfin"] == ["JELLYFIN_URL", "JELLYFIN_API_KEY", "VERIFY_SSL"]
    assert specs["sonarr"].required_missing({}) == ["SONARR_URL", "SONARR_API_KEY"]
    assert specs["qbittorrent"].required_missing({"QBIT_URL": "http://q"}) == []          # user/pass or key optional
    assert specs["plex"].setting("PLEX_TOKEN").type == "secret" and specs["sonarr"].setting("MEDIA_CHANNEL_ID").type == "channel"
    for n in ("sonarr", "radarr", "lidarr", "prowlarr"):
        assert specs[n].needs_webhook and specs[n].webhook_paths == [f"/{n}"]
    for n in ("qbittorrent", "sabnzbd", "plex", "jellyfin"):
        assert not specs[n].needs_webhook and specs[n].webhook_paths == []


def test_settings_only_one_service(monkeypatch):
    monkeypatch.setenv("SONARR_URL", "sonarr:8989/")
    monkeypatch.setenv("SONARR_API_KEY", "k")
    monkeypatch.setenv("RADARR_URL", "http://radarr")   # another service's key is ignored, even without its API key
    monkeypatch.setenv("ARR_QUEUE_STALL_MIN", "12")
    cfg = ArrSettings.from_env(only="sonarr")
    assert cfg.arr == {"sonarr": ("http://sonarr:8989", "k")} and cfg.enabled_services() == ["sonarr"]
    assert cfg.queue_stall_min == 12 and cfg.shared_only().queue_stall_min == 12 and cfg.shared_only().arr == {}
    monkeypatch.delenv("SONARR_URL")
    with pytest.raises(RuntimeError, match="SONARR_URL"):
        ArrSettings.from_env(only="sonarr")
    with pytest.raises(ValueError):
        ArrSettings.from_env(only="arr")


# ----- building on a presence -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_services_share_one_hub_and_board(tmp_path):
    rt = make_runtime(tmp_path, {"sonarr": SONARR, "radarr": RADARR})
    pres = await build_all(rt, "sonarr", "radarr")
    sonarr, radarr = rt.services["sonarr"], rt.services["radarr"]
    hub = pres.media_hub
    assert sonarr.media_hub is hub and radarr.media_hub is hub and hub.bot is sonarr      # first one built owns it
    assert hub.svc.names() == ["sonarr", "radarr"] and set(hub.services) == {"sonarr", "radarr"}
    # the cogs load once, namespaced under the owner; the second service adds no cogs of its own
    names = {c.qualified_name for c in pres.cogs.values()}
    assert names == {"sonarr:Webhooks", "sonarr:Queue", "sonarr:Media"}
    assert sonarr.get_cog("Media") is hub.media_cog and radarr.get_cog("Media") is None
    # one board, kept in the presence-wide state slot so it survives whichever service is built first
    assert hub.media_cog.board._state._prefix == "presence:default:board:arr:" and hub.media_cog.board.channel_id == 1
    # slash groups: one per service, nothing called /arr
    assert pres.tree.get_command("arr") is None
    for n in ("sonarr", "radarr"):
        g = pres.tree.get_command(n)
        assert isinstance(g, app_commands.Group)
        assert {c.name for c in g.commands} == {"board", "queue", "remove", "calendar", "search", "health"}
        assert g.get_command("remove").checks, f"/{n} remove must be admin-gated"
    # the hub's webhook routes on the shared server
    assert {("POST", "/sonarr"), ("POST", "/radarr")} <= routes(rt) and ("POST", "/lidarr") not in routes(rt)
    # per-app behaviour keys come from the owning service, alerts go through it
    assert hub.cfg_for("sonarr").queue_stall_min == 45 and hub.cfg_for("radarr").queue_stall_min == 30
    assert hub.media_channel_for("sonarr") == 7 and hub.media_channel_for("radarr") == 7   # falls back to the stack's
    assert hub.alerts_for("radarr") is radarr.alerts and hub.alerts_for("sonarr") is sonarr.alerts
    assert radarr.settings.alert_channel_id == 9 and sonarr.settings.alert_channel_id == 2
    await teardown(rt, "sonarr", "radarr")


@pytest.mark.asyncio
async def test_hub_grows_as_services_join(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, {"plex": PLEX, "sonarr": SONARR, "qbittorrent": {"QBIT_URL": "http://qb:8080", "QBIT_API_KEY": "qbt_x"}})
    pres = await build_all(rt, "plex")
    hub = pres.media_hub
    assert hub.bot is rt.services["plex"] and hub.svc.names() == ["plex"]
    assert {c.qualified_name for c in pres.cogs.values()} == {"plex:Webhooks", "plex:Queue", "plex:Media"}
    assert ("POST", "/sonarr") not in routes(rt)                       # no *arr app yet → no route
    assert {c.name for c in pres.tree.get_command("plex").commands} == {"board", "nowplaying"}

    await build_all(rt, "sonarr", "qbittorrent")
    assert pres.media_hub is hub and hub.svc.names() == ["sonarr", "qbittorrent", "plex"]
    assert ("POST", "/sonarr") in routes(rt)                           # added when sonarr registered
    assert {c.name for c in pres.tree.get_command("qbittorrent").commands} == {"board", "status"}
    assert {c.name for c in pres.tree.get_command("sonarr").commands} >= {"queue", "search", "health"}
    assert len([c for c in pres.cogs.values() if c.qualified_name.endswith(":Media")]) == 1
    st = rt.status()
    assert {st["services"][n]["presence"] for n in ("plex", "sonarr", "qbittorrent")} == {"default"}

    # every group's `board` renders the one shared board; `/plex nowplaying` only asks Plex
    async def sessions(self):
        return [{"type": "movie", "title": "Heat", "year": 1995, "duration": 100, "viewOffset": 50,
                 "User": {"title": "alice"}, "Player": {"product": "Plex Web", "state": "playing"}}]

    async def transfer_info(self):
        return {"dl_info_speed": 1024, "up_info_speed": 0}

    async def queue(self):
        return []

    async def diskspace(self):
        return []

    monkeypatch.setattr("periscope_arr.client.PlexClient.sessions", sessions)
    monkeypatch.setattr("periscope_arr.client.QbitClient.transfer_info", transfer_info)
    monkeypatch.setattr(ArrClient, "queue", queue)
    monkeypatch.setattr(ArrClient, "diskspace", diskspace)
    i = FakeInteraction()
    await pres.tree.get_command("qbittorrent").get_command("board").callback(i)
    e = i.sent[-1][1]["embed"]
    assert e.title.endswith("Media stack") and "🟢 sonarr" in e.description and "🟢 qbittorrent" in e.description and "🟢 plex" in e.description
    assert {f.name for f in e.fields} == {"Queues", "Transfer", "Streams (1)"}
    i = FakeInteraction()
    await pres.tree.get_command("plex").get_command("nowplaying").callback(i)
    e = i.sent[-1][1]["embed"]
    assert e.title.endswith("Now playing · 1 stream") and "Heat (1995)" in e.description and "alice" in e.description
    await teardown(rt, "plex", "sonarr", "qbittorrent")


@pytest.mark.asyncio
async def test_per_app_commands_pin_the_app(tmp_path, monkeypatch):
    items = {"sonarr": [{"id": 1, "title": "S.Rel", "size": 100, "sizeleft": 50, "status": "downloading",
                         "series": {"title": "Show"}, "episode": {"seasonNumber": 1, "episodeNumber": 2}}],
             "radarr": [{"id": 2, "title": "R.Rel", "size": 100, "sizeleft": 0, "status": "completed",
                         "movie": {"title": "Heat", "year": 1995}}]}

    async def fake_queue(self):
        return items[self.app]

    async def fake_health(self):
        return [] if self.app == "sonarr" else [{"type": "error", "message": "boom"}]

    monkeypatch.setattr(ArrClient, "queue", fake_queue)
    monkeypatch.setattr(ArrClient, "health", fake_health)
    rt = make_runtime(tmp_path, {"sonarr": SONARR, "radarr": RADARR})
    pres = await build_all(rt, "sonarr", "radarr")
    for app, expect, absent in (("sonarr", "Show S01E02", "Heat"), ("radarr", "Heat (1995)", "Show")):
        i = FakeInteraction()
        await pres.tree.get_command(app).get_command("queue").callback(i)
        text = i.sent[-1][1]["embed"].description
        assert expect in text and absent not in text and f"[{app}]" in text
    i = FakeInteraction()
    await pres.tree.get_command("radarr").get_command("health").callback(i)
    e = i.sent[-1][1]["embed"]
    assert e.title == "radarr health" and [f.name for f in e.fields] == ["🟡 radarr (1)"] and "boom" in e.fields[0].value
    i = FakeInteraction()
    await pres.tree.get_command("sonarr").get_command("health").callback(i)
    assert [f.name for f in i.sent[-1][1]["embed"].fields] == ["🟢 sonarr"]
    await teardown(rt, "sonarr", "radarr")


@pytest.mark.asyncio
async def test_webhooks_route_through_the_owning_service(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, {"sonarr": SONARR, "radarr": RADARR})
    pres = await build_all(rt, "sonarr", "radarr")
    chans: dict[int, FakeChannel] = {}

    async def get_channel_safe(cid):
        return chans.setdefault(cid, FakeChannel(cid))

    pres.get_channel_safe = get_channel_safe
    fired: list[tuple[str, str]] = []

    async def fire_via(name):
        async def fire(alert, force=False):
            fired.append((name, alert.fingerprint))
        return fire

    monkeypatch.setattr(rt.services["sonarr"].alerts, "fire", await fire_via("sonarr"))
    monkeypatch.setattr(rt.services["radarr"].alerts, "fire", await fire_via("radarr"))
    async with TestClient(TestServer(rt.webhook.app)) as client:
        r = await client.post("/sonarr", data=json.dumps(GRAB))
        assert r.status == 401                                             # shared secret enforced
        r = await client.post("/sonarr?token=s3cret", data=json.dumps(GRAB))
        assert r.status == 200
        r = await client.post("/radarr?token=s3cret", data=json.dumps(GRAB))
        assert r.status == 200
        health = {"eventType": "HealthIssue", "level": "error", "type": "IndexerStatusCheck", "message": "All indexers unavailable"}
        r = await client.post("/radarr?token=s3cret", data=json.dumps(health))
        assert r.status == 200
        r = await client.post("/lidarr?token=s3cret", data=json.dumps(GRAB))
        assert r.status == 404                                             # lidarr is not on this presence
    assert [e.title for e in chans[7].embeds] == ["Sonarr: ⬇️ Grabbed", "Radarr: ⬇️ Grabbed"]   # shared media channel
    assert fired == [("radarr", "arr:radarr:health:IndexerStatusCheck:" + fired[0][1].rsplit(":", 1)[1])]
    await teardown(rt, "sonarr", "radarr")


# ----- checks (network mocked) ------------------------------------------------------------------------

class FakeResp:
    def __init__(self, text: str = ""):
        self._text = text
        self.cookies = {"SID": "x"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return self._text.encode()

    async def text(self):
        return self._text


def fake_http(monkeypatch, *, good_secret: str):
    """HttpClient answers from canned data; any other credential gets a 401."""
    calls: list[tuple[str, str]] = []

    def creds(client: HttpClient, path: str, kw: dict) -> str:
        h = client._headers
        return (h.get("X-Api-Key") or h.get("X-Plex-Token") or h.get("X-Emby-Token") or
                (h.get("Authorization") or "").replace("Bearer ", "") or (kw.get("params") or {}).get("apikey") or "")

    async def get_json(self, path, **kw):
        calls.append((self.base_url, path))
        params = kw.get("params") or {}
        if path == "/api" and params.get("mode") == "version":
            return {"version": "4.3.2"}
        if path == "/api" and params.get("mode") == "queue":
            return {"queue": {}} if params.get("apikey") == good_secret else {"error": "API Key Incorrect"}
        if path.startswith("/identity"):
            return {"MediaContainer": {"version": "1.41.0"}}
        if creds(self, path, kw) != good_secret:
            raise HttpError(401, self.base_url + path, "unauthorized")
        if path.endswith("/system/status"):
            return {"appName": "Sonarr", "version": "4.0.9"}
        if path == "/System/Info":
            return {"Version": "10.9.11", "ServerName": "jelly"}
        if path == "/status/sessions":
            return {"MediaContainer": {}}
        return {}

    async def get_bytes(self, path, **kw):
        calls.append((self.base_url, path))
        if creds(self, path, kw) != good_secret and "Authorization" in self._headers:
            raise HttpError(403, self.base_url + path, "forbidden")
        return b"v5.0.4"

    async def request(self, method, path, **kw):
        calls.append((self.base_url, path))
        data = kw.get("data") or {}
        return FakeResp("Ok." if data.get("password") == good_secret else "Fails.")

    monkeypatch.setattr(HttpClient, "get_json", get_json)
    monkeypatch.setattr(HttpClient, "get_bytes", get_bytes)
    monkeypatch.setattr(HttpClient, "request", request)
    return calls


@pytest.mark.asyncio
async def test_checks(monkeypatch):
    calls = fake_http(monkeypatch, good_secret="good")
    checks = {s.name: s.check for s in SERVICES}
    for app in ("sonarr", "radarr", "lidarr", "prowlarr"):
        up = app.upper()
        assert await checks[app]({f"{up}_URL": "http://x", f"{up}_API_KEY": "good"}) == (True, "Sonarr 4.0.9 answered")
        ok, msg = await checks[app]({f"{up}_URL": "x:1", f"{up}_API_KEY": "bad"})
        assert not ok and "401" in msg and f"{up}_API_KEY" in msg
        assert (await checks[app]({f"{up}_URL": "http://x"}))[0] is False
    assert any(p.endswith("/api/v1/system/status") and b.startswith("http://") for b, p in calls)   # lidarr/prowlarr use v1
    assert await checks["qbittorrent"]({"QBIT_URL": "http://q", "QBIT_API_KEY": "good"}) == (True, "qBittorrent v5.0.4 answered (API key)")
    assert await checks["qbittorrent"]({"QBIT_URL": "http://q", "QBIT_USER": "u", "QBIT_PASS": "good"}) == (True, "qBittorrent v5.0.4 answered (user/password)")
    assert "QBIT_USER" in (await checks["qbittorrent"]({"QBIT_URL": "http://q", "QBIT_USER": "u", "QBIT_PASS": "bad"}))[1]
    assert "QBIT_API_KEY" in (await checks["qbittorrent"]({"QBIT_URL": "http://q", "QBIT_API_KEY": "bad"}))[1]
    assert await checks["sabnzbd"]({"SABNZBD_URL": "http://s", "SABNZBD_API_KEY": "good"}) == (True, "SABnzbd 4.3.2 answered")
    assert "API Key Incorrect" in (await checks["sabnzbd"]({"SABNZBD_URL": "http://s", "SABNZBD_API_KEY": "bad"}))[1]
    assert await checks["plex"]({"PLEX_URL": "http://p", "PLEX_TOKEN": "good"}) == (True, "Plex 1.41.0 answered")
    assert "PLEX_TOKEN" in (await checks["plex"]({"PLEX_URL": "http://p", "PLEX_TOKEN": "bad"}))[1]
    assert await checks["jellyfin"]({"JELLYFIN_URL": "http://j", "JELLYFIN_API_KEY": "good"}) == (True, "Jellyfin 10.9.11 (jelly) answered")
    assert "JELLYFIN_API_KEY" in (await checks["jellyfin"]({"JELLYFIN_URL": "http://j", "JELLYFIN_API_KEY": "bad"}))[1]
    assert (await checks["jellyfin"]({}))[0] is False


@pytest.mark.asyncio
async def test_check_unreachable(monkeypatch):
    async def boom(self, path, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(HttpClient, "get_json", boom)
    ok, msg = await next(s.check for s in SERVICES if s.name == "radarr")({"RADARR_URL": "http://r", "RADARR_API_KEY": "k"})
    assert not ok and "unreachable" in msg and "connection refused" in msg


# ----- v1 stays as it was -----------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_v1_bot_keeps_single_arr_group(tmp_path):
    settings = Settings(discord_token="x", data_dir=tmp_path, status_interval_s=60, webhook_secret="s")
    cfg = ArrSettings(arr={"sonarr": ("http://s", "k")}, plex_url="http://p", plex_token="t")
    b = ArrBot(settings, cfg)

    async def never_ready():
        await asyncio.Event().wait()

    b.wait_until_ready = never_ready
    for path in COGS:
        await b.load_extension(path)
    g = b.tree.get_command("arr")
    assert {c.name for c in g.commands} == {"queue", "remove", "calendar", "search", "health", "clients", "nowplaying"}
    assert [c.name for c in b.tree.get_commands()] == ["arr"]
    assert {p for m, p in {(r.method, r.resource.canonical) for r in b.webhook.app.router.routes()} if m == "POST"} == \
        {"/sonarr", "/radarr", "/lidarr", "/prowlarr"}
    assert b.svc is b.media_hub.svc and b.svc.names() == ["sonarr", "plex"] and not b.media_hub.split
    assert b.media_hub.media_cog.board._state._prefix == "board:arr:"       # same state key as before
    for cog in list(b.cogs):
        await b.remove_cog(cog)
    await b.close()

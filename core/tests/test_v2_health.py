"""The parts that decide whether a service shows as running or explains why not: late webhook routes, command
sync that survives a server the bot is not in, plain-language presence errors, the default bot identity."""

import asyncio
from types import SimpleNamespace

import discord
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from periscope import Store
from periscope.migrate import migrate_v1
from periscope.presence import Presence, explain_presence_error, invite_url
from periscope.runtime import ERROR, NEEDS_SETUP, RUNNING, STARTING, Runtime
from periscope.service import ServiceSpec, Setting
from periscope.webhook import WebhookServer


# ----- webhook server: routes may arrive after the listener is up --------------------------------------
@pytest.mark.asyncio
async def test_webhook_routes_added_after_start():
    srv = WebhookServer("127.0.0.1", 0, secret="s")

    async def early(_):
        return web.json_response({"who": "early"})

    srv.add_route("POST", "/early", early)
    async with TestClient(TestServer(srv.app)) as c:               # the router is frozen from here on
        async def late(_):
            return web.json_response({"who": "late"})

        srv.add_route("POST", "/late", late)                        # used to raise "frozen router"
        assert srv.paths == ["/early", "/late"]
        r = await c.post("/late?token=s")
        assert r.status == 200 and (await r.json())["who"] == "late"
        r = await c.post("/early/?token=s")                         # trailing slash tolerated
        assert r.status == 200 and (await r.json())["who"] == "early"
        r = await c.post("/late")                                   # secret still enforced
        assert r.status == 401
        r = await c.post("/nope?token=s")
        assert r.status == 404 and "/late" in (await r.json())["paths"]
        srv.remove_route("POST", "/late")
        assert (await c.post("/late?token=s")).status == 404
        assert (await c.get("/health")).status == 200


# ----- presence: command sync only where the bot actually is ------------------------------------------
class _Tree:
    def __init__(self, forbid: set[int] = frozenset()):
        self.synced: list[int | None] = []
        self.forbid = forbid

    def copy_global_to(self, *, guild):
        pass

    async def sync(self, *, guild=None):
        gid = guild.id if guild else None
        if gid in self.forbid:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"message": "Missing Access", "code": 50001})
        self.synced.append(gid)
        return []


def _presence(guild_id=42, member_of=(42,), forbid=()):
    pres = Presence("plex", "tok", guild_id=guild_id, admin_role_ids=[], lab_name="lab")
    tree = _Tree(set(forbid))
    pres._BotBase__tree = tree  # what the `tree` property reads

    async def fetch_guilds(limit=200):
        for gid in member_of:
            yield SimpleNamespace(id=gid)

    pres.fetch_guilds = fetch_guilds  # type: ignore[method-assign]
    return pres, tree


def _service(pres, name, guild_id=None, healthy=True):
    env = {"GUILD_ID": str(guild_id)} if guild_id else {}
    sb = SimpleNamespace(name=name, healthy=healthy, env=env, presence=pres)
    sb.guild_id = guild_id or pres.guild_id
    return sb


@pytest.mark.asyncio
async def test_sync_skips_servers_the_bot_is_not_in():
    pres, tree = _presence(guild_id=42, member_of=(77,))
    pres.services = [_service(pres, "plexrequests", guild_id=77)]  # its own server, not the lab's
    await pres.sync_commands()
    assert tree.synced == [77] and pres.missing_guilds == {} and pres._synced


@pytest.mark.asyncio
async def test_sync_records_missing_server_instead_of_dying():
    pres, tree = _presence(guild_id=42, member_of=(77,))
    pres.services = [_service(pres, "proxmox"), _service(pres, "plexrequests", guild_id=77)]
    await pres.sync_commands()                                       # no exception: the presence still connects
    assert tree.synced == [77] and pres.missing_guilds == {42: "proxmox"}


@pytest.mark.asyncio
async def test_sync_forbidden_is_recorded():
    pres, tree = _presence(guild_id=42, member_of=(42,), forbid=(42,))
    pres.services = [_service(pres, "proxmox")]
    await pres.sync_commands()
    assert tree.synced == [] and pres.missing_guilds == {42: "proxmox"}


def test_wanted_guilds_falls_back_to_lab():
    pres, _ = _presence(guild_id=42)
    pres.services = [_service(pres, "broken", healthy=False)]
    assert pres.wanted_guilds() == {42: "lab"}


def test_invite_url_and_plain_language_errors():
    pres, _ = _presence()
    assert invite_url(None) is None and "client_id=5" in invite_url(5) and "applications.commands" in invite_url(5)
    assert "rejected the token of bot 'plex'" in explain_presence_error(pres, discord.LoginFailure("Improper token"))
    msg = explain_presence_error(pres, discord.PrivilegedIntentsRequired(None))
    assert "Server Members Intent" in msg and "developers/applications" in msg
    forbidden = discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), {"message": "Missing Access", "code": 50001})
    assert "not in the server" in explain_presence_error(pres, forbidden)
    assert "cannot reach Discord" in explain_presence_error(pres, ConnectionError("dns"))
    assert explain_presence_error(pres, ValueError("x")) == "ValueError: x"


# ----- runtime: states and where to fix them ---------------------------------------------------------
def _spec(name, **kw):
    async def build(bot):
        pass

    return ServiceSpec(name=name, title=name.title(), description="", group="infra", settings=kw.get("settings", []), build=build)


def test_service_status_words(tmp_path):
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.services["a"] = {"enabled": True, "presence": "default", "env": {}}
    s.services["b"] = {"enabled": True, "presence": "default", "env": {}}
    rt = Runtime(s, tmp_path)
    rt.specs = {"a": _spec("a"), "b": _spec("b", settings=[Setting("B_URL", required=True)])}
    rt.assemble()
    pres = rt.presences["default"]
    a = rt.services["a"]
    assert rt.service_status(a)["state"] == STARTING
    pres.last_error = "Discord rejected the token of bot 'default' — paste a new token on the Bots page"
    st = rt.service_status(a)
    assert st["state"] == ERROR and st["fix"] == "bots" and "rejected" in st["problem"]
    pres.last_error, pres.connected = None, True
    assert rt.service_status(a)["state"] == RUNNING
    pres.missing_guilds = {a.guild_id: "a"}
    st = rt.service_status(a)
    assert st["state"] == ERROR and "not in server" in st["problem"]
    a.healthy, a.last_error = False, "RuntimeError: boom"
    st = rt.service_status(a)
    assert st["state"] == ERROR and st["fix"] == "logs" and st["problem"].startswith("failed to start")
    full = rt.status()
    assert full["services"]["b"]["state"] == NEEDS_SETUP and full["services"]["b"]["fix"] == "settings"
    assert full["services"]["b"]["error"].startswith("needs B Url")
    assert full["presences"]["default"]["missing_guilds"] == {str(a.guild_id): "a"}


# ----- store: the default bot identity ---------------------------------------------------------------
def test_default_presence_and_tidy(tmp_path):
    s = Store(tmp_path / "config" / "periscope.yaml")
    assert s.default_presence() == "default"                     # fresh install: the shared identity
    s.presences["arr"] = {"token": "A", "label": "arr"}
    assert s.default_presence() == "arr"                         # `default` empty → first identity with a token
    assert s.presence_for("sonarr") == "arr" and s.token_for("sonarr") == "A"
    s.services["x"] = {"enabled": False, "presence": "default", "env": {}}
    assert s.tidy() is False                                     # something still points at default → keep it
    s.services["x"]["presence"] = "arr"
    assert s.tidy() is True and "default" not in s.presences and s.default_presence() == "arr"
    s.presences["default"] = {"token": "D", "label": "periscope"}
    assert s.default_presence() == "default" and s.tidy() is False


def test_migration_drops_empty_default(tmp_path):
    (tmp_path / "bots" / "proxmox").mkdir(parents=True)
    (tmp_path / "bots" / "proxmox" / ".env").write_text("DISCORD_TOKEN=tok\nGUILD_ID=1\nPVE_URL=https://pve\n")
    s = Store(tmp_path / "config" / "periscope.yaml")
    migrate_v1(s, tmp_path)
    assert set(s.presences) == {"proxmox"} and s.presence_for("proxmox") == "proxmox"
    assert s.presence_for("unifi") == "proxmox"                  # a new service reuses the identity that exists


# ----- runtime: the supervisor explains, resets and retries -----------------------------------------
@pytest.mark.asyncio
async def test_supervisor_retries_with_plain_language(tmp_path, monkeypatch):
    import periscope.runtime as runtime_mod

    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.services["a"] = {"enabled": True, "presence": "default", "env": {}}
    rt = Runtime(s, tmp_path)
    rt.specs = {"a": _spec("a")}
    rt.assemble()
    pres = rt.presences["default"]
    attempts = []

    async def fake_start(token):
        attempts.append(token)
        if len(attempts) == 1:
            raise discord.LoginFailure("Improper token has been passed.")
        if len(attempts) == 2:
            return                       # discord.py closed the client itself → must not end supervision
        rt.request_stop()
        return

    resets = []

    async def fake_reset(p):
        resets.append(p.name)

    monkeypatch.setattr(pres, "start", fake_start)
    monkeypatch.setattr(runtime_mod, "_reset_client", fake_reset)

    async def no_sleep(_):
        pass

    monkeypatch.setattr(runtime_mod.asyncio, "sleep", no_sleep)
    await rt._supervise(pres)
    assert attempts == ["T", "T", "T"] and resets == ["default", "default"]
    assert "lost its Discord connection" in pres.last_error and not pres.connected
    assert rt.service_status(rt.services["a"])["state"] == ERROR


# ----- several Discord servers, one bot ----------------------------------------------------------------
def test_servers_and_per_service_server(tmp_path):
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.lab.update({"name": "ztechnus.com", "guild_id": "42", "alert_channel_id": "2", "log_level": "DEBUG"})
    assert s.servers["main"]["name"] == "ztechnus.com" and s.globals["log_level"] == "DEBUG"
    assert "log_level" not in s.servers["main"] and "name" not in s.globals   # per-server vs global stay apart
    plex = s.add_server("plex", "Plex")
    plex.update({"guild_id": "77", "alert_channel_id": "9"})
    s.services["proxmox"] = {"enabled": True, "presence": "default", "env": {}}
    s.services["plexrequests"] = {"enabled": True, "presence": "default", "server": "plex", "env": {}}
    assert s.server_for("proxmox") == "main" and s.server_for("plexrequests") == "plex"
    assert s.env_for("proxmox")["GUILD_ID"] == "42" and s.env_for("proxmox")["ALERT_CHANNEL_ID"] == "2"
    env = s.env_for("plexrequests")
    assert env["GUILD_ID"] == "77" and env["ALERT_CHANNEL_ID"] == "9" and env["LAB_NAME"] == "Plex"
    assert env["LOG_LEVEL"] == "DEBUG"                                   # global settings follow every server
    assert s.guild_ids() == {"main": "42", "plex": "77"}
    # round trip, then remove the second server: its services fall back to the first
    s.save()
    s2 = Store.load(s.path)
    assert set(s2.servers) == {"main", "plex"} and s2.server_for("plexrequests") == "plex"
    assert s2.remove_server("plex") == ["plexrequests"] and s2.server_for("plexrequests") == "main"
    assert s2.remove_server("main") == []                                # the last server stays


def test_old_config_becomes_one_server(tmp_path):
    """A config written before multiple servers: the `lab` block turns into the first server, nothing lost."""
    p = tmp_path / "config" / "periscope.yaml"
    p.parent.mkdir(parents=True)
    p.write_text("version: 2\nlab: {name: ztechnus.com, color: 5A189A, guild_id: '42', alert_channel_id: '2',\n"
                 "  admin_role_ids: ['4'], log_level: WARNING, status_interval_s: 30}\n"
                 "services: {proxmox: {enabled: true, presence: default, env: {PVE_URL: 'https://pve'}}}\n")
    s = Store.load(p)
    assert list(s.servers) == ["main"] and s.servers["main"]["name"] == "ztechnus.com"
    assert s.servers["main"]["admin_role_ids"] == ["4"] and s.globals == {"log_level": "WARNING", "status_interval_s": 30}
    env = s.env_for("proxmox")
    assert env["LAB_NAME"] == "ztechnus.com" and env["GUILD_ID"] == "42" and env["LOG_LEVEL"] == "WARNING"
    assert env["ADMIN_ROLE_IDS"] == "4" and env["STATUS_INTERVAL_S"] == "30" and env["PVE_URL"] == "https://pve"


@pytest.mark.asyncio
async def test_one_bot_serves_every_server_its_services_use():
    """A bot registers its commands in each server its services post in — not only the first one."""
    pres, tree = _presence(guild_id=42, member_of=(42, 77))
    pres.services = [_service(pres, "proxmox", guild_id=42), _service(pres, "plexrequests", guild_id=77)]
    await pres.sync_commands()
    assert sorted(tree.synced) == [42, 77] and pres.missing_guilds == {}

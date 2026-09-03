"""Fixtures: a fake Runtime (real Store + hand-made ServiceSpecs + stub presences with a fake guild), a mocked
Discord REST API (httpx.MockTransport) and an httpx client with a signed session cookie. No network, no Discord."""

from __future__ import annotations

import time
from types import SimpleNamespace

import discord
import httpx
import pytest
from periscope import Store
from periscope.service import ServiceSpec, Setting
from periscope_web.app import create_app
from periscope_web.auth import SESSION_COOKIE, User
from periscope_web.discordapi import DiscordAPI
from periscope_web.logs import LogBuffer

GUILD_ID = 42
GOOD_TOKEN = "good-token-abc"


# ----- fake discord objects ----------------------------------------------------------------------------
class FakeCategory:
    type = discord.ChannelType.category

    def __init__(self, cid, name):
        self.id, self.name = cid, name


class FakeChannel:
    type = discord.ChannelType.text

    def __init__(self, cid, name, category=None):
        self.id, self.name, self.category = cid, name, category
        self.slowmode_delay = 0
        self.overwrites = {}
        self.edits = []

    async def set_permissions(self, target, *, overwrite=None, reason=None):
        self.overwrites[target.name] = overwrite

    async def edit(self, **kw):
        self.edits.append(kw)

    async def history(self, limit=None):
        return
        yield  # pragma: no cover


class FakeRole:
    def __init__(self, rid, name, color=0):
        self.id, self.name = rid, name
        self.colour = SimpleNamespace(value=color)


class FakeMember:
    def __init__(self, uid, roles=()):
        self.id, self.roles = uid, list(roles)
        self.added = []

    async def add_roles(self, *roles, reason=None):
        self.roles.extend(roles)
        self.added.extend(roles)


class FakeGuild:
    def __init__(self, gid=GUILD_ID):
        self.id = gid
        self.categories = [FakeCategory(10, "🧪 LAB STATUS")]
        self.text_channels = [FakeChannel(1001, "lab-status", self.categories[0]), FakeChannel(1002, "lab-alerts", self.categories[0]),
                              FakeChannel(1003, "git-anthill"), FakeChannel(1004, "op-anthill"), FakeChannel(1005, "general")]
        self.roles = [FakeRole(1, "@everyone"), FakeRole(2001, "lab-admin", 0xE67E22), FakeRole(2002, "bots", 0x5865F2)]
        self.default_role = self.roles[0]
        self.members = {999: FakeMember(999)}
        self.created: list[tuple[str, str]] = []
        self._next = 5000

    async def fetch_channels(self):
        return [*self.categories, *self.text_channels]

    async def fetch_roles(self):
        return list(self.roles)

    async def create_role(self, *, name, colour=None, mentionable=False, reason=None):
        self._next += 1
        r = FakeRole(self._next, name, getattr(colour, "value", 0))
        self.roles.append(r)
        self.created.append(("role", name))
        return r

    async def create_category(self, name, *, reason=None):
        self._next += 1
        c = FakeCategory(self._next, name)
        self.categories.append(c)
        self.created.append(("category", name))
        return c

    async def create_text_channel(self, name, *, category=None, reason=None):
        self._next += 1
        ch = FakeChannel(self._next, name, category)
        self.text_channels.append(ch)
        self.created.append(("channel", name))
        return ch

    def get_member(self, uid):
        return self.members.get(uid)

    async def fetch_member(self, uid):
        if uid not in self.members:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Member")
        return self.members[uid]


class FakeUser:
    def __init__(self, uid, name="periscope#0001"):
        self.id, self.name = uid, name

    def __str__(self):
        return self.name


class FakePresence:
    def __init__(self, name, guild, *, connected=True, uid=999):
        self.name = name
        self.connected = connected
        self.user = FakeUser(uid)
        self.application_id = uid
        self.services = []
        self._guild = guild

    def get_guild(self, gid):
        return self._guild if gid == self._guild.id else None


# ----- fake runtime -----------------------------------------------------------------------------------
async def _build(bot):  # pragma: no cover - never called
    pass


async def pve_check(env):
    return bool(env.get("PVE_TOKEN_SECRET")), f"checked {env.get('PVE_URL', '?')}"


def make_specs() -> dict[str, ServiceSpec]:
    pve = ServiceSpec(
        name="pve", title="Proxmox VE", description="Cluster board and alerts.", group="infra", build=_build, check=pve_check, slash="/pve",
        settings=[Setting("PVE_URL", type="url", required=True, group="Proxmox VE", help="API base URL"),
                  Setting("PVE_TOKEN_SECRET", type="secret", required=True, group="Proxmox VE"),
                  Setting("PVE_CPU_WARN", type="int", default="85", group="Thresholds"),
                  Setting("PVE_VERIFY_SSL", type="bool", default="false", group="Proxmox VE"),
                  Setting("PVE_MODE", type="choice", default="auto", choices=["auto", "fast"], group="Thresholds"),
                  Setting("MEDIA_CHANNEL_ID", type="channel", group="Thresholds"),
                  Setting("PVE_ROLE_ID", type="role", group="Thresholds"),
                  Setting("PVE_TAGS", type="list", group="Thresholds")])
    sonarr = ServiceSpec(name="sonarr", title="Sonarr", description="TV.", group="media", build=_build, check=None, slash="/sonarr",
                         settings=[Setting("SONARR_URL", type="url", required=True), Setting("SONARR_API_KEY", type="secret", required=True)],
                         needs_webhook=True, webhook_paths=["/sonarr"])
    github = ServiceSpec(name="github", title="GitHub", description="Org feed.", group="dev", build=_build, check=None, slash="/gh",
                         settings=[Setting("GITHUB_ORG"), Setting("GITHUB_TOKEN", type="secret"), Setting("GITHUB_REPO_CHANNEL_MAP", type="list"),
                                   Setting("GITHUB_FEED_CHANNEL_ID", type="channel"), Setting("GITHUB_CI_CHANNEL_ID", type="channel"),
                                   Setting("GITHUB_MIRROR_TO_FEED", type="bool", default="false")])
    return {"pve": pve, "sonarr": sonarr, "github": github}


class FakeRuntime:
    def __init__(self, store: Store, root, specs, presences=None, states=None):
        self.store = store
        self.root = root
        self.data_dir = root / "data"
        self.specs = specs
        self.presences = presences or {}
        self.services = {}
        self.skipped = {}
        self.started = time.time()
        self.states = states or {}

    def status(self):
        out = {"pid": 1, "started": self.started, "uptime_s": int(time.time() - self.started), "presences": {}, "services": {}}
        for n, p in self.presences.items():
            out["presences"][n] = {"connected": p.connected, "user": str(p.user) if p.connected else None, "services": [s for s in p.services]}
        out["services"] = {k: dict(v) for k, v in self.states.items()}
        return out


def make_store(path) -> Store:
    s = Store(path)
    s.lab.update({"name": "testlab", "guild_id": str(GUILD_ID), "admin_role_ids": ["2001"], "alert_channel_id": "1002",
                  "status_channel_id": "1001"})
    s.web.update({"session_secret": "s" * 32, "oauth_client_id": "cid", "oauth_client_secret": "csecret", "base_url": "http://test"})
    s.presences["default"] = {"token": GOOD_TOKEN, "label": "periscope"}
    s.presences["arr"] = {"token": "", "label": "arr"}
    s.services["pve"] = {"enabled": True, "presence": "default", "env": {"PVE_URL": "https://pve:8006", "PVE_TOKEN_SECRET": "s3cret", "PVE_CPU_WARN": "90"}}
    s.services["sonarr"] = {"enabled": False, "presence": "arr", "env": {}}
    s.services["github"] = {"enabled": True, "presence": "default", "env": {"GITHUB_REPO_CHANNEL_MAP": "Anthill=1003,micro*=1003"}}
    s.save()
    return s


# ----- mocked Discord REST -----------------------------------------------------------------------------
def discord_handler(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        auth = request.headers.get("Authorization", "")
        calls.append((request.method, path, auth.split(" ")[0] if auth else ""))
        if path == "/api/v10/oauth2/token":
            body = request.content.decode()
            if "code=goodcode" in body:
                return httpx.Response(200, json={"access_token": "acc", "token_type": "Bearer"})
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "bad code"})
        if auth.startswith("Bearer "):
            if path == "/api/v10/users/@me":
                return httpx.Response(200, json={"id": "555", "username": "alice", "global_name": "Alice", "avatar": None})
            if path == f"/api/v10/users/@me/guilds/{GUILD_ID}/member":
                roles = ["2001"] if auth == "Bearer acc" else []
                return httpx.Response(200, json={"roles": roles, "user": {"id": "555"}})
            return httpx.Response(404, json={"message": "Unknown"})
        if auth != f"Bot {GOOD_TOKEN}":
            return httpx.Response(401, json={"message": "401: Unauthorized"})
        if path == "/api/v10/users/@me":
            return httpx.Response(200, json={"id": "777", "username": "periscope", "bot": True})
        if path == "/api/v10/users/@me/guilds":
            return httpx.Response(200, json=[{"id": str(GUILD_ID), "name": "THE LAB", "owner": True}, {"id": "43", "name": "Other", "owner": False}])
        if path == f"/api/v10/guilds/{GUILD_ID}":
            return httpx.Response(200, json={"id": str(GUILD_ID), "name": "THE LAB", "owner_id": "555"})
        if path == f"/api/v10/guilds/{GUILD_ID}/channels":
            return httpx.Response(200, json=[{"id": "10", "name": "LAB", "type": 4}, {"id": "1001", "name": "lab-status", "type": 0, "parent_id": "10"},
                                             {"id": "1002", "name": "lab-alerts", "type": 0, "parent_id": "10"}, {"id": "1003", "name": "git-anthill", "type": 0}])
        if path == f"/api/v10/guilds/{GUILD_ID}/roles":
            return httpx.Response(200, json=[{"id": "1", "name": "@everyone"}, {"id": "2001", "name": "lab-admin", "color": 0}, {"id": "2002", "name": "bots"}])
        return httpx.Response(404, json={"message": "Unknown"})

    return handler


# ----- fixtures ---------------------------------------------------------------------------------------
@pytest.fixture
def guild():
    return FakeGuild()


@pytest.fixture
def store(tmp_path):
    return make_store(tmp_path / "config" / "periscope.yaml")


@pytest.fixture
def api_calls():
    return []


@pytest.fixture
def runtime(store, tmp_path, guild):
    pres = FakePresence("default", guild)
    pres.services = ["pve", "github"]
    states = {"pve": {"state": "running", "presence": "default", "error": None},
              "github": {"state": "needs setup", "presence": "default", "error": "needs Github Org — fill them in under Settings", "fix": "settings"}}
    rt = FakeRuntime(store, tmp_path, make_specs(), presences={"default": pres}, states=states)
    rt.started = time.time() + 5  # the store was saved *before* the runtime started → not dirty
    return rt


@pytest.fixture
def logbuf():
    return LogBuffer(maxlen=50)


@pytest.fixture
def make_app(runtime, api_calls, logbuf, monkeypatch):
    """Factory: build the app against the current (possibly tweaked) store."""
    monkeypatch.delenv("PERISCOPE_WEB_NOAUTH", raising=False)

    def factory(setup_token="setup-token-xyz"):
        api = DiscordAPI(transport=httpx.MockTransport(discord_handler(api_calls)))
        application = create_app(runtime, discord_api=api, setup_token=setup_token, log_buffer=logbuf)
        application.state.restart_delay = 0.01
        return application

    return factory


@pytest.fixture
def app(make_app):
    return make_app()


@pytest.fixture
def reload(store):
    return lambda: Store.load(store.path)


@pytest.fixture
def user():
    return User(id="555", name="Alice", via="discord", csrf="csrf-token-1")


@pytest.fixture
async def anon(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False) as c:
        yield c


@pytest.fixture
async def client(app, user):
    """Signed-in client: session cookie + CSRF header the way HTMX sends it (hx-headers on <body>)."""
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", follow_redirects=False,
                                 headers={"X-CSRF-Token": user.csrf}, cookies={SESSION_COOKIE: app.state.sessions.cookie_value(user)}) as c:
        yield c



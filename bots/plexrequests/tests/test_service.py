"""The plexrequests service built through Store → Runtime.assemble() → spec.build(): registration (commands, cogs,
persistent views, intents), legacy state import, and the invite / request / watcher / revoke / feed / board flows
against fake Plex, Seerr and Radarr/Sonarr clients and fake Discord objects. No network."""

import asyncio
import json
from types import SimpleNamespace

import discord
import pytest
from periscope import Store
from periscope.http import HttpClient, HttpError
from periscope.runtime import Runtime

import periscope_plexrequests.service as service_mod
from periscope_plexrequests.cogs.invites import INVITE_MESSAGE_KEY, EmailModal
from periscope_plexrequests.cogs.requests import REQUEST_MESSAGE_KEY, ResultsView
from periscope_plexrequests.service import SERVICES, check, import_legacy_state

LAB_GUILD, PLEX_GUILD = 42, 77
INVITE_CH, REQ_CH, MOVIES_CH, TV_CH, STATUS_CH, NEW_CH = 100, 200, 300, 301, 400, 500
BASE = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "pt", "CHANNEL_ID": str(INVITE_CH), "REQUESTS_CHANNEL_ID": str(REQ_CH),
        "PLEXREQ_GUILD_ID": str(PLEX_GUILD), "SERVER_NAME": "lab.example", "PLEX_LINK": "plex.lab.example",
        "MOVIES_CHANNEL": "movies", "TV_CHANNEL": "", "STATUS_CHANNEL": "plex-status", "NEW_CHANNEL": str(NEW_CH),
        "REQUESTS_ROLE_NAME": "plex members", "AUTO_REVOKE": "1"}
ARR = {"RADARR_URL": "http://radarr:7878", "RADARR_API_KEY": "rk", "SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": "sk",
       "RADARR_PROFILE": "Ultra-HD", "FALLBACK_BEFORE_YEAR": "2016"}
SEERR = {"OVERSEERR_URL": "http://seerr:5055", "OVERSEERR_API_KEY": "ok"}


# ----- fakes: Discord ----------------------------------------------------------------------------------------

def not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "not found")


class FakeMessage:
    _ids = 1000

    def __init__(self, channel, content=None, embed=None, view=None, author=None, delete_after=None):
        FakeMessage._ids += 1
        self.id = FakeMessage._ids
        self.channel, self.content, self.author = channel, content, author
        self.embeds = [embed] if embed is not None else []
        self.view, self.delete_after = view, delete_after
        self.deleted, self.edits = False, []
        self.guild = getattr(channel, "guild", None)

    async def delete(self):
        self.deleted = True
        self.channel.messages.pop(self.id, None)

    async def edit(self, **kw):
        self.edits.append(kw)
        if kw.get("embed") is not None:
            self.embeds = [kw["embed"]]
        if "view" in kw:
            self.view = kw["view"]


class FakeChannel:
    def __init__(self, cid: int, name: str, guild=None):
        self.id, self.name, self.guild = cid, name, guild
        self.messages: dict[int, FakeMessage] = {}
        self.sent: list[FakeMessage] = []
        self.last_message_id = None

    async def send(self, content=None, *, embed=None, view=None, delete_after=None, **kw):
        msg = FakeMessage(self, content, embed, view, delete_after=delete_after)
        self.messages[msg.id] = msg
        self.sent.append(msg)
        self.last_message_id = msg.id
        return msg

    async def fetch_message(self, mid: int):
        if mid in self.messages:
            return self.messages[mid]
        raise not_found()


class FakeGuild:
    def __init__(self, gid: int, channels: list[FakeChannel], roles=()):
        self.id = gid
        self.text_channels = channels
        self.roles = list(roles)
        for c in channels:
            c.guild = self

    async def create_role(self, *, name, colour=None, reason=None):
        role = SimpleNamespace(id=900 + len(self.roles), name=name)
        self.roles.append(role)
        return role


class FakeMember:
    def __init__(self, uid: int, name: str, guild, roles=(), admin=False):
        self.id, self.display_name, self.guild = uid, name, guild
        self.roles = list(roles)
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.mention = f"<@{uid}>"
        self.dms: list[str] = []
        self.bot = False

    async def add_roles(self, role, reason=None):
        self.roles.append(role)

    async def send(self, text):
        self.dms.append(text)

    def __str__(self):
        return self.display_name


class FakeInteraction:
    def __init__(self, user, channel=None, client=None, message=None):
        self.user, self.channel, self.client, self.message = user, channel, client, message
        self.sent: list[tuple] = []
        self.edits: list[dict] = []
        self.modals: list = []
        self._done = False
        self.response = SimpleNamespace(defer=self._defer, send_message=self._send, is_done=lambda: self._done,
                                        send_modal=self._modal, edit_message=self._edit)
        self.followup = SimpleNamespace(send=self._send)

    async def _defer(self, **kw):
        self._done = True

    async def _send(self, content=None, **kw):
        self._done = True
        self.sent.append((content, kw))
        return FakeMessage(self.channel or FakeChannel(0, "eph"), content)

    async def _modal(self, modal):
        self.modals.append(modal)

    async def _edit(self, **kw):
        self.edits.append(kw)

    async def edit_original_response(self, **kw):
        self.edits.append(kw)


# ----- fakes: clients -----------------------------------------------------------------------------------------

def mk(title, year, media_type, status=1, tmdb=1, backend="arr"):
    d = {"tmdb_id": tmdb, "media_type": media_type, "title": title, "year": year, "status": status, "poster": None,
         "overview": f"about {title}", "backend": backend}
    if backend == "arr":
        d["arr_raw"] = {"title": title, "year": int(year) if year else None, "tmdbId": tmdb}
        if status != 1:
            d["arr_raw"]["id"] = 50 + tmdb
    return d


class FakePlex:
    instances: list = []

    def __init__(self, url, token, libraries="all", timeout=20):
        self.url, self.token, self.libraries = url, token, libraries
        self.invites: list[str] = []
        self.revoked: list[str] = []
        self.result = ("sent", "Invite sent!")
        self.streams = ["**alice** — Heat (42%)"]
        self.recent = [{"key": "1", "kind": "movie", "title": "Heat", "year": 1995, "summary": "cops"}]
        FakePlex.instances.append(self)

    def invite(self, email):
        self.invites.append(email)
        return self.result

    def revoke(self, email):
        self.revoked.append(email)
        return True

    def sessions(self):
        return self.streams

    def recently_added(self, limit=30):
        return self.recent


class FakeArr:
    def __init__(self, kind, base_url, api_key, profile_name="", root_folder="", fallback_profile="", fallback_before_year=0):
        self.kind, self.base, self.api_key = kind, base_url, api_key
        self.profile_name, self.fallback_before_year = profile_name, fallback_before_year
        self.media_type = "movie" if kind == "radarr" else "tv"
        self.added: list[dict] = []
        self.available: dict[int, int] = {}
        self.closed = False
        self.results = ([mk("Heat", "1995", "movie", tmdb=1), mk("Dune", "2021", "movie", status=5, tmdb=2)]
                        if kind == "radarr" else [mk("The Expanse", "2015", "tv", tmdb=3), mk("Severance", "2022", "tv", status=2, tmdb=4)])

    async def lookup(self, query, limit=8):
        return [r for r in self.results if query.lower() in r["title"].lower() or "*" in query][:limit]

    async def add(self, raw):
        if raw.get("id"):
            return (False, "already exists", raw["id"])
        self.added.append(raw)
        return (True, "added", 500 + len(self.added))

    async def is_available(self, arr_id):
        return self.available.get(arr_id)

    async def queue_summary(self, top=3):
        return (2, ["Heat — 5m"]) if self.kind == "radarr" else (0, [])

    async def disk_space(self):
        return [("/data", 500 * 1024 ** 3, 2 * 1024 ** 4)]

    async def close(self):
        self.closed = True


class FakeSeerr:
    def __init__(self, base_url, api_key):
        self.base, self.api_key = base_url, api_key
        self.requested: list[tuple] = []
        self.status: dict[int, int] = {}
        self.closed = False

    async def search(self, query, limit=8):
        return [mk("Heat", "1995", "movie", tmdb=1, backend="seerr"), mk("Dune", "2021", "movie", status=5, tmdb=2, backend="seerr")]

    async def request(self, media_type, tmdb_id):
        self.requested.append((media_type, tmdb_id))
        return (True, "requested", 900 + tmdb_id)

    async def media_status(self, media_id):
        return self.status.get(media_id)

    async def close(self):
        self.closed = True


# ----- harness -------------------------------------------------------------------------------------------------

@pytest.fixture
def fakes(monkeypatch):
    FakePlex.instances = []
    monkeypatch.setattr(service_mod, "PlexGateway", FakePlex)
    monkeypatch.setattr(service_mod, "ArrClient", FakeArr)
    monkeypatch.setattr(service_mod, "SeerrClient", FakeSeerr)


def make_runtime(tmp_path, env: dict, lab_guild: int | None = LAB_GUILD) -> Runtime:
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.lab.update({"guild_id": str(lab_guild) if lab_guild else "", "name": "lab1"})
    s.services["plexrequests"] = {"enabled": True, "presence": "default", "env": env}
    rt = Runtime(s, tmp_path)
    rt.assemble()
    assert not rt.skipped, rt.skipped
    return rt


class World:
    """A Plex guild with the channels the service uses, wired onto the presence."""

    def __init__(self, pres):
        self.invite = FakeChannel(INVITE_CH, "join-plex")
        self.requests = FakeChannel(REQ_CH, "media-requests")
        self.movies = FakeChannel(MOVIES_CH, "movies")
        self.tv = FakeChannel(TV_CH, "tv")
        self.status = FakeChannel(STATUS_CH, "plex-status")
        self.new = FakeChannel(NEW_CH, "new-on-plex")
        self.channels = {c.id: c for c in (self.invite, self.requests, self.movies, self.tv, self.status, self.new)}
        self.guild = FakeGuild(PLEX_GUILD, list(self.channels.values()))
        pres._connection._guilds[PLEX_GUILD] = self.guild
        pres.get_channel = lambda cid: self.channels.get(cid)

    def member(self, uid=1, name="alice", roles=(), admin=False) -> FakeMember:
        return FakeMember(uid, name, self.guild, roles, admin)

    def message(self, author, channel, content) -> FakeMessage:
        msg = FakeMessage(channel, content, author=author)
        channel.messages[msg.id] = msg
        channel.last_message_id = msg.id
        return msg


async def build(tmp_path, env: dict, lab_guild: int | None = LAB_GUILD):
    rt = make_runtime(tmp_path, env, lab_guild)
    pres = rt.presences["default"]

    async def never_ready():
        await asyncio.Event().wait()

    pres.wait_until_ready = never_ready
    sb = rt.services["plexrequests"]
    await sb.spec.build(sb)
    return rt, pres, sb, World(pres)


def cog(sb, name):
    return sb.get_cog(name)


# ----- spec + registration ----------------------------------------------------------------------------------------

def test_spec():
    spec = SERVICES[0]
    assert spec.name == "plexrequests" and spec.group == "media" and spec.slash == "/requests" and spec.check is check
    assert spec.intents == ["members", "message_content"] and not spec.needs_webhook and spec.webhook_paths == []
    assert "Plex" in spec.title


@pytest.mark.asyncio
async def test_build_registers_commands_cogs_views_and_intents(tmp_path, fakes):
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR})
    assert pres.intents.members and pres.intents.message_content         # requested by the spec, unioned by the runtime
    g = discord.Object(id=PLEX_GUILD)
    # the Plex guild differs from the lab guild → this service's commands live in the Plex guild only
    assert pres.tree.get_command("requests") is None and pres.tree.get_command("plexinvite") is None
    group = pres.tree.get_command("requests", guild=g)
    assert isinstance(group, discord.app_commands.Group)
    assert {c.name for c in group.commands} == {"request", "mystatus", "plexstats"}
    assert group.get_command("plexstats").checks and not group.get_command("request").checks
    assert pres.tree.get_command("plexinvite", guild=g) is not None
    assert sb.plexreq.command_guild.id == PLEX_GUILD and not sb.plexreq.synced
    # six cogs, namespaced on the presence
    names = {c.qualified_name for c in pres.cogs.values()}
    assert names == {f"plexrequests:{n}" for n in ("InvitesCog", "RequestsCog", "BoardCog", "NewOnPlexCog", "RevokeCog", "StatsCog")}
    # persistent views re-added on build: current custom ids plus the ones the standalone bot used
    ids = {v.children[0].custom_id for v in pres.persistent_views}
    assert ids >= {"plexrequests:invite", "plexrequests:request", "ztplex:invite", "ztplex:request"}
    assert len(sb.plexreq.persistent_views) == 4
    # clients wired from env: auto without Seerr → native arr with both apps, profile settings passed through
    ctx = sb.plexreq
    assert ctx.backend.active == "arr" and ctx.backend.seerr is None
    assert ctx.backend.radarr.profile_name == "Ultra-HD" and ctx.backend.radarr.fallback_before_year == 2016
    assert ctx.plex.url == "http://plex:32400" and ctx.plex.token == "pt" and ctx.cfg.guild_id == PLEX_GUILD
    assert cog(sb, "RequestsCog").watch_available.is_running()          # watcher armed (parked in before_loop)
    assert cog(sb, "BoardCog").status_board.is_running() and cog(sb, "NewOnPlexCog").new_on_plex.is_running()
    assert rt.status()["services"]["plexrequests"]["presence"] == "default"
    await sb.unload()
    assert ctx.backend.radarr.closed and ctx.backend.sonarr.closed
    assert pres.tree.get_command("requests", guild=g).commands == []   # cogs took their commands with them
    assert pres.tree.get_command("plexinvite", guild=g) is None


@pytest.mark.asyncio
async def test_same_guild_registers_globally_and_no_backend_parks_requests(tmp_path, fakes):
    env = {**BASE, "PLEXREQ_GUILD_ID": str(LAB_GUILD), "STATUS_CHANNEL": "", "NEW_CHANNEL": ""}
    rt, pres, sb, world = await build(tmp_path, env)
    assert sb.plexreq.command_guild is None
    assert pres.tree.get_command("requests") is not None and pres.tree.get_command("plexinvite") is not None
    assert sb.plexreq.backend.active == "" and not cog(sb, "RequestsCog").watch_available.is_running()
    assert not cog(sb, "BoardCog").status_board.is_running() and not cog(sb, "NewOnPlexCog").new_on_plex.is_running()
    await sb.plexreq.sync_commands()                                   # no-op for global commands
    assert not sb.plexreq.synced
    await sb.unload()


@pytest.mark.asyncio
async def test_plex_guild_without_lab_guild(tmp_path, fakes):
    rt, pres, sb, world = await build(tmp_path, {**BASE, **SEERR}, lab_guild=None)
    assert sb.plexreq.command_guild.id == PLEX_GUILD and sb.plexreq.backend.active == "seerr"
    assert pres.tree.get_command("requests", guild=discord.Object(id=PLEX_GUILD)) is not None
    await sb.unload()


# ----- legacy state ------------------------------------------------------------------------------------------------

LEGACY_STATE = {"invite_message_id": 111, "request_message_id": 222, "status_message_id": 333,
                "emails": {"1": "alice@example.com"}, "plex_seen": ["9", "8"],
                "watches": [{"backend": "seerr", "media_id": 5, "channel_id": REQ_CH, "message_id": 444, "requester": "alice",
                             "requester_id": 1, "title": "Heat", "added": 1.0}],
                "requests": {"1": [{"title": "Heat", "year": "1995", "type": "movie", "ts": 1.0, "status": "queued", "msg": 444}]}}
LEGACY_STATS = {"since": 1.0, "totals": {"search": 3}, "users": {"1": {"name": "alice", "last": 2.0, "events": {"search": 3}}}}


@pytest.mark.asyncio
async def test_import_legacy_state_once(tmp_path, fakes):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "state.json").write_text(json.dumps(LEGACY_STATE))
    (legacy / "stats.json").write_text(json.dumps(LEGACY_STATS))
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR})
    assert not sb.state.get("legacy_imported")                         # the default location does not exist here
    sb.state.set("invite_message_id", 999)                             # something the service already knows wins
    out = import_legacy_state(sb, legacy)
    assert out["imported"] and set(out["keys"]) == {"request_message_id", "status_message_id", "emails", "plex_seen", "watches",
                                                    "requests", "stats"}
    assert sb.state.get("invite_message_id") == 999 and sb.state.get("request_message_id") == 222
    assert sb.plexreq.records.email_for(1) == "alice@example.com" and sb.plexreq.records.watches()[0]["media_id"] == 5
    assert sb.plexreq.records.history(1)[0]["title"] == "Heat" and sb.plexreq.stats.data()["totals"]["search"] == 3
    assert sb.state.get("legacy_imported") is True and sb.state.get("legacy_imported_from") == str(legacy)
    # idempotent: a second call changes nothing, even after the old files change
    (legacy / "state.json").write_text(json.dumps({**LEGACY_STATE, "request_message_id": 1}))
    assert import_legacy_state(sb, legacy) == {"imported": False, "reason": "already imported"}
    assert sb.state.get("request_message_id") == 222
    # the file on disk carries the namespaced keys
    data = json.loads((tmp_path / "data" / "state.json").read_text())
    assert data["svc:plexrequests:legacy_imported"] is True and data["svc:plexrequests:emails"] == {"1": "alice@example.com"}
    await sb.unload()


@pytest.mark.asyncio
async def test_build_imports_from_the_default_location(tmp_path, fakes, monkeypatch):
    legacy = tmp_path / "opt-old"
    legacy.mkdir()
    (legacy / "state.json").write_text(json.dumps(LEGACY_STATE))
    monkeypatch.setattr(service_mod, "PLEXREQUESTS_LEGACY_DIR", legacy)
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR})
    assert sb.state.get("legacy_imported") and sb.plexreq.records.message_id(INVITE_MESSAGE_KEY) == 111
    assert sb.state.get("stats") is None                               # no stats.json → nothing invented
    await sb.unload()
    # nothing to import → flag stays unset so a later appearance is still picked up
    monkeypatch.setattr(service_mod, "PLEXREQUESTS_LEGACY_DIR", tmp_path / "nowhere")
    rt2, pres2, sb2, _ = await build(tmp_path / "second", {**BASE, **ARR})
    assert import_legacy_state(sb2)["imported"] is False and not sb2.state.get("legacy_imported")
    await sb2.unload()


# ----- invites ---------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invite_flow(tmp_path, fakes):
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR})
    inv = cog(sb, "InvitesCog")
    plex = FakePlex.instances[-1]
    alice = world.member()
    status, text = await inv.run_invite(alice, " alice@example.com ")
    assert status == "sent" and text.startswith("📬 Invite sent to `alice@example.com`") and "plex members" in text
    assert plex.invites == ["alice@example.com"] and plex.libraries == "all"
    assert [r.name for r in world.guild.roles] == ["plex members"] and alice.roles[0].name == "plex members"
    assert sb.plexreq.records.email_for(1) == "alice@example.com"
    assert sb.plexreq.stats.data()["totals"]["invite_sent"] == 1
    # already has access → refreshed share, still gets the role note only once (role already held)
    plex.result = ("updated", "That account already has access — library share refreshed.")
    status, text = await inv.run_invite(alice, "alice@example.com")
    assert status == "updated" and text.startswith("✅ That account already has access") and "role" not in text
    # bad input and the 3-per-10-minutes cooldown (the two calls above already count)
    assert (await inv.run_invite(alice, "not-an-email"))[0] == "error"
    assert (await inv.run_invite(alice, "alice@example.com"))[0] == "updated"
    status, text = await inv.run_invite(alice, "alice@example.com")
    assert status == "error" and "Too many attempts" in text and len(plex.invites) == 3
    # errors from Plex grant nothing
    bob = world.member(2, "bob")
    plex.result = ("error", "Plex said no: boom")
    status, text = await inv.run_invite(bob, "bob@example.com")
    assert status == "error" and text == "❌ Plex said no: boom" and bob.roles == [] and sb.plexreq.records.email_for(2) is None

    # typed email in the invite channel: message deleted, result by DM, short public note, sticky embed re-posted
    plex.result = ("sent", "Invite sent!")
    carol = world.member(3, "carol")
    msg = world.message(carol, world.invite, "hi, my plex is carol@example.com")
    await inv.on_message(msg)
    assert msg.deleted and carol.dms and carol.dms[0].startswith("📬 Invite sent to `carol@example.com`")
    note = world.invite.sent[0]
    assert note.content.startswith("<@3> 📬 I removed your message") and "on its way" in note.content and note.delete_after == 45
    sticky = world.invite.sent[-1]
    assert sticky.embeds[0].title == "🎬  lab.example Plex — get access" and sticky.view.children[0].custom_id == "plexrequests:invite"
    assert sb.plexreq.records.message_id(INVITE_MESSAGE_KEY) == sticky.id
    # a message somewhere else, or from a bot, is ignored
    dave = world.member(4, "dave")
    other = world.message(dave, world.movies, "dave@example.com")
    await inv.on_message(other)
    assert not other.deleted and dave.dms == []
    # slash command + modal both go through run_invite, ephemerally
    i = FakeInteraction(world.member(5, "erin"), client=pres)
    await inv.plexinvite.callback(inv, i, "erin@example.com") if hasattr(inv.plexinvite, "callback") else await inv.plexinvite(i, "erin@example.com")
    assert i.sent[-1][1]["ephemeral"] and i.sent[-1][0].startswith("📬 Invite sent to `erin@example.com`")
    modal = EmailModal(inv)
    modal.email._value = "frank@example.com"
    j = FakeInteraction(world.member(6, "frank"), client=pres)
    await modal.on_submit(j)
    assert j.sent[-1][0].startswith("📬 Invite sent to `frank@example.com`")
    assert plex.invites[-2:] == ["erin@example.com", "frank@example.com"]
    # the button opens the modal
    view = sb.plexreq.persistent_views[0]
    k = FakeInteraction(world.member(7, "gina"), client=pres)
    await view.on_click(k)
    assert isinstance(k.modals[0], EmailModal) and sb.plexreq.stats.data()["totals"]["invite_button"] == 1
    await sb.unload()


@pytest.mark.asyncio
async def test_on_ready_ensures_sticky_embeds_and_role(tmp_path, fakes):
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR})
    inv, req = cog(sb, "InvitesCog"), cog(sb, "RequestsCog")
    synced = []

    async def fake_sync(*, guild=None):
        synced.append(guild.id)
        return [1, 2, 3]

    pres.tree.sync = fake_sync
    sb.plexreq.records.set_message_id(REQUEST_MESSAGE_KEY, 5)          # remembered but gone → re-posted
    await inv.on_ready()
    await req.on_ready()
    assert synced == [PLEX_GUILD] and sb.plexreq.synced
    assert world.invite.sent[0].embeds[0].title.endswith("get access") and [r.name for r in world.guild.roles] == ["plex members"]
    assert world.requests.sent[0].embeds[0].title == "🍿  Request movies & TV shows"
    assert sb.plexreq.records.message_id(REQUEST_MESSAGE_KEY) == world.requests.sent[0].id
    # a second ready (reconnect) edits in place instead of posting again
    inv._ready_once = req._ready_once = False
    await inv.on_ready()
    await req.on_ready()
    assert len(world.invite.sent) == 1 and world.invite.sent[0].edits and len(world.requests.sent) == 1
    assert synced == [PLEX_GUILD]
    await sb.unload()


# ----- requests --------------------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_flow_native_arr(tmp_path, fakes):
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR})
    req = cog(sb, "RequestsCog")
    role = SimpleNamespace(id=1, name="plex members")
    alice = world.member(1, "alice", roles=[role])
    # gate: role required, admins pass, cooldown at 5 searches
    nobody = world.member(9, "nobody")
    err, _ = await req.start_request_search(nobody, "heat")
    assert err.startswith("🔒 You need the **plex members** role") and "<#100>" in err
    assert (await req.start_request_search(world.member(8, "root", admin=True), "heat"))[0] is None
    assert (await req.start_request_search(alice, "x"))[0] == "Give me a title between 2 and 100 characters."
    # arr mode interleaves Radarr + Sonarr results
    err, results = await req.start_request_search(alice, "**")
    assert err is None and [r["title"] for r in results] == ["Heat", "The Expanse", "Dune", "Severance"]
    assert (await req.start_request_search(alice, "zzz"))[0].startswith("🔍 No movies or shows found")
    for _ in range(3):
        await req.start_request_search(alice, "heat")
    assert (await req.start_request_search(alice, "heat"))[0].startswith("⏳ Too many searches")

    # already on Plex / already queued: no request made
    text, card = await req.submit_request(alice, results[2], world.requests)
    assert text == "✅ **Dune (2021)** is already on Plex — go watch it!" and card.title == "🎬  Dune (2021)"
    text, _ = await req.submit_request(alice, results[3], world.requests)
    assert text.startswith("⏳ **Severance (2022)** was already requested")
    assert sb.plexreq.backend.radarr.added == [] and sb.plexreq.backend.sonarr.added == []

    # a new movie: added to Radarr, card announced in #movies (by name), watched, in alice's history
    text, card = await req.submit_request(alice, results[0], world.requests)
    assert text == "🎬 **Heat (1995)** requested! Radarr has it now — it'll appear on Plex once it's downloaded."
    assert sb.plexreq.backend.radarr.added[0]["title"] == "Heat"
    ann = world.movies.sent[0]
    assert ann.embeds[0].title == "🎬  Heat (1995)" and ann.embeds[0].footer.text == "Requested by alice • added to the download queue"
    assert world.requests.sent == []                                   # nothing leaked into the requests channel
    w = sb.plexreq.records.watches()[0]
    assert w["backend"] == "arr" and w["kind"] == "radarr" and w["arr_id"] == 501 and w["message_id"] == ann.id and w["channel_id"] == MOVIES_CH
    assert sb.plexreq.records.history(1)[0] == {**sb.plexreq.records.history(1)[0], "title": "Heat", "status": "queued", "msg": ann.id}

    # a new show: TV_CHANNEL empty → the requests channel, so the sticky button embed is re-posted below it
    text, _ = await req.submit_request(alice, results[1], world.requests)
    assert text.startswith("📺 **The Expanse (2015)** requested! Sonarr")
    assert world.requests.sent[0].embeds[0].title == "📺  The Expanse (2015)"
    assert world.requests.sent[-1].embeds[0].title == "🍿  Request movies & TV shows" and world.requests.last_message_id == world.requests.sent[-1].id
    assert sb.plexreq.records.message_id(REQUEST_MESSAGE_KEY) == world.requests.sent[-1].id
    assert len(sb.plexreq.records.watches()) == 2

    # the watcher: Radarr says available → green card, footer with PLEX_LINK, 🎉 ping, history + watch updated
    sb.plexreq.backend.radarr.available[501] = 5
    sb.plexreq.backend.sonarr.available[501] = 2                        # the show is still downloading
    await req.check_watches()
    e = ann.embeds[0]
    assert e.colour == discord.Colour.from_str("#2ecc71") and e.footer.text == "Requested by alice • Available to watch on plex.lab.example now"
    ping = world.movies.sent[-1]
    assert ping.content == "🎉 <@1> — **Heat** is ready! Available to watch on plex.lab.example now."
    assert [w["title"] for w in sb.plexreq.records.watches()] == ["The Expanse"]
    assert sb.plexreq.records.history(1)[0]["status"] == "available" and sb.plexreq.records.history(1)[1]["status"] == "queued"
    st = sb.plexreq.stats.data()["totals"]
    assert st["request_ok"] == 2 and st["already_on_plex"] == 1 and st["already_requested"] == 1 and st["became_available"] == 1
    # stale watches are dropped quietly; a deleted announcement stops the watch
    sb.plexreq.records.drop_watches({w["message_id"] for w in sb.plexreq.records.watches()})
    sb.plexreq.records.add_watch({"backend": "arr", "kind": "sonarr", "arr_id": 77}, MOVIES_CH, 1, "x", 1, "old")
    await req.check_watches()
    assert sb.plexreq.records.watches() == [] or sb.plexreq.records.watches()[0]["arr_id"] == 77
    await sb.unload()


@pytest.mark.asyncio
async def test_request_flow_seerr_menus_and_typed_titles(tmp_path, fakes):
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR, **SEERR, "REQUESTS_ROLE_NAME": ""})
    req = cog(sb, "RequestsCog")
    seerr = sb.plexreq.backend.seerr
    assert sb.plexreq.backend.active == "seerr"                         # auto prefers Seerr over the arr apps
    alice = world.member(1, "alice")
    # button → modal (no role gate) → ephemeral results menu
    view = sb.plexreq.persistent_views[2]
    i = FakeInteraction(alice, world.requests, client=pres)
    await view.on_click(i)
    modal = i.modals[0]
    modal.query._value = "heat"
    j = FakeInteraction(alice, world.requests, client=pres)
    await modal.on_submit(j)
    content, kw = j.sent[-1]
    assert content == "🔍 Results for **heat** — pick one:" and kw["ephemeral"] and isinstance(kw["view"], ResultsView)
    menu: ResultsView = kw["view"]
    assert [o.label for o in menu.select.options] == ["Heat (1995)", "Dune (2021)"] and menu.select.options[1].description == "Movie · Already on Plex"
    # someone else cannot use the menu
    k = FakeInteraction(world.member(2, "bob"), world.requests, client=pres)
    menu.select._values = ["movie:1:0"]
    await menu.on_pick(k)
    assert k.sent[-1][0].startswith("That menu belongs to someone else")
    # the requester picks → progress note, request sent to Seerr, announcement, result edited in place
    p = FakeInteraction(alice, world.requests, client=pres)
    await menu.on_pick(p)
    assert p.edits[0]["content"] == "⏳ Requesting **Heat**…" and p.edits[0]["view"] is None
    assert seerr.requested == [("movie", 1)]
    assert p.edits[-1]["content"] == "🎬 **Heat (1995)** requested! Seerr has it now — it'll appear on Plex once it's downloaded."
    assert p.edits[-1]["embed"].title == "🎬  Heat (1995)" and menu.is_finished()
    w = sb.plexreq.records.watches()[0]
    assert w["backend"] == "seerr" and w["media_id"] == 901 and w["channel_id"] == MOVIES_CH
    # slash: /requests request title:
    s = FakeInteraction(alice, world.requests, client=pres)
    await req.request(s, "heat")
    assert s.sent[-1][0] == "🔍 Results for **heat** — pick one:"
    # typed title: message removed, public self-destructing menu, sticky embed re-posted; emails / slashes ignored
    msg = world.message(alice, world.requests, "Heat")
    await req.on_message(msg)
    assert msg.deleted
    menu_msg = world.requests.sent[0]
    assert menu_msg.content.startswith("🔍 <@1> — results for **Heat**, pick one")
    assert isinstance(menu_msg.view, ResultsView) and menu_msg.view.public and menu_msg.view.menu_message is menu_msg
    assert world.requests.sent[-1].embeds[0].title == "🍿  Request movies & TV shows"
    ignored = world.message(alice, world.requests, "/requests request title:x")
    await req.on_message(ignored)
    assert not ignored.deleted
    email_msg = world.message(alice, world.requests, "alice@example.com")
    await req.on_message(email_msg)
    assert not email_msg.deleted                                       # not a title → left to the invite rules
    # public pick: acknowledged privately, menu deleted from the channel
    pub: ResultsView = menu_msg.view
    pub.select._values = ["movie:1:0"]
    q = FakeInteraction(alice, world.requests, client=pres, message=menu_msg)
    await pub.on_pick(q)
    assert menu_msg.deleted and q.edits[-1]["content"].startswith("🎬 **Heat (1995)** requested!")
    # timeouts clean up after themselves
    await pub.on_timeout()
    eph = ResultsView(req, 1, [mk("Heat", "1995", "movie", backend="seerr")])
    eph.menu_message = FakeMessage(world.requests, "menu")
    await eph.on_timeout()
    assert eph.menu_message.edits[-1]["content"].startswith("⌛ Search expired")
    # the Seerr watcher flips the card once media status says available/partial
    seerr.status[901] = 4
    await req.check_watches()
    assert world.movies.sent[0].embeds[0].colour == discord.Colour.from_str("#2ecc71")
    assert sb.plexreq.stats.data()["totals"]["typed_request"] == 1 and sb.plexreq.stats.data()["totals"]["pick"] == 2
    await sb.unload()
    assert seerr.closed


@pytest.mark.asyncio
async def test_mystatus_and_plexstats(tmp_path, fakes):
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR})
    req, stats_cog = cog(sb, "RequestsCog"), cog(sb, "StatsCog")
    alice = world.member(1, "alice")
    i = FakeInteraction(alice, client=pres)
    await req.mystatus(i)
    assert i.sent[-1][0].startswith("You haven't requested anything yet") and i.sent[-1][1]["ephemeral"]
    sb.plexreq.records.track_request(1, mk("Heat", "1995", "movie"), 5)
    sb.plexreq.records.track_request(1, mk("The Expanse", "2015", "tv"), 6)
    sb.plexreq.records.mark_history_available(1, 5)
    i = FakeInteraction(alice, client=pres)
    await req.mystatus(i)
    e = i.sent[-1][1]["embed"]
    lines = e.description.splitlines()
    assert e.title == "📈 Your requests" and lines[0].startswith("⏳ 📺 **The Expanse (2015)** — queued") and lines[1].startswith("🟢 🎬 **Heat (1995)** — available")
    # plexstats is admin-gated: a Plex-server administrator passes, a plain member does not
    group = pres.tree.get_command("requests", guild=discord.Object(id=PLEX_GUILD))
    predicate = group.get_command("plexstats").checks[0]
    denied = FakeInteraction(alice, client=pres)
    assert await predicate(denied) is False and denied.sent[-1][0] == "🚫 Admin only."
    admin = FakeInteraction(world.member(2, "root", admin=True), client=pres)
    assert await predicate(admin) is True and admin.sent == []
    sb.plexreq.stats.bump("search", alice)
    await stats_cog.plexstats(admin)
    e = admin.sent[-1][1]["embed"]
    assert e.title == "📊 lab.example Plex — usage" and "Searches run" in e.description and "alice" in e.description
    assert admin.sent[-1][1]["ephemeral"] and sb.plexreq.stats.data()["totals"]["cmd_plexstats"] == 1
    await sb.unload()


# ----- revoke / feed / board --------------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_revoke_on_leave_and_role_loss(tmp_path, fakes):
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR})
    rev = cog(sb, "RevokeCog")
    plex = FakePlex.instances[-1]
    role = SimpleNamespace(id=1, name="plex members")
    alice = world.member(1, "alice", roles=[role])
    sb.plexreq.records.remember_email(1, "alice@example.com")
    sb.plexreq.records.remember_email(2, "bob@example.com")
    # losing the role
    after = world.member(1, "alice", roles=[])
    await rev.on_member_update(alice, after)
    assert plex.revoked == ["alice@example.com"] and sb.plexreq.records.email_for(1) is None
    await rev.on_member_update(after, after)                            # no change → nothing
    # leaving the server
    bob = world.member(2, "bob")
    await rev.on_member_remove(bob)
    assert plex.revoked == ["alice@example.com", "bob@example.com"] and sb.plexreq.stats.data()["totals"]["revoked"] == 2
    # unknown member, or a member of another guild, is ignored
    await rev.on_member_remove(world.member(3, "carol"))
    stranger = FakeMember(4, "dan", FakeGuild(1, []), [])
    sb.plexreq.records.remember_email(4, "dan@example.com")
    await rev.on_member_remove(stranger)
    assert len(plex.revoked) == 2 and sb.plexreq.records.email_for(4) == "dan@example.com"
    await sb.unload()
    # AUTO_REVOKE off → listeners do nothing even with a known email
    rt2, pres2, sb2, world2 = await build(tmp_path / "off", {**BASE, **ARR, "AUTO_REVOKE": "0"})
    sb2.plexreq.records.remember_email(1, "alice@example.com")
    await cog(sb2, "RevokeCog").on_member_remove(world2.member(1, "alice"))
    assert FakePlex.instances[-1].revoked == []
    await sb2.unload()


@pytest.mark.asyncio
async def test_new_on_plex_feed_baselines_then_announces(tmp_path, fakes):
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR})
    feed = cog(sb, "NewOnPlexCog")
    plex = FakePlex.instances[-1]
    await feed.new_on_plex.coro(feed)                                   # first pass: silent baseline
    assert world.new.sent == [] and sb.plexreq.records.plex_seen() == ["1"]
    plex.recent = [{"key": "3", "kind": "episode", "title": "The Expanse — S01E01 · Dulcinea", "year": None, "summary": ""},
                   {"key": "2", "kind": "movie", "title": "Dune", "year": 2021, "summary": "sand"}] + plex.recent
    await feed.new_on_plex.coro(feed)
    titles = [m.embeds[0].title for m in world.new.sent]
    assert titles == ["🆕 🎬  Dune (2021)", "🆕 📺  The Expanse — S01E01 · Dulcinea"]      # oldest first
    assert world.new.sent[0].embeds[0].footer.text == "Now on lab.example Plex" and world.new.sent[1].embeds[0].description is None
    assert sb.plexreq.records.plex_seen() == ["3", "2", "1"] and sb.plexreq.stats.data()["totals"]["new_on_plex"] == 2
    await feed.new_on_plex.coro(feed)
    assert len(world.new.sent) == 2                                     # nothing new → quiet
    await sb.unload()


@pytest.mark.asyncio
async def test_status_board_posts_then_edits(tmp_path, fakes):
    rt, pres, sb, world = await build(tmp_path, {**BASE, **ARR})
    board = cog(sb, "BoardCog")
    await board.status_board.coro(board)
    msg = world.status.sent[0]
    e = msg.embeds[0]
    assert e.title == "📊  lab.example Plex — live status"
    fields = {f.name: f.value for f in e.fields}
    assert fields["🎞️ Now streaming — 1"] == "**alice** — Heat (42%)"
    assert fields["🎬 Radarr queue — 2"] == "Heat — 5m" and fields["📺 Sonarr queue — 0"] == "Empty"
    assert fields["💾 Disk"] == "`/data` — 500.0 GiB free of 2.0 TiB" and e.footer.text == "lab.example Plex • refreshes every 60s"
    assert sb.plexreq.records.message_id("status_message_id") == msg.id
    await board.status_board.coro(board)
    assert len(world.status.sent) == 1 and msg.edits                    # edited in place
    # a restart finds the remembered message instead of posting a new one
    board._msg = None
    await board.status_board.coro(board)
    assert len(world.status.sent) == 1 and len(msg.edits) == 2
    # Plex down → the board says so instead of failing
    FakePlex.instances[-1].sessions = lambda: (_ for _ in ()).throw(RuntimeError("down"))
    await board.status_board.coro(board)
    assert {f.name: f.value for f in msg.embeds[0].fields}["🎞️ Now streaming"] == "*(Plex not reachable)*"
    await sb.unload()


# ----- check() (network mocked) ----------------------------------------------------------------------------------

def fake_http(monkeypatch, *, good: str):
    calls = []

    def cred(client: HttpClient) -> str:
        h = client._headers
        return h.get("X-Plex-Token") or h.get("X-Api-Key") or ""

    async def get_json(self, path, **kw):
        calls.append((self.base_url, path))
        if path.startswith("/identity"):
            return {"MediaContainer": {"version": "1.41.0"}}
        if path == "/api/v1/status":
            return {"version": "2.5.0"}
        if cred(self) != good:
            raise HttpError(401, self.base_url + path, "unauthorized")
        if path == "/status/sessions":
            return {"MediaContainer": {}}
        if path == "/api/v3/system/status":
            return {"version": "5.0.0"}
        if path == "/api/v1/auth/me":
            return {"id": 1}
        return {}

    monkeypatch.setattr(HttpClient, "get_json", get_json)
    return calls


@pytest.mark.asyncio
async def test_check(monkeypatch):
    calls = fake_http(monkeypatch, good="good")
    assert (await check({}))[1] == "PLEX_URL is required"
    ok, msg = await check({"PLEX_URL": "http://p"})
    assert not ok and "PLEX_TOKEN" in msg and "plex_token.py" in msg
    assert await check({"PLEX_URL": "http://p/", "PLEX_TOKEN": "good"}) == (True, "Plex 1.41.0 answered · requests off (no Seerr or Radarr/Sonarr configured)")
    ok, msg = await check({"PLEX_URL": "http://p", "PLEX_TOKEN": "bad"})
    assert not ok and "rejected the token" in msg and "PLEX_TOKEN" in msg
    env = {"PLEX_URL": "http://p", "PLEX_TOKEN": "good", "RADARR_URL": "http://r", "RADARR_API_KEY": "good",
           "SONARR_URL": "http://s", "SONARR_API_KEY": "good", "OVERSEERR_URL": "http://o", "OVERSEERR_API_KEY": "good"}
    assert await check(env) == (True, "Plex 1.41.0 answered · Seerr 2.5.0 · Radarr 5.0.0 · Sonarr 5.0.0 · requests via seerr")
    assert (await check({**env, "REQUEST_BACKEND": "arr"}))[1].endswith("requests via arr")
    ok, msg = await check({**env, "SONARR_API_KEY": "bad"})
    assert not ok and msg.endswith("FAILED: Sonarr rejected the API key (401)") and "Radarr 5.0.0" in msg
    ok, msg = await check({**env, "OVERSEERR_API_KEY": "bad"})
    assert not ok and "Seerr 2.5.0 rejected the API key (401)" in msg
    assert ("http://p", "/status/sessions") in calls and ("http://o", "/api/v1/auth/me") in calls


@pytest.mark.asyncio
async def test_check_unreachable(monkeypatch):
    async def boom(self, path, **kw):
        raise OSError("connection refused")

    monkeypatch.setattr(HttpClient, "get_json", boom)
    ok, msg = await check({"PLEX_URL": "http://p", "PLEX_TOKEN": "t"})
    assert not ok and msg == "Plex unreachable: connection refused"

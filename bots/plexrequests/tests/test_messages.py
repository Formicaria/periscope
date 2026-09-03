"""Message kinds: every plexrequests.* post previews from its sample, the sticky templates reproduce the wording the
bot used to build in code, and the send sites honour a customisation (the service built through the runtime, so the
runtime's own messages.yaml is what changes the posts)."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest
from periscope import Store
from periscope.messages import REGISTRY, STANDARD_VARIABLES, Messages, MessageStore, kinds_for, preview
from periscope.runtime import Runtime

import periscope_plexrequests.service as service_mod
from periscope_plexrequests.cogs.board import board_ctx, board_embed
from periscope_plexrequests.cogs.invites import INVITE_MESSAGE_KEY
from periscope_plexrequests.cogs.requests import REQUEST_MESSAGE_KEY, available_ctx
from periscope_plexrequests.common import (
    AVAILABLE_KIND,
    BOARD_KIND,
    INVITE_KIND,
    MYSTATUS_KIND,
    NEW_ON_PLEX_KIND,
    REQUEST_CARD_KIND,
    REQUEST_KIND,
    STATS_KIND,
    build_invite_embed,
    build_request_embed,
    sticky_ctx,
    sticky_embed,
)
from periscope_plexrequests.messages import SAMPLE_CFG, SAMPLE_NOW

STICKIES = {INVITE_KIND, REQUEST_KIND}
REPLIES = {STATS_KIND, MYSTATUS_KIND}                       # private answers to a slash command: no channel
EXPECTED = STICKIES | REPLIES | {BOARD_KIND, REQUEST_CARD_KIND, AVAILABLE_KIND, NEW_ON_PLEX_KIND}


def _parts(embed):
    """What a template reproduces of an embed."""
    return (embed.title, embed.description, embed.url, embed.color.value if embed.color else None,
            [(f.name, f.value, f.inline) for f in embed.fields], embed.footer.text if embed.footer else None,
            embed.thumbnail.url if embed.thumbnail else None)


# ----- the registry --------------------------------------------------------------------------------------------

def test_every_post_has_a_kind():
    assert {k.key for k in kinds_for("plexrequests")} == EXPECTED
    for k in kinds_for("plexrequests"):
        assert k.sample is not None and k.title and k.description and k.where and k.group
        assert (k.template is not None) == (k.key in STICKIES)   # the stickies carry their wording as a template
        assert bool(k.where_env) == (k.key not in REPLIES)        # every channel post says which setting names it
    assert {k.group for k in kinds_for("plexrequests")} == {"stickies", "boards", "cards", "feed"}


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_sample_previews(key):
    kind = REGISTRY[key]
    embed, ctx = kind.sample()
    assert embed is not None and embed.title
    json.dumps(ctx)                                                   # plain values only
    assert not set(ctx) & set(STANDARD_VARIABLES)                     # never shadows the embed's own parts
    assert set(kind.variables) == set(ctx)                            # what is documented is what is passed
    again, ctx_again = kind.sample()
    assert _parts(again) == _parts(embed) and ctx_again == ctx        # deterministic
    rendered, full, err = preview(kind, None)
    assert err is None and rendered is not None
    assert _parts(rendered) == _parts(embed)                          # the default template reproduces the post
    assert full["lab"] == "lab" and full["service"] == "plexrequests" and full["title"] == embed.title
    if key in STICKIES:
        assert rendered.timestamp is None                             # the button embeds never carry a time


# ----- the sticky templates say exactly what the code used to -------------------------------------------------

def legacy_invite_embed(cfg) -> discord.Embed:
    """The invite embed as `build_invite_embed` built it before it became a template."""
    e = discord.Embed(
        title=f"🎬  {cfg.plex_name} — get access",
        colour=discord.Colour.from_str("#e5a00d"),
        description=(
            f"Movies, TV shows and music, streamed from {cfg.plex_name}.\n\n"
            "**Three ways to get your invite:**\n"
            "🎟️ Click **Get Plex Access** below and enter your Plex email\n"
            "⌨️ Just type your email in this channel (I'll delete it right away)\n"
            "🔍 Use `/plexinvite email:you@example.com`\n\n"
            "You'll get an email from Plex — hit **Accept**, then watch at "
            "[app.plex.tv](https://app.plex.tv) or any Plex app.\n"
            "Don't have a Plex account? Create one first at "
            "[plex.tv/sign-up](https://www.plex.tv/sign-up/) with the same email."
        ),
    )
    e.set_footer(text=f"Invites are sent automatically • You'll get the {cfg.role_name} role")
    return e


def legacy_request_embed(cfg) -> discord.Embed:
    """The request embed as `build_request_embed` built it before it became a template."""
    e = discord.Embed(
        title="🍿  Request movies & TV shows",
        colour=discord.Colour.from_str("#5865f2"),
        description=(
            "Want something added to Plex? Ask here and it goes straight into the download queue.\n\n"
            "**Three ways to request:**\n"
            "🔎 Click **Search & Request** below\n"
            "⌨️ Just type the title in this channel (e.g. `Dune Part Two`) — I'll tidy your message away\n"
            "🎯 Use `/requests request title:...`\n\n"
            "Searching and picking happens privately — nothing shows up here until your request is actually sent.\n"
            "📈 `/requests mystatus` shows your requests and pings you when they're ready."
        ),
    )
    if cfg.requests_role_name:
        e.set_footer(text=f"Requires the {cfg.requests_role_name} role — get it in #{cfg.channel_name}")
    return e


@pytest.mark.parametrize("cfg", [SAMPLE_CFG, replace(SAMPLE_CFG, server_name="", role_name="viewers", channel_name="plex"),
                                 replace(SAMPLE_CFG, requests_role_name="")])
def test_sticky_defaults_equal_the_legacy_builders(cfg):
    plain = Messages(None, service="plexrequests", lab="THE LAB")   # no store: the default template every time
    for kind, legacy, builder in ((INVITE_KIND, legacy_invite_embed, build_invite_embed),
                                  (REQUEST_KIND, legacy_request_embed, build_request_embed)):
        want = legacy(cfg).to_dict()
        assert builder(cfg).to_dict() == want                                    # the thin wrapper
        assert plain.render(kind, sticky_ctx(cfg)).to_dict() == want             # what the cogs post
        assert sticky_embed(SimpleNamespace(), kind, cfg).to_dict() == want      # a bot without `messages`
        assert sticky_embed(SimpleNamespace(messages=plain), kind, cfg).to_dict() == want
    assert (build_request_embed(cfg).footer.text is None) == (not cfg.requests_role_name)   # conditional footer


def test_sticky_customisation_and_switch_off(tmp_path):
    store = MessageStore(tmp_path / "config" / "messages.yaml")
    bot = SimpleNamespace(messages=Messages(store, service="plexrequests", lab="THE LAB"))
    store.set(INVITE_KIND, {"title": "Join {{ plex_name }}", "description": "Ask in {{ invite_channel }} — {{ lab }}",
                            "color": "ok", "footer": "{{ role_name }}", "timestamp": False})
    e = sticky_embed(bot, INVITE_KIND, SAMPLE_CFG)
    assert e.title == "Join lab.example Plex" and e.description == "Ask in #join-plex — THE LAB"
    assert e.footer.text == "plex members" and e.color.value != build_invite_embed(SAMPLE_CFG).color.value
    assert sticky_embed(bot, REQUEST_KIND, SAMPLE_CFG).to_dict() == build_request_embed(SAMPLE_CFG).to_dict()
    store.set(INVITE_KIND, None, enabled=False)
    assert sticky_embed(bot, INVITE_KIND, SAMPLE_CFG) is None
    # a template that renders to nothing at all falls back to the default rather than posting an empty embed
    store.set(REQUEST_KIND, {"title": "{{ nothing }}", "description": ""})
    assert sticky_embed(bot, REQUEST_KIND, SAMPLE_CFG).to_dict() == build_request_embed(SAMPLE_CFG).to_dict()


# ----- the board and the cards from their facts ------------------------------------------------------------------

def test_board_from_its_facts():
    data = board_ctx(None, [{"app": "radarr", "ok": True, "total": 1, "top": ["Heat — 5m"], "error": ""},
                            {"app": "sonarr", "ok": False, "total": 0, "top": [], "error": "x" * 60}], [])
    assert data["plex_ok"] is False and data["streams"] == [] and data["interval_s"] == 60
    json.dumps(data)
    e = board_embed(data, "Plex", now=SAMPLE_NOW)
    assert e.title == "📊  Plex — live status" and e.timestamp == SAMPLE_NOW
    assert [(f.name, f.value, f.inline) for f in e.fields] == [
        ("🎞️ Now streaming", "*(Plex not reachable)*", False), ("🎬 Radarr queue — 1", "Heat — 5m", True),
        ("📺 Sonarr queue", f"*(unreachable: {'x' * 40})*", True)]
    assert e.footer.text == "Plex • refreshes every 60s"
    full = board_embed(board_ctx([f"**u{i}**" for i in range(8)], [], [{"path": f"/d{i}", "free": 1, "total": 2}
                                                                         for i in range(7)]), "Plex")
    fields = {f.name: f.value for f in full.fields}
    assert fields["🎞️ Now streaming — 8"].count("\n") == 5 and fields["💾 Disk"].count("\n") == 4   # 6 streams, 5 disks
    assert board_embed(board_ctx([], [], []), "Plex").fields[0].value == "Nobody right now"


def test_available_ctx_fills_in_what_older_watches_lack():
    new = {"backend": "seerr", "media_id": 9, "requester": "alice", "requester_id": 7, "title": "Dune",
           "media_type": "movie", "year": "2021"}
    ctx = available_ctx(new, SAMPLE_CFG)
    assert ctx["label"] == "Dune (2021)" and ctx["media_type"] == "movie" and ctx["requester_id"] == 7
    assert ctx["available_text"] == "Available to watch on plex.lab.example now" and ctx["backend"] == "seerr"
    # a watch from the standalone bot: type from the arr app that holds it, year unknown
    old = {"backend": "arr", "kind": "sonarr", "arr_id": 5, "requester": "bob", "title": "Severance"}
    ctx = available_ctx(old, SAMPLE_CFG)
    assert ctx["media_type"] == "tv" and ctx["year"] == "" and ctx["label"] == "Severance" and ctx["requester_id"] == 0
    assert available_ctx({"backend": "seerr", "requester": "bob", "title": "x"}, SAMPLE_CFG)["media_type"] == ""


# ----- the send sites honour a customisation (the service built through the runtime) -----------------------------

LAB_GUILD, PLEX_GUILD = 42, 77
INVITE_CH, REQ_CH, MOVIES_CH, STATUS_CH, NEW_CH = 100, 200, 300, 400, 500
ENV = {"PLEX_URL": "http://plex:32400", "PLEX_TOKEN": "pt", "CHANNEL_ID": str(INVITE_CH), "REQUESTS_CHANNEL_ID": str(REQ_CH),
       "PLEXREQ_GUILD_ID": str(PLEX_GUILD), "SERVER_NAME": "lab.example", "PLEX_LINK": "plex.lab.example",
       "MOVIES_CHANNEL": "movies", "STATUS_CHANNEL": "plex-status", "NEW_CHANNEL": str(NEW_CH), "REQUESTS_ROLE_NAME": "",
       "RADARR_URL": "http://radarr:7878", "RADARR_API_KEY": "rk", "SONARR_URL": "http://sonarr:8989", "SONARR_API_KEY": "sk"}


class FakeMessage:
    _ids = 1000

    def __init__(self, channel, content=None, embed=None, view=None, author=None, delete_after=None):
        FakeMessage._ids += 1
        self.id = FakeMessage._ids
        self.channel, self.content, self.author, self.delete_after = channel, content, author, delete_after
        self.embeds = [embed] if embed is not None else []
        self.view, self.deleted, self.edits = view, False, []
        self.guild = getattr(channel, "guild", None)

    async def delete(self):
        self.deleted = True
        self.channel.messages.pop(self.id, None)

    async def edit(self, **kw):
        self.edits.append(kw)
        if kw.get("embed") is not None:
            self.embeds = [kw["embed"]]


class FakeChannel:
    def __init__(self, cid, name, guild=None):
        self.id, self.name, self.guild = cid, name, guild
        self.messages, self.sent, self.last_message_id = {}, [], None

    async def send(self, content=None, *, embed=None, view=None, delete_after=None, **kw):
        msg = FakeMessage(self, content, embed, view, delete_after=delete_after)
        self.messages[msg.id] = msg
        self.sent.append(msg)
        self.last_message_id = msg.id
        return msg

    async def fetch_message(self, mid):
        if mid in self.messages:
            return self.messages[mid]
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "not found")

    def stickies(self):
        return [m for m in self.sent if m.view is not None and not m.deleted]


class FakeGuild:
    def __init__(self, gid, channels):
        self.id, self.text_channels, self.roles = gid, channels, []
        for c in channels:
            c.guild = self

    async def create_role(self, *, name, colour=None, reason=None):
        role = SimpleNamespace(id=900 + len(self.roles), name=name)
        self.roles.append(role)
        return role


class FakeMember:
    def __init__(self, uid, name, guild, admin=False):
        self.id, self.display_name, self.guild, self.roles = uid, name, guild, []
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.mention, self.dms, self.bot = f"<@{uid}>", [], False

    async def add_roles(self, role, reason=None):
        self.roles.append(role)

    async def send(self, text):
        self.dms.append(text)

    def __str__(self):
        return self.display_name


class FakeInteraction:
    def __init__(self, user, client=None):
        self.user, self.client, self.sent = user, client, []
        self.response = SimpleNamespace(send_message=self._send, is_done=lambda: bool(self.sent))

    async def _send(self, content=None, **kw):
        self.sent.append((content, kw))


def mk(title, year, media_type, tmdb):
    return {"tmdb_id": tmdb, "media_type": media_type, "title": title, "year": year, "status": 1, "poster": None,
            "overview": f"about {title}", "backend": "arr",
            "arr_raw": {"title": title, "year": int(year), "tmdbId": tmdb}}


class FakePlex:
    instances = []

    def __init__(self, url, token, libraries="all", timeout=20):
        self.streams = ["**alice** — Heat (42%)"]
        self.recent = [{"key": "1", "kind": "movie", "title": "Heat", "year": 1995, "summary": "cops"}]
        FakePlex.instances.append(self)

    def sessions(self):
        return self.streams

    def recently_added(self, limit=30):
        return self.recent


class FakeArr:
    def __init__(self, kind, base_url, api_key, profile_name="", root_folder="", fallback_profile="", fallback_before_year=0):
        self.kind, self.available = kind, {}

    async def add(self, raw):
        return (True, "added", 500 + raw["tmdbId"])

    async def is_available(self, arr_id):
        return self.available.get(arr_id)

    async def queue_summary(self, top=3):
        return (2, ["Heat — 5m"]) if self.kind == "radarr" else (0, [])

    async def disk_space(self):
        return [("/data", 500 * 1024 ** 3, 2 * 1024 ** 4)]

    async def close(self):
        pass


@pytest.fixture
def fakes(monkeypatch):
    FakePlex.instances = []
    monkeypatch.setattr(service_mod, "PlexGateway", FakePlex)
    monkeypatch.setattr(service_mod, "ArrClient", FakeArr)


class World:
    def __init__(self, pres):
        self.invite = FakeChannel(INVITE_CH, "join-plex")
        self.requests = FakeChannel(REQ_CH, "media-requests")
        self.movies = FakeChannel(MOVIES_CH, "movies")
        self.status = FakeChannel(STATUS_CH, "plex-status")
        self.new = FakeChannel(NEW_CH, "new-on-plex")
        self.channels = {c.id: c for c in (self.invite, self.requests, self.movies, self.status, self.new)}
        self.guild = FakeGuild(PLEX_GUILD, list(self.channels.values()))
        pres._connection._guilds[PLEX_GUILD] = self.guild
        pres.get_channel = lambda cid: self.channels.get(cid)

    def member(self, uid=1, name="alice", admin=False):
        return FakeMember(uid, name, self.guild, admin)

    def message(self, author, channel, content):
        msg = FakeMessage(channel, content, author=author)
        channel.messages[msg.id] = msg
        channel.last_message_id = msg.id
        return msg


async def build(tmp_path):
    """The service on the v2 runtime, whose messages.yaml (next to periscope.yaml) is the store the bot reads."""
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.lab.update({"guild_id": str(LAB_GUILD), "name": "THE LAB"})
    s.services["plexrequests"] = {"enabled": True, "presence": "default", "env": ENV}
    rt = Runtime(s, tmp_path)
    rt.assemble()
    assert not rt.skipped, rt.skipped
    pres = rt.presences["default"]

    async def never_ready():
        await asyncio.Event().wait()

    pres.wait_until_ready = never_ready
    pres.tree.sync = lambda *, guild=None: asyncio.sleep(0, result=[])
    sb = rt.services["plexrequests"]
    await sb.spec.build(sb)
    store = sb.messages.store
    assert isinstance(store, MessageStore) and store.path == tmp_path / "config" / "messages.yaml"
    return sb, World(pres), store


@pytest.mark.asyncio
async def test_customised_cards_and_switched_off_cards(tmp_path, fakes):
    sb, world, store = await build(tmp_path)
    req = sb.get_cog("RequestsCog")
    alice = world.member()
    heat, dune, expanse = mk("Heat", "1995", "movie", 1), mk("Dune", "2021", "movie", 2), mk("The Expanse", "2015", "tv", 3)

    text, _ = await req.submit_request(alice, heat, world.requests)                # no customisation: the bot's card
    plain = world.movies.sent[-1].embeds[0]
    assert text.startswith("🎬 **Heat (1995)** requested! Radarr has it now")
    assert plain.title == "🎬  Heat (1995)" and plain.footer.text == "Requested by alice • added to the download queue"
    assert plain.description == "about Heat" and not plain.fields

    store.set(REQUEST_CARD_KIND, {"title": "📥 {{ label }}", "description": "{{ overview }}", "color": "auto",
                                  "fields": [{"name": "Asked by", "value": "<@{{ requester_id }}> via {{ via }} ({{ lab }})",
                                              "inline": True}],
                                  "footer": "{{ plex_name }}", "timestamp": True})
    await req.submit_request(alice, dune, world.requests)
    custom = world.movies.sent[-1].embeds[0]
    assert custom.title == "📥 Dune (2021)" and custom.description == "about Dune" and custom.color.value == plain.color.value
    assert [(f.name, f.value, f.inline) for f in custom.fields] == [("Asked by", "<@1> via Radarr (THE LAB)", True)]
    assert custom.footer.text == "lab.example Plex" and custom.timestamp is not None
    assert [w["title"] for w in sb.plexreq.records.watches()] == ["Heat", "Dune"]   # still watched for availability
    assert sb.plexreq.records.watches()[-1]["media_type"] == "movie" and sb.plexreq.records.watches()[-1]["year"] == "2021"

    store.set(REQUEST_CARD_KIND, None, enabled=False)                              # switched off: nothing announced
    text, card = await req.submit_request(alice, expanse, world.requests)
    assert text.startswith("📺 **The Expanse (2015)** requested! Sonarr") and card.title == "📺  The Expanse (2015)"
    assert len(world.movies.sent) == 2 and world.requests.sent == []
    assert sb.plexreq.records.history(1)[-1]["title"] == "The Expanse"            # still in /requests mystatus …
    assert [w["title"] for w in sb.plexreq.records.watches()] == ["Heat", "Dune"]  # … but nothing to flip green
    store.reset(REQUEST_CARD_KIND)

    # the available flip: default, customised, switched off (the card stays, the ping still goes out)
    heat_msg, dune_msg = world.movies.sent
    sb.plexreq.backend.radarr.available[501] = 5
    await req.check_watches()
    e = heat_msg.embeds[0]
    assert e.color == discord.Colour.from_str("#2ecc71") and e.footer.text == "Requested by alice • Available to watch on plex.lab.example now"
    assert world.movies.sent[-1].content == "🎉 <@1> — **Heat** is ready! Available to watch on plex.lab.example now."
    store.set(AVAILABLE_KIND, {"title": "✅ {{ title }}", "description": "{{ available_text }}, {{ requester }}!",
                               "color": "auto", "footer": "{{ label }} · {{ media_type }}", "timestamp": False})
    sb.plexreq.backend.radarr.available[502] = 5
    await req.check_watches()
    e = dune_msg.embeds[0]
    assert e.title == "✅ 📥 Dune (2021)" and e.description == "Available to watch on plex.lab.example now, alice!"
    assert e.color == discord.Colour.from_str("#2ecc71") and e.footer.text == "Dune (2021) · movie"
    assert sb.plexreq.records.watches() == [] and sb.plexreq.stats.data()["totals"]["became_available"] == 2
    store.set(AVAILABLE_KIND, None, enabled=False)
    await req.submit_request(alice, mk("Alien", "1979", "movie", 4), world.requests)
    alien_msg = world.movies.sent[-1]
    sb.plexreq.backend.radarr.available[504] = 5
    await req.check_watches()
    assert alien_msg.edits == [] and alien_msg.embeds[0].footer.text.endswith("added to the download queue")
    assert world.movies.sent[-1].content.startswith("🎉 <@1> — **Alien** is ready!")
    assert sb.plexreq.records.watches() == [] and sb.plexreq.stats.data()["totals"]["became_available"] == 2
    await sb.unload()


@pytest.mark.asyncio
async def test_customised_feed_board_and_replies(tmp_path, fakes):
    sb, world, store = await build(tmp_path)
    feed, board, stats_cog, req = (sb.get_cog(n) for n in ("NewOnPlexCog", "BoardCog", "StatsCog", "RequestsCog"))
    plex = FakePlex.instances[-1]

    # the feed: baseline, then a customised card, then switched off (items still counted as seen)
    await feed.new_on_plex.coro(feed)
    store.set(NEW_ON_PLEX_KIND, {"title": "{{ kind }}: {{ label }}", "description": "{{ description }} — {{ plex_name }}",
                                 "color": "info", "timestamp": False})
    plex.recent = [{"key": "2", "kind": "movie", "title": "Dune", "year": 2021, "summary": "sand"}] + plex.recent
    await feed.new_on_plex.coro(feed)
    e = world.new.sent[-1].embeds[0]
    assert e.title == "movie: Dune (2021)" and e.description == "sand — lab.example Plex" and e.footer.text is None
    store.set(NEW_ON_PLEX_KIND, None, enabled=False)
    plex.recent = [{"key": "3", "kind": "episode", "title": "The Expanse — S01E01 · Dulcinea", "year": None, "summary": ""}] + plex.recent
    await feed.new_on_plex.coro(feed)
    assert len(world.new.sent) == 1 and sb.plexreq.records.plex_seen() == ["3", "2", "1"]
    store.reset(NEW_ON_PLEX_KIND)
    await feed.new_on_plex.coro(feed)
    assert len(world.new.sent) == 1                                     # nothing new: the switched-off pass baselined it

    # the board: customised through the core StatusBoard, taken down when switched off
    store.set(BOARD_KIND, {"title": "🎥 {{ lab }} Plex", "description": "{{ streams | length }} watching · Plex up: {{ plex_ok }}",
                           "color": "auto",
                           "fields": [{"repeat": "queues", "name": "{{ item.app }}", "value": "{{ item.total }} queued",
                                       "inline": True},
                                      {"repeat": "disks", "name": "{{ item.path }}", "value": "{{ item.free | bytes }} free"}],
                           "footer": "every {{ interval_s }}s", "timestamp": True})
    await board.status_board.coro(board)
    msg = world.status.sent[0]
    e = msg.embeds[0]
    assert e.title == "🎥 THE LAB Plex" and e.description == "1 watching · Plex up: True"
    assert [(f.name, f.value) for f in e.fields] == [("radarr", "2 queued"), ("sonarr", "0 queued"), ("/data", "500.0 GB free")]
    assert e.footer.text == "every 60s · plex-requests board" and e.timestamp is not None   # the board marker survives
    store.set(BOARD_KIND, None, enabled=False)
    await board.status_board.coro(board)
    assert msg.deleted and len(world.status.sent) == 1 and board.board._state.get("message_id") is None
    store.reset(BOARD_KIND)
    await board.status_board.coro(board)
    assert world.status.sent[-1].embeds[0].title == "📊  lab.example Plex — live status"

    # the private replies: a customised report, and plain text when the embed is switched off
    admin = world.member(2, "root", admin=True)
    sb.plexreq.stats.bump("search", admin)
    store.set(STATS_KIND, {"title": "{{ plex_name }}: {{ totals.search }} searches by {{ user_count }} people",
                           "description": "{{ report | cut(40) }}", "color": "auto", "timestamp": False})
    i = FakeInteraction(admin)
    await stats_cog.plexstats.callback(stats_cog, i) if hasattr(stats_cog.plexstats, "callback") else await stats_cog.plexstats(i)
    e = i.sent[-1][1]["embed"]
    assert e.title == "lab.example Plex: 1 searches by 1 people" and e.description.endswith("…") and i.sent[-1][1]["ephemeral"]
    store.set(STATS_KIND, None, enabled=False)
    i = FakeInteraction(admin)
    await stats_cog.plexstats.callback(stats_cog, i) if hasattr(stats_cog.plexstats, "callback") else await stats_cog.plexstats(i)
    assert i.sent[-1][0].startswith("```\n") and "Searches run" in i.sent[-1][0] and "embed" not in i.sent[-1][1]
    sb.plexreq.records.track_request(2, mk("Heat", "1995", "movie", 1), 5)
    store.set(MYSTATUS_KIND, {"title": "{{ requester }}: {{ count }}",
                              "fields": [{"repeat": "history", "name": "{{ item.name }}", "value": "{{ item.status }}"}],
                              "timestamp": False})
    i = FakeInteraction(admin)
    await req.mystatus(i)
    e = i.sent[-1][1]["embed"]
    assert e.title == "root: 1" and [(f.name, f.value) for f in e.fields] == [("Heat", "queued")]
    store.set(MYSTATUS_KIND, None, enabled=False)
    i = FakeInteraction(admin)
    await req.mystatus(i)
    assert i.sent[-1][0].startswith("📈 **Your requests**\n⏳ 🎬 **Heat (1995)** — queued") and "embed" not in i.sent[-1][1]
    await sb.unload()


@pytest.mark.asyncio
async def test_switched_off_stickies_are_neither_posted_nor_restuck(tmp_path, fakes):
    sb, world, store = await build(tmp_path)
    inv, req = sb.get_cog("InvitesCog"), sb.get_cog("RequestsCog")
    store.set(INVITE_KIND, None, enabled=False)
    store.set(REQUEST_KIND, {"title": "🍿 Ask away", "description": "Type a title in {{ invite_channel }}", "color": "auto",
                             "timestamp": False})
    old = await world.invite.send(embed=build_invite_embed(sb.plexreq.cfg), view=object())   # an earlier copy
    sb.plexreq.records.set_message_id(INVITE_MESSAGE_KEY, old.id)
    await inv.on_ready()
    await req.on_ready()
    assert world.invite.sent == [old] and old.edits == []                       # left alone, nothing posted
    sticky = world.requests.sent[0]
    assert sticky.embeds[0].title == "🍿 Ask away" and sticky.embeds[0].description == "Type a title in <#100>"
    assert sticky.view.children[0].custom_id == "plexrequests:request" and sticky.embeds[0].color.value == 0x5865F2
    assert sb.plexreq.records.message_id(REQUEST_MESSAGE_KEY) == sticky.id
    # a typed email in the invite channel is still handled, but the switched-off embed is not re-posted below it
    carol = world.member(3, "carol")
    sb.plexreq.plex.invite = lambda email: ("sent", "Invite sent!")
    msg = world.message(carol, world.invite, "carol@example.com")
    await inv.on_message(msg)
    assert msg.deleted and carol.dms and world.invite.stickies() == [old]
    # a typed title in the requests channel re-posts the customised one
    await req.on_message(world.message(carol, world.requests, "Heat"))
    assert world.requests.sent[-1].embeds[0].title == "🍿 Ask away" and sticky.deleted
    # switched back on with the default wording: the next start-up refreshes the earlier copy in place
    store.reset(INVITE_KIND)
    inv._ready_once = False
    await inv.on_ready()
    assert world.invite.stickies() == [old] and old.edits[-1]["embed"].title == "🎬  lab.example Plex — get access"
    await sb.unload()

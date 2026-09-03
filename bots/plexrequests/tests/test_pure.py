"""Pure parts of periscope_plexrequests: parsing, rate limiting, backend selection, Radarr/Sonarr profile logic,
result shaping, counters, state accessors and settings."""

import time
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
from periscope import JsonState, env_scope

from periscope_plexrequests.__main__ import store_from_env
from periscope_plexrequests.arr import (
    ArrClient,
    find_profile,
    is_uhd_profile,
    parse_lookup,
    pick_fallback,
    profile_for_year,
    status_of,
)
from periscope_plexrequests.backend import RequestBackend, select_backend
from periscope_plexrequests.cogs.newonplex import fresh_items, new_label
from periscope_plexrequests.cogs.revoke import lost_role
from periscope_plexrequests.common import (
    build_invite_embed,
    build_media_embed,
    build_options,
    build_request_embed,
    check_cooldown,
    find_email,
    fmt_bytes,
    parse_typed_title,
    requests_role_denial,
    requests_role_ok,
    valid_email,
    validate_query,
)
from periscope_plexrequests.config import PlexRequestsSettings
from periscope_plexrequests.plex import classify_invite_error, wanted_sections
from periscope_plexrequests.records import Records
from periscope_plexrequests.seerr import parse_search
from periscope_plexrequests.service import SERVICES
from periscope_plexrequests.stats import EVENT_LABELS, Stats

BASE = {"PLEX_URL": "http://plex:32400/", "PLEX_TOKEN": "pt", "CHANNEL_ID": "100"}


def settings(**extra) -> PlexRequestsSettings:
    with env_scope({**BASE, **extra}):
        return PlexRequestsSettings.from_env()


# ----- parsing ---------------------------------------------------------------------------------------

def test_email_parsing():
    assert valid_email("Someone.Name+plex@example.co.uk") and valid_email("  a@b.io ")
    assert not valid_email("nope") and not valid_email("a@b") and not valid_email("two@x.io words")
    assert find_email("my plex is bob@example.com thanks") == "bob@example.com"
    assert find_email("no address here") is None


def test_typed_title_parsing():
    assert parse_typed_title("  Dune Part Two ") == "Dune Part Two"
    assert parse_typed_title("/request dune") is None          # slash commands are not searches
    assert parse_typed_title("bob@example.com") is None        # emails belong to the invite flow
    assert parse_typed_title("   ") is None and parse_typed_title("") is None
    assert validate_query("x") and validate_query("a" * 101) and validate_query(" ok ") is None


def test_cooldown_window():
    bucket: dict = {}
    assert all(check_cooldown(bucket, 1, limit=3, window=600, now=1000 + i) for i in range(3))
    assert not check_cooldown(bucket, 1, limit=3, window=600, now=1003)        # 4th within the window
    assert check_cooldown(bucket, 2, limit=3, window=600, now=1003)            # other user unaffected
    assert check_cooldown(bucket, 1, limit=3, window=600, now=1000 + 601)      # window slid


# ----- backend selection ------------------------------------------------------------------------------

@pytest.mark.parametrize("mode,seerr,radarr,sonarr,expect", [
    ("auto", True, True, True, "seerr"),
    ("auto", False, True, False, "arr"),
    ("auto", False, False, True, "arr"),
    ("auto", False, False, False, ""),
    ("seerr", True, False, False, "seerr"),
    ("seerr", False, True, True, ""),          # seerr forced but not configured → off
    ("arr", True, True, False, "arr"),         # arr forced even though seerr exists
    ("arr", True, False, False, ""),
    ("ARR ", False, True, False, "arr"),
])
def test_select_backend(mode, seerr, radarr, sonarr, expect):
    assert select_backend(mode, has_seerr=seerr, has_radarr=radarr, has_sonarr=sonarr) == expect


def test_request_backend_describe():
    radarr = ArrClient("radarr", "http://r", "k")
    b = RequestBackend("auto", None, radarr, None)
    assert b.active == "arr" and b.describe() == "arr (radarr=on, sonarr=off)" and b.arr_for("movie") is radarr
    assert b.arr_for("tv") is None and RequestBackend("auto", None, None, None).describe() == "none"


# ----- arr profile logic ---------------------------------------------------------------------------------

P4K = {"id": 1, "name": "Ultra-HD", "items": []}
P1080 = {"id": 2, "name": "HD-1080p", "items": []}
PANY = {"id": 3, "name": "Any", "items": [{"allowed": True, "quality": {"name": "Bluray-720p"}},
                                          {"allowed": True, "quality": {"name": "Bluray-2160p"}}]}
PQ2160 = {"id": 4, "name": "Remux", "items": [{"allowed": True, "quality": {"name": "Remux-2160p"}},
                                              {"allowed": False, "quality": {"name": "Bluray-1080p"}},
                                              {"allowed": True, "items": [{"quality": {"name": "WEBDL-2160p"}}]}]}
PROFILES = [P4K, P1080, PANY, PQ2160]


def test_uhd_detection_and_fallback_pick():
    assert is_uhd_profile(P4K) and is_uhd_profile(PQ2160)          # by name / every allowed quality is 2160p
    assert not is_uhd_profile(P1080) and not is_uhd_profile(PANY)  # mixed qualities are not 4K-only
    assert find_profile(PROFILES, "hd-1080P") is P1080 and find_profile(PROFILES, "") is None
    assert pick_fallback(PROFILES, P4K) == 2                       # auto: first non-4K 1080p profile
    assert pick_fallback(PROFILES, P1080) is None                  # main profile is not 4K-only → no fallback
    assert pick_fallback(PROFILES, P4K, explicit="any") == 3       # explicit name wins
    assert pick_fallback(PROFILES, P4K, explicit="missing") is None
    assert pick_fallback([P4K, PQ2160], P4K) is None               # nothing usable to fall back to


def test_profile_for_year():
    assert profile_for_year(2009, 1, 2, 2016) == 2                 # older than the cutoff → fallback
    assert profile_for_year("2021", 1, 2, 2016) == 1
    assert profile_for_year(2009, 1, None, 2016) == 1              # no fallback configured
    assert profile_for_year(2009, 1, 2, 0) == 1                    # feature off
    assert profile_for_year(None, 1, 2, 2016) == 1 and profile_for_year("n/a", 1, 2, 2016) == 1


def test_arr_client_profile_for_uses_resolved_ids():
    c = ArrClient("radarr", "http://r/", "k", profile_name="Ultra-HD", fallback_before_year=2016)
    c._profile_id, c._fallback_id = 1, 2
    assert c.base == "http://r" and c.profile_for({"year": 1999}) == 2 and c.profile_for({"year": 2020}) == 1


def test_arr_status_and_lookup_shape():
    assert status_of("radarr", {"tmdbId": 1}) == 1
    assert status_of("radarr", {"id": 5, "hasFile": False}) == 2 and status_of("radarr", {"id": 5, "hasFile": True}) == 5
    assert status_of("sonarr", {"id": 5, "statistics": {"episodeFileCount": 0}}) == 2
    assert status_of("sonarr", {"id": 5, "statistics": {"episodeFileCount": 3, "percentOfEpisodes": 50}}) == 4
    assert status_of("sonarr", {"id": 5, "statistics": {"episodeFileCount": 6, "percentOfEpisodes": 100}}) == 5
    items = [{"tmdbId": 10, "title": "Heat", "year": 1995, "overview": "x" * 400, "remotePoster": "p"},
             {"title": "no id"}, {"tmdbId": 11, "title": "Dune", "year": None}]
    out = parse_lookup("radarr", items)
    assert [r["tmdb_id"] for r in out] == [10, 11] and out[0]["year"] == "1995" and out[1]["year"] == ""
    assert out[0]["overview"].endswith("…") and len(out[0]["overview"]) == 348
    assert out[0]["backend"] == "arr" and out[0]["arr_raw"] is items[0] and out[0]["media_type"] == "movie"
    tv = parse_lookup("sonarr", [{"tvdbId": 7, "title": "Show", "year": 2015}])
    assert tv[0]["media_type"] == "tv" and tv[0]["tmdb_id"] == 7


def test_seerr_search_shape():
    data = {"results": [
        {"mediaType": "movie", "id": 1, "title": "Heat", "releaseDate": "1995-12-15", "posterPath": "/h.jpg",
         "mediaInfo": {"status": 5}, "overview": "cops"},
        {"mediaType": "person", "id": 2, "name": "Al"},
        {"mediaType": "tv", "id": 3, "name": "The Expanse", "firstAirDate": "2015-12-14"},
    ]}
    out = parse_search(data)
    assert [r["tmdb_id"] for r in out] == [1, 3] and out[0]["status"] == 5 and out[1]["status"] == 1
    assert out[0]["poster"] == "https://image.tmdb.org/t/p/w342/h.jpg" and out[1]["title"] == "The Expanse"
    assert out[0]["year"] == "1995" and all(r["backend"] == "seerr" for r in out)
    assert parse_search(data, limit=1) == out[:1]


# ----- embeds / options ---------------------------------------------------------------------------------

def test_embeds_and_options():
    cfg = settings(SERVER_NAME="lab.example", REQUESTS_ROLE_NAME="plex members")
    pick = {"title": "Heat", "year": "1995", "media_type": "movie", "tmdb_id": 1, "status": 5, "poster": "http://p",
            "overview": "cops"}
    e = build_media_embed(pick, footer="f")
    assert e.title == "🎬  Heat (1995)" and e.thumbnail.url == "http://p" and e.footer.text == "f"
    opts = build_options([pick, {**pick, "year": "", "media_type": "tv", "status": 1, "tmdb_id": 2}])
    assert opts[0].label == "Heat (1995)" and opts[0].description == "Movie · Already on Plex" and opts[0].value == "movie:1:0"
    assert opts[1].label == "Heat" and opts[1].description == "TV Show" and opts[1].value == "tv:2:1"
    inv = build_invite_embed(cfg)
    assert "lab.example Plex" in inv.title and "/plexinvite" in inv.description and "plex members" in inv.footer.text
    req = build_request_embed(cfg)
    assert "/requests request" in req.description and "#join-plex" in req.footer.text
    assert build_request_embed(settings()).footer.text is None   # no role gate → no footer
    assert fmt_bytes(512) == "512 B" and fmt_bytes(1536 * 1024 ** 2) == "1.5 GiB"


def test_requests_role_gate():
    cfg = settings(REQUESTS_ROLE_NAME="plex members")
    member = SimpleNamespace(guild=object(), roles=[SimpleNamespace(name="plex members")],
                             guild_permissions=SimpleNamespace(administrator=False))
    admin = SimpleNamespace(guild=object(), roles=[], guild_permissions=SimpleNamespace(administrator=True))
    nobody = SimpleNamespace(guild=object(), roles=[], guild_permissions=SimpleNamespace(administrator=False))
    user = SimpleNamespace(id=1)   # DM user: no guild, no roles
    assert requests_role_ok(member, cfg) and requests_role_ok(admin, cfg)
    assert not requests_role_ok(nobody, cfg) and not requests_role_ok(user, cfg)
    assert requests_role_ok(user, settings())                 # no role configured → anyone
    assert "<#100>" in requests_role_denial(cfg) and "plex members" in requests_role_denial(cfg)


# ----- plex helpers --------------------------------------------------------------------------------------

def test_plex_helpers():
    secs = [SimpleNamespace(title="Movies"), SimpleNamespace(title="TV Shows"), SimpleNamespace(title="Music")]
    assert wanted_sections(secs, "all") == secs and wanted_sections(secs, " ALL ") == secs
    assert [s.title for s in wanted_sections(secs, "movies, tv shows")] == ["Movies", "TV Shows"]
    assert classify_invite_error("User is already sharing this server")[0] == "pending"
    assert classify_invite_error("already invited")[0] == "pending"
    assert classify_invite_error("boom")[0] == "error" and "boom" in classify_invite_error("boom")[1]


def test_new_on_plex_and_role_loss_helpers():
    items = [{"key": str(i), "title": f"t{i}", "year": 2000 + i} for i in range(8)]    # newest first
    assert fresh_items(items, ["5", "6", "7"]) == [items[4], items[3], items[2], items[1], items[0]]   # oldest first, max 5
    assert fresh_items(items, [i["key"] for i in items]) == []
    assert new_label({"title": "Heat", "year": 1995}) == "Heat (1995)" and new_label({"title": "x"}) == "x"
    r = SimpleNamespace(name="plex members")
    assert lost_role([r], [], "plex members") and not lost_role([], [], "plex members") and not lost_role([r], [r], "plex members")


# ----- counters + records ------------------------------------------------------------------------------

def test_stats_bump_and_report(tmp_path):
    st = Stats(JsonState(tmp_path / "s.json").namespace("svc:plexrequests"))
    assert "Nothing counted" in st.report()
    alice = SimpleNamespace(id=1, display_name="alice")
    for ev in ("invite_button", "search", "search", "pick", "request_ok", "invite_sent"):
        st.bump(ev, alice)
    st.bump("became_available")
    st.bump("weird_event")
    d = st.data()
    assert d["totals"]["search"] == 2 and d["users"]["1"]["events"]["search"] == 2 and d["users"]["1"]["name"] == "alice"
    rep = st.report(now=d["since"] + 3 * 86400 + 5)
    assert "(3d)" in rep and EVENT_LABELS["search"] in rep and "weird_event" in rep and "alice" in rep
    row = next(line for line in rep.splitlines() if line.startswith("alice"))
    assert row.split() == ["alice", "1", "2", "1", "1", "1", "3d", "ago"]        # buttons searches picks requests invites
    # bump never raises, even with a broken state
    Stats(SimpleNamespace(get=lambda k, d=None: (_ for _ in ()).throw(RuntimeError()), set=None)).bump("x")


def test_records_roundtrip(tmp_path):
    rec = Records(JsonState(tmp_path / "s.json").namespace("svc:plexrequests"))
    assert rec.message_id("invite_message_id") is None
    rec.set_message_id("invite_message_id", 123)
    assert rec.message_id("invite_message_id") == 123
    rec.remember_email(7, "a@b.io")
    assert rec.email_for(7) == "a@b.io" and rec.email_for(8) is None
    rec.forget_email(7)
    assert rec.email_for(7) is None
    rec.add_watch({"backend": "seerr", "media_id": 9}, 55, 66, "alice", 7, "Heat")
    w = rec.watches()[0]
    assert w["media_id"] == 9 and w["channel_id"] == 55 and w["message_id"] == 66 and w["added"] <= time.time()
    rec.drop_watches({66})
    assert rec.watches() == []
    for i in range(20):
        rec.track_request(7, {"title": f"t{i}", "year": "", "media_type": "movie"}, 1000 + i)
    hist = rec.history(7)
    assert len(hist) == 15 and hist[-1]["title"] == "t19" and hist[0]["title"] == "t5"     # last 15 kept
    rec.mark_history_available(7, 1019)
    assert rec.history(7)[-1]["status"] == "available" and rec.history(7)[-2]["status"] == "queued"
    assert rec.plex_seen() is None
    rec.set_plex_seen(["1", "2"])
    assert rec.plex_seen() == ["1", "2"]


# ----- settings -------------------------------------------------------------------------------------------

def test_settings_defaults_and_derived():
    cfg = settings()
    assert cfg.plex_url == "http://plex:32400" and cfg.channel_id == 100 and cfg.guild_id is None
    assert cfg.channel_name == "join-plex" and cfg.role_name == "plex members" and cfg.requests_role_name == ""
    assert cfg.request_backend == "auto" and cfg.fallback_before_year == 2016 and cfg.libraries == "all"
    assert cfg.plex_name == "Plex" and cfg.available_text == "Available to watch on Plex now"
    assert not cfg.has_seerr and not cfg.has_radarr and not cfg.has_sonarr and not cfg.auto_revoke
    assert cfg.announce_channel == {"movie": "", "tv": ""} and cfg.invite_channel_where() == "<#100>"


def test_settings_overrides_and_guild_fallback():
    cfg = settings(GUILD_ID="42", PLEXREQ_GUILD_ID="77", SERVER_NAME="lab.example", PLEX_LINK="plex.lab.example",
                   MOVIES_CHANNEL="movies", TV_CHANNEL="123", STATUS_CHANNEL="plex-status", AUTO_REVOKE="1",
                   REQUEST_BACKEND="Arr", RADARR_URL="http://r/", RADARR_API_KEY="k", FALLBACK_BEFORE_YEAR="0",
                   OVERSEERR_URL="http://s", OVERSEERR_API_KEY="sk", CHANNEL_NAME="plex-join")
    assert cfg.guild_id == 77 and cfg.plex_name == "lab.example Plex"
    assert cfg.available_text == "Available to watch on plex.lab.example now"
    assert cfg.announce_channel == {"movie": "movies", "tv": "123"} and cfg.status_channel == "plex-status"
    assert cfg.auto_revoke and cfg.request_backend == "arr" and cfg.radarr_url == "http://r" and cfg.has_radarr
    assert cfg.fallback_before_year == 0 and cfg.has_seerr and cfg.channel_name == "plex-join"
    assert settings(GUILD_ID="42").guild_id == 42                       # PLEXREQ_GUILD_ID empty → lab guild
    assert settings(AUTO_REVOKE="true").auto_revoke and not settings(AUTO_REVOKE="0").auto_revoke


@pytest.mark.parametrize("bad,match", [
    ({"PLEX_URL": "plex:32400"}, "PLEX_URL"),
    ({"PLEX_TOKEN": ""}, "PLEX_TOKEN"),
    ({"REQUEST_BACKEND": "jellyseerr"}, "REQUEST_BACKEND"),
])
def test_settings_validation(bad, match):
    with pytest.raises(RuntimeError, match=match):
        settings(**bad)


def test_spec_settings_match_example_and_store_from_env(tmp_path):
    spec = SERVICES[0]
    example = Path(__file__).resolve().parents[1] / ".env.example"
    keys_in_example = [line.split("=", 1)[0] for line in example.read_text().splitlines()
                       if line and not line.startswith("#")]
    shared = {"DISCORD_TOKEN", "LAB_NAME", "GUILD_ID", "DATA_DIR", "LOG_LEVEL"}
    assert [s.key for s in spec.settings] == [k for k in keys_in_example if k not in shared]
    assert spec.required_missing({}) == ["CHANNEL_ID", "PLEX_URL", "PLEX_TOKEN"]      # file order
    assert spec.required_missing({**BASE}) == [] and spec.intents == ["members", "message_content"]
    assert spec.setting("REQUEST_BACKEND").choices == ["auto", "seerr", "arr"]
    assert spec.setting("PLEX_TOKEN").type == "secret" and spec.setting("CHANNEL_ID").type == "channel"
    assert spec.setting("AUTO_REVOKE").type == "bool" and spec.setting("REQUESTS_CHANNEL_ID").type == "channel"
    assert all(s.help for s in spec.settings), [s.key for s in spec.settings if not s.help]
    # the standalone runner builds a one-service store from a flat .env
    environ = {"DISCORD_TOKEN": "tok", "GUILD_ID": "42", "LAB_NAME": "lab1", "ADMIN_ROLE_IDS": "4, 5", "PLEX_URL": "http://p",
               "PLEX_TOKEN": "t", "CHANNEL_ID": "1", "PLEXREQ_GUILD_ID": "77", "UNRELATED": "x", "RADARR_URL": ""}
    store = store_from_env(environ, tmp_path)
    assert store.presences["default"]["token"] == "tok" and store.lab["guild_id"] == "42" and store.lab["admin_role_ids"] == ["4", "5"]
    svc = store.services["plexrequests"]
    assert svc["enabled"] and svc["env"] == {"PLEX_URL": "http://p", "PLEX_TOKEN": "t", "CHANNEL_ID": "1", "PLEXREQ_GUILD_ID": "77"}
    env = store.env_for("plexrequests")
    assert env["DISCORD_TOKEN"] == "tok" and env["LAB_NAME"] == "lab1" and env["PLEXREQ_GUILD_ID"] == "77"


# ----- sticky embeds never duplicate -----------------------------------------------------------------------------
class _Msg:
    _ids = 500

    def __init__(self, channel, embed, author):
        _Msg._ids += 1
        self.id, self.channel, self.embeds, self.author = _Msg._ids, channel, [embed], author
        self.edits, self.deleted = [], False

    async def edit(self, **kw):
        self.edits.append(kw)

    async def delete(self):
        self.deleted = True
        self.channel.messages.pop(self.id, None)


class _Channel:
    name = "join-plex"

    def __init__(self):
        self.messages, self.sent = {}, []

    def add(self, embed, author):
        m = _Msg(self, embed, author)
        self.messages[m.id] = m
        return m

    async def send(self, *, embed=None, view=None):
        m = self.add(embed, ME)
        self.sent.append(m)
        return m

    async def fetch_message(self, mid):
        if mid in self.messages:
            return self.messages[mid]
        raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), {"message": "Unknown Message", "code": 10008})

    async def history(self, limit=100):
        for m in sorted(self.messages.values(), key=lambda m: m.id, reverse=True):
            yield m


ME = SimpleNamespace(id=42)


@pytest.mark.asyncio
async def test_sticky_ensure_adopts_and_deletes_strays(tmp_path):
    from periscope_plexrequests.records import Records
    from periscope_plexrequests.sticky import Sticky

    rec = Records(JsonState(tmp_path / "s.json"))
    st = Sticky(rec, me=lambda: ME)
    ch = _Channel()
    title = "🎬  get access"
    older = ch.add(discord.Embed(title=title), ME)
    newer = ch.add(discord.Embed(title=title), ME)
    someone = ch.add(discord.Embed(title=title), SimpleNamespace(id=7))          # not the bot's → untouched
    other = ch.add(discord.Embed(title="🍿  Request movies & TV shows"), ME)   # a different sticky → untouched
    await st.ensure(ch, "invite_message_id", discord.Embed(title=title), view=None)
    assert not ch.sent and rec.message_id("invite_message_id") == newer.id and newer.edits
    assert older.deleted and not someone.deleted and not other.deleted
    # the remembered one is gone and no copy exists → post once
    ch.messages.pop(newer.id)
    await st.ensure(ch, "invite_message_id", discord.Embed(title=title), view=None)
    assert len(ch.sent) == 1 and rec.message_id("invite_message_id") == ch.sent[0].id

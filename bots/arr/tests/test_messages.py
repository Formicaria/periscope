"""Message kinds: every media.* card previews from its sample, and the send sites honour a customisation."""

import json
from types import SimpleNamespace

import pytest
from periscope import JsonState, StatusBoard
from periscope.messages import REGISTRY, STANDARD_VARIABLES, Messages, MessageStore, kinds_for, preview

from periscope_arr import service  # noqa: F401  — importing the service module is what registers the kinds
from periscope_arr.cogs.media import BOARD_KIND, Stream, board_ctx, board_embed
from periscope_arr.cogs.webhooks import (EVENT_STYLE, OTHER_KIND, VARIABLES, Webhooks, event_ctx, event_embed, kind_for,
                                        parse_event)
from periscope_arr.config import ArrSettings
from periscope_arr.hub import BoardHost, MediaHub
from periscope_arr.messages import CARDS, LAB, SAMPLES

FEED = {"media.grab", "media.import", "media.upgrade", "media.rename", "media.added", "media.deleted", "media.manual",
        "media.health", "media.update", "media.test", "media.other"}
EXPECTED = FEED | {BOARD_KIND}


def _parts(embed):
    """What a template reproduces of an embed."""
    return (embed.title, embed.description, embed.url, embed.color.value if embed.color else None,
            [(f.name, f.value, f.inline) for f in embed.fields], embed.footer.text if embed.footer else None,
            embed.thumbnail.url if embed.thumbnail else None)


def test_every_card_has_a_kind():
    assert {k.key for k in kinds_for("media")} == EXPECTED
    for k in kinds_for("media"):
        assert k.sample is not None and k.title and k.description and k.where and k.where_env and k.group
    assert set(CARDS) == set(SAMPLES) == set(VARIABLES) == FEED and OTHER_KIND in FEED
    # every card the renderer knows how to draw is customised under a registered kind, unknown events as `other`
    for event_type in EVENT_STYLE:
        assert kind_for(parse_event("sonarr", {"eventType": event_type})) in FEED
    assert kind_for(parse_event("lidarr", {"eventType": "Weird"})) == OTHER_KIND


@pytest.mark.parametrize("key", sorted(EXPECTED))
def test_sample_previews(key):
    kind = REGISTRY[key]
    embed, ctx = kind.sample()
    assert embed is not None and embed.title and embed.footer.text == f"🧪 {LAB}"
    json.dumps(ctx)                                                   # plain values only
    assert not set(ctx) & set(STANDARD_VARIABLES)                     # never shadows the embed's own parts
    assert set(kind.variables) == set(ctx)                            # what is documented is what is passed
    again, ctx_again = kind.sample()
    assert _parts(again) == _parts(embed) and ctx_again == ctx        # deterministic
    rendered, full, err = preview(kind, None)
    assert err is None and rendered is not None
    assert _parts(rendered) == _parts(embed)                          # the identity template reproduces the card
    assert full["lab"] == "lab" and full["service"] == "media" and full["title"] == embed.title


@pytest.mark.parametrize("key", sorted(FEED))
def test_feed_samples_are_their_own_kind(key):
    app, payload = SAMPLES[key]
    ev = parse_event(app, payload)
    assert kind_for(ev) == key                                        # not the generic card in disguise
    assert f"{app.capitalize()}: " in event_embed(ev, LAB).title
    assert event_ctx(ev)["app"] == app and event_ctx(ev)["event"] == payload["eventType"]


def test_kind_follows_what_happened():
    grab = parse_event("radarr", {"eventType": "Grab", "movie": {"title": "Dune"}})
    assert kind_for(grab) == "media.grab"
    plain = parse_event("radarr", {"eventType": "Download", "movie": {"title": "Dune"}, "isUpgrade": False})
    better = parse_event("radarr", {"eventType": "Download", "movie": {"title": "Dune"}, "isUpgrade": True})
    assert kind_for(plain) == "media.import" and kind_for(better) == "media.upgrade"
    assert kind_for(parse_event("sonarr", {"eventType": "Upgrade"})) == "media.upgrade"
    assert kind_for(parse_event("lidarr", {"eventType": "ArtistAdd", "artist": {"name": "Air"}})) == "media.added"
    assert kind_for(parse_event("lidarr", {"eventType": "AlbumDelete", "artist": {"name": "Air"}})) == "media.deleted"
    restored = parse_event("radarr", {"eventType": "HealthRestored", "type": "X", "message": "m"})
    assert kind_for(restored) == "media.health"
    assert kind_for(parse_event("prowlarr", {"eventType": "Test"})) == "media.test"


def test_event_ctx_is_plain_and_complete():
    app, payload = SAMPLES["media.grab"]
    ctx = event_ctx(parse_event(app, payload))
    assert ctx["series"] == "The Expanse" and ctx["year"] == 2015 and ctx["movie"] == "" and ctx["albums"] == []
    assert ctx["episodes"] == [{"season": 6, "number": 1, "code": "S06E01", "title": "Strange Dogs"}]
    assert ctx["size"] == 2147483648 and ctx["indexer"] == "NZBgeek (Prowlarr)" and ctx["upgrade"] is False
    assert ctx["release"] == payload["release"]["releaseTitle"] and ctx["poster"].startswith("https://")
    # a bare payload still carries every variable of its kind, just empty
    bare = event_ctx(parse_event("radarr", {"eventType": "Grab"}))
    assert set(bare) == set(VARIABLES["media.grab"]) and bare["year"] == 0 and bare["media"] == "Unknown movie"
    # rename pairs and health / update facts
    renamed = event_ctx(parse_event(*SAMPLES["media.rename"]))["renamed"]
    assert len(renamed) == 2 and renamed[0]["to"].endswith("[Bluray-1080p].mkv") and "BORDURE" in renamed[0]["from"]
    health = event_ctx(parse_event(*SAMPLES["media.health"]))
    assert health["check"] == "IndexerLongTermStatusCheck" and health["level"] == "warning" and health["wiki"]
    update = event_ctx(parse_event(*SAMPLES["media.update"]))
    assert (update["previous_version"], update["new_version"]) == ("1.24.3.4754", "1.25.4.4818")
    problems = event_ctx(parse_event(*SAMPLES["media.manual"]))
    assert problems["status"] == "Warning" and problems["problems"][0].startswith("Found matching series")


def test_series_delete_says_whether_files_went_too():
    # Sonarr / Radarr send `deletedFiles: true` on a series / movie delete (an import lists the files it replaced)
    ev = parse_event("sonarr", {"eventType": "SeriesDelete", "series": {"title": "The Expanse", "year": 2015},
                                "deletedFiles": True})
    assert ev.fields["Deleted files"] == "yes" and kind_for(ev) == "media.deleted"
    ctx = event_ctx(ev)
    assert ctx["files_deleted"] is True and ctx["reason"] == "" and ctx["path"] == ""
    ev = parse_event("sonarr", {**SAMPLES["media.upgrade"][1]})
    assert ev.fields["Deleted files"] == "1" and event_ctx(ev)["deleted_files"] == 1


# ----- the board from its facts ---------------------------------------------------------------------------

def test_board_from_probe_results():
    results = {"sonarr": (True, [{"status": "downloading"}, {"status": "queued"}]),
               "lidarr": (False, OSError("Cannot connect to host lidarr:8686")),
               "prowlarr": (True, [{"type": "warning", "message": "x"}, {"type": "error", "message": "y"}]),
               "qbittorrent": (True, {"dl_info_speed": 1048576, "up_info_speed": 0})}
    data = board_ctx(results, [], [], [], plex=False, jellyfin=False)
    assert [s["name"] for s in data["services"]] == ["sonarr", "lidarr", "prowlarr", "qbittorrent"]
    assert data["down"] == ["lidarr"] and "lidarr:8686" in data["services"][1]["error"]
    assert data["services"][2]["issues"] == 2 and data["queues"] == [{"app": "sonarr", "queued": 2, "downloading": 1}]
    assert data["qbittorrent"] == {"down": 1048576, "up": 0} and data["sabnzbd"] == {} and data["disk"] == {}
    json.dumps(data)
    e = board_embed(data, "THE LAB")
    assert e.title == "🔴 Media stack" and e.description == "🟢 sonarr  🔴 lidarr  🟢 prowlarr (2 issues)  🟢 qbittorrent"
    assert [(f.name, f.value) for f in e.fields] == [("Queues", "sonarr: **2** queued, 1 downloading"),
                                                     ("Transfer", "qBit ⬇️ 1.0 MB/s ⬆️ 0.0 B/s")]
    # a media server that is configured but idle still gets its Streams field; one that errored is red
    stream = Stream(server="Plex", user="alice", title="Heat (1995)", player="Plex Web", pct=50.0, method="direct")
    disks = [{"path": "/data", "freeSpace": 25, "totalSpace": 100}]
    data = board_ctx({}, [stream], ["jellyfin: 502 Bad Gateway"], disks, plex=True, jellyfin=True)
    assert data["down"] == ["jellyfin"] and data["services"][1]["error"] == "502 Bad Gateway"
    assert data["streams"][0]["user"] == "alice" and data["disk"] == {"free": 25.0, "total": 100.0, "used_pct": 75.0}
    e = board_embed(data, "THE LAB")
    assert [(f.name, f.value) for f in e.fields] == [("Streams (1)", "▶️ Heat (1995) — alice"),
                                                     ("Disk", "`█████████░░░  75.0%` 25.0 B free of 100.0 B")]
    assert board_embed(board_ctx({}, [], [], [], plex=True, jellyfin=False), None).fields[0].value == "none"


# ----- a customisation at a send site ----------------------------------------------------------------------

class FakeChannel:
    def __init__(self):
        self.sent = []

    async def send(self, content=None, embed=None, **kw):
        self.sent.append(embed)
        return SimpleNamespace(id=len(self.sent), channel=self)


class FakeWebhookServer:
    def __init__(self):
        self.routes = []

    def add_route(self, method, path, handler):
        self.routes.append((method, path))


class FakeBot:
    """A v1-shaped owner: one hub, one media channel, its own `messages`."""

    def __init__(self, tmp_path, messages):
        self.state = JsonState(tmp_path / "state.json")
        self.lab_name = "THE LAB"
        self.settings = SimpleNamespace(alert_channel_id=1, status_channel_id=5)
        self.messages = messages
        self.alerts = SimpleNamespace()          # looked up per event; only health issues would use it
        self.webhook = FakeWebhookServer()
        self.channel = FakeChannel()
        self.media_hub = MediaHub(self, ArrSettings(arr={"sonarr": ("http://sonarr:8989", "k")}, media_channel_id=7))

    async def get_channel_safe(self, cid):
        return self.channel


@pytest.mark.asyncio
async def test_customised_template_changes_the_post(tmp_path):
    store = MessageStore(tmp_path / "config" / "messages.yaml")
    bot = FakeBot(tmp_path, Messages(store, service="sonarr", lab="THE LAB"))
    cog = Webhooks(bot)
    app, payload = SAMPLES["media.grab"]

    await cog.handle(parse_event(app, payload))                              # no customisation: the bot's card
    plain = bot.channel.sent[-1]
    assert plain.title == "Sonarr: ⬇️ Grabbed" and plain.thumbnail.url.startswith("https://")
    assert [f.name for f in plain.fields] == ["Quality", "Group", "Indexer", "Client", "Size"]

    store.set("media.grab", {"title": "🎬 {{ title }}", "description": "{{ description }}", "color": "auto",
                             "fields": [{"name": "Episode", "inline": True,
                                         "value": "{{ episodes[0].code }} · {{ size | bytes }} in {{ lab }}"}],
                             "timestamp": True})
    await cog.handle(parse_event(app, payload))
    custom = bot.channel.sent[-1]
    assert custom.title == "🎬 Sonarr: ⬇️ Grabbed" and custom.description == plain.description
    assert custom.color.value == plain.color.value and custom.timestamp is not None
    assert [(f.name, f.value, f.inline) for f in custom.fields] == [("Episode", "S06E01 · 2.0 GB in THE LAB", True)]

    store.set("media.grab", None, enabled=False)                             # switched off: nothing goes out
    await cog.handle(parse_event(app, payload))
    assert len(bot.channel.sent) == 2
    await cog.handle(parse_event(*SAMPLES["media.import"]))                   # other kinds are unaffected
    assert len(bot.channel.sent) == 3 and bot.channel.sent[-1].title == "🟢 Radarr: ✅ Imported"

    store.reset("media.grab")                                                # back to the bot's card
    await cog.handle(parse_event(app, payload))
    assert _parts(bot.channel.sent[-1]) == _parts(plain)
    await bot.media_hub.close()


def test_board_is_customised_through_the_hub_owner(tmp_path):
    store = MessageStore(tmp_path / "config" / "messages.yaml")
    bot = FakeBot(tmp_path, Messages(store, service="plex", lab="THE LAB"))
    host = BoardHost(bot.media_hub)
    assert host.messages is bot.messages
    board = StatusBoard(host, key="arr", kind=BOARD_KIND)
    embed, data = REGISTRY[BOARD_KIND].sample()
    assert _parts(board.customise(embed, data)) == _parts(embed)             # no customisation: untouched

    store.set(BOARD_KIND, {"title": "📺 {{ lab }} media",
                           "description": "{{ down | length }} down · {{ streams | length }} watching", "color": "auto",
                           "fields": [{"repeat": "queues", "name": "{{ item.app }}", "value": "{{ item.queued }} queued",
                                       "inline": True}],
                           "timestamp": True})
    custom = board.customise(embed, data)
    assert custom.title == "📺 THE LAB media" and custom.description == "0 down · 2 watching"
    assert [(f.name, f.value) for f in custom.fields] == [("sonarr", "2 queued"), ("radarr", "1 queued"),
                                                          ("lidarr", "0 queued")]
    store.set(BOARD_KIND, None, enabled=False)
    assert board.customise(embed, data) is None                              # switched off: the board comes down

"""The plexrequests service's message kinds: every post it makes, registered for the Messages page with a sample
to preview and customise it from.

Registering here is what lists a kind on the page. The two sticky button embeds carry their wording as an explicit
template (`template=`), so the literal text is what shows up in the editor; the cogs post `bot.messages.render(kind,
sticky_ctx(cfg))`. Everything else is built in code and passed through `bot.messages.apply(kind, embed, ctx)` right
before it is posted or edited (`cogs/requests.py` for the cards, `cogs/newonplex.py` for the feed, `cogs/stats.py`
and the `/requests mystatus` reply), with the same ctx a kind's sample returns here; the status board hands its data
to the core StatusBoard, which applies `plexrequests.board` itself. Plain-text replies, DMs and the ready ping are
not kinds. The samples are fixed picks, watches and probe results shaped like the real ones, run through the real
builders — no clocks, no random values.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import discord
from periscope.messages import MessageKind, register

from .cogs.board import board_ctx, board_embed
from .cogs.newonplex import new_on_plex_ctx, new_on_plex_embed
from .cogs.requests import (
    available_ctx,
    available_embed,
    mystatus_ctx,
    mystatus_embed,
    request_card,
    request_ctx,
)
from .cogs.stats import stats_ctx, stats_embed
from .common import (
    AVAILABLE_KIND,
    BOARD_KIND,
    INVITE_KIND,
    INVITE_TEMPLATE,
    MYSTATUS_KIND,
    NEW_ON_PLEX_KIND,
    REQUEST_CARD_KIND,
    REQUEST_KIND,
    REQUEST_TEMPLATE,
    STATS_KIND,
    build_invite_embed,
    build_request_embed,
    sticky_ctx,
)
from .config import PlexRequestsSettings
from .stats import Stats

# ----- sample data ----------------------------------------------------------------------------------------
SAMPLE_NOW = datetime(2026, 9, 2, 21, 14, 3, tzinfo=timezone.utc)
SAMPLE_CFG = PlexRequestsSettings(
    plex_url="http://plex:32400", plex_token="sample", channel_name="join-plex", requests_channel_id=200,
    role_name="plex members", requests_role_name="plex members", server_name="lab.example",
    plex_link="plex.lab.example",
)
REQUESTER, REQUESTER_ID = "alice", 200000000000000001

# a Seerr search pick, as the request flows see it
MOVIE = {
    "tmdb_id": 438631, "media_type": "movie", "title": "Dune", "year": "2021", "status": 1,
    "poster": "https://image.tmdb.org/t/p/w342/d5NXSklXo0qyIYkgV94XAgMIckC.jpg",
    "overview": "A young nobleman travels to a desert planet whose spice is the most valuable substance in the "
                "universe, and finds a destiny larger than his family's feud.",
    "backend": "seerr",
}
# the availability watch the request above leaves behind
WATCH = {"backend": "seerr", "media_id": 901, "channel_id": 300, "message_id": 4001, "requester": REQUESTER,
         "requester_id": REQUESTER_ID, "title": "Dune", "media_type": "movie", "year": "2021",
         "added": SAMPLE_NOW.timestamp() - 3 * 86400}
# a recently added item, as the Plex gateway lists it
EPISODE = {"key": "48213", "kind": "episode", "title": "The Expanse — S06E01 · Strange Dogs", "year": 2021,
           "summary": "The crew regroups after a strike on the inner planets while an old enemy resurfaces on Laconia."}
# what the board's probes answered: Plex streams, the Radarr / Sonarr queue summaries, the shared disk
STREAMS = ["**alice** — Dune (42%)", "**bob** — The Expanse · S06E01 (7%)"]
QUEUES = [{"app": "radarr", "ok": True, "total": 2, "top": ["Dune: Part Two — 00:06:30", "Heat — 01:12:04"],
           "error": ""},
          {"app": "sonarr", "ok": True, "total": 0, "top": [], "error": ""}]
DISKS = [{"path": "/data", "free": 872415232000, "total": 4000787030016}]
# a month of counters, two users
STATS = {
    "since": SAMPLE_NOW.timestamp() - 30 * 86400,
    "totals": {"invite_button": 12, "request_button": 40, "typed_request": 9, "search": 57, "pick": 31,
               "request_ok": 28, "already_on_plex": 3, "invite_sent": 9, "became_available": 22, "cmd_mystatus": 14,
               "new_on_plex": 61},
    "users": {
        "1": {"name": "alice", "last": SAMPLE_NOW.timestamp() - 3600,
              "events": {"request_button": 25, "search": 34, "pick": 20, "request_ok": 18, "invite_sent": 1}},
        "2": {"name": "bob", "last": SAMPLE_NOW.timestamp() - 2 * 86400,
              "events": {"invite_button": 1, "request_button": 15, "search": 23, "pick": 11, "request_ok": 10,
                         "invite_sent": 1}},
    },
}
# alice's request history, as /requests mystatus lists it
HISTORY = [{"title": "Heat", "year": "1995", "type": "movie", "ts": SAMPLE_NOW.timestamp() - 9 * 86400,
            "status": "available", "msg": 3990},
           {"title": "The Expanse", "year": "2015", "type": "tv", "ts": SAMPLE_NOW.timestamp() - 2 * 86400,
            "status": "queued", "msg": 3998},
           {"title": "Dune", "year": "2021", "type": "movie", "ts": SAMPLE_NOW.timestamp() - 3600,
            "status": "queued", "msg": 4001}]


# ----- samples --------------------------------------------------------------------------------------------
def _sample_invite() -> tuple[discord.Embed | None, dict[str, Any]]:
    return build_invite_embed(SAMPLE_CFG), sticky_ctx(SAMPLE_CFG)


def _sample_request() -> tuple[discord.Embed | None, dict[str, Any]]:
    return build_request_embed(SAMPLE_CFG), sticky_ctx(SAMPLE_CFG)


def _sample_board() -> tuple[discord.Embed | None, dict[str, Any]]:
    data = board_ctx(STREAMS, QUEUES, DISKS)
    return board_embed(data, SAMPLE_CFG.plex_name, now=SAMPLE_NOW), data


def _sample_request_card() -> tuple[discord.Embed | None, dict[str, Any]]:
    return request_card(MOVIE, REQUESTER), request_ctx(MOVIE, REQUESTER, REQUESTER_ID, SAMPLE_CFG)


def _sample_available() -> tuple[discord.Embed | None, dict[str, Any]]:
    card = request_card(MOVIE, REQUESTER)          # the card as it was posted, then flipped in place
    return available_embed(card, REQUESTER, SAMPLE_CFG), available_ctx(WATCH, SAMPLE_CFG)


def _sample_new_on_plex() -> tuple[discord.Embed | None, dict[str, Any]]:
    return new_on_plex_embed(EPISODE, SAMPLE_CFG.plex_name), new_on_plex_ctx(EPISODE, SAMPLE_CFG.plex_name)


def _sample_stats() -> tuple[discord.Embed | None, dict[str, Any]]:
    report = Stats({"stats": STATS}).report(now=SAMPLE_NOW.timestamp())   # a dict answers .get like the state
    return stats_embed(report, SAMPLE_CFG.plex_name), stats_ctx(report, STATS, SAMPLE_CFG.plex_name)


def _sample_mystatus() -> tuple[discord.Embed | None, dict[str, Any]]:
    return mystatus_embed(HISTORY), mystatus_ctx(HISTORY, REQUESTER, SAMPLE_CFG)


# ----- variables (name → meaning, shown next to the editor) ------------------------------------------------
PLEX_NAME = "the server as the bot names it: SERVER_NAME followed by Plex, or just Plex"
REQUESTER_VARS = {"requester": "who asked for it (their display name)",
                  "requester_id": "their Discord user id — <@{{ requester_id }}> mentions them"}

STICKY_VARIABLES = {
    "plex_name": PLEX_NAME,
    "role_name": "the role invitees get (ROLE_NAME)",
    "requests_role_name": "the role needed to request media (REQUESTS_ROLE_NAME); empty when anyone may",
    "channel_name": "the invite channel's name (CHANNEL_NAME), for places a mention cannot go (the footer)",
    "invite_channel": "the invite channel as a mention (<#id>) for the description, or #name when only the name is set",
    "plex_link": "your Plex address (PLEX_LINK, e.g. plex.example.com); empty when unset",
}
MEDIA_VARIABLES = {
    "name": "the movie or show's name", "year": "its release year; empty when unknown",
    "label": "name and year as the card shows them: Dune (2021)", "media_type": "movie or tv",
}
REQUEST_CARD_VARIABLES = {
    **MEDIA_VARIABLES,
    "overview": "the synopsis (first 350 characters)", "poster": "the poster image url; empty when there is none",
    "tmdb_id": "its TMDB id (TVDB id for shows found through Sonarr)",
    "backend": "where the request went: seerr or arr", "via": "the app that took it, in words: Seerr · Radarr · Sonarr",
    **REQUESTER_VARS, "plex_name": PLEX_NAME,
}
AVAILABLE_VARIABLES = {
    **MEDIA_VARIABLES, **REQUESTER_VARS,
    "backend": "where the request went: seerr or arr",
    "available_text": "the ready line: Available to watch on <PLEX_LINK> now",
    "plex_link": "your Plex address (PLEX_LINK); empty when unset", "plex_name": PLEX_NAME,
}
NEW_ON_PLEX_VARIABLES = {
    "name": "the item's title as Plex lists it (Show — S01E01 · Episode for an episode)",
    "year": "its year; empty when Plex has none", "kind": "what was added: movie · episode · season · show · album · …",
    "label": "name and year as the card shows them", "plex_name": PLEX_NAME,
}
BOARD_VARIABLES = {
    "plex_ok": "false when Plex did not answer",
    "streams": "one line per active stream, as Plex reports them: **user** — title (42%)",
    "queues": "one entry per configured Radarr / Sonarr: item.app · item.ok · item.total · item.top (the first titles "
              "with their time left) · item.error",
    "disks": "the apps' root folders: item.path · item.free · item.total, in bytes (| bytes makes them readable)",
    "interval_s": "how often the board refreshes, in seconds",
}
STATS_VARIABLES = {
    "report": "the report as plain text (what the code block holds)",
    "totals": "the counters by event: totals.search · totals.request_ok · totals.invite_sent · …",
    "user_count": "how many people have been counted", "plex_name": PLEX_NAME,
}
MYSTATUS_VARIABLES = {
    "history": "the user's requests, newest first: item.name · item.year · item.media_type · item.status (queued · "
               "available) · item.when",
    "count": "how many requests are listed", "requester": "who asked (their display name)", "plex_name": PLEX_NAME,
}

# ----- the kinds -------------------------------------------------------------------------------------------
CARD_WHERE = "#movies / #tv when set (MOVIES_CHANNEL / TV_CHANNEL), else the requests channel"

register(
    MessageKind(INVITE_KIND, "Get Plex Access (sticky)",
                "the button embed that stays at the bottom of the invite channel: posted on start-up when there is "
                "none, re-posted whenever something else lands below it; switched off, nothing is posted and an "
                "existing copy is left as it is",
                where="the invite channel", where_env="CHANNEL_ID", sample=_sample_invite, variables=STICKY_VARIABLES,
                template=INVITE_TEMPLATE, group="stickies"),
    MessageKind(REQUEST_KIND, "Search & Request (sticky)",
                "the button embed that stays at the bottom of the requests channel: posted on start-up when there is "
                "none, re-posted whenever something else lands below it; switched off, nothing is posted and an "
                "existing copy is left as it is",
                where="the requests channel", where_env="REQUESTS_CHANNEL_ID", sample=_sample_request,
                variables=STICKY_VARIABLES, template=REQUEST_TEMPLATE, group="stickies"),
    MessageKind(BOARD_KIND, "Live status board",
                "the one message edited in place every 60 s: who is streaming what, the Radarr / Sonarr queues with "
                "their time left, disk space; switched off, the board is taken down",
                where="the status channel", where_env="STATUS_CHANNEL", sample=_sample_board, variables=BOARD_VARIABLES,
                group="boards"),
    MessageKind(REQUEST_CARD_KIND, "Request card",
                "the media card posted once Seerr / Radarr / Sonarr accepted a request; switched off, requests still "
                "go through but nothing is announced, so there is no card to flip green later either",
                where=CARD_WHERE, where_env="REQUESTS_CHANNEL_ID", sample=_sample_request_card,
                variables=REQUEST_CARD_VARIABLES, group="cards"),
    MessageKind(AVAILABLE_KIND, "Available on Plex",
                "how the request card looks once the title is on Plex — edited in place, green, with the ready line "
                "in the footer; the requester is pinged separately. Switched off, the card is left as it was",
                where="the request card's channel (edited in place)", where_env="REQUESTS_CHANNEL_ID",
                sample=_sample_available, variables=AVAILABLE_VARIABLES, group="cards"),
    MessageKind(NEW_ON_PLEX_KIND, "New on Plex",
                "posted for every item that appeared on Plex since the last look (every 5 minutes, at most 5 per "
                "pass); switched off, new items are still remembered so nothing floods in when it comes back",
                where="the new-on-Plex channel", where_env="NEW_CHANNEL", sample=_sample_new_on_plex,
                variables=NEW_ON_PLEX_VARIABLES, group="feed"),
    MessageKind(STATS_KIND, "Usage report",
                "the answer to /requests plexstats (admins only, shown only to them); switched off, the same report "
                "comes as plain text",
                where="a private reply to whoever ran /requests plexstats", sample=_sample_stats,
                variables=STATS_VARIABLES, group="cards"),
    MessageKind(MYSTATUS_KIND, "Your requests",
                "the answer to /requests mystatus: the user's own requests and where they are (shown only to them); "
                "switched off, the same list comes as plain text",
                where="a private reply to whoever ran /requests mystatus", sample=_sample_mystatus,
                variables=MYSTATUS_VARIABLES, group="cards"),
)

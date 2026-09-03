"""Pure helpers shared by the cogs: input parsing, rate limiting, embed builders, channel lookup, and the message
kinds every post is customised under (Messages page)."""

from __future__ import annotations

import re
import time
from typing import Any

import discord
from periscope.messages import embed_ctx, render_template

from .config import PlexRequestsSettings

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# message kinds: the keys the send sites customise their posts under (registered in messages.py)
INVITE_KIND = "plexrequests.invite"             # the sticky Get Plex Access embed
REQUEST_KIND = "plexrequests.request"           # the sticky Search & Request embed
BOARD_KIND = "plexrequests.board"               # the live status board
REQUEST_CARD_KIND = "plexrequests.request_card"  # the card announcing a sent request
AVAILABLE_KIND = "plexrequests.available"       # that card once the title is on Plex
NEW_ON_PLEX_KIND = "plexrequests.new_on_plex"   # the new-on-Plex feed card
STATS_KIND = "plexrequests.stats"               # the /requests plexstats report
MYSTATUS_KIND = "plexrequests.mystatus"         # the /requests mystatus list

# Seerr-style media status codes, also produced for Radarr/Sonarr lookups
STATUS_UNKNOWN, STATUS_PENDING, STATUS_PROCESSING, STATUS_PARTIAL, STATUS_AVAILABLE = 1, 2, 3, 4, 5
STATUS_LABEL = {
    STATUS_PENDING: "⏳ Already requested",
    STATUS_PROCESSING: "⏳ Requested — processing",
    STATUS_PARTIAL: "🟡 Partially on Plex",
    STATUS_AVAILABLE: "✅ Already on Plex",
}

TYPE_EMOJI = {"movie": "🎬", "tv": "📺"}
TYPE_LABEL = {"movie": "Movie", "tv": "TV Show"}
TYPE_COLOUR = {"movie": "#e5a00d", "tv": "#5865f2"}
PLEX_GOLD = "#e5a00d"
BLURPLE = "#5865f2"
AVAILABLE_COLOUR = "#2ecc71"   # cards flip from Plex gold to green when watchable

RESULT_PREFIX = {"sent": "📬", "pending": "⏳", "updated": "✅", "error": "❌"}

QUERY_MIN, QUERY_MAX = 2, 100


# ----- parsing -------------------------------------------------------------------------------------

def valid_email(text: str) -> bool:
    return bool(EMAIL_RE.fullmatch((text or "").strip()))


def find_email(text: str) -> str | None:
    """First email address inside a free-form message, or None."""
    m = EMAIL_RE.search(text or "")
    return m.group(0) if m else None


def parse_typed_title(content: str) -> str | None:
    """A message typed into the requests channel that should be treated as a search: not empty, not a slash
    command, not an email (those belong to the invite flow). Returns the trimmed title or None."""
    text = (content or "").strip()
    if not text or text.startswith("/") or EMAIL_RE.search(text):
        return None
    return text


def validate_query(query: str) -> str | None:
    """Error text when a search query is unusable, else None."""
    q = (query or "").strip()
    if not QUERY_MIN <= len(q) <= QUERY_MAX:
        return f"Give me a title between {QUERY_MIN} and {QUERY_MAX} characters."
    return None


def check_cooldown(bucket: dict[int, list[float]], user_id: int, limit: int = 3, window: int = 600,
                   now: float | None = None) -> bool:
    """Sliding-window rate limit: True when the call is allowed (and recorded), False when over `limit`."""
    now = time.time() if now is None else now
    hits = [t for t in bucket.get(user_id, []) if now - t < window]
    if len(hits) >= limit:
        bucket[user_id] = hits
        return False
    hits.append(now)
    bucket[user_id] = hits
    return True


def title_label(pick: dict[str, Any], bold: bool = True) -> str:
    text = f"{pick['title']} ({pick['year']})" if pick.get("year") else str(pick["title"])
    return f"**{text}**" if bold else text


def fmt_bytes(n: float) -> str:
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "?"


# ----- embeds ---------------------------------------------------------------------------------------

def build_media_embed(pick: dict[str, Any], footer: str | None = None) -> discord.Embed:
    """Info card for a search pick: artwork, title, year, description."""
    e = discord.Embed(
        title=f"{TYPE_EMOJI[pick['media_type']]}  {title_label(pick, bold=False)}",
        colour=discord.Colour.from_str(TYPE_COLOUR[pick["media_type"]]),
        description=pick.get("overview") or "No description available.",
    )
    if pick.get("poster"):
        e.set_thumbnail(url=pick["poster"])
    if footer:
        e.set_footer(text=footer)
    return e


def build_options(results: list[dict[str, Any]]) -> list[discord.SelectOption]:
    opts = []
    for i, r in enumerate(results):
        desc = TYPE_LABEL[r["media_type"]]
        if r["status"] in STATUS_LABEL:
            desc += f" · {STATUS_LABEL[r['status']].split(' ', 1)[1]}"
        opts.append(discord.SelectOption(
            label=title_label(r, bold=False)[:100],
            description=desc[:100],
            value=result_key(r, i),
            emoji=TYPE_EMOJI[r["media_type"]],
        ))
    return opts


def result_key(r: dict[str, Any], i: int) -> str:
    return f"{r['media_type']}:{r['tmdb_id']}:{i}"


def via_label(pick: dict[str, Any]) -> str:
    """Which app took the request: Seerr, else Radarr for movies and Sonarr for shows."""
    if pick.get("backend") == "seerr":
        return "Seerr"
    return "Radarr" if pick["media_type"] == "movie" else "Sonarr"


def media_ctx(pick: dict[str, Any]) -> dict[str, Any]:
    """A search pick as plain template variables (the media-card kinds add who asked for it)."""
    return {
        "name": str(pick["title"]), "year": pick.get("year") or "", "label": title_label(pick, bold=False),
        "media_type": pick["media_type"], "overview": pick.get("overview") or "", "poster": pick.get("poster") or "",
        "tmdb_id": pick.get("tmdb_id") or 0, "backend": pick.get("backend") or "seerr", "via": via_label(pick),
    }


# ----- the sticky button embeds ------------------------------------------------------------------------
# Their wording is a message template (Messages page) rather than code, so it can be edited without a release.
# `build_invite_embed` / `build_request_embed` render the defaults; the cogs post whatever `bot.messages.render`
# gives them, which is the same thing until someone customises it.

INVITE_TEMPLATE: dict[str, Any] = {
    "title": "🎬  {{ plex_name }} — get access",
    "description": (
        "Movies, TV shows and music, streamed from {{ plex_name }}.\n\n"
        "**Three ways to get your invite:**\n"
        "🎟️ Click **Get Plex Access** below and enter your Plex email\n"
        "⌨️ Just type your email in this channel (I'll delete it right away)\n"
        "🔍 Use `/plexinvite email:you@example.com`\n\n"
        "You'll get an email from Plex — hit **Accept**, then watch at "
        "[app.plex.tv](https://app.plex.tv) or any Plex app.\n"
        "Don't have a Plex account? Create one first at "
        "[plex.tv/sign-up](https://www.plex.tv/sign-up/) with the same email."
    ),
    "color": PLEX_GOLD,
    "footer": "Invites are sent automatically • You'll get the {{ role_name }} role",
    "timestamp": False,
}

REQUEST_TEMPLATE: dict[str, Any] = {
    "title": "🍿  Request movies & TV shows",
    "description": (
        "Want something added to Plex? Ask here and it goes straight into the download queue.\n\n"
        "**Three ways to request:**\n"
        "🔎 Click **Search & Request** below\n"
        "⌨️ Just type the title in this channel (e.g. `Dune Part Two`) — I'll tidy your message away\n"
        "🎯 Use `/requests request title:...`\n\n"
        "Searching and picking happens privately — nothing shows up here until your request is actually sent.\n"
        "📈 `/requests mystatus` shows your requests and pings you when they're ready."
    ),
    "color": BLURPLE,
    "footer": "{% if requests_role_name %}Requires the {{ requests_role_name }} role — get it in "
              "#{{ channel_name }}{% endif %}",
    "timestamp": False,
}

STICKY_TEMPLATES = {INVITE_KIND: INVITE_TEMPLATE, REQUEST_KIND: REQUEST_TEMPLATE}


def sticky_ctx(cfg: PlexRequestsSettings) -> dict[str, Any]:
    """The settings the sticky templates can mention, as plain variables."""
    return {"plex_name": cfg.plex_name, "role_name": cfg.role_name, "requests_role_name": cfg.requests_role_name,
            "channel_name": cfg.channel_name, "invite_channel": cfg.invite_channel_where(), "plex_link": cfg.plex_link}


def render_sticky(template: dict[str, Any], cfg: PlexRequestsSettings) -> discord.Embed:
    """A sticky's default wording: its template over the settings (no embed parts to inherit, so those are empty)."""
    embed = render_template(template, {**embed_ctx(None), **sticky_ctx(cfg)})
    assert embed is not None   # the defaults always carry a title
    return embed


def build_invite_embed(cfg: PlexRequestsSettings) -> discord.Embed:
    return render_sticky(INVITE_TEMPLATE, cfg)


def build_request_embed(cfg: PlexRequestsSettings) -> discord.Embed:
    return render_sticky(REQUEST_TEMPLATE, cfg)


def sticky_embed(bot: Any, kind: str, cfg: PlexRequestsSettings) -> discord.Embed | None:
    """The sticky as it should read now: the user's template when there is one, else the default. None when the
    kind is switched off on the Messages page — then nothing is posted and an existing copy is left alone."""
    messages = getattr(bot, "messages", None)
    if messages is None:
        return render_sticky(STICKY_TEMPLATES[kind], cfg)
    if not messages.enabled(kind):
        return None
    # `render` also answers None when the kind is not registered (a cog loaded on its own): post the default then
    return messages.render(kind, sticky_ctx(cfg)) or render_sticky(STICKY_TEMPLATES[kind], cfg)


# ----- discord lookups -------------------------------------------------------------------------------

def is_member(user: Any) -> bool:
    """A guild member (has roles + a guild) as opposed to a bare User from a DM."""
    return getattr(user, "guild", None) is not None and hasattr(user, "roles")


def requests_role_ok(member: Any, cfg: PlexRequestsSettings) -> bool:
    """Gate for the request flows: no role configured → everyone; admins are never locked out."""
    if not cfg.requests_role_name:
        return True
    if not is_member(member):
        return False
    if member.guild_permissions.administrator:
        return True
    return discord.utils.get(member.roles, name=cfg.requests_role_name) is not None


def requests_role_denial(cfg: PlexRequestsSettings) -> str:
    return (f"🔒 You need the **{cfg.requests_role_name}** role to request media. "
            f"Grab Plex access in {cfg.invite_channel_where()} first!")


def resolve_channel(bot: Any, ref: str, guild_id: int | None = None) -> Any:
    """Channel setting (name or id) → channel object, or None. Names are looked up in the service's guild first."""
    if not ref:
        return None
    if ref.isdigit():
        return bot.get_channel(int(ref))
    guilds = list(bot.guilds)
    if guild_id:
        own = bot.get_guild(guild_id)
        if own is not None:
            guilds = [own] + [g for g in guilds if g.id != guild_id]
    for g in guilds:
        ch = discord.utils.get(g.text_channels, name=ref)
        if ch:
            return ch
    return None

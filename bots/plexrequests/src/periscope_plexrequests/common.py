"""Pure helpers shared by the cogs: input parsing, rate limiting, embed builders, channel lookup."""

from __future__ import annotations

import re
import time
from typing import Any

import discord

from .config import PlexRequestsSettings

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

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


def build_invite_embed(cfg: PlexRequestsSettings) -> discord.Embed:
    e = discord.Embed(
        title=f"🎬  {cfg.plex_name} — get access",
        colour=discord.Colour.from_str(PLEX_GOLD),
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


def build_request_embed(cfg: PlexRequestsSettings) -> discord.Embed:
    e = discord.Embed(
        title="🍿  Request movies & TV shows",
        colour=discord.Colour.from_str(BLURPLE),
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

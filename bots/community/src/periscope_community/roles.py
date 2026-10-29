"""Who hears about it: role names or ids (from RESOURCES and PSA_ROLE_ID) → one mention line.

Names are matched against the server's roles case-insensitively at send time, so renaming a role in Discord only
means updating RESOURCES if the old name was used; ids never break. Unknown names are logged and skipped — a typo
must not stop the post going out.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

import discord

log = logging.getLogger(__name__)
ID_RE = re.compile(r"^(?:<@&)?(\d{15,22})>?$")


def resolve(bot: Any, tokens: Iterable[str]) -> list[int]:
    guild = None
    gid = getattr(bot, "guild_id", None)
    if gid:
        try:
            guild = bot.get_guild(int(gid))
        except Exception:  # noqa: BLE001
            guild = None
    out: list[int] = []
    for raw in tokens:
        token = str(raw or "").strip().lstrip("@")
        if not token:
            continue
        m = ID_RE.match(token)
        rid = int(m.group(1)) if m else None
        if rid is None and guild is not None:
            role = next((r for r in guild.roles if r.name.lower() == token.lower()), None)
            rid = role.id if role else None
        if rid is None:
            log.warning("community: no role called %r on this server — not pinging it", token)
            continue
        if rid not in out:
            out.append(rid)
    return out


def mention(bot: Any, tokens: Iterable[str], fallback: int | None = None) -> tuple[str | None, discord.AllowedMentions]:
    """(content, allowed_mentions) for a post: the roles named, else the fallback role, else nobody."""
    ids = resolve(bot, tokens)
    if not ids and fallback:
        ids = [int(fallback)]
    if not ids:
        return None, discord.AllowedMentions.none()
    return " ".join(f"<@&{i}>" for i in ids), discord.AllowedMentions(
        everyone=False, users=False, roles=[discord.Object(id=i) for i in ids])


def announcement(bot: Any, setting: str) -> tuple[str | None, discord.AllowedMentions]:
    """Who an announcement or changelog pings (ANNOUNCE_PING): everyone (the default), here, some roles, or none.
    Outages and maintenance never come through here — those ping the roles of whatever is affected."""
    s = (setting or "").strip()
    low = s.lower().lstrip("@")
    if low in ("none", "off", "nobody", "no"):
        return None, discord.AllowedMentions.none()
    if low in ("", "everyone", "here"):
        return f"@{low or 'everyone'}", discord.AllowedMentions(everyone=True, users=False, roles=False)
    return mention(bot, [t for t in s.split(",") if t.strip()])


def tokens_for(resources: Iterable[Any], names: Iterable[str] | None) -> list[str]:
    """The ping roles of the named resources (None or empty names = every resource)."""
    wanted = {n.lower() for n in (names or [])}
    out: list[str] = []
    for r in resources:
        if wanted and r.name.lower() not in wanted:
            continue
        for t in getattr(r, "ping", []) or []:
            if t not in out:
                out.append(t)
    return out

"""New-on-Plex feed in NEW_CHANNEL: announces recently added items every 5 minutes. The first pass only
records what is already there (silent baseline) so an install never spams history."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands, tasks

from ..common import AVAILABLE_COLOUR, resolve_channel
from ..context import PlexRequests

log = logging.getLogger(__name__)

MAX_PER_PASS = 5


def new_label(it: dict[str, Any]) -> str:
    year = f" ({it['year']})" if it.get("year") else ""
    return f"{it['title']}{year}"


def fresh_items(items: list[dict[str, Any]], seen: list[str]) -> list[dict[str, Any]]:
    """Items whose key is not in the baseline, oldest first, capped per pass."""
    seen_set = set(seen)
    fresh = [i for i in items if i["key"] not in seen_set]
    return list(reversed(fresh[:MAX_PER_PASS]))


class NewOnPlexCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot
        self.ctx: PlexRequests = bot.plexreq
        self.cfg = self.ctx.cfg

    async def cog_load(self) -> None:
        if self.cfg.new_channel and self.cfg.plex_token:
            self.new_on_plex.start()

    async def cog_unload(self) -> None:
        self.new_on_plex.cancel()

    @tasks.loop(minutes=5)
    async def new_on_plex(self) -> None:
        channel = resolve_channel(self.bot, self.cfg.new_channel, self.cfg.guild_id)
        if channel is None:
            return
        try:
            items = await asyncio.to_thread(self.ctx.plex.recently_added)
        except Exception as ex:  # noqa: BLE001
            log.warning("new-on-plex: %s", ex)
            return
        keys = [i["key"] for i in items]
        seen = self.ctx.records.plex_seen()
        if seen is None:                       # first pass: baseline quietly
            self.ctx.records.set_plex_seen(keys)
            log.info("new-on-plex: baselined %d items", len(keys))
            return
        for it in fresh_items(items, seen):
            emoji = "🎬" if it["kind"] == "movie" else "📺"
            e = discord.Embed(title=f"🆕 {emoji}  {new_label(it)}", description=it["summary"] or None,
                              colour=discord.Colour.from_str(AVAILABLE_COLOUR))
            e.set_footer(text=f"Now on {self.cfg.plex_name}")
            try:
                await channel.send(embed=e)
                self.ctx.stats.bump("new_on_plex")
            except discord.HTTPException:
                break
        self.ctx.records.set_plex_seen(keys)

    @new_on_plex.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Any) -> None:
    await bot.add_cog(NewOnPlexCog(bot))

"""`/requests plexstats` — the usage report (every button press, search, request, invite; per user), admin only."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

from ..common import BLURPLE, STATS_KIND
from ..context import PlexRequests, plex_admin_only, slash

log = logging.getLogger(__name__)


def stats_embed(report: str, plex_name: str) -> discord.Embed:
    """The report as a code block (Discord keeps the columns aligned that way)."""
    return discord.Embed(title=f"📊 {plex_name} — usage", description=f"```\n{report[:3900]}\n```",
                         colour=discord.Colour.from_str(BLURPLE))


def stats_ctx(report: str, data: dict[str, Any], plex_name: str) -> dict[str, Any]:
    """What a plexrequests.stats template can use besides the embed's own parts: the report text and the raw
    counters it was made from."""
    return {"report": report, "totals": dict(data.get("totals") or {}), "user_count": len(data.get("users") or {}),
            "plex_name": plex_name}


class StatsCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot
        self.ctx: PlexRequests = bot.plexreq

    async def cog_load(self) -> None:
        self.ctx.register(slash("plexstats", "Usage report: buttons, searches, requests, invites — per user (admin)",
                                self.plexstats))

    async def cog_unload(self) -> None:
        self.ctx.unregister("plexstats")

    @plex_admin_only()
    async def plexstats(self, interaction: discord.Interaction) -> None:
        self.ctx.stats.bump("cmd_plexstats", interaction.user)
        report = self.ctx.stats.report()
        plex_name = self.ctx.cfg.plex_name
        e = self.bot.messages.apply(STATS_KIND, stats_embed(report, plex_name),
                                    stats_ctx(report, self.ctx.stats.data(), plex_name))
        if e is None:                          # the embed is switched off: the same report as plain text
            await interaction.response.send_message(f"```\n{report[:1900]}\n```", ephemeral=True)
            return
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot: Any) -> None:
    await bot.add_cog(StatsCog(bot))

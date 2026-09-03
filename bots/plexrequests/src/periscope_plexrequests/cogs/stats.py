"""`/requests plexstats` — the usage report (every button press, search, request, invite; per user), admin only."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

from ..common import BLURPLE
from ..context import PlexRequests, plex_admin_only, slash

log = logging.getLogger(__name__)


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
        e = discord.Embed(title=f"📊 {self.ctx.cfg.plex_name} — usage", description=f"```\n{report[:3900]}\n```",
                          colour=discord.Colour.from_str(BLURPLE))
        await interaction.response.send_message(embed=e, ephemeral=True)


async def setup(bot: Any) -> None:
    await bot.add_cog(StatsCog(bot))

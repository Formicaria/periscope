"""Live status board in STATUS_CHANNEL: who is streaming what, Radarr/Sonarr queues with ETAs, disk space.
One embed, edited in place every minute."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands, tasks

from ..common import PLEX_GOLD, fmt_bytes, resolve_channel
from ..context import PlexRequests

log = logging.getLogger(__name__)

STATUS_MESSAGE_KEY = "status_message_id"
INTERVAL_S = 60


class BoardCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot
        self.ctx: PlexRequests = bot.plexreq
        self.cfg = self.ctx.cfg
        self._msg: discord.Message | None = None

    async def cog_load(self) -> None:
        if self.cfg.status_channel:
            self.status_board.start()

    async def cog_unload(self) -> None:
        self.status_board.cancel()

    async def build_status_embed(self) -> discord.Embed:
        e = discord.Embed(title=f"📊  {self.cfg.plex_name} — live status", colour=discord.Colour.from_str(PLEX_GOLD))
        try:
            streams = await asyncio.to_thread(self.ctx.plex.sessions)
        except Exception as ex:  # noqa: BLE001
            log.warning("status board: plex sessions failed: %s", ex)
            streams = None
        if streams is None:
            e.add_field(name="🎞️ Now streaming", value="*(Plex not reachable)*", inline=False)
        else:
            e.add_field(name=f"🎞️ Now streaming — {len(streams)}", value="\n".join(streams[:6]) or "Nobody right now",
                        inline=False)

        backend = self.ctx.backend
        for client, label in ((backend.radarr, "🎬 Radarr queue"), (backend.sonarr, "📺 Sonarr queue")):
            if client is None:
                continue
            try:
                total, top = await client.queue_summary()
                e.add_field(name=f"{label} — {total}", value="\n".join(top) or "Empty", inline=True)
            except Exception as ex:  # noqa: BLE001
                e.add_field(name=label, value=f"*(unreachable: {str(ex)[:40]})*", inline=True)

        disks, seen_paths = [], set()
        for client in (backend.radarr, backend.sonarr):
            if client is None:
                continue
            try:
                for path, free, total in await client.disk_space():
                    if path in seen_paths or not total:
                        continue
                    seen_paths.add(path)
                    disks.append(f"`{path}` — {fmt_bytes(free)} free of {fmt_bytes(total)}")
            except Exception:  # noqa: BLE001
                pass
        if disks:
            e.add_field(name="💾 Disk", value="\n".join(disks[:5]), inline=False)
        e.set_footer(text=f"{self.cfg.plex_name} • refreshes every {INTERVAL_S}s")
        e.timestamp = discord.utils.utcnow()
        return e

    @tasks.loop(seconds=INTERVAL_S)
    async def status_board(self) -> None:
        channel = resolve_channel(self.bot, self.cfg.status_channel, self.cfg.guild_id)
        if channel is None:
            return
        try:
            embed = await self.build_status_embed()
        except Exception:  # noqa: BLE001
            log.exception("status board build failed")
            return
        if self._msg is not None:
            try:
                await self._msg.edit(embed=embed)
                return
            except discord.HTTPException:
                self._msg = None
        mid = self.ctx.records.message_id(STATUS_MESSAGE_KEY)
        if mid:
            try:
                self._msg = await channel.fetch_message(mid)
                await self._msg.edit(embed=embed)
                return
            except discord.HTTPException:
                self._msg = None
        try:
            self._msg = await channel.send(embed=embed)
        except discord.Forbidden:
            log.error("cannot post the status board in #%s", getattr(channel, "name", channel))
            return
        self.ctx.records.set_message_id(STATUS_MESSAGE_KEY, self._msg.id)

    @status_board.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Any) -> None:
    await bot.add_cog(BoardCog(bot))

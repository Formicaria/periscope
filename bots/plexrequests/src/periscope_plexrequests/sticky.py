"""Sticky button embeds: one message per channel that is edited in place on start-up and re-posted whenever
something newer was posted below it, so the buttons always sit at the bottom of their channel."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from .records import Records

log = logging.getLogger(__name__)


class Sticky:
    def __init__(self, records: Records):
        self.records = records
        self._lock = asyncio.Lock()

    async def ensure(self, channel: Any, key: str, embed: discord.Embed, view: discord.ui.View) -> None:
        """Start-up: refresh the remembered message (text + fresh view), or post it if it is gone."""
        mid = self.records.message_id(key)
        if mid:
            try:
                msg = await channel.fetch_message(mid)
                await msg.edit(embed=embed, view=view)
                return
            except (discord.NotFound, discord.Forbidden):
                pass
            except discord.HTTPException as e:
                log.warning("sticky %s: edit failed (%s) — re-posting", key, e)
        try:
            msg = await channel.send(embed=embed, view=view)
        except discord.Forbidden:
            log.error("Cannot post the %s embed in #%s", key, getattr(channel, "name", channel))
            return
        self.records.set_message_id(key, msg.id)
        log.info("posted %s in #%s", key, getattr(channel, "name", channel))

    async def restick(self, channel: Any, key: str, embed: discord.Embed, view: discord.ui.View) -> None:
        """Keep the button embed at the bottom: if anything was posted after it, delete it and re-post it."""
        async with self._lock:
            old_id = self.records.message_id(key)
            if old_id and getattr(channel, "last_message_id", None) == old_id:
                return
            if old_id:
                try:
                    old = await channel.fetch_message(old_id)
                    await old.delete()
                except discord.HTTPException:
                    pass
            try:
                msg = await channel.send(embed=embed, view=view)
            except discord.Forbidden:
                log.error("Cannot re-post the %s embed in #%s", key, getattr(channel, "name", channel))
                return
            self.records.set_message_id(key, msg.id)

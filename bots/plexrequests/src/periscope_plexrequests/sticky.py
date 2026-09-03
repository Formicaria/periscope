"""Sticky button embeds: one message per channel that is edited in place on start-up and re-posted whenever
something newer was posted below it, so the buttons always sit at the bottom of their channel. There is never
more than one: start-up adopts an earlier copy the bot posted (lost state) instead of posting again, and deletes
any other copies it finds in the channel's recent history."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from .records import Records

log = logging.getLogger(__name__)

HISTORY_LIMIT = 100


class Sticky:
    def __init__(self, records: Records, me: Any = None):
        self.records = records
        self._me = me   # callable → the bot user, so earlier copies are recognised by author
        self._lock = asyncio.Lock()

    def _mine(self, message: Any) -> bool:
        me = self._me() if callable(self._me) else self._me
        author = getattr(message, "author", None)
        return bool(me and author and getattr(author, "id", None) == getattr(me, "id", None))

    async def _copies(self, channel: Any, embed: discord.Embed) -> list[Any]:
        """Earlier copies of this embed the bot posted in the channel (same title), newest first."""
        out = []
        try:
            async for m in channel.history(limit=HISTORY_LIMIT):
                if self._mine(m) and any(getattr(e, "title", None) == embed.title for e in (m.embeds or [])):
                    out.append(m)
        except (discord.HTTPException, AttributeError, TypeError):
            pass
        return out

    async def ensure(self, channel: Any, key: str, embed: discord.Embed, view: discord.ui.View) -> None:
        """Start-up: refresh the remembered message (text + fresh view); adopt an earlier copy if the memory is
        gone; post only when there is none. Stray copies are deleted either way."""
        keep = None
        mid = self.records.message_id(key)
        if mid:
            try:
                keep = await channel.fetch_message(mid)
                await keep.edit(embed=embed, view=view)
            except (discord.NotFound, discord.Forbidden):
                keep = None
            except discord.HTTPException as e:
                log.warning("sticky %s: edit failed (%s) — keeping it", key, e)
        copies = await self._copies(channel, embed)
        if keep is None and copies:
            keep = copies[0]
            try:
                await keep.edit(embed=embed, view=view)
            except discord.HTTPException as e:
                log.warning("sticky %s: could not refresh the adopted message (%s)", key, e)
            self.records.set_message_id(key, keep.id)
            log.info("adopted the earlier %s message in #%s", key, getattr(channel, "name", channel))
        if keep is None:
            try:
                keep = await channel.send(embed=embed, view=view)
            except discord.Forbidden:
                log.error("Cannot post the %s embed in #%s", key, getattr(channel, "name", channel))
                return
            self.records.set_message_id(key, keep.id)
            log.info("posted %s in #%s", key, getattr(channel, "name", channel))
        for m in copies:
            if m.id != keep.id:
                try:
                    await m.delete()
                    log.info("deleted a stray %s copy in #%s", key, getattr(channel, "name", channel))
                except discord.HTTPException:
                    pass

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

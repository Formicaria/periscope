"""A single 'live' message per channel that the bot edits in place on a schedule."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .bot import LabBot

log = logging.getLogger(__name__)


class StatusBoard:
    def __init__(self, bot: "LabBot", key: str, channel_id: int | None = None):
        self.bot = bot
        self.key = key
        self.channel_id = channel_id or bot.settings.status_channel_id
        self._state = bot.state.namespace(f"board:{key}")

    async def _fetch(self) -> tuple[discord.abc.Messageable | None, discord.Message | None]:
        if not self.channel_id:
            return None, None
        ch = await self.bot.get_channel_safe(self.channel_id)
        if ch is None:
            return None, None
        mid = self._state.get("message_id")
        if mid:
            try:
                return ch, await ch.fetch_message(mid)  # type: ignore[attr-defined]
            except discord.NotFound:
                self._state.pop("message_id")
        return ch, None

    async def render(self, embed: discord.Embed | None = None, *, embeds: list[discord.Embed] | None = None,
                     view: discord.ui.View | None = None, pin: bool = True) -> discord.Message | None:
        ch, msg = await self._fetch()
        if ch is None:
            log.warning("StatusBoard[%s]: no channel configured", self.key)
            return None
        kwargs = {"embeds": embeds} if embeds is not None else {"embed": embed}
        if msg is None:
            msg = await ch.send(view=view, **kwargs)
            self._state.set("message_id", msg.id)
            if pin:
                try:
                    await msg.pin(reason="lab status board")
                except discord.HTTPException:
                    pass
        else:
            await msg.edit(view=view, **kwargs)
        return msg

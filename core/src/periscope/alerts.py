"""Alert routing with dedupe, cooldown, and resolve-in-place editing."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from .embeds import Severity, lab_embed, truncate

if TYPE_CHECKING:
    from .bot import LabBot

log = logging.getLogger(__name__)


@dataclass
class Alert:
    fingerprint: str
    title: str
    description: str = ""
    severity: Severity = Severity.WARNING
    fields: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    mention: bool | None = None  # None → mention only on CRITICAL

    def to_embed(self, lab_name: str, resolved: bool = False) -> discord.Embed:
        sev = Severity.OK if resolved else self.severity
        title = f"RESOLVED: {self.title}" if resolved else self.title
        e = lab_embed(title, truncate(self.description, 4000) or None, severity=sev, lab_name=lab_name, url=self.url)
        for k, v in list(self.fields.items())[:25]:
            e.add_field(name=truncate(str(k), 256), value=truncate(str(v) or "—", 1024), inline=True)
        return e


class AlertRouter:
    """Sends alerts to the configured alert channel, deduplicated by fingerprint.

    - Repeat alerts with the same fingerprint inside `cooldown_s` are dropped.
    - `resolve(fingerprint)` edits the original message to green and clears state.
    - Message ids persist in bot state so restarts don't orphan alerts.
    """

    def __init__(self, bot: "LabBot", cooldown_s: int = 300):
        self.bot = bot
        self.cooldown_s = cooldown_s
        self._state = bot.state.namespace("alerts")
        self._last_sent: dict[str, float] = {}

    async def _channel(self) -> discord.abc.Messageable | None:
        cid = self.bot.settings.alert_channel_id
        if not cid:
            log.warning("ALERT_CHANNEL_ID not set; dropping alert")
            return None
        return await self.bot.get_channel_safe(cid)

    async def fire(self, alert: Alert, force: bool = False) -> discord.Message | None:
        now = time.time()
        last = self._last_sent.get(alert.fingerprint, 0)
        if not force and now - last < self.cooldown_s:
            log.debug("alert %s suppressed (cooldown)", alert.fingerprint)
            return None
        ch = await self._channel()
        if ch is None:
            return None

        mention = alert.mention if alert.mention is not None else alert.severity is Severity.CRITICAL
        content = None
        if mention and self.bot.settings.alert_role_id:
            content = f"<@&{self.bot.settings.alert_role_id}>"

        existing = self._state.get(alert.fingerprint)
        embed = alert.to_embed(self.bot.settings.lab_name)
        msg: discord.Message | None = None
        if existing:
            try:
                msg = await ch.fetch_message(existing["message_id"])  # type: ignore[attr-defined]
                await msg.edit(embed=embed)
            except discord.NotFound:
                msg = None
        if msg is None:
            msg = await ch.send(content=content, embed=embed,
                                allowed_mentions=discord.AllowedMentions(roles=True))
            self._state.set(alert.fingerprint, {"message_id": msg.id, "channel_id": msg.channel.id, "ts": now})
        self._last_sent[alert.fingerprint] = now
        return msg

    async def resolve(self, fingerprint: str, note: str | None = None) -> bool:
        self._last_sent.pop(fingerprint, None)
        if self._state.get(fingerprint) is None:
            return False  # nothing to resolve; avoid a state write
        existing = self._state.pop(fingerprint)
        ch = await self.bot.get_channel_safe(existing["channel_id"])
        if ch is None:
            return False
        try:
            msg = await ch.fetch_message(existing["message_id"])  # type: ignore[attr-defined]
        except discord.NotFound:
            return False
        if not msg.embeds:
            return False
        old = msg.embeds[0]
        e = discord.Embed.from_dict(old.to_dict())
        e.color = Severity.OK.color
        e.title = f"🟢 RESOLVED: {old.title.split(' ', 1)[-1] if old.title else ''}".strip()
        if note:
            e.add_field(name="Resolution", value=truncate(note, 1024), inline=False)
        await msg.edit(content=None, embed=e)
        return True

    def active(self) -> list[str]:
        # `bot.state` is the root JsonState for a v1 LabBot but a NamespacedState for a v2 ServiceBot;
        # the alerts namespace always hangs off the root, so walk to it from there.
        prefix = self._state._prefix
        return [k[len(prefix):] for k in self._state._p._data if k.startswith(prefix)]

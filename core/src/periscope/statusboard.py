"""A single 'live' message per channel that the bot edits in place on a schedule.

There is exactly one board message per (bot, channel, key). The message id is remembered in state; when that
is missing or stale (fresh install, migrated state, a deleted message) the board does not simply post another
copy — it looks for its own previous message in the channel (pinned messages first, then recent history),
adopts the newest one and deletes the rest, and only posts when there is nothing to adopt. Every board embed
carries a footer marker (`… · <key> board`) so a board finds itself deterministically; boards posted before
the marker existed are matched by the stem of their title.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

import discord

if TYPE_CHECKING:
    from .bot import LabBot

log = logging.getLogger(__name__)

MARK_RE = re.compile(r"·\s*(?P<key>[a-z0-9_-]+) board\s*$", re.IGNORECASE)
_SEV_PREFIX = re.compile(r"^[\U0001F534\U0001F7E1\U0001F7E2\U0001F535⚪⚫\U0001F7E0\U0001F7E3]\s*")
_STEM_SPLIT = re.compile(r"\s+(?:·|—|–|-|:)\s+|:\s*")
HISTORY_LIMIT = 100


def marker(key: str) -> str:
    return f"{key} board"


def stamp(embed: discord.Embed, key: str) -> discord.Embed:
    """Append the board marker to the embed footer (idempotent)."""
    text = (embed.footer.text or "") if embed.footer else ""
    if MARK_RE.search(text):
        text = MARK_RE.sub(f"· {marker(key)}", text)
    else:
        text = f"{text} · {marker(key)}" if text else marker(key)
    embed.set_footer(text=text[:2048], icon_url=embed.footer.icon_url if embed.footer else None)
    return embed


def board_key_of(message: Any) -> str | None:
    """The board key a message carries in its footer marker, if any."""
    for e in getattr(message, "embeds", None) or []:
        footer = getattr(e, "footer", None)
        text = getattr(footer, "text", None) or ""
        m = MARK_RE.search(text)
        if m:
            return m.group("key").lower()
    return None


def title_stem(title: str | None) -> str:
    """'🟢 Proxmox · homelab' → 'proxmox'; 'UniFi — default' → 'unifi'; 'GitHub: Org' → 'github'."""
    if not title:
        return ""
    t = _SEV_PREFIX.sub("", title.strip())
    return _STEM_SPLIT.split(t, 1)[0].strip().lower()


def _first_title(embeds: list[discord.Embed] | None) -> str | None:
    for e in embeds or []:
        if getattr(e, "title", None):
            return e.title
    return None


class StatusBoard:
    def __init__(self, bot: "LabBot", key: str, channel_id: int | None = None):
        self.bot = bot
        self.key = key
        self.channel_id = channel_id or bot.settings.status_channel_id
        self._state = bot.state.namespace(f"board:{key}")
        self._scanned = False  # the adoption scan runs once per process, on the first render

    # ----- lookup ------------------------------------------------------------------------------------
    async def _fetch(self) -> tuple[Any, discord.Message | None]:
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
            except discord.Forbidden:
                log.error("StatusBoard[%s]: cannot read message %s in channel %s (missing Read Message History?)",
                          self.key, mid, self.channel_id)
                return ch, None
        return ch, None

    def _mine(self, message: Any) -> bool:
        me = getattr(self.bot, "user", None)
        author = getattr(message, "author", None)
        return bool(me and author and getattr(author, "id", None) == getattr(me, "id", None))

    def _is_this_board(self, message: Any, stem: str) -> bool:
        key = board_key_of(message)
        if key is not None:
            return key == self.key.lower()
        # a board posted before the marker existed: same bot, same title stem, and it looks like a board
        title = _first_title(getattr(message, "embeds", None) or [])
        return bool(stem) and title_stem(title) == stem

    async def _candidates(self, ch: Any, stem: str) -> list[Any]:
        """This board's earlier messages in the channel: pinned ones first, then recent history; newest first."""
        seen: dict[int, Any] = {}
        try:
            pins = ch.pins()
            if hasattr(pins, "__aiter__"):            # discord.py ≥ 2.6: async iterator
                pinned = [m async for m in pins]
            else:                                      # older: coroutine → list
                pinned = await pins
            for m in pinned:
                if self._mine(m) and self._is_this_board(m, stem):
                    seen[m.id] = m
        except (discord.HTTPException, AttributeError, TypeError) as e:
            log.debug("StatusBoard[%s]: pins unavailable (%s)", self.key, e)
        try:
            async for m in ch.history(limit=HISTORY_LIMIT):
                if m.id not in seen and self._mine(m) and self._is_this_board(m, stem):
                    seen[m.id] = m
        except (discord.HTTPException, AttributeError, TypeError) as e:
            log.debug("StatusBoard[%s]: history unavailable (%s)", self.key, e)
        return sorted(seen.values(), key=lambda m: m.id, reverse=True)

    async def adopt(self, ch: Any, embeds: list[discord.Embed], keep: Any = None) -> discord.Message | None:
        """Keep exactly one copy of this board in the channel: the remembered message when there is one, else
        the newest earlier copy; every other copy is deleted. Returns the message to edit (None = post one)."""
        stem = title_stem(_first_title(embeds))
        found = await self._candidates(ch, stem)
        if keep is None:
            if not found:
                return None
            keep = found[0]
            self._state.set("message_id", keep.id)
            log.info("StatusBoard[%s]: adopted existing message %s in channel %s", self.key, keep.id, self.channel_id)
        for m in found:
            if m.id == keep.id:
                continue
            try:
                await m.delete()
                log.info("StatusBoard[%s]: deleted a stale copy (%s) in channel %s", self.key, m.id, self.channel_id)
            except discord.HTTPException as e:
                log.warning("StatusBoard[%s]: could not delete stale copy %s: %s", self.key, m.id, e)
        return keep

    # ----- render ------------------------------------------------------------------------------------
    async def render(self, embed: discord.Embed | None = None, *, embeds: list[discord.Embed] | None = None,
                     view: discord.ui.View | None = None, pin: bool = True) -> discord.Message | None:
        ch, msg = await self._fetch()
        if ch is None:
            log.warning("StatusBoard[%s]: no channel configured", self.key)
            return None
        all_embeds = list(embeds) if embeds is not None else ([embed] if embed is not None else [])
        if all_embeds:
            stamp(all_embeds[0], self.key)
        kwargs = {"embeds": all_embeds} if embeds is not None else {"embed": embed}
        if not self._scanned:
            # once per process: reuse an earlier copy when the remembered one is gone, and sweep stale copies
            self._scanned = True
            msg = await self.adopt(ch, all_embeds, keep=msg)
        if msg is None:
            msg = await ch.send(view=view, **kwargs)
            self._state.set("message_id", msg.id)
            log.info("StatusBoard[%s]: posted a new board (%s) in channel %s", self.key, msg.id, self.channel_id)
            if pin:
                try:
                    await msg.pin(reason="lab status board")
                except discord.HTTPException:
                    pass
            return msg
        try:
            await msg.edit(view=view, **kwargs)
        except discord.NotFound:
            # deleted between fetch and edit: post once, never loop
            self._state.pop("message_id")
            msg = await ch.send(view=view, **kwargs)
            self._state.set("message_id", msg.id)
            if pin:
                try:
                    await msg.pin(reason="lab status board")
                except discord.HTTPException:
                    pass
        if pin and not getattr(msg, "pinned", True):
            try:
                await msg.pin(reason="lab status board")
            except discord.HTTPException:
                pass
        return msg

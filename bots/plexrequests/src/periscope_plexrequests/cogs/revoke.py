"""Auto-revoke (AUTO_REVOKE=1): pull the Plex share, or cancel the pending invite, when a member leaves the
server or loses ROLE_NAME. Needs the Server Members intent, which the service declares."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from discord.ext import commands

from ..context import PlexRequests

log = logging.getLogger(__name__)


def lost_role(before_roles: list[Any], after_roles: list[Any], role_name: str) -> bool:
    had = discord.utils.get(before_roles, name=role_name) is not None
    has = discord.utils.get(after_roles, name=role_name) is not None
    return had and not has


class RevokeCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot
        self.ctx: PlexRequests = bot.plexreq
        self.cfg = self.ctx.cfg

    async def cog_load(self) -> None:
        if self.cfg.auto_revoke:
            log.info("[%s] auto-revoke armed: leaving the server or losing %r pulls the Plex share",
                     self.bot.name, self.cfg.role_name)

    def _ours(self, member: Any) -> bool:
        guild = getattr(member, "guild", None)
        return bool(self.cfg.auto_revoke) and (not self.cfg.guild_id or getattr(guild, "id", None) == self.cfg.guild_id)

    async def revoke_member(self, member: Any, reason: str) -> bool:
        email = self.ctx.records.email_for(member.id)
        if not email:
            return False
        try:
            ok = await asyncio.to_thread(self.ctx.plex.revoke, email)
        except Exception as ex:  # noqa: BLE001
            log.warning("revoke failed for %s: %s", member, ex)
            return False
        log.info("revoke: %s (%s) %s -> %s", member, member.id, reason, "removed" if ok else "no plex share found")
        if ok:
            self.ctx.stats.bump("revoked", member)
            self.ctx.records.forget_email(member.id)
        return ok

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if self._ours(member):
            await self.revoke_member(member, "left the server")

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        if not self._ours(after) or not self.cfg.role_name:
            return
        if lost_role(list(before.roles), list(after.roles), self.cfg.role_name):
            await self.revoke_member(after, f"lost the {self.cfg.role_name} role")


async def setup(bot: Any) -> None:
    await bot.add_cog(RevokeCog(bot))

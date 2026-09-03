"""A presence is one Discord identity (bot token) hosting any number of services in the same process."""

from __future__ import annotations

import logging
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from .service import ServiceBot

log = logging.getLogger(__name__)


def build_intents(names: Iterable[str]) -> discord.Intents:
    """discord.Intents.default() plus every flag named in `names` (the union of what the presence's services
    declared in ServiceSpec.intents). Unknown names are logged and ignored rather than sinking the presence."""
    intents = discord.Intents.default()
    for name in sorted(set(names)):
        if name in discord.Intents.VALID_FLAGS:
            setattr(intents, name, True)
        else:
            log.warning("unknown gateway intent %r requested by a service — ignored", name)
    return intents


class Presence(commands.Bot):
    def __init__(self, name: str, token: str, *, guild_id: int | None, admin_role_ids: list[int], lab_name: str,
                 intents: discord.Intents | None = None):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents or discord.Intents.default(),
                         help_command=None, description=f"periscope presence {name}")
        self.name = name
        self.token = token
        self.guild_id = guild_id
        self.admin_role_ids = admin_role_ids
        self.lab_name = lab_name
        self.services: list[ServiceBot] = []
        self.connected = False
        self.build_errors: dict[str, str] = {}

    # ----- lifecycle -------------------------------------------------------------------------
    async def setup_hook(self) -> None:
        for sb in list(self.services):
            try:
                await sb.spec.build(sb)
                log.info("[%s] service %s built", self.name, sb.name)
            except Exception as e:  # noqa: BLE001 - one broken service must not sink the presence
                sb.healthy, sb.last_error = False, f"{type(e).__name__}: {e}"
                self.build_errors[sb.name] = sb.last_error
                log.exception("[%s] service %s failed to build", self.name, sb.name)
                await sb.unload()
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("[%s] synced %d app commands to guild %s", self.name, len(synced), self.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("[%s] synced %d global app commands", self.name, len(synced))

    async def on_ready(self) -> None:
        self.connected = True
        names = ", ".join(sb.name for sb in self.services) or "no services"
        log.info("[%s] ready as %s (%s)", self.name, self.user, names)
        try:
            await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=self.lab_name))
        except Exception:  # noqa: BLE001
            pass

    async def on_disconnect(self) -> None:
        self.connected = False

    async def on_resumed(self) -> None:
        self.connected = True

    # ----- helpers ---------------------------------------------------------------------------
    async def get_channel_safe(self, channel_id: int) -> discord.abc.Messageable | None:
        ch = self.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                log.error("[%s] channel %s not found or forbidden", self.name, channel_id)
                return None
        return ch  # type: ignore[return-value]

    def is_admin(self, user: discord.abc.User | discord.Member) -> bool:
        if not self.admin_role_ids:
            perms = getattr(user, "guild_permissions", None)
            return bool(perms and perms.administrator)
        roles = getattr(user, "roles", [])
        return any(r.id in self.admin_role_ids for r in roles)

    def service(self, name: str) -> ServiceBot | None:
        return next((s for s in self.services if s.name == name), None)


def admin_only():
    """app_commands check usable by v2 services (interaction.client is the Presence)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        client = interaction.client
        ok = client.is_admin(interaction.user) if hasattr(client, "is_admin") else False
        if ok:
            return True
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return False

    return app_commands.check(predicate)

"""Everything the cogs share: settings, the Plex gateway, the request backend, state accessors, counters,
sticky embeds, rate-limit buckets and the `/requests` slash group (plus where it is registered)."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

import discord
from discord import app_commands

from .backend import RequestBackend
from .config import PlexRequestsSettings
from .plex import PlexGateway
from .records import Records
from .stats import Stats
from .sticky import Sticky

log = logging.getLogger(__name__)

WATCH_MAX_AGE = 30 * 86400     # stop polling a request after 30 days


def slash(name: str, description: str, callback: Callable[..., Awaitable[None]]) -> app_commands.Command:
    """Wrap a bound cog method into an app command (keeps @describe / check metadata)."""
    return app_commands.Command(name=name, description=description, callback=callback)  # type: ignore[arg-type]


def plex_admin_only():
    """Admin check that works in the Plex server too: a server administrator there, or a lab admin."""

    async def predicate(interaction: discord.Interaction) -> bool:
        perms = getattr(interaction.user, "guild_permissions", None)
        client = interaction.client
        ok = bool(perms and perms.administrator) or (client.is_admin(interaction.user) if hasattr(client, "is_admin") else False)
        if ok:
            return True
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return False

    return app_commands.check(predicate)


class PlexRequests:
    """Lives on the ServiceBot as `bot.plexreq`."""

    def __init__(self, bot: Any, cfg: PlexRequestsSettings, plex: PlexGateway, backend: RequestBackend):
        self.bot = bot
        self.cfg = cfg
        self.plex = plex
        self.backend = backend
        self.records = Records(bot.state)
        self.stats = Stats(bot.state)
        self.sticky = Sticky(self.records, me=lambda: getattr(bot, "user", None))
        self.invite_cd: dict[int, list[float]] = {}
        self.request_cd: dict[int, list[float]] = {}
        self.group = app_commands.Group(name="requests", description="Plex media requests")
        # The invite/request channels may live in a different Discord server than the lab. When they do, this
        # service's commands are registered for that server only (the presence syncs the lab's guild itself).
        pres_guild = getattr(bot.presence, "guild_id", None)
        self.command_guild: discord.Object | None = (
            discord.Object(id=cfg.guild_id) if cfg.guild_id and cfg.guild_id != pres_guild else None)
        self.synced = False
        self.persistent_views: list[discord.ui.View] = []

    # ----- slash commands -----
    def add_top_level(self, cmd: app_commands.Command) -> None:
        self.bot.tree.add_command(cmd, guild=self.command_guild, override=True)

    def remove_top_level(self, name: str) -> None:
        self.bot.tree.remove_command(name, guild=self.command_guild)

    def register(self, *cmds: app_commands.Command) -> None:
        for cmd in cmds:
            self.group.add_command(cmd, override=True)

    def unregister(self, *names: str) -> None:
        for n in names:
            self.group.remove_command(n)

    async def sync_commands(self) -> None:
        """Push this service's commands to its own guild (once). No-op when they are global — the presence
        syncs those with the lab guild."""
        if self.synced or self.command_guild is None:
            return
        self.synced = True
        try:
            synced = await self.bot.tree.sync(guild=self.command_guild)
            log.info("[%s] synced %d app commands to guild %s", self.bot.name, len(synced), self.command_guild.id)
        except Exception as e:  # noqa: BLE001
            self.synced = False
            log.warning("[%s] command sync for guild %s failed: %s", self.bot.name, self.command_guild.id, e)

    # ----- views -----
    def add_persistent_view(self, view: discord.ui.View) -> None:
        self.bot.add_view(view)
        self.persistent_views.append(view)

    # ----- channels -----
    def guild(self) -> discord.Guild | None:
        if self.cfg.guild_id:
            return self.bot.get_guild(self.cfg.guild_id)
        return next(iter(self.bot.guilds), None)

    def is_invite_channel(self, channel: Any) -> bool:
        if self.cfg.channel_id:
            return getattr(channel, "id", None) == self.cfg.channel_id
        return getattr(channel, "name", None) == self.cfg.channel_name

    def is_requests_channel(self, channel: Any) -> bool:
        return bool(self.cfg.requests_channel_id) and getattr(channel, "id", None) == self.cfg.requests_channel_id

    def invite_channel(self) -> Any:
        ch = self.bot.get_channel(self.cfg.channel_id) if self.cfg.channel_id else None
        if ch is None:
            for g in self.bot.guilds:
                ch = discord.utils.get(g.text_channels, name=self.cfg.channel_name)
                if ch:
                    break
        return ch

    def requests_channel(self) -> Any:
        return self.bot.get_channel(self.cfg.requests_channel_id) if self.cfg.requests_channel_id else None

    async def close(self) -> None:
        await self.backend.close()

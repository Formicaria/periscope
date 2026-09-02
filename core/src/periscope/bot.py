"""LabBot: discord.py Bot subclass with lab identity, state, alerts, webhook server, and admin checks."""

from __future__ import annotations

import logging
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from .alerts import AlertRouter
from .config import Settings
from .logging import setup_logging
from .state import JsonState
from .webhook import WebhookServer

log = logging.getLogger(__name__)


class LabBot(commands.Bot):
    """
    Subclass or instantiate directly:

        bot = LabBot(settings, cogs=["mybot.cogs.status"], webhook=True)
        bot.run_forever()
    """

    def __init__(
        self,
        settings: Settings,
        *,
        cogs: Iterable[str] = (),
        webhook: bool = False,
        intents: discord.Intents | None = None,
        description: str = "",
    ):
        setup_logging(settings.log_level)
        intents = intents or discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, description=description,
                         help_command=None)
        self.settings = settings
        self.lab_name = settings.lab_name
        self.state = JsonState(settings.data_dir / "state.json")
        self.alerts = AlertRouter(self)
        self._cog_paths = list(cogs)
        self.webhook: WebhookServer | None = (
            WebhookServer(settings.webhook_host, settings.webhook_port, settings.webhook_secret) if webhook else None
        )
        self._connected = False

    # ----- lifecycle -------------------------------------------------------

    async def setup_hook(self) -> None:
        for path in self._cog_paths:
            await self.load_extension(path)
            log.info("loaded cog %s", path)
        if self.webhook:
            self.webhook.set_health_check(lambda: self._connected)
            await self.webhook.start()
        if self.settings.guild_id:
            guild = discord.Object(id=self.settings.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("synced %d app commands to guild %s", len(synced), self.settings.guild_id)
        else:
            synced = await self.tree.sync()
            log.info("synced %d global app commands (may take up to an hour to appear)", len(synced))

    async def on_ready(self) -> None:
        self._connected = True
        log.info("ready as %s (lab=%s)", self.user, self.lab_name)
        await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching,
                                                             name=f"{self.lab_name}"))

    async def on_disconnect(self) -> None:
        self._connected = False

    async def close(self) -> None:
        if self.webhook:
            await self.webhook.stop()
        await super().close()

    def run_forever(self) -> None:
        self.run(self.settings.discord_token, log_handler=None)

    # ----- helpers ---------------------------------------------------------

    async def get_channel_safe(self, channel_id: int) -> discord.abc.Messageable | None:
        ch = self.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                log.error("channel %s not found or forbidden", channel_id)
                return None
        return ch  # type: ignore[return-value]

    def is_admin(self, user: discord.abc.User | discord.Member) -> bool:
        if not self.settings.admin_role_ids:
            perms = getattr(user, "guild_permissions", None)
            return bool(perms and perms.administrator)
        roles = getattr(user, "roles", [])
        return any(r.id in self.settings.admin_role_ids for r in roles)


def admin_only():
    """app_commands check: user must be in ADMIN_ROLE_IDS (or a server admin if unset)."""

    async def predicate(interaction: discord.Interaction) -> bool:
        bot: LabBot = interaction.client  # type: ignore[assignment]
        if bot.is_admin(interaction.user):
            return True
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return False

    return app_commands.check(predicate)

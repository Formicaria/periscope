"""A presence is one Discord identity (bot token) hosting any number of services in the same process."""

from __future__ import annotations

import logging
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from .service import ServiceBot

log = logging.getLogger(__name__)

# bot + applications.commands, permissions: view/send/embed/attach/history/manage messages + channels + roles
INVITE_PERMISSIONS = 268659728
PORTAL = "https://discord.com/developers/applications"


def invite_url(app_id: int | str | None) -> str | None:
    if not app_id:
        return None
    return f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot%20applications.commands&permissions={INVITE_PERMISSIONS}"


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
        self.last_error: str | None = None      # plain-language reason the presence is not connected
        self.missing_guilds: dict[int, str] = {}  # server id → who needs it, when the bot is not a member
        self._synced = False

    # ----- identity ------------------------------------------------------------------------------
    @property
    def app_id(self) -> int | None:
        return self.application_id or (self.user.id if self.user else None)

    def invite_url(self) -> str | None:
        return invite_url(self.app_id)

    def portal_url(self) -> str:
        return f"{PORTAL}/{self.app_id}/bot" if self.app_id else PORTAL

    # ----- lifecycle -------------------------------------------------------------------------
    async def setup_hook(self) -> None:
        # runs inside login(); a failed start retries login(), so build each service only once
        for sb in list(self.services):
            if sb.built:
                continue
            try:
                await sb.spec.build(sb)
                sb.built = True
                log.info("[%s] service %s built", self.name, sb.name)
            except Exception as e:  # noqa: BLE001 - one broken service must not sink the presence
                sb.healthy, sb.last_error = False, f"{type(e).__name__}: {e}"
                self.build_errors[sb.name] = sb.last_error
                log.exception("[%s] service %s failed to build", self.name, sb.name)
                await sb.unload()
        if not self._synced:
            await self.sync_commands()

    def wanted_guilds(self) -> dict[int, str]:
        """Servers this presence must serve: each healthy service's own (usually the lab's), by who needs it."""
        wanted: dict[int, str] = {}
        for sb in self.services:
            if sb.healthy and sb.guild_id:
                wanted.setdefault(sb.guild_id, sb.name)
        if not wanted and self.guild_id:
            wanted[self.guild_id] = "lab"
        return wanted

    async def sync_commands(self) -> None:
        """Register slash commands on every server the presence serves — but only on servers the bot is
        actually in: syncing to a server it was never invited to is a 403 that used to take the whole
        presence down. Servers it is missing from are recorded (and logged with the invite link) instead."""
        wanted = self.wanted_guilds()
        if not wanted:
            synced = await self.tree.sync()
            log.info("[%s] synced %d global app commands", self.name, len(synced))
            self._synced = True
            return
        member_of: set[int] | None = None
        try:
            member_of = {g.id async for g in self.fetch_guilds(limit=200)}
        except discord.HTTPException as e:
            log.warning("[%s] could not list the bot's servers (%s) — trying each one", self.name, e)
        self.missing_guilds = {}
        for gid, who in wanted.items():
            if member_of is not None and gid not in member_of:
                self.missing_guilds[gid] = who
                log.error("[%s] the bot is not in server %s (needed by %s) — invite it: %s", self.name, gid, who, self.invite_url())
                continue
            guild = discord.Object(id=gid)
            self.tree.copy_global_to(guild=guild)
            try:
                synced = await self.tree.sync(guild=guild)
                log.info("[%s] synced %d app commands to server %s", self.name, len(synced), gid)
            except discord.Forbidden:
                self.missing_guilds[gid] = who
                log.error("[%s] server %s refused the slash commands — re-invite the bot with the applications.commands scope: %s",
                          self.name, gid, self.invite_url())
            except discord.HTTPException as e:
                log.error("[%s] slash-command sync to server %s failed: %s", self.name, gid, e)
        self._synced = True

    async def on_ready(self) -> None:
        self.connected = True
        self.last_error = None
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


def explain_presence_error(pres: Presence, e: BaseException) -> str:
    """Turn the exceptions a bot start can raise into one sentence that says what to do."""
    if isinstance(e, discord.LoginFailure):
        return f"Discord rejected the token of bot '{pres.name}' — paste a new token on the Bots page"
    if isinstance(e, discord.PrivilegedIntentsRequired):
        return (f"bot '{pres.name}' needs privileged intents switched on: Developer Portal → Bot → enable "
                f"Server Members Intent and Message Content Intent ({pres.portal_url()}), then restart")
    if isinstance(e, discord.Forbidden):
        return (f"Discord refused bot '{pres.name}' ({e.status} {e.text}) — it is probably not in the server; "
                f"invite it: {pres.invite_url() or 'set a token first'}")
    if isinstance(e, discord.HTTPException):
        return f"Discord API error for bot '{pres.name}': {e.status} {e.text}"
    if isinstance(e, (ConnectionError, OSError)):
        return f"cannot reach Discord from this box ({e}) — check DNS / network"
    return f"{type(e).__name__}: {e}"


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

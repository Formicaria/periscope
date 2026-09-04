"""Plex library invites: the Get Plex Access button + modal, emails typed into the invite channel (deleted,
result by DM), and the top-level `/plexinvite` command. Successful invitees get ROLE_NAME."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord
from periscope.hooks import NullHistory
from discord import app_commands
from discord.ext import commands

from ..common import INVITE_KIND, RESULT_PREFIX, check_cooldown, find_email, is_member, sticky_embed, valid_email
from ..context import PlexRequests, slash

log = logging.getLogger(__name__)
# a bot assembled by hand (a test, a bare install) has no event log; recording is never worth a crash
NO_LOG = NullHistory()

INVITE_CUSTOM_ID = "plexrequests:invite"
LEGACY_INVITE_CUSTOM_ID = "ztplex:invite"      # buttons on embeds posted by the standalone bot keep working
INVITE_MESSAGE_KEY = "invite_message_id"


class EmailModal(discord.ui.Modal, title="Get Plex Access"):
    email = discord.ui.TextInput(label="Your Plex account email", placeholder="you@example.com", max_length=120)

    def __init__(self, cog: "InvitesCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, text = await self.cog.run_invite(interaction.user, str(self.email.value))
        await interaction.followup.send(text, ephemeral=True)


class InviteView(discord.ui.View):
    """Persistent 'Get Plex Access' button."""

    def __init__(self, cog: "InvitesCog", custom_id: str = INVITE_CUSTOM_ID):
        super().__init__(timeout=None)
        self.cog = cog
        button: discord.ui.Button = discord.ui.Button(label="Get Plex Access", style=discord.ButtonStyle.success,
                                                      emoji="🎟️", custom_id=custom_id)
        button.callback = self.on_click  # type: ignore[method-assign]
        self.add_item(button)

    async def on_click(self, interaction: discord.Interaction) -> None:
        self.cog.ctx.stats.bump("invite_button", interaction.user)
        await interaction.response.send_modal(EmailModal(self.cog))


class InvitesCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot
        self.history = getattr(bot, "history", NO_LOG)   # a no-op when this bot has none
        self.ctx: PlexRequests = bot.plexreq
        self.cfg = self.ctx.cfg
        self._ready_once = False

    async def cog_load(self) -> None:
        self.ctx.add_persistent_view(InviteView(self))
        self.ctx.add_persistent_view(InviteView(self, custom_id=LEGACY_INVITE_CUSTOM_ID))
        cmd = slash("plexinvite", f"Get invited to the {self.cfg.plex_name} server", self.plexinvite)
        self.ctx.add_top_level(cmd)

    async def cog_unload(self) -> None:
        self.ctx.remove_top_level("plexinvite")

    def view(self) -> InviteView:
        return InviteView(self)

    def embed(self) -> discord.Embed | None:
        """The sticky invite embed as customised on the Messages page (None = switched off)."""
        return sticky_embed(self.bot, INVITE_KIND, self.cfg)

    # ----- roles -----

    async def ensure_role(self, guild: discord.Guild) -> discord.Role | None:
        role = discord.utils.get(guild.roles, name=self.cfg.role_name)
        if role is None:
            try:
                role = await guild.create_role(name=self.cfg.role_name, colour=discord.Colour.from_str("#e5a00d"),
                                               reason="Created by periscope (Plex invites)")
                log.info("created role %r in %s", self.cfg.role_name, guild)
            except discord.Forbidden:
                log.error("no permission to create role %r", self.cfg.role_name)
                return None
        return role

    async def grant_role(self, member: Any) -> str:
        if not is_member(member):
            return ""
        role = await self.ensure_role(member.guild)
        if role is None or role in member.roles:
            return ""
        try:
            await member.add_roles(role, reason="Plex invite sent")
            return f" You've been given the **{self.cfg.role_name}** role."
        except discord.Forbidden:
            log.error("missing permission to assign %r (check role order)", self.cfg.role_name)
            return ""

    # ----- the invite flow -----

    async def run_invite(self, member: Any, email: str) -> tuple[str, str]:
        """Validate, rate-limit, share on Plex, grant the role. Returns (status, text for the user)."""
        email = (email or "").strip()
        if not valid_email(email):
            return ("error", f"{RESULT_PREFIX['error']} That doesn't look like a valid email address — try again.")
        if not check_cooldown(self.ctx.invite_cd, member.id):
            return ("error", f"{RESULT_PREFIX['error']} Too many attempts — wait a few minutes and try again.")

        status, detail = await asyncio.to_thread(self.ctx.plex.invite, email)
        log.info("invite: discord=%s (%s) -> %s", member, member.id, status)
        self.ctx.stats.bump(f"invite_{status}", member)
        self.history.record(service="plexrequests", kind="invite", key=status, server=self.bot.lab_name,
                            severity="warning" if status == "error" else "info",
                            title=f"Plex invite {status} for {getattr(member, 'display_name', member)}",
                            payload={"user_id": str(getattr(member, "id", ""))})

        role_note = ""
        if status in ("sent", "pending", "updated"):
            role_note = await self.grant_role(member)
            self.ctx.records.remember_email(member.id, email)      # for auto-revoke

        if status == "sent":
            text = (f"Invite sent to `{email}`! Open the invitation email from Plex (check spam too), "
                    f"hit **Accept**, then sign in at <https://app.plex.tv>.{role_note}")
        else:
            text = f"{detail}{role_note}"
        return (status, f"{RESULT_PREFIX[status]} {text}")

    @app_commands.describe(email="The email address of your Plex account")
    async def plexinvite(self, interaction: discord.Interaction, email: str) -> None:
        self.ctx.stats.bump("cmd_plexinvite", interaction.user)
        await interaction.response.defer(ephemeral=True, thinking=True)
        _, text = await self.run_invite(interaction.user, email)
        await interaction.followup.send(text, ephemeral=True)

    async def handle_invite_message(self, message: Any) -> None:
        email = find_email(message.content or "")
        if not email:
            return
        self.ctx.stats.bump("typed_email", message.author)
        try:
            await message.delete()
        except discord.HTTPException:
            pass

        status, text = await self.run_invite(message.author, email)

        dm_ok = True
        try:
            await message.author.send(text)
        except discord.HTTPException:
            dm_ok = False

        note = "📬 I removed your message to keep your email private."
        if status == "sent":
            summary = "Your Plex invite is on its way — check your email!"
        elif status in ("pending", "updated"):
            summary = "Check your DMs — that address is already set up or invited."
        else:
            summary = "That didn't work — check your DMs for details." if dm_ok else text
        try:
            await message.channel.send(f"{message.author.mention} {note} {summary}", delete_after=45)
        except discord.HTTPException:
            pass
        embed = self.embed()
        if embed is not None:
            await self.ctx.sticky.restick(message.channel, INVITE_MESSAGE_KEY, embed, self.view())

    # ----- events -----

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        if self.ctx.is_invite_channel(message.channel):
            await self.handle_invite_message(message)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await self.ctx.sync_commands()
        if self._ready_once:
            return
        self._ready_once = True
        channel = self.ctx.invite_channel()
        if channel is None:
            log.error("could not find the invite channel (#%s / %s)", self.cfg.channel_name, self.cfg.channel_id)
            return
        embed = self.embed()
        if embed is None:
            log.info("[%s] the invite embed is switched off (Messages page) — not posting it in #%s", self.bot.name,
                     getattr(channel, "name", channel))
        else:
            await self.ctx.sticky.ensure(channel, INVITE_MESSAGE_KEY, embed, self.view())
        await self.ensure_role(channel.guild)


async def setup(bot: Any) -> None:
    await bot.add_cog(InvitesCog(bot))

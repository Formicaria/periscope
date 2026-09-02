"""Shared `/unifi` slash-command group and small helpers used by every cog.

All three cogs attach their commands to the single `unifi` group below. Because the group
lives at module level, discord.py's Cog machinery does not bind `self` for us; `attach()`
does that by setting `Command.binding` for every command defined in the cog's module.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands
from periscope import ConfirmView, Severity, lab_embed, truncate

if TYPE_CHECKING:
    from ..bot import UnifiBot

log = logging.getLogger(__name__)


class UnifiGroup(app_commands.Group):
    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CheckFailure):
            return  # admin_only() already answered
        cause = getattr(error, "original", error)
        log.error("command /%s failed: %s", interaction.command.qualified_name if interaction.command else "?",
                  cause)
        msg = f"❌ {type(cause).__name__}: {truncate(str(cause), 300)}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass


unifi = UnifiGroup(name="unifi", description="UniFi network monitoring")


def attach(bot: UnifiBot, cog: commands.Cog) -> None:
    """Bind this cog's group commands to the cog instance and register the group once."""
    for cmd in unifi.commands:
        if cmd.module == type(cog).__module__:
            cmd.binding = cog
    if bot.tree.get_command(unifi.name) is None:
        bot.tree.add_command(unifi)


async def confirm(interaction: discord.Interaction, prompt: str, *, danger: bool = True) -> bool:
    """Ephemeral Confirm/Cancel prompt. Returns True only on explicit confirmation."""
    view = ConfirmView(interaction.user.id, danger=danger)
    await interaction.response.send_message(prompt, view=view, ephemeral=True)
    await view.wait()
    if view.value is None:
        await interaction.edit_original_response(content="⌛ Timed out — nothing was done.", view=None)
        return False
    if not view.value:
        await interaction.edit_original_response(content="Cancelled — nothing was done.", view=None)
        return False
    return True


def paginate(title: str, lines: Iterable[str], *, lab_name: str, per_page: int = 10,
             severity: Severity = Severity.INFO, empty: str = "Nothing to show.") -> list[discord.Embed]:
    lines = list(lines)
    if not lines:
        return [lab_embed(title, empty, severity=severity, lab_name=lab_name)]
    pages = []
    for i in range(0, len(lines), per_page):
        chunk = lines[i:i + per_page]
        pages.append(lab_embed(title, truncate("\n".join(chunk), 4000), severity=severity, lab_name=lab_name))
    return pages

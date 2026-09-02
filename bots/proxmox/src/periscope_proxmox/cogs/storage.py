"""/pve storage — per-node storage detail."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from periscope import PaginatorView, Severity, human_bytes, lab_embed, progress_bar, truncate

from ..bot import ProxmoxBot, node_autocomplete, slash
from ..config import PveSettings

log = logging.getLogger(__name__)


def storage_embed(node: str, rows: list[dict[str, Any]], cfg: PveSettings, *, lab_name: str) -> discord.Embed:
    worst = Severity.OK
    e = lab_embed(f"Storage on {node}", lab_name=lab_name)
    for s in sorted(rows, key=lambda r: str(r.get("storage"))):
        name = str(s.get("storage", "?"))
        if not s.get("active") or not s.get("enabled", 1):
            e.add_field(name=f"⚪ {name}", value=f"{s.get('type', '?')} · inactive", inline=False)
            continue
        used, total = int(s.get("used") or 0), int(s.get("total") or 0)
        pct = 100.0 * used / total if total else 0.0
        if pct >= cfg.storage_crit:
            dot, worst = "🔴", Severity.CRITICAL
        elif pct >= cfg.storage_warn:
            dot = "🟡"
            worst = Severity.WARNING if worst is Severity.OK else worst
        else:
            dot = "🟢"
        shared = " · shared" if s.get("shared") else ""
        body = (f"`{progress_bar(pct)}`\n{human_bytes(used)} used · {human_bytes(s.get('avail'))} free · "
                f"{human_bytes(total)} total\n{s.get('type', '?')}{shared} · content: `{s.get('content', '')}`")
        e.add_field(name=f"{dot} {name}", value=truncate(body, 1024), inline=False)
    if not rows:
        e.description = "No storage reported for this node."
    e.color = worst.color
    return e


class StorageCog(commands.Cog):
    def __init__(self, bot: ProxmoxBot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.register_commands(slash("storage", "Storage usage per node", self.storage))

    async def cog_unload(self) -> None:
        self.bot.unregister_commands("storage")

    @app_commands.describe(node="Only this node (default: every online node)")
    @app_commands.autocomplete(node=node_autocomplete)
    async def storage(self, interaction: discord.Interaction, node: str | None = None) -> None:
        await interaction.response.defer()
        try:
            snap = await self.bot.pve.snapshot(max_age=15)
            names = [node] if node else [n.name for n in snap.nodes if n.online]
            pages = [storage_embed(n, await self.bot.pve.node_storage(n), self.bot.pve_cfg,
                                   lab_name=self.bot.lab_name) for n in names]
        except Exception as exc:
            await interaction.followup.send(f"❌ Proxmox API error: `{truncate(str(exc), 300)}`", ephemeral=True)
            return
        if not pages:
            await interaction.followup.send("No online nodes.", ephemeral=True)
            return
        view = PaginatorView(pages, user_id=interaction.user.id) if len(pages) > 1 else None
        await interaction.followup.send(embed=pages[0], view=view)


async def setup(bot: ProxmoxBot) -> None:
    await bot.add_cog(StorageCog(bot))

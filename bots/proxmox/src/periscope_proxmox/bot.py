"""ProxmoxBot: LabBot plus a shared PVE client and the single `/pve` slash-command group."""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

import discord
from discord import app_commands
from periscope import LabBot, Settings

from .client import PveClient
from .config import PveSettings

log = logging.getLogger(__name__)

COGS = [
    "periscope_proxmox.cogs.status",
    "periscope_proxmox.cogs.vms",
    "periscope_proxmox.cogs.tasks",
    "periscope_proxmox.cogs.storage",
]


class ProxmoxBot(LabBot):
    def __init__(self, settings: Settings, pve_cfg: PveSettings):
        super().__init__(settings, cogs=COGS, webhook=True, description="Proxmox VE monitoring + control")
        self.pve_cfg = pve_cfg
        self.pve = PveClient(pve_cfg)
        self.pve_group = app_commands.Group(name="pve", description="Proxmox VE monitoring and control")
        self.tree.add_command(self.pve_group, override=True)

    def register_commands(self, *commands: app_commands.Command) -> None:
        """Cogs call this from cog_load with bound-method commands so all share one `/pve` group."""
        for cmd in commands:
            self.pve_group.add_command(cmd, override=True)

    def unregister_commands(self, *names: str) -> None:
        for name in names:
            self.pve_group.remove_command(name)

    async def close(self) -> None:
        await self.pve.close()
        await super().close()


def slash(name: str, description: str, callback: Callable[..., Awaitable[None]]) -> app_commands.Command:
    """Wrap a bound cog method into an app command (keeps @describe/@autocomplete/@admin_only metadata)."""
    return app_commands.Command(name=name, description=description, callback=callback)  # type: ignore[arg-type]


async def node_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    bot: ProxmoxBot = interaction.client  # type: ignore[assignment]
    snap = bot.pve.cached
    if snap is None:
        return []
    cur = current.lower()
    return [app_commands.Choice(name=n.name, value=n.name) for n in snap.nodes if cur in n.name.lower()][:25]


async def vmid_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[int]]:
    bot: ProxmoxBot = interaction.client  # type: ignore[assignment]
    snap = bot.pve.cached
    if snap is None:
        return []
    cur = current.lower()
    out = []
    for g in snap.guests:
        if g.template:
            continue
        if cur and cur not in g.name.lower() and cur not in str(g.vmid):
            continue
        out.append(app_commands.Choice(name=f"{g.vmid} · {g.name} ({g.label}, {g.status})"[:100], value=g.vmid))
    return out[:25]

"""/unifi clients, client, kick, block, unblock."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from periscope import PaginatorView, Severity, human_bytes, human_duration, lab_embed
from periscope.bot import admin_only

from ..bot import UnifiBot
from ..util import client_line, client_link, client_name, client_signal, find_client, is_mac, normalize_mac
from . import attach, confirm, paginate, unifi

log = logging.getLogger(__name__)


class ClientsCog(commands.Cog):
    def __init__(self, bot: UnifiBot):
        self.bot = bot

    # ----- helpers ------------------------------------------------------

    async def _clients(self) -> list[dict[str, Any]]:
        return await self.bot.unifi.active_clients()

    def _devices_by_mac(self) -> dict[str, dict[str, Any]]:
        return {d.get("mac", ""): d for d in self.bot.unifi.cached_devices}

    async def _autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        cache = self.bot.unifi.cached_clients
        if not cache:
            try:
                cache = await self._clients()
            except Exception as e:
                log.debug("autocomplete fetch failed: %s", e)
                return []
        q = current.lower()
        out = []
        for c in sorted(cache, key=lambda c: client_name(c).lower()):
            name, mac = client_name(c), c.get("mac", "")
            if q and q not in name.lower() and q not in mac and q not in str(c.get("ip") or ""):
                continue
            out.append(app_commands.Choice(name=f"{name} ({c.get('ip') or mac})"[:100], value=mac))
            if len(out) == 25:
                break
        return out

    def _detail(self, c: dict[str, Any]) -> discord.Embed:
        e = lab_embed(client_name(c), client_link(c, self._devices_by_mac()),
                      severity=Severity.WARNING if c.get("blocked") else Severity.INFO, lab_name=self.bot.lab_name)
        e.add_field(name="IP", value=f"`{c.get('ip') or '—'}`")
        e.add_field(name="MAC", value=f"`{c.get('mac', '?')}`")
        if c.get("hostname"):
            e.add_field(name="Hostname", value=c["hostname"])
        if c.get("oui"):
            e.add_field(name="Vendor", value=c["oui"])
        e.add_field(name="Network", value=c.get("network") or "—")
        e.add_field(name="Uptime", value=human_duration(c.get("uptime")))
        if not c.get("is_wired"):
            e.add_field(name="Signal", value=client_signal(c) or "—")
            radio = c.get("radio_proto") or c.get("radio") or "?"
            e.add_field(name="Channel", value=f"{c.get('channel', '—')} ({radio})")
            if c.get("satisfaction") is not None:
                e.add_field(name="Satisfaction", value=f"{c['satisfaction']}%")
        e.add_field(name="Traffic", value=f"↓ {human_bytes(c.get('rx_bytes'))} · ↑ {human_bytes(c.get('tx_bytes'))}")
        if c.get("is_guest"):
            e.add_field(name="Guest", value="yes")
        if c.get("blocked"):
            e.add_field(name="Blocked", value="yes")
        return e

    async def _sta_action(self, interaction: discord.Interaction, mac: str, verb: str, danger: bool = True) -> None:
        mac = normalize_mac(mac)
        if not is_mac(mac):
            await interaction.response.send_message(f"`{mac}` is not a MAC address.", ephemeral=True)
            return
        who = find_client(self.bot.unifi.cached_clients, mac)
        label = f"**{client_name(who)}** (`{mac}`)" if who else f"`{mac}`"
        if not await confirm(interaction, f"{verb.capitalize()} {label}?", danger=danger):
            return
        fn = {"kick": self.bot.unifi.kick_client, "block": self.bot.unifi.block_client,
              "unblock": self.bot.unifi.unblock_client}[verb]
        await fn(mac)
        log.info("%s %s by %s", verb, mac, interaction.user)
        await interaction.edit_original_response(content=f"✅ {verb.capitalize()}ed {label}.", view=None)

    # ----- commands -----------------------------------------------------

    @unifi.command(name="clients", description="List active clients")
    @app_commands.describe(wireless_only="Only show Wi-Fi clients", search="Filter by name, hostname, IP or MAC")
    async def clients(self, interaction: discord.Interaction, wireless_only: bool = False,
                      search: str | None = None) -> None:
        await interaction.response.defer(ephemeral=True)
        clients = await self._clients()
        if wireless_only:
            clients = [c for c in clients if not c.get("is_wired")]
        if search:
            q = search.lower()
            clients = [c for c in clients if q in client_name(c).lower() or q in str(c.get("hostname") or "").lower()
                       or q in str(c.get("ip") or "") or q in normalize_mac(str(c.get("mac") or ""))
                       or q in str(c.get("essid") or "").lower()]
        clients.sort(key=lambda c: (bool(c.get("is_wired")), client_name(c).lower()))
        by_mac = self._devices_by_mac()
        title = f"Clients ({len(clients)})" + (" · wireless" if wireless_only else "")
        if search:
            title += f" · “{search}”"
        pages = paginate(title, (client_line(c, by_mac) for c in clients), lab_name=self.bot.lab_name, per_page=8,
                         empty="No matching clients.")
        view = PaginatorView(pages, user_id=interaction.user.id)
        await interaction.followup.send(embed=pages[0], view=view, ephemeral=True)

    @unifi.command(name="client", description="Show details for one client")
    @app_commands.describe(mac_or_name="MAC address, name or hostname")
    async def client(self, interaction: discord.Interaction, mac_or_name: str) -> None:
        await interaction.response.defer(ephemeral=True)
        c = find_client(await self._clients(), mac_or_name)
        if c is None:
            await interaction.followup.send(f"No active client matches `{mac_or_name}`.", ephemeral=True)
            return
        await interaction.followup.send(embed=self._detail(c), ephemeral=True)

    @client.autocomplete("mac_or_name")
    async def client_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete(interaction, current)

    @unifi.command(name="kick", description="Disconnect a client (it may reconnect)")
    @app_commands.describe(mac="Client MAC address")
    @admin_only()
    async def kick(self, interaction: discord.Interaction, mac: str) -> None:
        await self._sta_action(interaction, mac, "kick")

    @unifi.command(name="block", description="Block a client from the network")
    @app_commands.describe(mac="Client MAC address")
    @admin_only()
    async def block(self, interaction: discord.Interaction, mac: str) -> None:
        await self._sta_action(interaction, mac, "block")

    @unifi.command(name="unblock", description="Unblock a previously blocked client")
    @app_commands.describe(mac="Client MAC address")
    @admin_only()
    async def unblock(self, interaction: discord.Interaction, mac: str) -> None:
        await self._sta_action(interaction, mac, "unblock", danger=False)

    @kick.autocomplete("mac")
    @block.autocomplete("mac")
    @unblock.autocomplete("mac")
    async def mac_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._autocomplete(interaction, current)


async def setup(bot: UnifiBot) -> None:
    cog = ClientsCog(bot)
    await bot.add_cog(cog)
    attach(bot, cog)

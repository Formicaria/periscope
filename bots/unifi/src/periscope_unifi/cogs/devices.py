"""/unifi devices, device, restart, wan, events, alarms."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from periscope import PaginatorView, Severity, human_bytes, human_duration, lab_embed, status_dot
from periscope.bot import admin_only

from ..bot import UnifiBot
from ..util import (
    client_counts,
    device_clients,
    device_cpu,
    device_line,
    device_mem,
    device_name,
    device_online,
    device_temp,
    device_type,
    event_line,
    find_device,
    health_map,
    status_dot_for,
    wan_summary,
)
from . import attach, confirm, paginate, unifi

log = logging.getLogger(__name__)


class RestartView(discord.ui.View):
    """Restart button under a device detail embed. Admin-gated, then confirmed."""

    def __init__(self, cog: DevicesCog, mac: str, name: str):
        super().__init__(timeout=300)
        self.cog, self.mac, self.name = cog, mac, name

    @discord.ui.button(label="Restart", emoji="🔁", style=discord.ButtonStyle.danger)
    async def restart(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.cog.bot.is_admin(interaction.user):
            await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
            return
        await self.cog.do_restart(interaction, self.mac, self.name)


class DevicesCog(commands.Cog):
    def __init__(self, bot: UnifiBot):
        self.bot = bot

    # ----- helpers ------------------------------------------------------

    async def _device_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        cache = self.bot.unifi.cached_devices
        if not cache:
            try:
                cache = await self.bot.unifi.devices()
            except Exception as e:
                log.debug("autocomplete fetch failed: %s", e)
                return []
        q = current.lower()
        return [app_commands.Choice(name=f"{device_name(d)} ({d.get('model', '?')})"[:100], value=device_name(d))
                for d in sorted(cache, key=lambda d: device_name(d).lower())
                if not q or q in device_name(d).lower() or q in str(d.get("model", "")).lower()][:25]

    def _detail(self, d: dict[str, Any]) -> discord.Embed:
        online = device_online(d)
        e = lab_embed(f"{device_name(d)} — {d.get('model', '?')}", f"{device_type(d)} · {status_dot(online)} "
                      f"{'online' if online else 'offline'}", severity=Severity.OK if online else Severity.CRITICAL,
                      lab_name=self.bot.lab_name)
        e.add_field(name="IP", value=f"`{d.get('ip') or '—'}`")
        e.add_field(name="MAC", value=f"`{d.get('mac', '?')}`")
        ver = f"`{d.get('version', '?')}`"
        if d.get("upgradable"):
            ver += f"\n⬆ `{d.get('upgrade_to_firmware') or 'update available'}`"
        e.add_field(name="Firmware", value=ver)
        e.add_field(name="Uptime", value=human_duration(d.get("uptime")))
        cpu, mem, temp = device_cpu(d), device_mem(d), device_temp(d)
        e.add_field(name="CPU", value=f"{cpu:.0f}%" if cpu is not None else "—")
        e.add_field(name="Memory", value=f"{mem:.0f}%" if mem is not None else "—")
        if temp is not None:
            e.add_field(name="Temp", value=f"{temp:.0f}°C")
        e.add_field(name="Clients", value=str(device_clients(d)))
        if d.get("satisfaction") is not None:
            e.add_field(name="Satisfaction", value=f"{d['satisfaction']}%")
        up = d.get("uplink") or {}
        if up:
            uplink = up.get("uplink_device_name") or up.get("name") or up.get("uplink_mac") or "?"
            speed = f" @ {up.get('speed')} Mbps" if up.get("speed") else ""
            e.add_field(name="Uplink", value=f"{uplink}{speed}")
        return e

    async def do_restart(self, interaction: discord.Interaction, mac: str, name: str) -> None:
        if not await confirm(interaction, f"Restart **{name}** (`{mac}`)? Clients on it will drop briefly."):
            return
        await self.bot.unifi.restart_device(mac)
        log.info("restart %s (%s) by %s", name, mac, interaction.user)
        await interaction.edit_original_response(content=f"✅ Restart sent to **{name}**.", view=None)

    async def _find(self, interaction: discord.Interaction, name: str) -> dict[str, Any] | None:
        d = find_device(await self.bot.unifi.devices(), name)
        if d is None:
            await interaction.followup.send(f"No device matches `{name}`.", ephemeral=True)
        return d

    # ----- commands -----------------------------------------------------

    @unifi.command(name="devices", description="List all UniFi devices")
    async def devices(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        devs = sorted(await self.bot.unifi.devices(), key=lambda d: (device_online(d), device_name(d).lower()))
        lines = []
        for d in devs:
            extra = [device_type(d), f"v{d.get('version', '?')}", f"up {human_duration(d.get('uptime'))}"]
            if (mem := device_mem(d)) is not None:
                extra.append(f"mem {mem:.0f}%")
            lines.append(device_line(d) + "\n└ " + " · ".join(extra))
        down = sum(1 for d in devs if not device_online(d))
        pages = paginate(f"Devices ({len(devs)})", lines, lab_name=self.bot.lab_name, per_page=8,
                         severity=Severity.CRITICAL if down else Severity.OK, empty="No devices adopted.")
        await interaction.followup.send(embed=pages[0], view=PaginatorView(pages, user_id=interaction.user.id),
                                        ephemeral=True)

    @unifi.command(name="device", description="Show details for one device")
    @app_commands.describe(name="Device name (or MAC)")
    async def device(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer(ephemeral=True)
        d = await self._find(interaction, name)
        if d is None:
            return
        view = RestartView(self, d.get("mac", ""), device_name(d)) if self.bot.is_admin(interaction.user) else None
        await interaction.followup.send(embed=self._detail(d), view=view, ephemeral=True)

    @unifi.command(name="restart", description="Restart a UniFi device")
    @app_commands.describe(name="Device name (or MAC)")
    @admin_only()
    async def restart(self, interaction: discord.Interaction, name: str) -> None:
        d = find_device(self.bot.unifi.cached_devices, name) or find_device(await self.bot.unifi.devices(), name)
        if d is None:
            await interaction.response.send_message(f"No device matches `{name}`.", ephemeral=True)
            return
        await self.do_restart(interaction, d.get("mac", ""), device_name(d))

    @device.autocomplete("name")
    @restart.autocomplete("name")
    async def device_name_ac(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return await self._device_ac(interaction, current)

    @unifi.command(name="wan", description="WAN / internet status")
    async def wan(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        health = await self.bot.unifi.health()
        w = wan_summary(health)
        hm = health_map(health)
        if not w.present:
            await interaction.followup.send(embed=lab_embed("WAN", "This site reports no WAN/gateway subsystem.",
                                                            severity=Severity.UNKNOWN, lab_name=self.bot.lab_name))
            return
        e = lab_embed("WAN", f"{status_dot_for('ok' if w.ok else 'error')} status `{w.status}`",
                      severity=Severity.OK if w.ok else Severity.CRITICAL, lab_name=self.bot.lab_name)
        e.add_field(name="IP", value=f"`{w.ip or '—'}`")
        e.add_field(name="Latency", value=f"{w.latency_ms:.0f} ms" if w.latency_ms is not None else "—")
        e.add_field(name="Uptime", value=human_duration(w.uptime_s))
        e.add_field(name="Gateway", value=w.gw_name or "—")
        if w.isp:
            e.add_field(name="ISP", value=w.isp)
        e.add_field(name="Throughput", value=f"⬇ {human_bytes(w.rx_bps, 'B/s')} · ⬆ {human_bytes(w.tx_bps, 'B/s')}")
        www = hm.get("www", {})
        if www.get("xput_down") or www.get("xput_up"):
            e.add_field(name="Last speedtest",
                        value=f"⬇ {www.get('xput_down', '—')} · ⬆ {www.get('xput_up', '—')} Mbps")
        c = client_counts(health)
        e.add_field(name="Clients",
                    value=f"{c['total']} ({c['wired']} wired · {c['wireless']} wireless · {c['guest']} guest)")
        await interaction.followup.send(embed=e, ephemeral=True)

    @unifi.command(name="events", description="Recent controller events")
    @app_commands.describe(limit="How many events (1–50)")
    async def events(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 50] = 20) -> None:
        await interaction.response.defer(ephemeral=True)
        evs = await self.bot.unifi.events(limit)
        pages = paginate(f"Events (last {len(evs)})", (event_line(ev) for ev in evs), lab_name=self.bot.lab_name,
                         per_page=10, empty="No recent events.")
        await interaction.followup.send(embed=pages[0], view=PaginatorView(pages, user_id=interaction.user.id),
                                        ephemeral=True)

    @unifi.command(name="alarms", description="Active (unarchived) controller alarms")
    async def alarms(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        alarms = sorted(await self.bot.unifi.alarms(), key=lambda a: a.get("time", 0), reverse=True)
        lines = [f"🔴 {event_line(a)}" for a in alarms]
        pages = paginate(f"Alarms ({len(alarms)})", lines, lab_name=self.bot.lab_name, per_page=10,
                         severity=Severity.CRITICAL if alarms else Severity.OK, empty="No active alarms. 🎉")
        await interaction.followup.send(embed=pages[0], view=PaginatorView(pages, user_id=interaction.user.id),
                                        ephemeral=True)


async def setup(bot: UnifiBot) -> None:
    cog = DevicesCog(bot)
    await bot.add_cog(cog)
    attach(bot, cog)

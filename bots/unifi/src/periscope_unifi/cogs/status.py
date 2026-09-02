"""Live status board + threshold/state-change alerts for the UniFi site."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import discord
from discord.ext import commands, tasks
from periscope import Alert, RefreshView, Severity, StatusBoard, human_bytes, human_duration, lab_embed, truncate

from ..bot import UnifiBot
from ..util import (
    client_counts,
    client_link,
    client_name,
    device_cpu,
    device_line,
    device_name,
    device_online,
    health_map,
    status_dot_for,
    wan_summary,
)
from . import attach

log = logging.getLogger(__name__)

FP_UNREACHABLE = "unifi:unreachable"
FP_WAN_DOWN = "unifi:wan:down"
FP_WAN_LATENCY = "unifi:wan:latency"
STRIKES = 3  # consecutive polls over threshold before a WARNING fires


@dataclass
class Snapshot:
    health: list[dict[str, Any]] = field(default_factory=list)
    devices: list[dict[str, Any]] = field(default_factory=list)
    clients: list[dict[str, Any]] = field(default_factory=list)

    @property
    def devices_by_mac(self) -> dict[str, dict[str, Any]]:
        return {d.get("mac", ""): d for d in self.devices}


class StatusCog(commands.Cog):
    def __init__(self, bot: UnifiBot):
        self.bot = bot
        self.board = StatusBoard(bot, key="unifi")
        self.view = RefreshView(self.build_board, custom_id="unifi:refresh")
        self.state = bot.state.namespace("unifi")
        self._failures = 0
        self._latency_strikes = 0
        self._cpu_strikes: dict[str, int] = {}
        self.tick.change_interval(seconds=bot.settings.status_interval_s)

    async def cog_load(self) -> None:
        self.bot.add_view(self.view)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    # ----- polling loop -------------------------------------------------

    @tasks.loop(seconds=60)
    async def tick(self) -> None:
        try:
            snap = await self.snapshot()
        except Exception as e:  # never let the loop die
            self._failures += 1
            log.warning("UniFi poll failed (%d in a row): %s", self._failures, e)
            if self._failures == STRIKES:
                await self._safe(self.bot.alerts.fire(Alert(
                    FP_UNREACHABLE, "UniFi unreachable",
                    f"{STRIKES} consecutive polls of `{self.bot.cfg.url}` failed.\n"
                    f"Last error: `{truncate(str(e), 300)}`",
                    severity=Severity.CRITICAL)))
            return
        if self._failures >= STRIKES:
            await self._safe(self.bot.alerts.resolve(FP_UNREACHABLE, "Controller reachable again"))
        self._failures = 0
        try:
            await self.evaluate(snap)
        except Exception:
            log.exception("alert evaluation failed")
        try:
            await self.board.render(self.render(snap), view=self.view)
        except Exception:
            log.exception("status board render failed")

    @tick.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def snapshot(self) -> Snapshot:
        u = self.bot.unifi
        health, devices, clients = await asyncio.gather(u.health(), u.devices(), u.active_clients())
        return Snapshot(health=health, devices=devices, clients=clients)

    async def build_board(self) -> discord.Embed:
        """Used by the 🔄 RefreshView button."""
        try:
            return self.render(await self.snapshot())
        except Exception as e:
            return lab_embed("UniFi", f"Controller unreachable: `{truncate(str(e), 300)}`",
                             severity=Severity.CRITICAL, lab_name=self.bot.lab_name)

    @staticmethod
    async def _safe(coro) -> None:
        try:
            await coro
        except Exception:
            log.exception("alert delivery failed")

    # ----- rendering ----------------------------------------------------

    def render(self, snap: Snapshot) -> discord.Embed:
        wan = wan_summary(snap.health)
        hm = health_map(snap.health)
        counts = client_counts(snap.health)
        down = [d for d in snap.devices if not device_online(d)]

        sev = Severity.OK
        if any(str(h.get("status")) == "warning" for h in snap.health) or self._latency_strikes >= STRIKES:
            sev = Severity.WARNING
        if wan.ok is False or down:
            sev = Severity.CRITICAL

        lines = []
        if wan.present:
            bits = [f"`{wan.ip or '—'}`"]
            if wan.latency_ms is not None:
                bits.append(f"{wan.latency_ms:.0f} ms")
            if wan.uptime_s:
                bits.append(f"up {human_duration(wan.uptime_s)}")
            if wan.isp:
                bits.append(wan.isp)
            lines.append(f"{status_dot_for('ok' if wan.ok else 'error')} **WAN** " + " · ".join(bits))
        else:
            lines.append("⚪ **WAN** not managed here")
        lines.append(f"{status_dot_for(hm.get('lan', {}).get('status'))} LAN · "
                     f"{status_dot_for(hm.get('wlan', {}).get('status'))} WLAN")
        lines.append(f"👥 **{counts['total'] or len(snap.clients)} clients** · {counts['wired']} wired · "
                     f"{counts['wireless']} wireless · {counts['guest']} guest")
        if wan.rx_bps is not None or wan.tx_bps is not None:
            lines.append(f"⬇ {human_bytes(wan.rx_bps, 'B/s')} ⬆ {human_bytes(wan.tx_bps, 'B/s')}")

        e = lab_embed(f"UniFi — {self.bot.cfg.site}", "\n".join(lines), severity=sev, lab_name=self.bot.lab_name)
        devices = sorted(snap.devices, key=lambda d: (device_online(d), device_name(d).lower()))
        if devices:
            chunk, size, n = [], 0, 1
            for d in devices:
                line = device_line(d)
                if size + len(line) + 1 > 1000:
                    e.add_field(name=f"Devices ({len(devices)})" if n == 1 else "\u200b", value="\n".join(chunk),
                                inline=False)
                    chunk, size, n = [], 0, n + 1
                chunk.append(line)
                size += len(line) + 1
            if chunk:
                e.add_field(name=f"Devices ({len(devices)})" if n == 1 else "\u200b", value="\n".join(chunk),
                            inline=False)
        else:
            e.add_field(name="Devices", value="none adopted", inline=False)
        return e

    # ----- alerts -------------------------------------------------------

    async def evaluate(self, snap: Snapshot) -> None:
        active = set(self.bot.alerts.active())
        cfg = self.bot.cfg
        wan = wan_summary(snap.health)

        # WAN down / latency
        if wan.ok is False:
            await self._safe(self.bot.alerts.fire(Alert(
                FP_WAN_DOWN, "WAN down", f"Gateway `{wan.gw_name or '?'}` reports WAN status `{wan.status}`.",
                severity=Severity.CRITICAL, fields={"WAN IP": wan.ip or "—"})))
        elif FP_WAN_DOWN in active:
            await self._safe(self.bot.alerts.resolve(FP_WAN_DOWN, f"WAN back, IP `{wan.ip or '—'}`"))

        if wan.latency_ms is not None and wan.latency_ms > cfg.wan_latency_warn_ms:
            self._latency_strikes += 1
            if self._latency_strikes >= STRIKES:
                await self._safe(self.bot.alerts.fire(Alert(
                    FP_WAN_LATENCY, "WAN latency high",
                    f"{wan.latency_ms:.0f} ms > {cfg.wan_latency_warn_ms} ms for {self._latency_strikes} polls.",
                    severity=Severity.WARNING)))
        else:
            self._latency_strikes = 0
            if FP_WAN_LATENCY in active:
                await self._safe(self.bot.alerts.resolve(
                    FP_WAN_LATENCY, f"Latency {wan.latency_ms:.0f} ms" if wan.latency_ms is not None else None))

        # Devices: offline, CPU, firmware
        notified: list[str] = self.state.get("upgrade_notified", [])
        for d in snap.devices:
            mac = d.get("mac") or "?"
            name = device_name(d)
            fp_down = f"unifi:device:{mac}:down"
            if not device_online(d):
                await self._safe(self.bot.alerts.fire(Alert(
                    fp_down, f"Device offline: {name}", f"`{d.get('model', '?')}` at `{d.get('ip') or '—'}` "
                    f"(state {d.get('state')}).", severity=Severity.CRITICAL, fields={"MAC": f"`{mac}`"})))
                self._cpu_strikes.pop(mac, None)
                continue
            if fp_down in active:
                await self._safe(self.bot.alerts.resolve(fp_down, f"{name} back online"))

            fp_cpu = f"unifi:device:{mac}:cpu"
            cpu = device_cpu(d)
            if cpu is not None and cpu > cfg.device_cpu_warn:
                self._cpu_strikes[mac] = self._cpu_strikes.get(mac, 0) + 1
                if self._cpu_strikes[mac] >= STRIKES:
                    await self._safe(self.bot.alerts.fire(Alert(
                        fp_cpu, f"High CPU: {name}",
                        f"{cpu:.0f}% > {cfg.device_cpu_warn}% for {self._cpu_strikes[mac]} polls.",
                        severity=Severity.WARNING)))
            else:
                self._cpu_strikes.pop(mac, None)
                if fp_cpu in active:
                    await self._safe(self.bot.alerts.resolve(fp_cpu, f"CPU {cpu:.0f}%" if cpu is not None else None))

            if d.get("upgradable"):
                ver = str(d.get("upgrade_to_firmware") or "new").strip()
                fp_up = f"unifi:device:{mac}:upgrade:{ver}"
                if fp_up not in notified:
                    await self._safe(self.bot.alerts.fire(Alert(
                        fp_up, f"Firmware available: {name}",
                        f"`{d.get('version', '?')}` → `{ver}`", severity=Severity.INFO)))
                    notified.append(fp_up)
        if notified != self.state.get("upgrade_notified", []):
            self.state.set("upgrade_notified", notified[-200:])

        await self.track_clients(snap)

    async def track_clients(self, snap: Snapshot) -> None:
        """Remember every client MAC with a last-seen timestamp; announce ones not seen within the TTL."""
        now = time.time()
        ttl = self.bot.cfg.ttl_seconds
        known: dict[str, float] = self.state.get("known_clients") or {}
        seeded = self.state.get("known_seeded", False)
        newcomers = []
        for c in snap.clients:
            mac = c.get("mac")
            if not mac:
                continue
            last = known.get(mac)
            if seeded and (last is None or now - last > ttl):
                newcomers.append(c)
            known[mac] = now
        known = {m: t for m, t in known.items() if now - t <= ttl}
        self.state.set("known_clients", known)
        if not seeded:
            self.state.set("known_seeded", True)
            log.info("seeded %d known clients (no alerts on first run)", len(known))
            return
        if newcomers and self.bot.cfg.alert_new_clients:
            ch = self.bot.settings.alert_channel_id and await self.bot.get_channel_safe(
                self.bot.settings.alert_channel_id)
            if not ch:
                return
            by_mac = snap.devices_by_mac
            for c in newcomers[:10]:
                e = lab_embed(f"New client: {client_name(c)}", client_link(c, by_mac), severity=Severity.INFO,
                              lab_name=self.bot.lab_name)
                e.add_field(name="MAC", value=f"`{c.get('mac')}`")
                e.add_field(name="IP", value=f"`{c.get('ip') or '—'}`")
                if c.get("hostname") and c.get("name"):
                    e.add_field(name="Hostname", value=c["hostname"])
                if c.get("oui"):
                    e.add_field(name="Vendor", value=c["oui"])
                await self._safe(ch.send(embed=e))
            if len(newcomers) > 10:
                await self._safe(ch.send(f"…and {len(newcomers) - 10} more new clients."))


async def setup(bot: UnifiBot) -> None:
    cog = StatusCog(bot)
    await bot.add_cog(cog)
    attach(bot, cog)

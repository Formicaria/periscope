"""Live cluster status board + threshold / state-change alerts."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks
from periscope import (
    Alert,
    RefreshView,
    Severity,
    StatusBoard,
    human_bytes,
    human_duration,
    lab_embed,
    progress_bar,
    status_dot,
    truncate,
)

from ..bot import ProxmoxBot
from ..client import Snapshot
from ..config import PveSettings

log = logging.getLogger(__name__)

FP_UNREACHABLE = "pve:unreachable"
CPU_STREAK_POLLS = 3
MAX_FAILURES = 3


def build_board(snap: Snapshot, cfg: PveSettings, *, lab_name: str, cluster: str | None = None,
                active_alerts: int = 0) -> discord.Embed:
    nodes_online = sum(1 for n in snap.nodes if n.online)
    guests = [g for g in snap.guests if not g.template]
    running = sum(1 for g in guests if g.running)
    vms = [g for g in guests if g.kind == "qemu"]
    cts = [g for g in guests if g.kind == "lxc"]
    worst = Severity.OK
    if nodes_online < len(snap.nodes):
        worst = Severity.CRITICAL
    elif active_alerts:
        worst = Severity.WARNING

    title = f"Proxmox · {cluster}" if cluster else "Proxmox"
    desc = (
        f"**{nodes_online}/{len(snap.nodes)}** nodes online · "
        f"**{running}/{len(guests)}** guests running "
        f"({sum(1 for g in vms if g.running)}/{len(vms)} VM, {sum(1 for g in cts if g.running)}/{len(cts)} CT)"
    )
    if active_alerts:
        desc += f" · ⚠️ {active_alerts} active alert{'s' if active_alerts != 1 else ''}"
    e = lab_embed(title, desc, severity=worst, lab_name=lab_name)

    for n in snap.nodes[:20]:
        on_node = [g for g in snap.guests_on(n.name) if not g.template]
        run_here = sum(1 for g in on_node if g.running)
        if not n.online:
            body = "🔴 **offline**"
        else:
            cpu_flag = " ⚠️" if n.cpu_pct > cfg.cpu_warn else ""
            mem_flag = " ⚠️" if n.mem_pct > cfg.mem_warn else ""
            body = (
                f"`CPU {progress_bar(n.cpu_pct)}`{cpu_flag} ({n.maxcpu} cores)\n"
                f"`MEM {progress_bar(n.mem_pct)}`{mem_flag} "
                f"({human_bytes(n.mem_used)} / {human_bytes(n.mem_total)})\n"
                f"⏱ up {human_duration(n.uptime)} · {run_here}/{len(on_node)} guests running"
            )
        e.add_field(name=f"{status_dot(n.online)} {n.name}", value=body, inline=False)

    lines = []
    for s in snap.unique_storages():
        if not s.available:
            lines.append(f"⚪ **{s.key}** — unavailable")
            continue
        flag = "🔴" if s.pct >= cfg.storage_crit else "🟡" if s.pct >= cfg.storage_warn else "🟢"
        lines.append(f"{flag} **{s.key}** `{progress_bar(s.pct, 10)}` {human_bytes(s.used)} / {human_bytes(s.total)}")
    if lines:
        e.add_field(name="Storage", value=truncate("\n".join(lines), 1024), inline=False)
    return e


class StatusCog(commands.Cog):
    def __init__(self, bot: ProxmoxBot):
        self.bot = bot
        self.cfg = bot.pve_cfg
        self.board = StatusBoard(bot, key="pve")
        self.view = RefreshView(self.build_embed, custom_id="pve:refresh")
        self._state = bot.state.namespace("pve")
        self._guest_status: dict[str, str] = dict(self._state.get("guest_status", {}) or {})
        self._cpu_streak: dict[str, int] = dict(self._state.get("cpu_streak", {}) or {})
        self._failures = 0
        self._cluster: str | None = None
        self._active: set[str] = set()
        self.tick.change_interval(seconds=max(15, bot.settings.status_interval_s))

    async def cog_load(self) -> None:
        self.bot.add_view(self.view)
        self.tick.start()

    async def cog_unload(self) -> None:
        self.tick.cancel()

    # ----- loop -----

    @tasks.loop(seconds=60)
    async def tick(self) -> None:
        try:
            snap = await self.bot.pve.snapshot(max_age=0)
            if self._cluster is None:
                try:
                    self._cluster = await self.bot.pve.cluster_name() or ""
                except Exception:  # cluster name is cosmetic
                    self._cluster = ""
        except Exception as exc:
            self._failures += 1
            log.warning("proxmox poll failed (%d/%d): %s", self._failures, MAX_FAILURES, exc)
            if self._failures >= MAX_FAILURES:
                await self.bot.alerts.fire(Alert(
                    fingerprint=FP_UNREACHABLE, title="Proxmox unreachable", severity=Severity.CRITICAL,
                    description=f"{MAX_FAILURES} consecutive API failures.\n`{truncate(str(exc), 300)}`",
                ))
            return
        if self._failures >= MAX_FAILURES:
            await self.bot.alerts.resolve(FP_UNREACHABLE, note="API responding again")
        self._failures = 0

        try:
            await self.check_alerts(snap)
        except Exception:
            log.exception("alert evaluation failed")
        if not self.board.channel_id:
            return  # STATUS_CHANNEL_ID unset: alerts only, no dashboard
        try:
            await self.board.render(self._embed(snap), view=self.view)
        except Exception:
            log.exception("status board render failed")

    @tick.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    def _embed(self, snap: Snapshot) -> discord.Embed:
        active = [fp for fp in self.bot.alerts.active() if fp.startswith("pve:")]
        return build_board(snap, self.cfg, lab_name=self.bot.lab_name, cluster=self._cluster or None,
                           active_alerts=len(active))

    async def build_embed(self) -> discord.Embed:
        try:
            snap = await self.bot.pve.snapshot(max_age=5)
        except Exception as exc:
            return lab_embed("Proxmox", f"API error: `{truncate(str(exc), 500)}`",
                             severity=Severity.CRITICAL, lab_name=self.bot.lab_name)
        return self._embed(snap)

    # ----- alerting -----

    async def _resolve(self, fp: str, note: str) -> None:
        """Resolve only if the alert is actually open (avoids a state write per poll per fingerprint)."""
        if fp in self._active:
            await self.bot.alerts.resolve(fp, note=note)
            self._active.discard(fp)

    async def check_alerts(self, snap: Snapshot) -> None:
        alerts = self.bot.alerts
        cfg = self.cfg
        self._active = set(alerts.active())

        for n in snap.nodes:
            fp_down, fp_cpu, fp_mem = (f"pve:node:{n.name}:down", f"pve:node:{n.name}:cpu", f"pve:node:{n.name}:mem")
            if not n.online:
                await alerts.fire(Alert(fingerprint=fp_down, title=f"Node {n.name} is offline",
                                        severity=Severity.CRITICAL,
                                        description="Node reported as not online in cluster resources."))
                self._cpu_streak.pop(n.name, None)
                continue
            await self._resolve(fp_down, note="Node back online")

            if n.cpu_pct > cfg.cpu_warn:
                self._cpu_streak[n.name] = self._cpu_streak.get(n.name, 0) + 1
                if self._cpu_streak[n.name] >= CPU_STREAK_POLLS:
                    await alerts.fire(Alert(
                        fingerprint=fp_cpu, title=f"High CPU on {n.name}", severity=Severity.WARNING,
                        description=f"CPU above {cfg.cpu_warn}% for {self._cpu_streak[n.name]} consecutive polls.",
                        fields={"CPU": f"{n.cpu_pct:.1f}%"},
                    ))
            else:
                if self._cpu_streak.pop(n.name, None):
                    await self._resolve(fp_cpu, note=f"CPU back to {n.cpu_pct:.1f}%")

            if n.mem_pct > cfg.mem_warn:
                await alerts.fire(Alert(
                    fingerprint=fp_mem, title=f"High memory on {n.name}", severity=Severity.WARNING,
                    description=f"Memory above {cfg.mem_warn}%.",
                    fields={"Memory": f"{n.mem_pct:.1f}% ({human_bytes(n.mem_used)} / {human_bytes(n.mem_total)})"},
                ))
            else:
                await self._resolve(fp_mem, note=f"Memory back to {n.mem_pct:.1f}%")

        for s in snap.unique_storages():
            fp = f"pve:storage:{s.key}:full"
            if not s.available:
                continue
            if s.pct >= cfg.storage_warn:
                sev = Severity.CRITICAL if s.pct >= cfg.storage_crit else Severity.WARNING
                await alerts.fire(Alert(
                    fingerprint=fp, title=f"Storage {s.key} at {s.pct:.0f}%", severity=sev,
                    description=f"{human_bytes(s.used)} used of {human_bytes(s.total)} "
                                f"({human_bytes(s.total - s.used)} free).",
                ))
            else:
                await self._resolve(fp, note=f"Usage back to {s.pct:.0f}%")

        new_status: dict[str, str] = {}
        for g in snap.guests:
            if g.template:
                continue
            key = str(g.vmid)
            new_status[key] = g.status
            prev = self._guest_status.get(key)
            fp = f"pve:vm:{g.vmid}:stopped"
            if prev == "running" and g.status == "stopped":
                await alerts.fire(Alert(
                    fingerprint=fp, title=f"{g.label} {g.vmid} ({g.name}) stopped", severity=Severity.WARNING,
                    description=f"Was running on **{g.node}**, now `{g.status}`.",
                ))
            elif g.running:
                await self._resolve(fp, note="Running again")
        self._guest_status = new_status
        self._state.set("guest_status", new_status)
        self._state.set("cpu_streak", self._cpu_streak)


async def setup(bot: ProxmoxBot) -> None:
    await bot.add_cog(StatusCog(bot))

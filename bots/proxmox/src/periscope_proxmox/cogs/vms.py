"""/pve nodes, /pve vms, /pve vm and the start/stop/shutdown/reboot power commands."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands
from periscope import ConfirmView, PaginatorView, Severity, human_bytes, human_duration, lab_embed, status_dot, truncate
from periscope.bot import admin_only

from ..bot import ProxmoxBot, node_autocomplete, slash, vmid_autocomplete
from ..client import Guest, Snapshot

log = logging.getLogger(__name__)

PAGE_SIZE = 10
STATUS_EMOJI = {"running": "🟢", "stopped": "🔴", "paused": "🟡", "suspended": "🟡"}
ACTION_VERB = {"start": "Starting", "stop": "Force-stopping", "shutdown": "Shutting down", "reboot": "Rebooting"}
DESTRUCTIVE = {"stop", "reboot"}


def guest_line(g: Guest) -> str:
    dot = STATUS_EMOJI.get(g.status, "⚪")
    tpl = " *(template)*" if g.template else ""
    perf = f" · cpu {g.cpu_pct:.0f}% · mem {human_bytes(g.mem_used)}/{human_bytes(g.mem_total)}" if g.running else ""
    return f"{dot} `{g.vmid:>5}` **{g.name}**{tpl} · {g.label} · {g.node} · {g.status}{perf}"


def guest_pages(guests: list[Guest], *, lab_name: str, title: str) -> list[discord.Embed]:
    if not guests:
        return [lab_embed(title, "No guests match.", lab_name=lab_name)]
    pages = []
    for i in range(0, len(guests), PAGE_SIZE):
        chunk = guests[i:i + PAGE_SIZE]
        e = lab_embed(title, "\n".join(guest_line(g) for g in chunk), lab_name=lab_name)
        e.description = truncate(e.description or "", 4000)
        e.set_author(name=f"{len(guests)} guests · page {i // PAGE_SIZE + 1}/{(len(guests) - 1) // PAGE_SIZE + 1}")
        pages.append(e)
    return pages


def guest_detail(g: Guest, cur: dict[str, Any], *, lab_name: str, pve_url: str) -> discord.Embed:
    running = cur.get("status") == "running"
    sev = Severity.OK if running else Severity.WARNING if cur.get("status") == "stopped" else Severity.INFO
    e = lab_embed(f"{g.label} {g.vmid} · {cur.get('name') or g.name}", severity=sev, lab_name=lab_name,
                  url=f"{pve_url}/#v1:0:={g.kind}%2F{g.vmid}")
    e.add_field(name="Status", value=f"{status_dot(running)} {cur.get('status', g.status)}"
                + (f" ({cur['qmpstatus']})" if cur.get("qmpstatus") and cur["qmpstatus"] != cur.get("status") else ""))
    e.add_field(name="Node", value=g.node)
    e.add_field(name="Type", value="QEMU VM" if g.kind == "qemu" else "LXC container")
    e.add_field(name="CPU", value=f"{100 * float(cur.get('cpu') or 0):.1f}% of {cur.get('cpus', '?')} vCPU")
    e.add_field(name="Memory", value=f"{human_bytes(cur.get('mem'))} / {human_bytes(cur.get('maxmem'))}")
    e.add_field(name="Disk", value=f"{human_bytes(cur.get('disk')) + ' / ' if cur.get('disk') else ''}"
                                   f"{human_bytes(cur.get('maxdisk'))}")
    e.add_field(name="Uptime", value=human_duration(cur.get("uptime")) if running else "—")
    e.add_field(name="Net in / out", value=f"{human_bytes(cur.get('netin'))} / {human_bytes(cur.get('netout'))}")
    ha = cur.get("ha") or {}
    e.add_field(name="HA", value=f"managed ({ha.get('state', '?')})" if ha.get("managed") else "no")
    extras = []
    if cur.get("tags"):
        extras.append(f"tags: `{cur['tags']}`")
    if cur.get("lock"):
        extras.append(f"lock: `{cur['lock']}`")
    if g.kind == "qemu" and cur.get("agent"):
        extras.append("guest agent: enabled")
    if extras:
        e.add_field(name="Misc", value=" · ".join(extras), inline=False)
    return e


class GuestControlView(discord.ui.View):
    """Start / Shutdown / Reboot / Stop buttons on the `/pve vm` detail embed."""

    def __init__(self, cog: "VmsCog", guest: Guest):
        super().__init__(timeout=300)
        self.cog = cog
        self.guest = guest

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.cog.bot.is_admin(interaction.user):
            return True
        await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
        return False

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success, emoji="▶️")
    async def start(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.run_action(interaction, self.guest.vmid, "start", confirm=False)

    @discord.ui.button(label="Shutdown", style=discord.ButtonStyle.primary, emoji="⏹️")
    async def shutdown(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.run_action(interaction, self.guest.vmid, "shutdown", confirm=False)

    @discord.ui.button(label="Reboot", style=discord.ButtonStyle.secondary, emoji="🔄")
    async def reboot(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.run_action(interaction, self.guest.vmid, "reboot")

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⚠️")
    async def stop(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cog.run_action(interaction, self.guest.vmid, "stop")


class VmsCog(commands.Cog):
    def __init__(self, bot: ProxmoxBot):
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.register_commands(
            slash("nodes", "List cluster nodes with load and guest counts", self.nodes),
            slash("vms", "List VMs and containers", self.vms),
            slash("vm", "Show one VM/CT with power buttons", self.vm),
            slash("start", "Start a VM/CT", self.start),
            slash("shutdown", "Gracefully shut down a VM/CT (ACPI / init)", self.shutdown),
            slash("reboot", "Reboot a VM/CT", self.reboot),
            slash("stop", "Force-stop a VM/CT (like pulling the plug)", self.stop),
        )

    async def cog_unload(self) -> None:
        self.bot.unregister_commands("nodes", "vms", "vm", "start", "shutdown", "reboot", "stop")

    async def _snapshot(self, interaction: discord.Interaction) -> Snapshot | None:
        try:
            return await self.bot.pve.snapshot(max_age=15)
        except Exception as exc:
            log.warning("snapshot failed: %s", exc)
            msg = f"❌ Proxmox API error: `{truncate(str(exc), 300)}`"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
            return None

    # ----- read-only commands -----

    async def nodes(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        snap = await self._snapshot(interaction)
        if snap is None:
            return
        e = lab_embed("Proxmox nodes", lab_name=self.bot.lab_name)
        for n in snap.nodes:
            guests = [g for g in snap.guests_on(n.name) if not g.template]
            if n.online:
                val = (f"CPU {n.cpu_pct:.1f}% ({n.maxcpu} cores) · MEM {n.mem_pct:.1f}% "
                       f"({human_bytes(n.mem_used)} / {human_bytes(n.mem_total)})\n"
                       f"up {human_duration(n.uptime)} · {sum(1 for g in guests if g.running)}/{len(guests)} running")
            else:
                val = "offline"
            e.add_field(name=f"{status_dot(n.online)} {n.name}", value=val, inline=False)
        if not snap.nodes:
            e.description = "No nodes returned by the API."
        await interaction.followup.send(embed=e)

    @app_commands.describe(node="Only guests on this node", running_only="Hide stopped guests")
    @app_commands.autocomplete(node=node_autocomplete)
    async def vms(self, interaction: discord.Interaction, node: str | None = None, running_only: bool = False) -> None:
        await interaction.response.defer()
        snap = await self._snapshot(interaction)
        if snap is None:
            return
        guests = [g for g in snap.guests if (node is None or g.node == node) and (not running_only or g.running)]
        title = "Proxmox guests" + (f" on {node}" if node else "") + (" (running)" if running_only else "")
        pages = guest_pages(guests, lab_name=self.bot.lab_name, title=title)
        view = PaginatorView(pages, user_id=interaction.user.id) if len(pages) > 1 else None
        await interaction.followup.send(embed=pages[0], view=view)

    @app_commands.describe(vmid="VM/CT id (autocompletes by name)")
    @app_commands.autocomplete(vmid=vmid_autocomplete)
    async def vm(self, interaction: discord.Interaction, vmid: int) -> None:
        await interaction.response.defer()
        snap = await self._snapshot(interaction)
        if snap is None:
            return
        g = await self.bot.pve.resolve_guest(vmid)
        if g is None:
            await interaction.followup.send(f"❌ No VM or container with id `{vmid}`.", ephemeral=True)
            return
        try:
            cur = await self.bot.pve.guest_current(g.node, g.kind, g.vmid)
        except Exception as exc:
            await interaction.followup.send(f"❌ Could not fetch status: `{truncate(str(exc), 300)}`", ephemeral=True)
            return
        e = guest_detail(g, cur, lab_name=self.bot.lab_name, pve_url=self.bot.pve_cfg.url)
        await interaction.followup.send(embed=e, view=GuestControlView(self, g))

    # ----- power actions -----

    async def run_action(self, interaction: discord.Interaction, vmid: int, action: str, *, confirm: bool = True):
        """Shared by slash commands and buttons. Always answers ephemerally; confirms destructive actions."""
        try:
            g = await self.bot.pve.resolve_guest(vmid)
        except Exception as exc:
            await interaction.response.send_message(f"❌ Proxmox API error: `{truncate(str(exc), 300)}`",
                                                    ephemeral=True)
            return
        if g is None:
            await interaction.response.send_message(f"❌ No VM or container with id `{vmid}`.", ephemeral=True)
            return
        label = f"{g.label} **{g.vmid}** ({g.name}) on `{g.node}`"
        if confirm or action in DESTRUCTIVE:
            view = ConfirmView(interaction.user.id, danger=action in DESTRUCTIVE)
            warn = " This is a hard power-off." if action == "stop" else ""
            await interaction.response.send_message(f"**{action.upper()}** {label}?{warn}", view=view, ephemeral=True)
            await view.wait()
            if not view.value:
                await interaction.edit_original_response(content=f"Cancelled `{action}` for {label}.", view=None)
                return
        else:
            await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            upid = await self.bot.pve.guest_action(g.node, g.kind, g.vmid, action)
        except Exception as exc:
            await interaction.edit_original_response(content=f"❌ `{action}` failed for {label}: "
                                                             f"`{truncate(str(exc), 300)}`", view=None)
            return
        log.info("%s requested %s for %s %s (%s)", interaction.user, action, g.kind, g.vmid, upid)
        await interaction.edit_original_response(
            content=f"✅ {ACTION_VERB[action]} {label}.\nTask `{upid or 'submitted'}`", view=None)
        self.bot.pve.invalidate()  # next read re-fetches so the new state shows up quickly

    @admin_only()
    @app_commands.describe(vmid="VM/CT id")
    @app_commands.autocomplete(vmid=vmid_autocomplete)
    async def start(self, interaction: discord.Interaction, vmid: int) -> None:
        await self.run_action(interaction, vmid, "start")

    @admin_only()
    @app_commands.describe(vmid="VM/CT id")
    @app_commands.autocomplete(vmid=vmid_autocomplete)
    async def shutdown(self, interaction: discord.Interaction, vmid: int) -> None:
        await self.run_action(interaction, vmid, "shutdown")

    @admin_only()
    @app_commands.describe(vmid="VM/CT id")
    @app_commands.autocomplete(vmid=vmid_autocomplete)
    async def reboot(self, interaction: discord.Interaction, vmid: int) -> None:
        await self.run_action(interaction, vmid, "reboot")

    @admin_only()
    @app_commands.describe(vmid="VM/CT id")
    @app_commands.autocomplete(vmid=vmid_autocomplete)
    async def stop(self, interaction: discord.Interaction, vmid: int) -> None:
        await self.run_action(interaction, vmid, "stop")


async def setup(bot: ProxmoxBot) -> None:
    await bot.add_cog(VmsCog(bot))

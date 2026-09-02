"""/pve tasks plus the vzdump backup watcher."""

from __future__ import annotations

import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks
from periscope import Alert, Severity, human_duration, lab_embed, truncate

from ..bot import ProxmoxBot, node_autocomplete, slash
from ..client import parse_upid, summarize_vzdump_log, task_ok

log = logging.getLogger(__name__)

BACKUP_POLL_S = 300
SEEN_KEEP = 200
LOOKBACK_S = 12 * 3600
DEFAULT_LIMIT = 15
MAX_LIMIT = 50


def task_line(t: dict[str, Any]) -> str:
    ok = task_ok(t)
    dot = "🔵" if ok is None else "🟢" if ok else "🔴"
    info = parse_upid(str(t.get("upid", "")))
    ttype = t.get("type") or info["type"]
    tid = t.get("id") or info["id"]
    user = str(t.get("user") or info["user"]).split("!")[0]
    start = int(t.get("starttime") or info["starttime"] or 0)
    end = int(t.get("endtime") or 0)
    when = f"<t:{start}:R>" if start else "—"
    dur = f" · {human_duration(end - start)}" if end and start else " · running" if ok is None else ""
    status = "" if ok in (True, None) else f"\n   ↳ `{truncate(str(t.get('status')), 150)}`"
    target = f" `{tid}`" if tid else ""
    return f"{dot} **{ttype}**{target} · {t.get('node', info['node'])} · {user} · {when}{dur}{status}"


class TasksCog(commands.Cog):
    def __init__(self, bot: ProxmoxBot):
        self.bot = bot
        self._state = bot.state.namespace("pve:backups")
        self._seen: list[str] = list(self._state.get("seen", []) or [])
        self._last_check: int = int(self._state.get("last_check", 0) or 0)

    async def cog_load(self) -> None:
        self.bot.register_commands(slash("tasks", "Recent cluster tasks (backups, migrations, power ops)", self.tasks_cmd))
        if self.bot.pve_cfg.watch_backups:
            self.backup_watch.start()

    async def cog_unload(self) -> None:
        self.bot.unregister_commands("tasks")
        self.backup_watch.cancel()

    # ----- /pve tasks -----

    @app_commands.describe(node="Only tasks from this node", limit=f"How many to show (default {DEFAULT_LIMIT})")
    @app_commands.autocomplete(node=node_autocomplete)
    async def tasks_cmd(self, interaction: discord.Interaction, node: str | None = None,
                    limit: app_commands.Range[int, 1, MAX_LIMIT] = DEFAULT_LIMIT) -> None:
        await interaction.response.defer()
        try:
            snap = await self.bot.pve.snapshot(max_age=15)
            nodes = [node] if node else [n.name for n in snap.nodes if n.online]
            found: list[dict[str, Any]] = []
            for name in nodes:
                found.extend(await self.bot.pve.node_tasks(name, limit=limit))
        except Exception as exc:
            await interaction.followup.send(f"❌ Proxmox API error: `{truncate(str(exc), 300)}`", ephemeral=True)
            return
        found.sort(key=lambda t: int(t.get("starttime") or 0), reverse=True)
        found = found[:limit]
        title = "Recent tasks" + (f" on {node}" if node else "")
        body = "\n".join(task_line(t) for t in found) or "No tasks found."
        e = lab_embed(title, truncate(body, 4000), lab_name=self.bot.lab_name)
        await interaction.followup.send(embed=e)

    # ----- backup watcher -----

    @tasks.loop(seconds=BACKUP_POLL_S)
    async def backup_watch(self) -> None:
        now = int(time.time())
        since = self._last_check or now - BACKUP_POLL_S
        try:
            snap = await self.bot.pve.snapshot(max_age=60)
            finished: list[dict[str, Any]] = []
            for n in snap.nodes:
                if not n.online:
                    continue
                # `since` filters on start time; long backups start well before they finish, so look back further
                rows = await self.bot.pve.node_tasks(n.name, limit=50, since=since - LOOKBACK_S, typefilter="vzdump")
                finished.extend(t for t in rows if task_ok(t) is not None and int(t.get("endtime") or 0) >= since)
        except Exception as exc:
            log.warning("backup watcher poll failed: %s", exc)
            return
        for t in sorted(finished, key=lambda t: int(t.get("endtime") or 0)):
            upid = str(t.get("upid", ""))
            if not upid or upid in self._seen:
                continue
            try:
                await self.report_backup(t)
            except Exception:
                log.exception("failed to report backup task %s", upid)
            self._seen.append(upid)
        self._seen = self._seen[-SEEN_KEEP:]
        self._last_check = now
        self._state.set("seen", self._seen)
        self._state.set("last_check", now)

    @backup_watch.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def report_backup(self, t: dict[str, Any]) -> None:
        upid = str(t["upid"])
        info = parse_upid(upid)
        node = str(t.get("node") or info["node"])
        start, end = int(t.get("starttime") or info["starttime"]), int(t.get("endtime") or 0)
        ok = task_ok(t)
        try:
            summary = summarize_vzdump_log(await self.bot.pve.task_log(node, upid))
        except Exception as exc:
            log.warning("could not fetch vzdump log for %s: %s", upid, exc)
            summary = {"ok": [], "failed": []}
        lines = [f"✅ VM {r['vmid']} · {r['duration']}" + (f" · {r['size']}" if r["size"] else "") for r in summary["ok"]]
        lines += [f"❌ VM {r['vmid']} · {truncate(r['reason'], 200)}" for r in summary["failed"]]
        detail = "\n".join(lines) or "(no per-guest details in task log)"
        fields = {"Node": node, "Duration": human_duration(end - start) if end and start else "—",
                  "Started": f"<t:{start}:f>" if start else "—", "User": str(t.get("user") or info["user"])}

        if ok:
            e = lab_embed(f"Backup finished on {node}: {len(summary['ok'])} guest(s)", truncate(detail, 4000),
                          severity=Severity.INFO, lab_name=self.bot.lab_name)
            for k, v in fields.items():
                e.add_field(name=k, value=v)
            e.set_footer(text=f"🧪 {self.bot.lab_name} · {upid}")
            cid = self.bot.settings.alert_channel_id
            ch = await self.bot.get_channel_safe(cid) if cid else None
            if ch is None:
                log.info("backup OK on %s (%s) but no ALERT_CHANNEL_ID to post to", node, upid)
                return
            await ch.send(embed=e)
            return

        err = truncate(str(t.get("status") or "unknown error"), 300)
        await self.bot.alerts.fire(Alert(
            fingerprint=f"pve:backup:{upid}",
            title=f"Backup FAILED on {node}" + (f" ({len(summary['failed'])} guest(s))" if summary["failed"] else ""),
            description=f"`{err}`\n\n{truncate(detail, 3000)}",
            severity=Severity.WARNING,
            fields=fields,
        ))


async def setup(bot: ProxmoxBot) -> None:
    await bot.add_cog(TasksCog(bot))

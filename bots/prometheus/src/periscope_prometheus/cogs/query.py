"""Prometheus: /prom query, /prom targets, and the scrape-target watcher loop."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands, tasks

from periscope import Alert, Severity, lab_embed, truncate

from ..bot import PromBot, prom_group
from ..format import format_instant_result, group_targets, target_fingerprint

log = logging.getLogger(__name__)


class QueryCog(commands.Cog):
    def __init__(self, bot: PromBot):
        self.bot = bot
        self._down: set[str] = set()  # fingerprints currently alerting
        self.watch_targets.change_interval(seconds=max(15, bot.settings.status_interval_s))
        if bot.cfg.target_watch:
            self.watch_targets.start()

    def cog_unload(self) -> None:
        self.watch_targets.cancel()

    # ----- target watcher ---------------------------------------------------

    @tasks.loop(seconds=60)
    async def watch_targets(self) -> None:
        try:
            targets = await self.bot.prom.targets()
        except Exception as e:
            log.warning("target watch: Prometheus unreachable: %s", e)
            return
        seen_down: set[str] = set()
        for t in targets:
            labels = t.get("labels") or {}
            job = str(labels.get("job") or t.get("scrapePool") or "unknown")
            instance = str(labels.get("instance") or t.get("scrapeUrl") or "?")
            fp = target_fingerprint(job, instance)
            if str(t.get("health", "")).lower() == "up":
                continue
            seen_down.add(fp)
            if fp in self._down:
                continue
            try:
                await self.bot.alerts.fire(Alert(
                    fingerprint=fp, title=f"Scrape target down: {job}",
                    description=truncate(str(t.get("lastError") or "target health is not `up`"), 1000),
                    severity=Severity.WARNING,
                    fields={"job": job, "instance": instance, "health": str(t.get("health", "unknown"))},
                    url=f"{self.bot.prom.base_url}/targets",
                ))
                self._down.add(fp)
            except Exception:
                log.exception("failed to fire target alert %s", fp)
        for fp in list(self._down - seen_down):
            try:
                await self.bot.alerts.resolve(fp, note="target is scraping again")
            except Exception:
                log.exception("failed to resolve %s", fp)
            self._down.discard(fp)

    @watch_targets.before_loop
    async def _before_watch(self) -> None:
        await self.bot.wait_until_ready()
        # Re-adopt alerts that were firing before a restart so we can resolve them.
        self._down = {fp for fp in self.bot.alerts.active() if fp.startswith("prom:target:")}

    # ----- commands ----------------------------------------------------------

    async def cmd_query(self, interaction: discord.Interaction, expr: str) -> None:
        await interaction.response.defer()
        try:
            data = await self.bot.prom.query(expr)
        except Exception as e:
            await interaction.followup.send(embed=lab_embed(
                "Query failed", f"```\n{truncate(str(e), 1500)}\n```", severity=Severity.CRITICAL,
                lab_name=self.bot.lab_name))
            return
        text, total = format_instant_result(data)
        e = lab_embed("PromQL", f"```promql\n{truncate(expr, 500)}\n```\n```\n{truncate(text, 3300)}\n```",
                      lab_name=self.bot.lab_name, severity=Severity.OK if total else Severity.INFO)
        e.add_field(name="rows", value=str(total), inline=True)
        e.add_field(name="type", value=str(data.get("resultType", "?")), inline=True)
        await interaction.followup.send(embed=e)

    async def cmd_targets(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            targets = await self.bot.prom.targets()
        except Exception as e:
            await interaction.followup.send(f"❌ Prometheus unreachable: {truncate(str(e), 300)}")
            return
        groups = group_targets(targets)
        total_up = sum(g["up"] for g in groups.values())
        total_down = sum(g["down"] for g in groups.values())
        sev = Severity.CRITICAL if total_down else Severity.OK
        e = lab_embed("Scrape targets", f"**{total_up}** up · **{total_down}** down · {len(groups)} jobs",
                      severity=sev, lab_name=self.bot.lab_name, url=f"{self.bot.prom.base_url}/targets")
        for job, g in list(groups.items())[:25]:
            lines = [f"🟢 {g['up']} up" + (f" · 🔴 {g['down']} down" if g["down"] else "")
                     + (f" · ⚪ {g['unknown']} unknown" if g["unknown"] else "")]
            for inst, err in g["down_list"][:5]:
                lines.append(f"• `{inst}`" + (f" — {truncate(err, 120)}" if err else ""))
            if len(g["down_list"]) > 5:
                lines.append(f"• … {len(g['down_list']) - 5} more down")
            e.add_field(name=job, value=truncate("\n".join(lines), 1024), inline=not g["down"])
        await interaction.followup.send(embed=e)


async def setup(bot: PromBot) -> None:
    cog = QueryCog(bot)
    await bot.add_cog(cog)
    group = prom_group(bot)

    @group.command(name="query", description="Run an instant PromQL query")
    @discord.app_commands.describe(expr="PromQL expression, e.g. up == 0")
    async def query(interaction: discord.Interaction, expr: str):
        await cog.cmd_query(interaction, expr)

    @group.command(name="targets", description="Scrape target health grouped by job")
    async def targets(interaction: discord.Interaction):
        await cog.cmd_targets(interaction)

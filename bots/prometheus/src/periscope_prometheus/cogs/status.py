"""Live status board: service reachability, firing alerts, targets, silences.

The board shows whatever clients its bot carries. Under v2 the Alertmanager and Grafana clients may belong to
sibling services on the same presence (`alertmanager`, `grafana`); they are picked up from there so one board
still covers the whole monitoring stack. Anything not configured anywhere is shown as such.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands, tasks

from periscope import Alert, RefreshView, Severity, StatusBoard, lab_embed, status_dot

from ..bot import PromBot, prom_group
from ..format import count_by_severity, group_targets

log = logging.getLogger(__name__)

FAIL_THRESHOLD = 3
SIBLINGS = {"prom": "prometheus", "am": "alertmanager", "grafana": "grafana"}


class StatusCog(commands.Cog):
    def __init__(self, bot: PromBot):
        self.bot = bot
        self.board = StatusBoard(bot, key="prometheus")
        self.view = RefreshView(self.build_embed, custom_id="periscope_prometheus:refresh")
        self._failures = {"prometheus": 0, "alertmanager": 0, "grafana": 0}
        self._alerting: set[str] = set()
        self.tick.change_interval(seconds=max(15, bot.settings.status_interval_s))
        self.tick.start()

    def cog_unload(self) -> None:
        self.tick.cancel()

    # ----- clients (own first, then a sibling service on the same presence) ------

    def owner(self, attr: str):
        """The bot carrying client `attr`: this one, else the sibling service on the same presence, else None."""
        if getattr(self.bot, attr, None) is not None:
            return self.bot
        presence = getattr(self.bot, "presence", None)
        sibling = presence.service(SIBLINGS[attr]) if presence is not None else None
        return sibling if sibling is not None and getattr(sibling, attr, None) is not None else None

    def client(self, attr: str):
        owner = self.owner(attr)
        return getattr(owner, attr, None) if owner is not None else None

    def _grafana(self):
        gf = self.client("grafana")
        if gf is None or not getattr(gf, "base_url", ""):
            return None
        return gf

    # ----- unreachable tracking ----------------------------------------------

    async def _track(self, service: str, ok: bool) -> None:
        fp = f"prom:{service}:unreachable"
        if ok:
            self._failures[service] = 0
            if fp in self._alerting:
                self._alerting.discard(fp)
                await self.bot.alerts.resolve(fp, note="reachable again")
            return
        self._failures[service] += 1
        if self._failures[service] >= FAIL_THRESHOLD and fp not in self._alerting:
            self._alerting.add(fp)
            await self.bot.alerts.fire(Alert(
                fingerprint=fp, title=f"{service.capitalize()} unreachable",
                description=f"{self._failures[service]} consecutive failed checks.",
                severity=Severity.CRITICAL))

    # ----- board ---------------------------------------------------------------

    async def build_embed(self) -> discord.Embed:
        bot = self.bot
        cfg = bot.cfg
        prom, am, grafana = self.client("prom"), self.client("am"), self._grafana()

        async def probe(client) -> bool | None:
            return await client.healthy() if client is not None else None

        prom_ok, am_ok, gf_ok = await asyncio.gather(probe(prom), probe(am), probe(grafana))

        firing = targets = silences = None
        if prom_ok:
            try:
                targets = await prom.targets()
            except Exception as e:
                log.warning("targets fetch failed: %s", e)
                prom_ok = False
        if am_ok:
            try:
                firing, silences = await asyncio.gather(am.alerts(), am.silences())
            except Exception as e:
                log.warning("alertmanager fetch failed: %s", e)
                am_ok = False

        for svc, ok in (("prometheus", prom_ok), ("alertmanager", am_ok), ("grafana", gf_ok)):
            if ok is None:
                continue
            try:
                await self._track(svc, bool(ok))
            except Exception:
                log.exception("alert routing failed for %s", svc)

        counts = count_by_severity(firing or [])
        groups = group_targets(targets or [])
        up = sum(g["up"] for g in groups.values())
        down = sum(g["down"] for g in groups.values())

        if prom_ok is False or am_ok is False or gf_ok is False:
            sev = Severity.CRITICAL
        elif counts["critical"] or down:
            sev = Severity.CRITICAL
        elif counts["warning"]:
            sev = Severity.WARNING
        else:
            sev = Severity.OK

        def state(ok: bool | None) -> str:
            return f"{status_dot(ok)} " + ("not configured" if ok is None else "up" if ok else "down")

        home = next((c.base_url for c in (prom, am, grafana) if c is not None), None)
        e = lab_embed("Monitoring status", severity=sev, lab_name=bot.lab_name, url=home)
        e.add_field(name="Prometheus", value=state(prom_ok))
        e.add_field(name="Alertmanager", value=state(am_ok))
        e.add_field(name="Grafana", value=state(gf_ok))
        e.add_field(name="Firing alerts",
                    value=(f"🔴 {counts['critical']} critical\n🟡 {counts['warning']} warning\n🔵 {counts['info']} info")
                    if firing is not None else "—")
        e.add_field(name="Scrape targets", value=f"🟢 {up} up\n🔴 {down} down\n{len(groups)} jobs"
                    if targets is not None else "—")
        e.add_field(name="Active silences", value=f"🔕 {len(silences)}" if silences is not None else "—")
        if down:
            lines = [f"• {job}: " + ", ".join(f"`{i}`" for i, _ in g["down_list"][:4])
                     for job, g in groups.items() if g["down"]]
            e.add_field(name="Down targets", value="\n".join(lines)[:1024], inline=False)
        links = []
        if prom is not None:
            links.append(f"[Prometheus]({prom.base_url})")
        if am is not None:
            links.append(f"[Alertmanager]({am.base_url})")
        if grafana is not None:
            links.append(f"[Grafana]({grafana.base_url})")
            uid = getattr(getattr(self.owner("grafana"), "cfg", cfg), "default_dashboard_uid", None)
            if uid:
                links.append(f"[Dashboard]({grafana.dashboard_url(uid)})")
        if links:
            e.add_field(name="Links", value=" · ".join(links), inline=False)
        return e

    @tasks.loop(seconds=60)
    async def tick(self) -> None:
        try:
            embed = await self.build_embed()
            if self.board.channel_id:
                await self.board.render(embed, view=self.view)
        except Exception:
            log.exception("status board tick failed")

    @tick.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()
        self.bot.add_view(self.view)

    async def cmd_status(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await interaction.followup.send(embed=await self.build_embed())


async def setup(bot: PromBot) -> None:
    cog = StatusCog(bot)
    await bot.add_cog(cog)
    group = prom_group(bot)

    @group.command(name="status", description="Prometheus / Alertmanager / Grafana overview")
    async def status(interaction: discord.Interaction):
        await cog.cmd_status(interaction)

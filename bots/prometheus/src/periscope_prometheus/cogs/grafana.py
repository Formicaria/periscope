"""Grafana: /prom dashboards, /prom panel (PNG render), /prom grafana (health)."""

from __future__ import annotations

import io
import logging
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from periscope import PaginatorView, Severity, lab_embed, truncate

from ..bot import PromBot, prom_group
from ..format import parse_range

log = logging.getLogger(__name__)

CACHE_TTL_S = 300


class GrafanaCog(commands.Cog):
    def __init__(self, bot: PromBot):
        self.bot = bot
        self._cache: list[dict[str, Any]] = []
        self._cache_at = 0.0

    @property
    def enabled(self) -> bool:
        return self.bot.cfg.grafana_enabled

    async def dashboards(self, force: bool = False) -> list[dict[str, Any]]:
        if force or time.monotonic() - self._cache_at > CACHE_TTL_S:
            self._cache = await self.bot.grafana.search_dashboards()
            self._cache_at = time.monotonic()
        return self._cache

    async def resolve_dashboard(self, text: str) -> dict[str, Any] | None:
        """Accept a uid or a title (exact, then case-insensitive substring)."""
        text = text.strip()
        items = await self.dashboards()
        for d in items:
            if d.get("uid") == text:
                return d
        low = text.lower()
        for d in items:
            if str(d.get("title", "")).lower() == low:
                return d
        matches = [d for d in items if low in str(d.get("title", "")).lower()]
        return matches[0] if len(matches) == 1 else None

    async def _not_configured(self, interaction: discord.Interaction) -> bool:
        if self.enabled:
            return False
        await interaction.response.send_message("Grafana is not configured (set GRAFANA_URL + GRAFANA_TOKEN).",
                                                ephemeral=True)
        return True

    async def autocomplete_dashboard(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not self.enabled:
            return []
        try:
            items = await self.dashboards()
        except Exception:
            return []
        low = current.lower()
        out = []
        for d in items:
            title = str(d.get("title", ""))
            if low in title.lower():
                out.append(app_commands.Choice(name=truncate(title, 100), value=str(d.get("uid", ""))))
            if len(out) >= 25:
                break
        return out

    # ----- commands ----------------------------------------------------------

    async def cmd_dashboards(self, interaction: discord.Interaction, search: str | None) -> None:
        if await self._not_configured(interaction):
            return
        await interaction.response.defer()
        try:
            items = await self.bot.grafana.search_dashboards(search or "") if search else await self.dashboards(True)
        except Exception as e:
            await interaction.followup.send(f"❌ Grafana unreachable: {truncate(str(e), 300)}")
            return
        if not items:
            await interaction.followup.send(embed=lab_embed("No dashboards found", lab_name=self.bot.lab_name))
            return
        pages, per = [], 10
        for i in range(0, len(items), per):
            lines = []
            for d in items[i:i + per]:
                url = self.bot.grafana.base_url + str(d.get("url", ""))
                lines.append(f"• [{d.get('title')}]({url}) — `{d.get('uid')}`")
            pages.append(lab_embed(f"Grafana dashboards ({len(items)})", "\n".join(lines), lab_name=self.bot.lab_name))
        await interaction.followup.send(embed=pages[0], view=PaginatorView(pages, user_id=interaction.user.id))

    async def cmd_panel(self, interaction: discord.Interaction, dashboard: str, panel_id: int,
                        range_: str | None) -> None:
        if await self._not_configured(interaction):
            return
        try:
            rng = parse_range(range_)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            d = await self.resolve_dashboard(dashboard)
            if d is None:
                await interaction.followup.send(f"❌ No unique dashboard matching `{truncate(dashboard, 80)}`.")
                return
            uid = str(d["uid"])
            full = await self.bot.grafana.dashboard(uid)
            slug = str((full.get("meta") or {}).get("slug") or "d")
            title = self._panel_title(full.get("dashboard") or {}, panel_id)
            png = await self.bot.grafana.render_panel(uid, slug, panel_id, range_=rng)
        except Exception as e:
            log.warning("panel render failed: %s", e)
            await interaction.followup.send(embed=lab_embed(
                "Render failed", f"```\n{truncate(str(e), 800)}\n```\nIs the `grafana-image-renderer` plugin installed?",
                severity=Severity.CRITICAL, lab_name=self.bot.lab_name))
            return
        if not png or png[:4] != b"\x89PNG":
            await interaction.followup.send("❌ Grafana did not return a PNG (renderer plugin missing or token lacks access).")
            return
        url = f"{self.bot.grafana.dashboard_url(uid, slug)}?viewPanel={panel_id}&from=now-{rng}&to=now&orgId={self.bot.grafana.org_id}"
        e = lab_embed(f"{d.get('title')} — {title or f'panel {panel_id}'}", f"last {rng} · [open in Grafana]({url})",
                      lab_name=self.bot.lab_name, url=url)
        e.set_image(url="attachment://panel.png")
        await interaction.followup.send(embed=e, file=discord.File(io.BytesIO(png), filename="panel.png"))

    @staticmethod
    def _panel_title(dash: dict[str, Any], panel_id: int) -> str | None:
        stack = list(dash.get("panels") or [])
        while stack:
            p = stack.pop()
            if p.get("id") == panel_id:
                return p.get("title") or None
            stack.extend(p.get("panels") or [])
        return None

    async def cmd_grafana(self, interaction: discord.Interaction) -> None:
        if await self._not_configured(interaction):
            return
        await interaction.response.defer()
        try:
            h = await self.bot.grafana.health()
            n = len(await self.dashboards())
        except Exception as e:
            await interaction.followup.send(embed=lab_embed("Grafana unreachable", truncate(str(e), 500),
                                                            severity=Severity.CRITICAL, lab_name=self.bot.lab_name))
            return
        ok = h.get("database") == "ok"
        e = lab_embed("Grafana", severity=Severity.OK if ok else Severity.WARNING, lab_name=self.bot.lab_name,
                      url=self.bot.grafana.base_url)
        e.add_field(name="database", value=str(h.get("database", "?")))
        e.add_field(name="version", value=str(h.get("version", "?")))
        e.add_field(name="dashboards", value=str(n))
        await interaction.followup.send(embed=e)


async def setup(bot: PromBot) -> None:
    cog = GrafanaCog(bot)
    await bot.add_cog(cog)
    group = prom_group(bot)

    @group.command(name="dashboards", description="List Grafana dashboards")
    @app_commands.describe(search="Filter by title")
    async def dashboards(interaction: discord.Interaction, search: str | None = None):
        await cog.cmd_dashboards(interaction, search)

    @group.command(name="panel", description="Render a Grafana panel as an image")
    @app_commands.describe(dashboard="Dashboard (title or uid)", panel_id="Panel id (from the panel URL: viewPanel=N)",
                           range="Relative time range, e.g. 30m, 6h, 2d (default 6h)")
    async def panel(interaction: discord.Interaction, dashboard: str, panel_id: app_commands.Range[int, 1, 10000],
                    range: str | None = None):
        await cog.cmd_panel(interaction, dashboard, int(panel_id), range)

    @panel.autocomplete("dashboard")
    async def _panel_ac(interaction: discord.Interaction, current: str):
        return await cog.autocomplete_dashboard(interaction, current)

    @group.command(name="grafana", description="Grafana health")
    async def grafana(interaction: discord.Interaction):
        await cog.cmd_grafana(interaction)

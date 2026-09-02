"""Alertmanager: inbound webhook, /prom alerts (with silence buttons), /prom silences, /prom unsilence."""

from __future__ import annotations

import logging
from typing import Any

import discord
from aiohttp import web
from discord.ext import commands

from periscope import ConfirmView, PaginatorView, Severity, lab_embed, truncate
from periscope.bot import admin_only

from ..bot import PromBot, prom_group
from ..format import alert_from_am, severity_from_labels, silence_summary

log = logging.getLogger(__name__)

SILENCE_CHOICES = {"1h": 3600, "24h": 86400}


class AlertPaginator(PaginatorView):
    """Paginator over firing alerts with admin-only Silence buttons acting on the current page."""

    def __init__(self, cog: "AlertmanagerCog", alerts: list[dict[str, Any]], pages: list[discord.Embed],
                 user_id: int):
        super().__init__(pages, user_id=user_id)
        self.cog = cog
        self.alerts = alerts

    async def _silence(self, interaction: discord.Interaction, label: str) -> None:
        bot: PromBot = self.cog.bot
        if not bot.is_admin(interaction.user):
            await interaction.response.send_message("🚫 Admin only.", ephemeral=True)
            return
        alert = self.alerts[self.index]
        labels = dict(alert.get("labels") or {})
        name = labels.get("alertname", "alert")
        confirm = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Silence **{name}** for {label} (matching all {len(labels)} labels)?", view=confirm, ephemeral=True)
        await confirm.wait()
        if not confirm.value:
            await interaction.edit_original_response(content="Cancelled.", view=None)
            return
        try:
            sid = await bot.am.create_silence(
                labels, SILENCE_CHOICES[label], created_by=str(interaction.user),
                comment=f"silenced via Discord by {interaction.user}")
        except Exception as e:
            log.warning("silence failed: %s", e)
            await interaction.edit_original_response(content=f"❌ Silence failed: {truncate(str(e), 300)}", view=None)
            return
        await interaction.edit_original_response(content=f"🔕 Silenced **{name}** for {label} — id `{sid}`", view=None)

    @discord.ui.button(label="Silence 1h", style=discord.ButtonStyle.primary, emoji="🔕", row=1)
    async def silence_1h(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._silence(interaction, "1h")

    @discord.ui.button(label="Silence 24h", style=discord.ButtonStyle.danger, emoji="🔕", row=1)
    async def silence_24h(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._silence(interaction, "24h")


class AlertmanagerCog(commands.Cog):
    def __init__(self, bot: PromBot):
        self.bot = bot
        if bot.webhook:
            bot.webhook.add_route("POST", "/alertmanager", self.handle_webhook)

    # ----- webhook ---------------------------------------------------------

    async def handle_webhook(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "invalid json"}, status=400)
        alerts = payload.get("alerts") if isinstance(payload, dict) else None
        if not isinstance(alerts, list):
            return web.json_response({"error": "expected Alertmanager webhook payload with alerts[]"}, status=400)
        fired = resolved = 0
        for raw in alerts:
            if not isinstance(raw, dict):
                continue
            alert = alert_from_am(raw)
            try:
                if raw.get("status") == "resolved":
                    await self.bot.alerts.resolve(alert.fingerprint)
                    resolved += 1
                else:
                    await self.bot.alerts.fire(alert, force=True)
                    fired += 1
            except Exception:
                log.exception("failed to route alert %s", alert.fingerprint)
        log.info("alertmanager webhook: %d firing, %d resolved (receiver=%s)", fired, resolved,
                 payload.get("receiver"))
        return web.json_response({"ok": True, "fired": fired, "resolved": resolved})

    # ----- embeds ----------------------------------------------------------

    def alert_embed(self, a: dict[str, Any], idx: int, total: int) -> discord.Embed:
        alert = alert_from_am(a)
        e = alert.to_embed(self.bot.lab_name)
        e.title = f"{alert.severity.emoji} {alert.title}  ({idx}/{total})"
        started = str(a.get("startsAt") or "")[:19].replace("T", " ")
        if started:
            e.add_field(name="since", value=f"{started} UTC", inline=True)
        state = (a.get("status") or {}).get("state")
        if state and state != "active":
            e.add_field(name="state", value=state, inline=True)
        return e

    # ----- commands (bound in setup) ----------------------------------------

    async def cmd_alerts(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            alerts = await self.bot.am.alerts()
        except Exception as e:
            await interaction.followup.send(f"❌ Alertmanager unreachable: {truncate(str(e), 300)}")
            return
        alerts.sort(key=lambda a: (severity_from_labels(a.get("labels") or {}) is not Severity.CRITICAL,
                                   str((a.get("labels") or {}).get("alertname", ""))))
        if not alerts:
            await interaction.followup.send(embed=lab_embed("No firing alerts", "Alertmanager reports nothing active.",
                                                            severity=Severity.OK, lab_name=self.bot.lab_name))
            return
        pages = [self.alert_embed(a, i + 1, len(alerts)) for i, a in enumerate(alerts)]
        view = AlertPaginator(self, alerts, pages, interaction.user.id)
        await interaction.followup.send(embed=pages[0], view=view)

    async def cmd_silences(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        try:
            silences = await self.bot.am.silences()
        except Exception as e:
            await interaction.followup.send(f"❌ Alertmanager unreachable: {truncate(str(e), 300)}")
            return
        if not silences:
            await interaction.followup.send(embed=lab_embed("No active silences", severity=Severity.OK,
                                                            lab_name=self.bot.lab_name))
            return
        pages = []
        per_page = 6
        for i in range(0, len(silences), per_page):
            chunk = silences[i:i + per_page]
            e = lab_embed(f"Active silences ({len(silences)})", lab_name=self.bot.lab_name)
            for s in chunk:
                e.add_field(name=(s.get("status") or {}).get("state", "active"), value=truncate(silence_summary(s), 1024),
                            inline=False)
            pages.append(e)
        await interaction.followup.send(embed=pages[0], view=PaginatorView(pages, user_id=interaction.user.id))

    async def cmd_unsilence(self, interaction: discord.Interaction, silence_id: str) -> None:
        silence_id = silence_id.strip()
        confirm = ConfirmView(interaction.user.id)
        await interaction.response.send_message(f"Expire silence `{silence_id}`?", view=confirm, ephemeral=True)
        await confirm.wait()
        if not confirm.value:
            await interaction.edit_original_response(content="Cancelled.", view=None)
            return
        try:
            await self.bot.am.delete_silence(silence_id)
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Failed: {truncate(str(e), 300)}", view=None)
            return
        await interaction.edit_original_response(content=f"🔔 Silence `{silence_id}` expired.", view=None)


async def setup(bot: PromBot) -> None:
    cog = AlertmanagerCog(bot)
    await bot.add_cog(cog)
    group = prom_group(bot)

    @group.command(name="alerts", description="List firing alerts from Alertmanager (with silence buttons)")
    async def alerts(interaction: discord.Interaction):
        await cog.cmd_alerts(interaction)

    @group.command(name="silences", description="List active Alertmanager silences")
    async def silences(interaction: discord.Interaction):
        await cog.cmd_silences(interaction)

    @group.command(name="unsilence", description="Expire an Alertmanager silence by id (admin)")
    @discord.app_commands.describe(silence_id="Silence id (from /prom silences)")
    @admin_only()
    async def unsilence(interaction: discord.Interaction, silence_id: str):
        await cog.cmd_unsilence(interaction, silence_id)

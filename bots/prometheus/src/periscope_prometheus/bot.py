"""PromBot: LabBot plus the three API clients and the shared `/prom` command group."""

from __future__ import annotations

from discord import app_commands

from periscope import LabBot, Settings

from .client import AlertmanagerClient, GrafanaClient, PrometheusClient
from .config import PromSettings

COGS = [
    "periscope_prometheus.cogs.alertmanager",
    "periscope_prometheus.cogs.query",
    "periscope_prometheus.cogs.grafana",
    "periscope_prometheus.cogs.status",
]


class PromBot(LabBot):
    def __init__(self, settings: Settings, cfg: PromSettings):
        super().__init__(settings, cogs=COGS, webhook=True,
                         description="Prometheus / Alertmanager / Grafana bridge")
        self.cfg = cfg
        self.prom = PrometheusClient(cfg)
        self.am = AlertmanagerClient(cfg)
        self.grafana = GrafanaClient(cfg)

    async def close(self) -> None:
        for c in (self.prom, self.am, self.grafana):
            try:
                await c.close()
            except Exception:
                pass
        await super().close()


def prom_group(bot: LabBot) -> app_commands.Group:
    """Return the single top-level `/prom` group, creating and registering it on first use."""
    existing = bot.tree.get_command("prom")
    if isinstance(existing, app_commands.Group):
        return existing
    group = app_commands.Group(name="prom", description="Prometheus, Alertmanager and Grafana")
    bot.tree.add_command(group)
    return group

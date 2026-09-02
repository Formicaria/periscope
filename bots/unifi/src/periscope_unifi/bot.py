"""LabBot subclass carrying the UniFi config and API client."""

from __future__ import annotations

from periscope import LabBot, Settings

from .client import UnifiClient
from .config import UnifiConfig

COGS = ["periscope_unifi.cogs.status", "periscope_unifi.cogs.clients", "periscope_unifi.cogs.devices"]


class UnifiBot(LabBot):
    def __init__(self, settings: Settings, cfg: UnifiConfig):
        # webhook=True with no routes: exposes GET /health so the Docker HEALTHCHECK works.
        super().__init__(settings, cogs=COGS, webhook=True, description="UniFi network monitoring")
        self.cfg = cfg
        self.unifi = UnifiClient(cfg)

    async def close(self) -> None:
        await self.unifi.close()
        await super().close()

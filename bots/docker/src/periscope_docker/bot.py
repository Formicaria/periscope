"""LabBot subclass carrying the Docker config and Engine API client."""

from __future__ import annotations

from periscope import LabBot, Settings

from .client import DockerClient
from .config import DockerConfig

COGS = ["periscope_docker.cogs.status", "periscope_docker.cogs.containers"]


class DockerBot(LabBot):
    def __init__(self, settings: Settings, cfg: DockerConfig):
        # webhook=True with no routes: exposes GET /health so the Docker HEALTHCHECK works.
        super().__init__(settings, cogs=COGS, webhook=True, description="Docker container monitoring")
        self.cfg = cfg
        self.docker = DockerClient(cfg)

    async def close(self) -> None:
        await self.docker.close()
        await super().close()

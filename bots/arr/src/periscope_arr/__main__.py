"""Entrypoint: python -m periscope_arr"""

from __future__ import annotations

import logging
import sys

from periscope import LabBot, Settings

from . import messages  # noqa: F401  — importing messages registers the media.* message kinds
from .config import ArrSettings
from .hub import COGS, MediaHub


class ArrBot(LabBot):
    def __init__(self, settings: Settings, cfg: ArrSettings):
        super().__init__(settings, cogs=COGS, webhook=True, description="*arr / media bot")
        self.cfg = cfg
        self.media_hub = MediaHub(self, cfg)  # every configured client, one /arr group
        self.svc = self.media_hub.svc

    async def close(self) -> None:
        await self.media_hub.close()
        await super().close()


def main() -> None:
    try:
        settings = Settings.from_env()
        cfg = ArrSettings.from_env()
    except RuntimeError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    bot = ArrBot(settings, cfg)
    logging.getLogger(__name__).info("enabled services: %s", ", ".join(cfg.enabled_services()))
    bot.run_forever()


if __name__ == "__main__":
    main()

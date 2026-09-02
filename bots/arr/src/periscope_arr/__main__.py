"""Entrypoint: python -m periscope_arr"""

from __future__ import annotations

import logging
import sys

from periscope import LabBot, Settings

from .client import Services
from .config import ArrSettings

COGS = ["periscope_arr.cogs.webhooks", "periscope_arr.cogs.queue", "periscope_arr.cogs.media"]


class ArrBot(LabBot):
    def __init__(self, settings: Settings, cfg: ArrSettings):
        super().__init__(settings, cogs=COGS, webhook=True, description="*arr / media bot")
        self.cfg = cfg
        self.svc = Services(cfg)

    async def close(self) -> None:
        await self.svc.close()
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

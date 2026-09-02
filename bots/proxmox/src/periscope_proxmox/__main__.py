"""Entry point: `python -m periscope_proxmox`."""

from __future__ import annotations

import sys

from periscope import Settings

from .bot import ProxmoxBot
from .config import PveSettings


def main() -> None:
    try:
        settings = Settings.from_env()
        pve_cfg = PveSettings.from_env()
    except RuntimeError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        sys.exit(2)
    bot = ProxmoxBot(settings, pve_cfg)
    bot.run_forever()


if __name__ == "__main__":
    main()

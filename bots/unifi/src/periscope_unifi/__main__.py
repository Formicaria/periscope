"""Entrypoint: `python -m periscope_unifi`."""

from __future__ import annotations

import sys

from periscope import Settings

from .bot import UnifiBot
from .config import UnifiConfig


def main() -> None:
    try:
        settings = Settings.from_env()
        cfg = UnifiConfig.from_env()
    except RuntimeError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    bot = UnifiBot(settings, cfg)
    bot.run_forever()


if __name__ == "__main__":
    main()

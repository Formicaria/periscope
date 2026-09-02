"""Entrypoint: `python -m periscope_prometheus`."""

import sys

from periscope import Settings

from .bot import PromBot
from .config import PromSettings


def main() -> None:
    try:
        settings = Settings.from_env()
        cfg = PromSettings.from_env()
    except RuntimeError as e:
        print(f"configuration error: {e}", file=sys.stderr)
        sys.exit(2)
    bot = PromBot(settings, cfg)
    bot.run_forever()


if __name__ == "__main__":
    main()

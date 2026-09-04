"""Entrypoint: `python -m periscope_docker`."""

from __future__ import annotations

import sys

from periscope import Settings

from .bot import DockerBot
from .config import DockerConfig


def main() -> None:
    try:
        settings = Settings.from_env()
        cfg = DockerConfig.from_env()
    except RuntimeError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    bot = DockerBot(settings, cfg)
    bot.run_forever()


if __name__ == "__main__":
    main()

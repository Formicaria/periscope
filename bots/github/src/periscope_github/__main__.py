import logging
import sys

from periscope import Settings

from . import build_bot
from .config import GithubSettings


def main() -> None:
    try:
        settings = Settings.from_env()
        gh = GithubSettings.from_env()
    except RuntimeError as e:
        print(f"config error: {e}", file=sys.stderr)
        sys.exit(2)
    if not settings.alert_channel_id and not gh.feed_channel_id:
        print("config error: set GITHUB_FEED_CHANNEL_ID (or ALERT_CHANNEL_ID) so the feed has somewhere to go",
              file=sys.stderr)
        sys.exit(2)
    bot = build_bot(settings, gh)
    logging.getLogger(__name__).info("starting periscope-github for org %s (poll=%s)", gh.org, gh.poll_enabled)
    bot.run_forever()


if __name__ == "__main__":
    main()

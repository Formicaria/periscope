"""periscope-github: GitHub organization activity feed."""

from .config import GithubSettings

__all__ = ["GithubSettings", "COGS", "build_bot"]
__version__ = "0.2.0"

COGS = ["periscope_github.cogs.events", "periscope_github.cogs.commands", "periscope_github.cogs.poller"]


def build_bot(settings=None, gh_settings=None):
    """Build the LabBot with the GitHub cogs attached (used by __main__ and tests)."""
    from periscope import LabBot, Settings

    from .client import GithubClient

    settings = settings or Settings.from_env()
    gh_settings = gh_settings or GithubSettings.from_env()
    bot = LabBot(settings, cogs=COGS, webhook=True, description="GitHub org feed")
    bot.gh_settings = gh_settings
    bot.gh_client = GithubClient(gh_settings)
    return bot

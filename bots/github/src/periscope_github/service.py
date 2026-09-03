"""v2 service definition for the GitHub org feed: same wiring as `build_bot()`, on a shared presence."""

from __future__ import annotations

import logging
from pathlib import Path

from periscope import ServiceBot, ServiceSpec, env_scope, settings_from_example
from periscope.http import HttpClient, HttpError

from . import COGS
from .client import GithubClient
from .config import GithubSettings

log = logging.getLogger(__name__)

EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
API_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
               "User-Agent": "periscope-github"}


async def build(bot: ServiceBot) -> None:
    with env_scope(bot.env):
        gh = GithubSettings.from_env()
    if not bot.settings.alert_channel_id and not gh.feed_channel_id:
        log.warning("[github] neither GITHUB_FEED_CHANNEL_ID nor ALERT_CHANNEL_ID is set: "
                    "unmapped repos have no feed channel")
    bot.gh_settings = gh
    bot.gh_client = GithubClient(gh)
    for path in COGS:
        await bot.load_extension(path)


async def check(env: dict[str, str]) -> tuple[bool, str]:
    """With a PAT: GET /user must answer. Without one: the organization must at least be visible."""
    token = env.get("GITHUB_TOKEN", "").strip()
    org = env.get("GITHUB_ORG", "").strip() or "formicaria"
    api = (env.get("GITHUB_API_URL", "").strip() or "https://api.github.com").rstrip("/")
    headers = dict(API_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    client = HttpClient(api, headers=headers, timeout_s=10)
    try:
        if token:
            me = await client.get_json("/user")
            login = me.get("login", "?") if isinstance(me, dict) else "?"
            try:
                data = await client.get_json(f"/orgs/{org}")
            except HttpError as e:
                if e.status == 404:
                    return False, f"token works ({login}) but organization {org} was not found"
                raise
            repos = int((data or {}).get("public_repos") or 0) + int((data or {}).get("total_private_repos") or 0)
            return True, f"GitHub token works — {login}; {org}: {repos} repos visible"
        data = await client.get_json(f"/orgs/{org}")
        repos = (data or {}).get("public_repos", "?")
        return True, (f"organization {org} found ({repos} public repos); "
                      "no token: webhook-only, /gh and polling stay off")
    except HttpError as e:
        if e.status in (401, 403):
            return False, f"GitHub rejected the token ({e.status}): check GITHUB_TOKEN"
        if e.status == 404:
            return False, f"organization {org} not found ({e.status}): check GITHUB_ORG"
        return False, f"GitHub answered {e.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    finally:
        await client.close()


SERVICES = [
    ServiceSpec(
        name="github",
        title="GitHub",
        description="Org activity feed per repo (webhook + polling), live CI train cards, CI-failure alerts, "
                    "/gh repos, prs, issues, runs, commits, activity, watch.",
        group="dev",
        settings=settings_from_example(EXAMPLE),
        build=build,
        check=check,
        slash="/gh",
        webhook_paths=["/github"],
        needs_webhook=True,
    )
]

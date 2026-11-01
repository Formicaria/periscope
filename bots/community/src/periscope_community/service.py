"""v2 service definition for `community`: the public face of the lab in a members' Discord server.

A status board of everything the server's members use, an outage post (with the reason) when something stays
down, and admin-run maintenance announcements via /psa — so "is Plex down?" gets answered before anyone asks.
"""

from __future__ import annotations

import logging
from pathlib import Path

from periscope import ServiceBot, ServiceSpec, env_scope, settings_from_example

from . import messages  # noqa: F401 — registers the community.* message kinds
from .book import MaintBook
from .cogs.psa import load_tz
from .config import CommunitySettings, parse_resources, slug
from .pve import PveLite, borrowed_env

log = logging.getLogger(__name__)
EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
COGS = ["periscope_community.cogs.watch", "periscope_community.cogs.psa", "periscope_community.cogs.link"]


def make_settings():
    out = settings_from_example(EXAMPLE, required=("PSA_CHANNEL_ID", "RESOURCES"))
    by_key = {s.key: s for s in out}
    by_key["PSA_CHANNEL_ID"].label = "PSA channel"
    by_key["RESOURCES"].label = "What to watch"
    by_key["COMMUNITY_BOARD"].type = "bool"
    by_key["LINK_MINECRAFT"].label = "Minecraft chat bridges (for /link minecraft)"
    by_key["LINK_PLEX_ROLE"].label = "Role for verified Plex users (/link plex)"
    return out


async def build(bot: ServiceBot) -> None:
    with env_scope(bot.env):
        cfg = CommunitySettings.from_env()
    for p in cfg.problems:
        log.warning("community: settings — %s", p)
    if not cfg.resources:
        raise RuntimeError("RESOURCES lists nothing it can check — see the settings page for the format")
    bot.community_cfg = cfg
    bot.community_tz = load_tz(cfg.timezone)
    bot.community_book = MaintBook(bot.state, bot.windows, bot.community_tz, slugify=slug)
    bot.community_pve = PveLite(borrowed_env(cfg.pve_from))
    bot._client = bot.community_pve                    # ServiceBot.unload() closes it on rebuild
    bot.community_rows = []
    if not bot.community_pve.enabled and any(r.vmid is not None for r in cfg.resources):
        log.warning("community: no Proxmox access borrowed from %r — outages will say what, not why", cfg.pve_from)
    if cfg.outbox:
        try:
            cfg.outbox.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("community: outbox %s unusable: %s", cfg.outbox, e)
    if cfg.pages:
        try:
            cfg.pages.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            log.warning("community: pages folder %s unusable: %s", cfg.pages, e)
    for path in COGS:
        await bot.load_extension(path)


async def check(env: dict[str, str]) -> tuple[bool, str]:
    resources, problems = parse_resources(env.get("RESOURCES", ""))
    if not resources:
        return False, "RESOURCES has nothing usable" + (f": {problems[0]}" if problems else "")
    pve = PveLite(borrowed_env(env.get("PVE_FROM_SERVICE", "proxmox")))
    try:
        view = await pve.view()
    finally:
        await pve.close()
    why = "can explain outages via Proxmox" if view.ok else f"no Proxmox diagnosis ({view.error})"
    extra = f" · {len(problems)} entr{'y' if len(problems) == 1 else 'ies'} skipped: {problems[0]}" if problems else ""
    return True, f"{len(resources)} resources to watch · {why}{extra}"


SERVICES = [
    ServiceSpec(
        name="community",
        title="Community status",
        description="Public status board, outage posts that say why, and /psa maintenance announcements.",
        group="media",
        settings=make_settings(),
        build=build,
        check=check,
        slash="/psa",                                  # and /link (its own cog)
        default_presence="plex",
        intents=["members", "message_content"],     # role grants; reading what the Minecraft chat bridges relay
    )
]

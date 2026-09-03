"""MediaHub: everything the v1 arr bot kept on itself, shared by every media service of one presence.

The hub owns the clients (`svc`), the one "Media stack" status board, the queue/stall poller and the inbound
webhook routes. The v1 bot creates a hub with every configured client and one `/arr` group. Under v2 the
first media service built on a presence creates the hub (stored as `presence.media_hub`) and loads the cogs
once; every media service then `register()`s its client and gets its own slash group (`/sonarr`, `/plex`, …)
whose commands call into the shared cogs with the app fixed. Alerts and feed events for an app go through the
service that owns it, so per-service alert/feed channels keep working.
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from periscope.bot import admin_only

from .client import Services, build_client
from .config import ARR_APPS, ArrSettings

log = logging.getLogger(__name__)

COGS = ["periscope_arr.cogs.webhooks", "periscope_arr.cogs.queue", "periscope_arr.cogs.media"]
QUEUE_APPS = ("sonarr", "radarr", "lidarr")
TITLES = {"sonarr": "Sonarr", "radarr": "Radarr", "lidarr": "Lidarr", "prowlarr": "Prowlarr",
          "qbittorrent": "qBittorrent", "sabnzbd": "SABnzbd", "plex": "Plex", "jellyfin": "Jellyfin"}
GROUP_DESCRIPTIONS = {
    "sonarr": "Sonarr: TV queue, calendar, search and health",
    "radarr": "Radarr: movie queue, calendar, search and health",
    "lidarr": "Lidarr: music queue, calendar, search and health",
    "prowlarr": "Prowlarr: indexer health",
    "qbittorrent": "qBittorrent transfer status",
    "sabnzbd": "SABnzbd queue status",
    "plex": "Plex: who is watching what",
    "jellyfin": "Jellyfin: who is watching what",
}


class BoardHost:
    """What the shared board sees as `bot`: the owner's channels and settings, plus the state slot every
    service on the presence shares (so the pinned message survives whichever service happens to be built
    first). A v1 bot has no shared slot and keeps using its own state, exactly as before."""

    def __init__(self, hub: "MediaHub"):
        self.hub = hub

    @property
    def state(self):
        return getattr(self.hub.bot, "shared_state", None) or self.hub.bot.state

    @property
    def settings(self):
        return self.hub.bot.settings

    async def get_channel_safe(self, channel_id: int):
        return await self.hub.bot.get_channel_safe(channel_id)


class MediaHub:
    def __init__(self, bot: Any, cfg: ArrSettings | None = None, *, split: bool = False):
        self.bot = bot                     # owner: the v1 ArrBot, or the first media ServiceBot built on the presence
        self.cfg = cfg or ArrSettings()    # shared defaults: VERIFY_SSL, MEDIA_CHANNEL_ID, ARR_QUEUE_STALL_MIN
        self.svc = Services(self.cfg)      # v1: every configured client; v2: filled by register()
        self.split = split                 # v2: one slash group per service instead of the single /arr
        self.services: dict[str, Any] = {}  # v2: service name -> its ServiceBot
        self.groups: dict[str, app_commands.Group] = {}
        self.loaded = not split            # v1: LabBot loads the cogs itself
        self.board_host = BoardHost(self)
        # the cogs announce themselves here when they load
        self.webhooks_cog: Any = None
        self.queue_cog: Any = None
        self.media_cog: Any = None

    @classmethod
    def for_bot(cls, bot: Any, cfg: ArrSettings) -> "MediaHub":
        """The presence-wide hub for a v2 service, created by the first media service built there."""
        hub = getattr(bot, "media_hub", None)
        if hub is None:
            hub = cls(bot, cfg.shared_only(), split=True)
            host = getattr(bot, "presence", None)
            if host is None:
                host = bot
            host.media_hub = hub
            log.info("[%s] media hub created on presence %s", bot.name, getattr(host, "name", "?"))
        return hub

    # ----- lookups used by the cogs ------------------------------------------------------------

    def bot_for(self, name: str) -> Any:
        """The bot that owns media service `name` (v2: its ServiceBot; v1 / unknown: the owner)."""
        return self.services.get(name, self.bot)

    def alerts_for(self, name: str):
        return self.bot_for(name).alerts

    def cfg_for(self, name: str) -> ArrSettings:
        own = getattr(self.services.get(name), "media_cfg", None)
        return own if own is not None else self.cfg

    def media_channel_for(self, name: str) -> int | None:
        """Feed channel for an app's events: its own MEDIA_CHANNEL_ID, else the hub owner's, else the owning
        service's ALERT_CHANNEL_ID."""
        return (self.cfg_for(name).media_channel_id or self.cfg.media_channel_id
                or self.bot_for(name).settings.alert_channel_id)

    @property
    def lab_name(self) -> str:
        return self.bot.lab_name

    def webhook_apps(self) -> list[str]:
        """Which /<app> routes to expose: v1 always all four, v2 the *arr apps registered so far."""
        return list(ARR_APPS) if not self.split else [a for a in ARR_APPS if a in self.svc.arr]

    async def close(self) -> None:
        await self.svc.close()

    # ----- v2 registration -----------------------------------------------------------------------

    async def register(self, sb: Any, name: str, cfg: ArrSettings) -> None:
        client = build_client(name, cfg)
        if client is None:
            raise RuntimeError(f"{name}: URL is not set")
        old = self.svc.get(name)
        if old is not None:
            try:
                await old.close()
            except Exception:  # noqa: BLE001
                pass
        self.svc.add(name, client)
        self.services[name] = sb
        sb.media_cfg = cfg
        sb.media_hub = self
        if not self.loaded:
            await self._load()
        if name in ARR_APPS and self.webhooks_cog is not None:
            self.webhooks_cog.ensure_route(name)
        group = self.group_for(name)
        sb.tree.add_command(group, override=True)
        self.groups[name] = group
        log.info("[%s] registered with the media hub (%s)", name, ", ".join(self.svc.names()))

    async def _load(self) -> None:
        self.loaded = True
        for path in COGS:
            await self.bot.load_extension(path)

    # ----- per-service slash groups ---------------------------------------------------------------

    def group_for(self, name: str) -> app_commands.Group:
        """`/<name>` with the v1 `/arr` commands that apply to this app, each pinned to it."""
        hub = self  # cogs are looked up per call so a reloaded cog is picked up
        title = TITLES.get(name, name)
        group = app_commands.Group(name=name, description=GROUP_DESCRIPTIONS.get(name, title))

        @group.command(name="board", description="The shared Media stack board")
        async def board(interaction: discord.Interaction):
            await hub.media_cog.board_cmd(interaction)

        if name in QUEUE_APPS:
            @group.command(name="queue", description=f"{title}: active downloads with progress")
            async def queue_cmd(interaction: discord.Interaction):
                await hub.queue_cog.queue(interaction, app=name)

            @group.command(name="remove", description=f"{title}: remove an item from the download queue (admin)")
            @app_commands.describe(queue_id=f"Queue item id (shown as #id in /{name} queue)",
                                   blocklist="Also blocklist the release so it isn't grabbed again")
            @admin_only()
            async def remove_cmd(interaction: discord.Interaction, queue_id: int, blocklist: bool = False):
                await hub.queue_cog.remove(interaction, name, queue_id, blocklist)

            @group.command(name="calendar", description=f"{title}: upcoming releases")
            @app_commands.describe(days="How many days ahead (1–30)")
            async def calendar_cmd(interaction: discord.Interaction, days: app_commands.Range[int, 1, 30] = 7):
                await hub.queue_cog.calendar(interaction, days, app=name)

            @group.command(name="search", description=f"{title}: look up a title (read-only)")
            @app_commands.describe(term="Title to look up")
            async def search_cmd(interaction: discord.Interaction, term: str):
                await hub.queue_cog.search(interaction, name, term)

        if name in ARR_APPS:
            @group.command(name="health", description=f"{title}: health messages"
                           + (" and indexer status" if name == "prowlarr" else ""))
            async def health_cmd(interaction: discord.Interaction):
                await hub.queue_cog.health(interaction, app=name)

        if name in ("qbittorrent", "sabnzbd"):
            @group.command(name="status", description=f"{title}: transfer summary")
            async def status_cmd(interaction: discord.Interaction):
                await hub.queue_cog.clients(interaction, client=name)

        if name in ("plex", "jellyfin"):
            @group.command(name="nowplaying", description=f"Who is watching what on {title}")
            async def nowplaying_cmd(interaction: discord.Interaction):
                await hub.media_cog.nowplaying(interaction, server=name)

        return group

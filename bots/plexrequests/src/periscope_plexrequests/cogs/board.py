"""Live status board in STATUS_CHANNEL: who is streaming what, Radarr/Sonarr queues with ETAs, disk space.
One embed, edited in place every minute — the core StatusBoard keeps it a single message (it adopts the
standalone bot's old board and deletes stray copies instead of posting again)."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

import discord
from discord.ext import commands, tasks
from periscope.statusboard import StatusBoard

from ..common import BOARD_KIND, PLEX_GOLD, fmt_bytes, resolve_channel
from ..context import PlexRequests

log = logging.getLogger(__name__)

STATUS_MESSAGE_KEY = "status_message_id"   # the standalone bot's key, kept in sync for /requests plexstats etc.
BOARD_KEY = "plex-requests"
INTERVAL_S = 60
QUEUE_LABEL = {"radarr": "🎬 Radarr queue", "sonarr": "📺 Sonarr queue"}


# ----- the board as data + drawing (pure, so the Messages page can preview it from sample data) -------------

def board_ctx(streams: list[str] | None, queues: list[dict[str, Any]],
              disks: list[dict[str, Any]]) -> dict[str, Any]:
    """The board's facts as plain values: what `board_embed` draws and what a plexrequests.board template can use.

    `streams` is what Plex reported, one line per stream (None when Plex did not answer); `queues` has one entry
    per configured Radarr / Sonarr — app · ok · total · top (the first few titles with their time left) · error;
    `disks` the apps' root folders — path · free · total, in bytes.
    """
    return {"plex_ok": streams is not None, "streams": list(streams or []), "queues": queues, "disks": disks,
            "interval_s": INTERVAL_S}


def board_embed(data: dict[str, Any], plex_name: str, now: datetime | None = None) -> discord.Embed:
    """The live status board from `board_ctx()` data (the message edited in place in STATUS_CHANNEL)."""
    e = discord.Embed(title=f"📊  {plex_name} — live status", colour=discord.Colour.from_str(PLEX_GOLD))
    if not data["plex_ok"]:
        e.add_field(name="🎞️ Now streaming", value="*(Plex not reachable)*", inline=False)
    else:
        streams = data["streams"]
        e.add_field(name=f"🎞️ Now streaming — {len(streams)}", value="\n".join(streams[:6]) or "Nobody right now",
                    inline=False)
    for q in data["queues"]:
        label = QUEUE_LABEL.get(q["app"], q["app"])
        if q["ok"]:
            e.add_field(name=f"{label} — {q['total']}", value="\n".join(q["top"]) or "Empty", inline=True)
        else:
            e.add_field(name=label, value=f"*(unreachable: {q['error'][:40]})*", inline=True)
    disks = [f"`{d['path']}` — {fmt_bytes(d['free'])} free of {fmt_bytes(d['total'])}" for d in data["disks"][:5]]
    if disks:
        e.add_field(name="💾 Disk", value="\n".join(disks), inline=False)
    e.set_footer(text=f"{plex_name} • refreshes every {data['interval_s']}s")
    e.timestamp = now or discord.utils.utcnow()
    return e


class BoardCog(commands.Cog):
    def __init__(self, bot: Any):
        self.bot = bot
        self.ctx: PlexRequests = bot.plexreq
        self.cfg = self.ctx.cfg
        # channel resolved on every tick (name or id); customised as plexrequests.board on the Messages page
        self.board = StatusBoard(bot, key=BOARD_KEY, channel_id=0, kind=BOARD_KIND)
        legacy = self.ctx.records.message_id(STATUS_MESSAGE_KEY)
        if legacy and not self.board._state.get("message_id"):
            self.board._state.set("message_id", legacy)               # the standalone bot's board carries on

    async def cog_load(self) -> None:
        if self.cfg.status_channel:
            self.status_board.start()

    async def cog_unload(self) -> None:
        self.status_board.cancel()

    async def board_data(self) -> dict[str, Any]:
        """Ask Plex for its streams and Radarr / Sonarr for their queues and disks; a service that does not
        answer shows as such on the board instead of failing it."""
        try:
            streams = await asyncio.to_thread(self.ctx.plex.sessions)
        except Exception as ex:  # noqa: BLE001
            log.warning("status board: plex sessions failed: %s", ex)
            streams = None
        backend = self.ctx.backend
        clients = [(app, c) for app, c in (("radarr", backend.radarr), ("sonarr", backend.sonarr)) if c is not None]
        queues: list[dict[str, Any]] = []
        for app, client in clients:
            try:
                total, top = await client.queue_summary()
                queues.append({"app": app, "ok": True, "total": total, "top": list(top), "error": ""})
            except Exception as ex:  # noqa: BLE001
                queues.append({"app": app, "ok": False, "total": 0, "top": [], "error": str(ex)})
        disks: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        for _, client in clients:
            try:
                for path, free, total in await client.disk_space():
                    if path in seen_paths or not total:
                        continue
                    seen_paths.add(path)
                    disks.append({"path": path, "free": free, "total": total})
            except Exception:  # noqa: BLE001
                pass
        return board_ctx(streams, queues, disks)

    @tasks.loop(seconds=INTERVAL_S)
    async def status_board(self) -> None:
        channel = resolve_channel(self.bot, self.cfg.status_channel, self.cfg.guild_id)
        if channel is None:
            return
        try:
            data = await self.board_data()
        except Exception:  # noqa: BLE001
            log.exception("status board build failed")
            return
        self.board.channel_id = channel.id
        try:
            msg = await self.board.render(board_embed(data, self.cfg.plex_name), pin=False, ctx=data)
        except discord.Forbidden:
            log.error("cannot post the status board in #%s", getattr(channel, "name", channel))
            return
        except discord.HTTPException as e:
            log.warning("status board update failed: %s", e)
            return
        if msg is not None and self.ctx.records.message_id(STATUS_MESSAGE_KEY) != msg.id:
            self.ctx.records.set_message_id(STATUS_MESSAGE_KEY, msg.id)

    @status_board.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot: Any) -> None:
    await bot.add_cog(BoardCog(bot))

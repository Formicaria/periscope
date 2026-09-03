"""/arr nowplaying (Plex + Jellyfin) and the live "Media stack" status board, one per media hub."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import discord
from discord.ext import commands, tasks

from periscope import RefreshView, Severity, StatusBoard, human_bytes, lab_embed, progress_bar, status_dot, truncate

from ..client import note_reachability
from . import register

log = logging.getLogger(__name__)

MediaServer = Literal["plex", "jellyfin"]


@dataclass
class Stream:
    server: str
    user: str
    title: str
    player: str
    pct: float
    method: str  # "direct" | "transcode"
    paused: bool = False

    def line(self) -> str:
        icon = "⏸️" if self.paused else "▶️"
        badge = "🔁 transcode" if self.method == "transcode" else "⚡ direct"
        return (f"{icon} **{truncate(self.title, 90)}**\n"
                f"`{progress_bar(self.pct, 10)}` {self.user} · {self.player} · {badge} · {self.server}")


def parse_plex_session(m: dict) -> Stream:
    typ = m.get("type")
    if typ == "episode":
        title = (f"{m.get('grandparentTitle', '?')} – S{int(m.get('parentIndex') or 0):02d}"
                 f"E{int(m.get('index') or 0):02d} {m.get('title', '')}").strip()
    elif typ == "track":
        title = f"{m.get('grandparentTitle', '?')} – {m.get('title', '?')}"
    else:
        title = f"{m.get('title', '?')}" + (f" ({m['year']})" if m.get("year") else "")
    duration = float(m.get("duration") or 0)
    offset = float(m.get("viewOffset") or 0)
    pct = offset / duration * 100 if duration else 0.0
    player = m.get("Player") or {}
    ts = m.get("TranscodeSession") or {}
    transcode = any(ts.get(k) == "transcode" for k in ("videoDecision", "audioDecision"))
    return Stream(server="Plex", user=(m.get("User") or {}).get("title", "?"), title=title,
                  player=player.get("product") or player.get("title") or "?", pct=pct,
                  method="transcode" if transcode else "direct", paused=player.get("state") == "paused")


def parse_jellyfin_session(s: dict) -> Stream | None:
    item = s.get("NowPlayingItem")
    if not item:
        return None
    typ = item.get("Type")
    if typ == "Episode":
        title = (f"{item.get('SeriesName', '?')} – S{int(item.get('ParentIndexNumber') or 0):02d}"
                 f"E{int(item.get('IndexNumber') or 0):02d} {item.get('Name', '')}").strip()
    elif typ == "Audio":
        title = f"{(item.get('Artists') or ['?'])[0]} – {item.get('Name', '?')}"
    else:
        title = f"{item.get('Name', '?')}" + (f" ({item['ProductionYear']})" if item.get("ProductionYear") else "")
    ps = s.get("PlayState") or {}
    runtime = float(item.get("RunTimeTicks") or 0)
    pos = float(ps.get("PositionTicks") or 0)
    pct = pos / runtime * 100 if runtime else 0.0
    method = "transcode" if (ps.get("PlayMethod") == "Transcode" or s.get("TranscodingInfo")) else "direct"
    return Stream(server="Jellyfin", user=s.get("UserName") or "?", title=title,
                  player=s.get("Client") or s.get("DeviceName") or "?", pct=pct, method=method,
                  paused=bool(ps.get("IsPaused")))


def sum_diskspace(entries: list[dict]) -> tuple[float, float]:
    """Sum (free, total) over unique paths from one or more `diskspace` responses."""
    seen: dict[str, tuple[float, float]] = {}
    for d in entries:
        path = d.get("path") or d.get("label") or ""
        if path and path not in seen:
            seen[path] = (float(d.get("freeSpace") or 0), float(d.get("totalSpace") or 0))
    return sum(f for f, _ in seen.values()), sum(t for _, t in seen.values())


class Media(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.hub = bot.media_hub
        self.hub.media_cog = self
        self.svc = self.hub.svc
        self.board = StatusBoard(self.hub.board_host, key="arr")
        self.view = RefreshView(self.build_board, custom_id="periscope_arr:refresh")
        bot.add_view(self.view)
        if not self.hub.split:  # v1: under /arr; v2: the hub builds /plex nowplaying, /jellyfin nowplaying, /<app> board
            register(bot, ("nowplaying", "Who is watching what on Plex / Jellyfin", self.nowplaying))
        self.status_loop.change_interval(seconds=bot.settings.status_interval_s)
        self.status_loop.start()

    async def cog_unload(self):
        self.status_loop.cancel()

    # ----- streams ---------------------------------------------------------------------

    async def _streams(self, server: str | None = None) -> tuple[list[Stream], list[str]]:
        streams: list[Stream] = []
        errors: list[str] = []
        if self.svc.plex and server in (None, "plex"):
            try:
                streams += [parse_plex_session(m) for m in await self.svc.plex.sessions()]
                await note_reachability(self.bot, "plex", True)
            except Exception as e:
                errors.append(f"plex: {e}")
                await note_reachability(self.bot, "plex", False, str(e))
        if self.svc.jellyfin and server in (None, "jellyfin"):
            try:
                for s in await self.svc.jellyfin.sessions():
                    st = parse_jellyfin_session(s)
                    if st:
                        streams.append(st)
                await note_reachability(self.bot, "jellyfin", True)
            except Exception as e:
                errors.append(f"jellyfin: {e}")
                await note_reachability(self.bot, "jellyfin", False, str(e))
        return streams, errors

    @discord.app_commands.describe(server="Limit to one media server (default: both)")
    async def nowplaying(self, interaction: discord.Interaction, server: MediaServer | None = None):
        plex = self.svc.plex if server in (None, "plex") else None
        jellyfin = self.svc.jellyfin if server in (None, "jellyfin") else None
        if not plex and not jellyfin:
            msg = (f"🚫 {server} is not configured (set {server.upper()}_URL)." if server
                   else "🚫 No media server configured (PLEX_URL / JELLYFIN_URL).")
            await interaction.response.send_message(msg, ephemeral=True)
            return
        await interaction.response.defer()
        streams, errors = await self._streams(server)
        body = "\n\n".join(s.line() for s in streams) or "Nobody is watching anything right now."
        if errors:
            body += "\n\n" + "\n".join(f"🔴 {truncate(e, 150)}" for e in errors)
        e = lab_embed(f"Now playing · {len(streams)} stream{'s' if len(streams) != 1 else ''}", truncate(body, 4000),
                      severity=Severity.CRITICAL if errors else Severity.INFO, lab_name=self.bot.lab_name)
        await interaction.followup.send(embed=e)

    async def board_cmd(self, interaction: discord.Interaction):
        """The shared Media stack board on demand (v2: `/<service> board`)."""
        await interaction.response.defer()
        await interaction.followup.send(embed=await self.build_board())

    # ----- status board ----------------------------------------------------------------

    async def _probe(self, name: str, coro) -> tuple[bool, object]:
        try:
            result = await asyncio.wait_for(coro, timeout=15)
        except Exception as e:
            await note_reachability(self.bot, name, False, str(e))
            return False, e
        await note_reachability(self.bot, name, True)
        return True, result

    async def build_board(self) -> discord.Embed:
        dots: list[str] = []
        queues: list[str] = []
        disk_entries: list[dict] = []
        worst_down = False

        for app, client in self.svc.arr.items():
            if app == "prowlarr":
                ok, res = await self._probe(app, client.health())
                dots.append(f"{status_dot(ok)} {app}" + (f" ({len(res)} issues)" if ok and res else ""))
            else:
                ok, res = await self._probe(app, client.queue())
                dots.append(f"{status_dot(ok)} {app}")
                if ok:
                    active = sum(1 for i in res if i.get("status") == "downloading")
                    queues.append(f"{app}: **{len(res)}** queued, {active} downloading")
                    try:
                        disk_entries += await client.diskspace()
                    except Exception as e:
                        log.debug("%s diskspace failed: %s", app, e)
            worst_down |= not ok

        speeds: list[str] = []
        if self.svc.qbit:
            ok, res = await self._probe("qbittorrent", self.svc.qbit.transfer_info())
            dots.append(f"{status_dot(ok)} qbittorrent")
            if ok:
                speeds.append(f"qBit ⬇️ {human_bytes(res.get('dl_info_speed'))}/s ⬆️ {human_bytes(res.get('up_info_speed'))}/s")
            worst_down |= not ok
        if self.svc.sab:
            ok, res = await self._probe("sabnzbd", self.svc.sab.queue())
            dots.append(f"{status_dot(ok)} sabnzbd")
            if ok:
                speeds.append(f"SAB ⬇️ {human_bytes(float(res.get('kbpersec') or 0) * 1024)}/s · {res.get('noofslots', 0)} active")
            worst_down |= not ok

        streams, errors = await self._streams()
        if self.svc.plex:
            dots.append(f"{status_dot(not any(e.startswith('plex') for e in errors))} plex")
        if self.svc.jellyfin:
            dots.append(f"{status_dot(not any(e.startswith('jellyfin') for e in errors))} jellyfin")
        worst_down |= bool(errors)

        sev = Severity.CRITICAL if worst_down else Severity.OK
        e = lab_embed("Media stack", "  ".join(dots), severity=sev, lab_name=self.bot.lab_name)
        if queues:
            e.add_field(name="Queues", value="\n".join(queues), inline=False)
        if speeds:
            e.add_field(name="Transfer", value="\n".join(speeds), inline=False)
        if self.svc.plex or self.svc.jellyfin:
            lines = [f"{'⏸️' if s.paused else '▶️'} {truncate(s.title, 60)} — {s.user}" for s in streams[:8]]
            if len(streams) > 8:
                lines.append(f"… and {len(streams) - 8} more")
            e.add_field(name=f"Streams ({len(streams)})", value="\n".join(lines) or "none", inline=False)
        if disk_entries:
            free, total = sum_diskspace(disk_entries)
            used_pct = (total - free) / total * 100 if total else 0
            e.add_field(name="Disk", value=f"`{progress_bar(used_pct)}` {human_bytes(free)} free of {human_bytes(total)}",
                        inline=False)
        return e

    @tasks.loop(seconds=60)
    async def status_loop(self):
        if not self.bot.settings.status_channel_id:
            return
        try:
            await self.board.render(await self.build_board(), view=self.view)
        except Exception:
            log.exception("status board render failed")

    @status_loop.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @status_loop.error
    async def _loop_error(self, err: BaseException):
        log.exception("status loop crashed, restarting", exc_info=err)
        self.status_loop.restart()


async def setup(bot):
    await bot.add_cog(Media(bot))

"""/arr nowplaying (Plex + Jellyfin) and the live "Media stack" status board, one per media hub.

The board is the `media.board` message kind: `board_ctx` turns what the probes returned into plain facts,
`board_embed` draws them, and the hub owner's `bot.messages` (through the hub's BoardHost) applies the user's
template on render.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Literal

import discord
from discord.ext import commands, tasks

from periscope import RefreshView, Severity, StatusBoard, human_bytes, lab_embed, progress_bar, status_dot, truncate

from ..client import note_reachability
from . import register

log = logging.getLogger(__name__)

MediaServer = Literal["plex", "jellyfin"]
BOARD_KIND = "media.board"    # the message kind the board is customised under (Messages page)
MEDIA_SERVERS = ("plex", "jellyfin")


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


# ----- the board as data + drawing (pure, so the Messages page can preview it from sample data) -------------

def board_ctx(results: dict[str, tuple[bool, Any]], streams: list[Stream], errors: list[str],
              disk_entries: list[dict], *, plex: bool, jellyfin: bool) -> dict[str, Any]:
    """The board's facts as plain values: what `board_embed` draws and what a media.board template can use.

    `results` maps each probed service, in board order, to (answered, what it said): the queue for Sonarr /
    Radarr / Lidarr, health messages for Prowlarr, transfer info for qBittorrent, the queue summary for SABnzbd —
    or the error when it did not answer. `streams` and `errors` are what `_streams()` found on the media servers
    `plex` / `jellyfin` say are configured; `disk_entries` are the apps' `diskspace` rows.
    """
    services: list[dict[str, Any]] = []
    queues: list[dict[str, Any]] = []
    qbit: dict[str, Any] = {}
    sab: dict[str, Any] = {}
    for name, (ok, res) in results.items():
        entry = {"name": name, "ok": ok, "error": "" if ok else truncate(str(res), 200), "issues": 0}
        if ok and name == "prowlarr":
            entry["issues"] = len(res) if res else 0
        elif ok and name == "qbittorrent":
            qbit = {"down": res.get("dl_info_speed"), "up": res.get("up_info_speed")}
        elif ok and name == "sabnzbd":
            sab = {"down": float(res.get("kbpersec") or 0) * 1024, "active": res.get("noofslots", 0)}
        elif ok:
            queues.append({"app": name, "queued": len(res),
                           "downloading": sum(1 for i in res if i.get("status") == "downloading")})
        services.append(entry)
    for name, on in (("plex", plex), ("jellyfin", jellyfin)):
        if on:
            error = next((e for e in errors if e.startswith(name)), "")
            services.append({"name": name, "ok": not error, "error": error.split(": ", 1)[-1] if error else "",
                             "issues": 0})
    disk: dict[str, Any] = {}
    if disk_entries:
        free, total = sum_diskspace(disk_entries)
        disk = {"free": free, "total": total, "used_pct": (total - free) / total * 100 if total else 0}
    return {
        "services": services, "down": [s["name"] for s in services if not s["ok"]], "queues": queues,
        "qbittorrent": qbit, "sabnzbd": sab,
        "streams": [{"server": s.server, "user": s.user, "title": s.title, "player": s.player, "pct": round(s.pct, 1),
                     "method": s.method, "paused": s.paused} for s in streams],
        "disk": disk,
    }


def board_embed(data: dict[str, Any], lab_name: str | None) -> discord.Embed:
    """The Media stack board from `board_ctx()` data (the pinned message in STATUS_CHANNEL_ID)."""
    dots = [f"{status_dot(s['ok'])} {s['name']}" + (f" ({s['issues']} issues)" if s["ok"] and s["issues"] else "")
            for s in data["services"]]
    sev = Severity.CRITICAL if data["down"] else Severity.OK
    e = lab_embed("Media stack", "  ".join(dots), severity=sev, lab_name=lab_name)
    if data["queues"]:
        queues = [f"{q['app']}: **{q['queued']}** queued, {q['downloading']} downloading" for q in data["queues"]]
        e.add_field(name="Queues", value="\n".join(queues), inline=False)
    speeds: list[str] = []
    if data["qbittorrent"]:
        q = data["qbittorrent"]
        speeds.append(f"qBit ⬇️ {human_bytes(q['down'])}/s ⬆️ {human_bytes(q['up'])}/s")
    if data["sabnzbd"]:
        s = data["sabnzbd"]
        speeds.append(f"SAB ⬇️ {human_bytes(s['down'])}/s · {s['active']} active")
    if speeds:
        e.add_field(name="Transfer", value="\n".join(speeds), inline=False)
    if any(s["name"] in MEDIA_SERVERS for s in data["services"]):
        streams = data["streams"]
        lines = [f"{'⏸️' if s['paused'] else '▶️'} {truncate(s['title'], 60)} — {s['user']}" for s in streams[:8]]
        if len(streams) > 8:
            lines.append(f"… and {len(streams) - 8} more")
        e.add_field(name=f"Streams ({len(streams)})", value="\n".join(lines) or "none", inline=False)
    if data["disk"]:
        d = data["disk"]
        e.add_field(name="Disk", inline=False,
                    value=f"`{progress_bar(d['used_pct'])}` {human_bytes(d['free'])} free of {human_bytes(d['total'])}")
    return e


class Media(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.hub = bot.media_hub
        self.hub.media_cog = self
        self.svc = self.hub.svc
        self.board = StatusBoard(self.hub.board_host, key="arr", kind=BOARD_KIND)
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

    async def board_data(self) -> dict[str, Any]:
        """Probe every configured service and reduce the answers to the board's facts (`board_ctx`)."""
        results: dict[str, tuple[bool, Any]] = {}
        disk_entries: list[dict] = []
        for app, client in self.svc.arr.items():
            if app == "prowlarr":
                results[app] = await self._probe(app, client.health())
            else:
                ok, res = results[app] = await self._probe(app, client.queue())
                if ok:
                    try:
                        disk_entries += await client.diskspace()
                    except Exception as e:
                        log.debug("%s diskspace failed: %s", app, e)
        if self.svc.qbit:
            results["qbittorrent"] = await self._probe("qbittorrent", self.svc.qbit.transfer_info())
        if self.svc.sab:
            results["sabnzbd"] = await self._probe("sabnzbd", self.svc.sab.queue())
        streams, errors = await self._streams()
        return board_ctx(results, streams, errors, disk_entries,
                         plex=bool(self.svc.plex), jellyfin=bool(self.svc.jellyfin))

    async def build_board(self) -> discord.Embed:
        """The board as it should look now, through the user's template — the 🔄 button's and `/<app> board`'s
        builder, so they match the scheduled render (which StatusBoard customises itself). A switched-off board
        shows plain here; the next scheduled render takes it down."""
        data = await self.board_data()
        embed = board_embed(data, self.bot.lab_name)
        return self.bot.messages.apply(BOARD_KIND, embed, data) or embed

    @tasks.loop(seconds=60)
    async def status_loop(self):
        if not self.bot.settings.status_channel_id:
            return
        try:
            data = await self.board_data()
            await self.board.render(board_embed(data, self.bot.lab_name), ctx=data, view=self.view)
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

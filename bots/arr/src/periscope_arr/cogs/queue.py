"""/arr queue|remove|calendar|search|health|clients + the stalled-download watcher."""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Literal

import discord
from discord.ext import commands, tasks

from periscope import Alert, ConfirmView, PaginatorView, Severity, human_bytes, lab_embed, progress_bar, truncate
from periscope.bot import admin_only

from ..client import note_reachability
from . import register

log = logging.getLogger(__name__)

QueueApp = Literal["sonarr", "radarr", "lidarr"]
STATUS_ICON = {"downloading": "⬇️", "completed": "📦", "queued": "🕒", "paused": "⏸️", "warning": "⚠️",
               "failed": "❌", "delay": "⏳", "importPending": "📥", "importing": "📥"}


# ----- pure helpers (unit tested) -----------------------------------------------------------

def queue_item_name(app: str, item: dict) -> str:
    if app == "sonarr" and item.get("series"):
        ep = item.get("episode") or {}
        code = f" S{ep.get('seasonNumber', 0):02d}E{ep.get('episodeNumber', 0):02d}" if ep else ""
        return f"{item['series'].get('title', '?')}{code}"
    if app == "radarr" and item.get("movie"):
        m = item["movie"]
        return f"{m.get('title', '?')} ({m.get('year')})" if m.get("year") else m.get("title", "?")
    if app == "lidarr" and item.get("artist"):
        return f"{item['artist'].get('artistName', '?')} – {(item.get('album') or {}).get('title', '?')}"
    return item.get("title") or "?"


def queue_pct(item: dict) -> float:
    size = float(item.get("size") or 0)
    left = float(item.get("sizeleft") or 0)
    if size <= 0:
        return 0.0
    return max(0.0, min(100.0, (size - left) / size * 100))


def format_queue_item(app: str, item: dict) -> str:
    status = item.get("status") or "unknown"
    tds = item.get("trackedDownloadState") or ""
    icon = STATUS_ICON.get(tds if tds in STATUS_ICON else status, "•")
    size = float(item.get("size") or 0)
    left = float(item.get("sizeleft") or 0)
    eta = item.get("timeleft") or "—"
    line = (f"{icon} **{truncate(queue_item_name(app, item), 80)}** `#{item.get('id')}`\n"
            f"`{progress_bar(queue_pct(item))}` {human_bytes(size - left)} / {human_bytes(size)} · {status}"
            f"{' / ' + tds if tds and tds != status else ''} · ETA {eta}")
    msgs = item.get("statusMessages") or []
    if msgs and item.get("trackedDownloadStatus") == "warning":
        first = msgs[0].get("messages") or [msgs[0].get("title") or ""]
        line += f"\n⚠️ {truncate(str(first[0]), 120)}"
    return line


def calendar_entries(app: str, items: list[dict], start: dt.date, end: dt.date) -> list[tuple[dt.date, str]]:
    """Flatten calendar payloads into (day, text) tuples, only inside [start, end]."""
    out: list[tuple[dt.date, str]] = []

    def day_of(value: str | None) -> dt.date | None:
        if not value:
            return None
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None

    for it in items:
        if app == "sonarr":
            d = day_of(it.get("airDateUtc") or it.get("airDate"))
            series = (it.get("series") or {}).get("title", "?")
            text = f"📺 {series} S{it.get('seasonNumber', 0):02d}E{it.get('episodeNumber', 0):02d}"
            if it.get("title"):
                text += f" – {it['title']}"
            if it.get("hasFile"):
                text += " ✅"
            if d:
                out.append((d, text))
        elif app == "radarr":
            title = f"🎬 {it.get('title', '?')}" + (f" ({it['year']})" if it.get("year") else "")
            for key, tag in (("inCinemas", "cinema"), ("digitalRelease", "digital"), ("physicalRelease", "physical")):
                d = day_of(it.get(key))
                if d and start <= d <= end:
                    out.append((d, f"{title} · {tag}" + (" ✅" if it.get("hasFile") else "")))
        elif app == "lidarr":
            d = day_of(it.get("releaseDate"))
            artist = (it.get("artist") or {}).get("artistName", "?")
            if d:
                out.append((d, f"🎵 {artist} – {it.get('title', '?')}"))
    return [(d, t) for d, t in out if start <= d <= end]


def group_by_day(entries: list[tuple[dt.date, str]]) -> list[tuple[dt.date, list[str]]]:
    days: dict[dt.date, list[str]] = {}
    for d, t in sorted(entries, key=lambda x: (x[0], x[1])):
        days.setdefault(d, []).append(t)
    return sorted(days.items())


class StallTracker:
    """Remembers sizeleft per queue item; reports items that haven't progressed for `stall_s`."""

    def __init__(self, stall_s: float):
        self.stall_s = stall_s
        self._seen: dict[str, tuple[float, float]] = {}  # key -> (sizeleft, unchanged_since)
        self.stalled: set[str] = set()

    def update(self, app: str, items: list[dict], now: float | None = None) -> tuple[list[tuple[str, dict]], list[str]]:
        """Returns (newly_stalled [(key, item)], recovered keys)."""
        now = time.time() if now is None else now
        new_stalled: list[tuple[str, dict]] = []
        recovered: list[str] = []
        current: set[str] = set()
        for it in items:
            key = f"{app}:{it.get('id')}"
            current.add(key)
            left = float(it.get("sizeleft") or 0)
            downloading = (it.get("status") == "downloading") and left > 0
            prev = self._seen.get(key)
            if prev is None or prev[0] != left or not downloading:
                self._seen[key] = (left, now)
                if key in self.stalled:
                    self.stalled.discard(key)
                    recovered.append(key)
                continue
            if now - prev[1] >= self.stall_s and key not in self.stalled:
                self.stalled.add(key)
                new_stalled.append((key, it))
        for key in [k for k in self._seen if k.startswith(f"{app}:") and k not in current]:
            self._seen.pop(key)
            if key in self.stalled:
                self.stalled.discard(key)
                recovered.append(key)
        return new_stalled, recovered


# ----- cog --------------------------------------------------------------------------------

class Queue(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.svc = bot.svc
        self.tracker = StallTracker(bot.cfg.queue_stall_min * 60)
        register(bot,
                 ("queue", "Active downloads with progress", self.queue),
                 ("remove", "Remove an item from a download queue (admin)", self.remove),
                 ("calendar", "Upcoming episodes, movies and albums", self.calendar),
                 ("search", "Look up a series / movie / artist (read-only)", self.search),
                 ("health", "Health messages from every app + Prowlarr indexer status", self.health),
                 ("clients", "qBittorrent / SABnzbd transfer summary", self.clients))
        self.stall_watch.start()

    async def cog_unload(self):
        self.stall_watch.cancel()

    def _arr(self, app: str):
        return self.svc.arr.get(app)

    async def _apps_or_error(self, interaction: discord.Interaction, app: str | None, allowed=("sonarr", "radarr", "lidarr")):
        apps = [a for a in allowed if a in self.svc.arr and (app is None or a == app)]
        if not apps:
            msg = (f"🚫 {app} is not configured (set {app.upper()}_URL)." if app
                   else "🚫 No Sonarr/Radarr/Lidarr configured (set SONARR_URL, RADARR_URL or LIDARR_URL).")
            await interaction.response.send_message(msg, ephemeral=True)
        return apps

    # ----- /arr queue ------------------------------------------------------------------

    @discord.app_commands.describe(app="Limit to one app (default: all configured)")
    async def queue(self, interaction: discord.Interaction, app: QueueApp | None = None):
        apps = await self._apps_or_error(interaction, app)
        if not apps:
            return
        await interaction.response.defer()
        lines: list[str] = []
        for a in apps:
            try:
                items = await self._arr(a).queue()
            except Exception as e:
                lines.append(f"🔴 **{a}**: {truncate(str(e), 150)}")
                continue
            for it in items:
                lines.append(f"[{a}] " + format_queue_item(a, it))
        if not lines:
            await interaction.followup.send(embed=lab_embed("Download queue", "Nothing in the queue. 🎉",
                                                            severity=Severity.OK, lab_name=self.bot.lab_name))
            return
        pages = []
        for i in range(0, len(lines), 6):
            e = lab_embed("Download queue", "\n\n".join(lines[i:i + 6]), lab_name=self.bot.lab_name)
            pages.append(e)
        view = PaginatorView(pages, user_id=interaction.user.id)
        await interaction.followup.send(embed=pages[0], view=view if len(pages) > 1 else None)

    # ----- /arr remove -----------------------------------------------------------------

    @discord.app_commands.describe(app="Which app", queue_id="Queue item id (shown as #id in /arr queue)",
                                   blocklist="Also blocklist the release so it isn't grabbed again")
    @admin_only()
    async def remove(self, interaction: discord.Interaction, app: QueueApp, queue_id: int, blocklist: bool = False):
        client = self._arr(app)
        if client is None:
            await interaction.response.send_message(f"🚫 {app} is not configured.", ephemeral=True)
            return
        view = ConfirmView(interaction.user.id)
        await interaction.response.send_message(
            f"Remove queue item `#{queue_id}` from **{app}** (removeFromClient=true, blocklist={str(blocklist).lower()})?",
            view=view, ephemeral=True)
        await view.wait()
        if not view.value:
            await interaction.edit_original_response(content="Cancelled.", view=None)
            return
        try:
            await client.queue_delete(queue_id, remove_from_client=True, blocklist=blocklist)
            await client.command("RefreshMonitoredDownloads")
        except Exception as e:
            await interaction.edit_original_response(content=f"❌ Failed: {truncate(str(e), 300)}", view=None)
            return
        await interaction.edit_original_response(content=f"🗑️ Removed `#{queue_id}` from {app}"
                                                         f"{' and blocklisted it' if blocklist else ''}.", view=None)

    # ----- /arr calendar ---------------------------------------------------------------

    @discord.app_commands.describe(days="How many days ahead (1–30)")
    async def calendar(self, interaction: discord.Interaction, days: discord.app_commands.Range[int, 1, 30] = 7):
        apps = await self._apps_or_error(interaction, None)
        if not apps:
            return
        await interaction.response.defer()
        now = dt.datetime.now(dt.timezone.utc)
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + dt.timedelta(days=days)
        entries: list[tuple[dt.date, str]] = []
        errors: list[str] = []
        for a in apps:
            try:
                entries += calendar_entries(a, await self._arr(a).calendar(start, end), start.date(), end.date())
            except Exception as e:
                errors.append(f"🔴 {a}: {truncate(str(e), 120)}")
        grouped = group_by_day(entries)
        pages: list[discord.Embed] = []
        title = f"Calendar · next {days} day{'s' if days != 1 else ''}"
        if not grouped:
            e = lab_embed(title, "\n".join(errors) or "Nothing scheduled.", lab_name=self.bot.lab_name)
            await interaction.followup.send(embed=e)
            return
        for i in range(0, len(grouped), 7):
            e = lab_embed(title, "\n".join(errors) or None, lab_name=self.bot.lab_name)
            for day, texts in grouped[i:i + 7]:
                value = "\n".join(texts)
                e.add_field(name=day.strftime("%a %d %b"), value=truncate(value, 1024), inline=False)
            pages.append(e)
        view = PaginatorView(pages, user_id=interaction.user.id)
        await interaction.followup.send(embed=pages[0], view=view if len(pages) > 1 else None)

    # ----- /arr search -----------------------------------------------------------------

    @discord.app_commands.describe(app="Which app to search in", term="Title to look up")
    async def search(self, interaction: discord.Interaction, app: QueueApp, term: str):
        client = self._arr(app)
        if client is None:
            await interaction.response.send_message(f"🚫 {app} is not configured.", ephemeral=True)
            return
        await interaction.response.defer()
        try:
            results = (await client.lookup(term))[:5]
        except Exception as e:
            await interaction.followup.send(f"❌ Lookup failed: {truncate(str(e), 300)}")
            return
        e = lab_embed(f"{app} lookup: {truncate(term, 100)}", None if results else "No results.",
                      lab_name=self.bot.lab_name)
        for i, r in enumerate(results, 1):
            name = r.get("title") or r.get("artistName") or "?"
            if r.get("year"):
                name += f" ({r['year']})"
            ids = [f"{k}: `{r[v]}`" for k, v in (("tvdb", "tvdbId"), ("tmdb", "tmdbId"), ("imdb", "imdbId"),
                                                 ("mbid", "foreignArtistId")) if r.get(v)]
            in_lib = "📚 in library" if r.get("id") else ""
            meta = " · ".join(x for x in [*ids, r.get("status") or "", r.get("network") or "", in_lib] if x)
            overview = truncate((r.get("overview") or "").strip(), 200)
            e.add_field(name=f"{i}. {truncate(name, 200)}", value=truncate(f"{meta}\n{overview}".strip() or "—", 1024),
                        inline=False)
            if i == 1:
                poster = next((im.get("remoteUrl") or im.get("url") for im in r.get("images") or []
                               if im.get("coverType") == "poster"), None)
                if poster and poster.startswith("http"):
                    e.set_thumbnail(url=poster)
        await interaction.followup.send(embed=e)

    # ----- /arr health -----------------------------------------------------------------

    async def health(self, interaction: discord.Interaction):
        await interaction.response.defer()
        e = lab_embed("*arr health", lab_name=self.bot.lab_name)
        worst = Severity.OK
        for app, client in self.svc.arr.items():
            try:
                issues = await client.health()
            except Exception as ex:
                e.add_field(name=f"🔴 {app}", value=truncate(f"unreachable: {ex}", 1024), inline=False)
                worst = Severity.CRITICAL
                continue
            if not issues:
                e.add_field(name=f"🟢 {app}", value="No health issues.", inline=False)
                continue
            lines = []
            for h in issues:
                icon = "🔴" if (h.get("type") or "").lower() == "error" else "🟡"
                lines.append(f"{icon} {h.get('message', '?')}")
                if icon == "🔴":
                    worst = Severity.CRITICAL
                elif worst is Severity.OK:
                    worst = Severity.WARNING
            e.add_field(name=f"🟡 {app} ({len(issues)})", value=truncate("\n".join(lines), 1024), inline=False)
        prowlarr = self.svc.arr.get("prowlarr")
        if prowlarr:
            try:
                statuses = await prowlarr.indexer_status()
                names = {i.get("id"): i.get("name") for i in await prowlarr.indexers()}
                if statuses:
                    lines = []
                    for s in statuses:
                        name = names.get(s.get("indexerId"), f"indexer #{s.get('indexerId')}")
                        till = (s.get("disabledTill") or "")[:16].replace("T", " ")
                        lines.append(f"🔴 {name} · disabled till {till or '?'} · {truncate(s.get('mostRecentFailure') or '', 80)}")
                    e.add_field(name=f"Prowlarr indexers with failures ({len(statuses)})",
                                value=truncate("\n".join(lines), 1024), inline=False)
                    if worst is Severity.OK:
                        worst = Severity.WARNING
                else:
                    e.add_field(name="Prowlarr indexers", value=f"All {len(names)} indexers healthy.", inline=False)
            except Exception as ex:
                e.add_field(name="Prowlarr indexers", value=truncate(f"🔴 {ex}", 1024), inline=False)
        e.color = worst.color
        await interaction.followup.send(embed=e)

    # ----- /arr clients ----------------------------------------------------------------

    async def clients(self, interaction: discord.Interaction):
        if not self.svc.qbit and not self.svc.sab:
            await interaction.response.send_message("🚫 No download client configured (QBIT_URL / SABNZBD_URL).",
                                                    ephemeral=True)
            return
        await interaction.response.defer()
        e = lab_embed("Download clients", lab_name=self.bot.lab_name)
        if self.svc.qbit:
            try:
                info = await self.svc.qbit.transfer_info()
                active = await self.svc.qbit.torrents_info("downloading")
                e.add_field(name="qBittorrent",
                            value=(f"⬇️ {human_bytes(info.get('dl_info_speed'))}/s · ⬆️ {human_bytes(info.get('up_info_speed'))}/s\n"
                                   f"Active: **{len(active)}** · Session: ⬇️ {human_bytes(info.get('dl_info_data'))} "
                                   f"⬆️ {human_bytes(info.get('up_info_data'))}\n"
                                   f"Connection: {info.get('connection_status', '?')}"), inline=False)
            except Exception as ex:
                e.add_field(name="🔴 qBittorrent", value=truncate(str(ex), 1024), inline=False)
        if self.svc.sab:
            try:
                q = await self.svc.sab.queue()
                e.add_field(name="SABnzbd",
                            value=(f"⬇️ {human_bytes(float(q.get('kbpersec') or 0) * 1024)}/s · status: {q.get('status', '?')}\n"
                                   f"Active: **{q.get('noofslots', 0)}** · {float(q.get('mbleft') or 0):.0f} MB left"
                                   f"{' · ⏸️ paused' if q.get('paused') else ''}\n"
                                   f"Disk free: {q.get('diskspace1', '?')} GB"), inline=False)
            except Exception as ex:
                e.add_field(name="🔴 SABnzbd", value=truncate(str(ex), 1024), inline=False)
        await interaction.followup.send(embed=e)

    # ----- stall watcher ---------------------------------------------------------------

    @tasks.loop(minutes=5)
    async def stall_watch(self):
        for app, client in self.svc.arr.items():
            if app == "prowlarr":
                continue
            try:
                items = await client.queue()
            except Exception as e:
                log.warning("stall watcher: %s queue failed: %s", app, e)
                await note_reachability(self.bot, app, False, str(e))
                continue
            await note_reachability(self.bot, app, True)
            new_stalled, recovered = self.tracker.update(app, items)
            for key, it in new_stalled:
                mins = self.bot.cfg.queue_stall_min
                await self.bot.alerts.fire(Alert(
                    fingerprint=f"arr:{app}:stalled:{it.get('id')}",
                    title=f"{app}: download stalled",
                    description=f"**{queue_item_name(app, it)}** has not progressed in {mins} min.\n"
                                f"`{progress_bar(queue_pct(it))}` {human_bytes(it.get('sizeleft'))} left",
                    severity=Severity.WARNING,
                    fields={"Client": it.get("downloadClient") or "—", "Indexer": it.get("indexer") or "—",
                            "Queue id": str(it.get("id"))}))
            for key in recovered:
                await self.bot.alerts.resolve(f"arr:{key.replace(':', ':stalled:', 1)}", note="Progressing or gone")

    @stall_watch.before_loop
    async def _wait(self):
        await self.bot.wait_until_ready()

    @stall_watch.error
    async def _stall_error(self, err: BaseException):
        log.exception("stall watcher crashed, restarting", exc_info=err)
        self.stall_watch.restart()


async def setup(bot):
    await bot.add_cog(Queue(bot))

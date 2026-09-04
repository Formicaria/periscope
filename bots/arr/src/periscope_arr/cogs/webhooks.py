"""Inbound webhooks from Sonarr / Radarr / Lidarr / Prowlarr → rich embeds + health alerts.

Every card is a message kind on the Messages page (`media.grab`, `media.import`, …, see `kind_for`): `_post`
passes the embed through the owning service's `bot.messages.apply` with the plain facts from `event_ctx`, so the
wording can be customised or the card switched off. Health issues are alerts (`core.alert`), not cards.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

import discord
from aiohttp import web
from discord.ext import commands

from periscope import Alert, Severity, human_bytes, lab_embed, truncate
from periscope.hooks import NullHistory

log = logging.getLogger(__name__)
# a bot assembled by hand (a test, a bare install) has no event log; recording is never worth a crash
NO_LOG = NullHistory()

APPS = ("sonarr", "radarr", "lidarr", "prowlarr")

EVENT_STYLE: dict[str, tuple[str, Severity]] = {
    "Grab": ("⬇️ Grabbed", Severity.INFO),
    "Download": ("✅ Imported", Severity.OK),
    "Upgrade": ("⬆️ Upgraded", Severity.OK),
    "Rename": ("✏️ Renamed", Severity.INFO),
    "SeriesAdd": ("➕ Series added", Severity.INFO),
    "MovieAdded": ("➕ Movie added", Severity.INFO),
    "SeriesDelete": ("🗑️ Series deleted", Severity.WARNING),
    "MovieDelete": ("🗑️ Movie deleted", Severity.WARNING),
    "EpisodeFileDelete": ("🗑️ Episode file deleted", Severity.WARNING),
    "MovieFileDelete": ("🗑️ Movie file deleted", Severity.WARNING),
    "HealthIssue": ("🩺 Health issue", Severity.WARNING),
    "HealthRestored": ("🩺 Health restored", Severity.OK),
    "ApplicationUpdate": ("🆕 Application updated", Severity.INFO),
    "ManualInteractionRequired": ("✋ Manual interaction required", Severity.WARNING),
    "Test": ("🔔 Test notification", Severity.INFO),
}

# ----- message kinds (Messages page) --------------------------------------------------------------------
# the kind each event type's card is customised under; anything else is the generic `media.other` card
OTHER_KIND = "media.other"
EVENT_KIND: dict[str, str] = {
    "Grab": "media.grab",
    "Download": "media.import",
    "Upgrade": "media.upgrade",
    "Rename": "media.rename",
    "SeriesAdd": "media.added", "MovieAdded": "media.added", "ArtistAdd": "media.added",
    "SeriesDelete": "media.deleted", "MovieDelete": "media.deleted", "ArtistDelete": "media.deleted",
    "AlbumDelete": "media.deleted", "EpisodeFileDelete": "media.deleted", "MovieFileDelete": "media.deleted",
    "HealthIssue": "media.health", "HealthRestored": "media.health",
    "ApplicationUpdate": "media.update",
    "ManualInteractionRequired": "media.manual",
    "Test": "media.test",
}

# the template variables each kind carries (name → meaning), the same keys for every event of that kind so a
# template can rely on them; `event_ctx` fills them from the payload, empty when it had nothing to say
_COMMON = {
    "app": "which app sent it: sonarr · radarr · lidarr · prowlarr",
    "event": "the webhook's event type (Grab, Download, …)",
    "instance": "the app's instance name, when it sends one",
}
_MEDIA = {
    "media": "the series, movie or artist as the card writes it (with year and episode codes)",
    "series": "the series' title (Sonarr)", "movie": "the movie's title (Radarr)",
    "artist": "the artist's name (Lidarr)", "year": "the series' or movie's year, 0 when unknown",
    "episodes": "the episodes: item.season · item.number · item.code (S06E01) · item.title",
    "albums": "the albums' titles (Lidarr)", "poster": "poster image url",
}
_RELEASE = {
    "quality": "the quality (WEBDL-1080p, …)", "group": "the release group", "indexer": "the indexer it came from",
    "client": "the download client", "size": "size in bytes (the release's, else the file's)",
    "release": "the release's title", "upgrade": "true when it replaces an existing file",
}
_FILE = {
    "path": "the file's path inside the series or movie folder",
    "deleted_files": "how many existing files the import replaced",
}
VARIABLES: dict[str, dict[str, str]] = {
    "media.grab": {**_COMMON, **_MEDIA, **_RELEASE},
    "media.import": {**_COMMON, **_MEDIA, **_RELEASE, **_FILE},
    "media.upgrade": {**_COMMON, **_MEDIA, **_RELEASE, **_FILE},
    "media.manual": {**_COMMON, **_MEDIA, **_RELEASE,
                     "status": "the download's status in the client",
                     "problems": "what the app says is wrong, as a list"},
    "media.rename": {**_COMMON, **_MEDIA, "renamed": "the renamed files: item.from · item.to"},
    "media.added": {**_COMMON, **_MEDIA},
    "media.deleted": {**_COMMON, **_MEDIA,
                      "path": "the deleted file's path inside the series or movie folder",
                      "quality": "the deleted file's quality", "size": "the deleted file's size in bytes",
                      "reason": "why it was deleted (upgrade, manual, …), for file deletes",
                      "files_deleted": "true when the files went with it, for series / movie / artist deletes"},
    "media.health": {**_COMMON,
                     "check": "the health check (IndexerStatusCheck, …)", "message": "what the app says",
                     "level": "warning · error", "wiki": "the app's wiki page on it"},
    "media.update": {**_COMMON, "message": "what the app says", "previous_version": "the version before",
                     "new_version": "the version now"},
    "media.test": dict(_COMMON),
    OTHER_KIND: {**_COMMON, **_MEDIA, **_RELEASE},
}


@dataclass
class WebhookEvent:
    app: str
    event_type: str
    title: str
    description: str = ""
    severity: Severity = Severity.INFO
    poster: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    health_type: str | None = None
    health_message: str | None = None
    payload: dict = field(default_factory=dict, repr=False)   # the webhook as received, for `event_ctx`

    @property
    def health_fingerprint(self) -> str | None:
        if self.health_type is None:
            return None
        h = hashlib.sha1((self.health_message or "").encode()).hexdigest()[:10]
        return f"arr:{self.app}:health:{self.health_type}:{h}"


def _poster(obj: dict | None) -> str | None:
    for img in (obj or {}).get("images") or []:
        if img.get("coverType") == "poster":
            url = img.get("remoteUrl") or img.get("url")
            if url and url.startswith("http"):
                return url
    return None


def _year(obj: dict | None) -> str:
    y = (obj or {}).get("year")
    return f" ({y})" if y else ""


def media_title(app: str, p: dict) -> tuple[str, str]:
    """Return (headline, detail line) for the media in a webhook payload."""
    if app == "sonarr":
        s = p.get("series") or {}
        eps = p.get("episodes") or []
        codes = [f"S{e.get('seasonNumber', 0):02d}E{e.get('episodeNumber', 0):02d}" for e in eps]
        head = f"{s.get('title', 'Unknown series')}{_year(s)}"
        if codes:
            head += " – " + (codes[0] if len(codes) == 1 else f"{codes[0]}…{codes[-1]} ({len(codes)} eps)")
        return head, ", ".join(e.get("title") or "" for e in eps[:3] if e.get("title"))
    if app == "radarr":
        m = p.get("movie") or p.get("remoteMovie") or {}
        return f"{m.get('title', 'Unknown movie')}{_year(m)}", ""
    if app == "lidarr":
        a = p.get("artist") or {}
        albums = p.get("albums") or []
        head = a.get("name") or "Unknown artist"
        if albums:
            head += " – " + ", ".join(al.get("title", "?") for al in albums[:2])
        return head, ""
    return "", ""


def parse_event(app: str, p: dict) -> WebhookEvent:
    et = p.get("eventType") or "Unknown"
    label, sev = EVENT_STYLE.get(et, (f"📣 {et}", Severity.INFO))
    ev = WebhookEvent(app=app, event_type=et, title=f"{app.capitalize()}: {label}", severity=sev, payload=p)

    if et == "HealthIssue" or et == "HealthRestored":
        ev.health_type = p.get("type") or "unknown"
        ev.health_message = p.get("message") or ""
        ev.description = ev.health_message
        level = (p.get("level") or "").lower()
        if et == "HealthIssue" and level == "error":
            ev.severity = Severity.CRITICAL
        ev.fields["Type"] = ev.health_type
        if p.get("wikiUrl"):
            ev.fields["Wiki"] = p["wikiUrl"]
        return ev
    if et == "ApplicationUpdate":
        ev.description = p.get("message") or ""
        if p.get("previousVersion"):
            ev.fields["Version"] = f"{p['previousVersion']} → {p.get('newVersion', '?')}"
        return ev
    if et == "Test":
        ev.description = f"Webhook from {app} is wired up correctly."
        return ev

    head, detail = media_title(app, p)
    ev.description = f"**{head}**" + (f"\n{detail}" if detail else "")
    ev.poster = _poster(p.get("series") or p.get("movie") or p.get("remoteMovie") or p.get("artist"))

    release = p.get("release") or {}
    file_ = p.get("episodeFile") or p.get("movieFile") or {}
    quality = release.get("quality") or file_.get("quality")
    if isinstance(quality, dict):
        quality = (quality.get("quality") or {}).get("name")
    group = release.get("releaseGroup") or file_.get("releaseGroup")
    if quality:
        ev.fields["Quality"] = str(quality)
    if group:
        ev.fields["Group"] = str(group)
    if release.get("indexer"):
        ev.fields["Indexer"] = release["indexer"]
    if p.get("downloadClient"):
        ev.fields["Client"] = p["downloadClient"]
    if release.get("size"):
        ev.fields["Size"] = human_bytes(release["size"])
    if p.get("isUpgrade"):
        ev.fields["Upgrade"] = "yes"
    if p.get("deletedFiles"):
        # an import lists the files it replaced; a series / movie delete just says whether files went with it
        deleted = p["deletedFiles"]
        ev.fields["Deleted files"] = "yes" if isinstance(deleted, bool) else str(len(deleted))
    if p.get("deleteReason"):
        ev.fields["Reason"] = str(p["deleteReason"])
    return ev


def kind_for(ev: WebhookEvent) -> str:
    """The message kind a card is customised under: one per card type, `media.other` for events without a card
    of their own. An import that replaced an existing file is an upgrade, whatever the app called it."""
    if ev.event_type == "Download" and ev.payload.get("isUpgrade"):
        return "media.upgrade"
    return EVENT_KIND.get(ev.event_type, OTHER_KIND)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _facts(ev: WebhookEvent) -> dict[str, Any]:
    """Every plain fact a card's template could use, from the payload; `event_ctx` hands out the kind's share."""
    p = ev.payload
    series = p.get("series") or {}
    movie = p.get("movie") or p.get("remoteMovie") or {}
    artist = p.get("artist") or {}
    release = p.get("release") or {}
    file_ = p.get("episodeFile") or p.get("movieFile") or {}
    quality = release.get("quality") or file_.get("quality")
    if isinstance(quality, dict):
        quality = (quality.get("quality") or {}).get("name")
    renamed = p.get("renamedEpisodeFiles") or p.get("renamedMovieFiles") or p.get("renamedTrackFiles") or []
    problems = [str(m) for note in p.get("downloadStatusMessages") or []
                for m in (note.get("messages") or [note.get("title") or ""]) if m]
    deleted = p.get("deletedFiles")
    return {
        "app": ev.app, "event": ev.event_type, "instance": p.get("instanceName") or "",
        "media": media_title(ev.app, p)[0], "series": series.get("title") or "", "movie": movie.get("title") or "",
        "artist": artist.get("name") or "", "year": _int((series or movie).get("year")),
        "episodes": [{"season": _int(e.get("seasonNumber")), "number": _int(e.get("episodeNumber")),
                      "code": f"S{_int(e.get('seasonNumber')):02d}E{_int(e.get('episodeNumber')):02d}",
                      "title": e.get("title") or ""} for e in p.get("episodes") or []],
        "albums": [al.get("title") or "" for al in p.get("albums") or []], "poster": ev.poster or "",
        "quality": str(quality or ""), "group": str(release.get("releaseGroup") or file_.get("releaseGroup") or ""),
        "indexer": str(release.get("indexer") or ""), "client": str(p.get("downloadClient") or ""),
        "size": _int(release.get("size") or file_.get("size")), "release": str(release.get("releaseTitle") or ""),
        "upgrade": bool(p.get("isUpgrade")),
        "path": str(file_.get("relativePath") or file_.get("path") or ""),
        "deleted_files": len(deleted) if isinstance(deleted, list) else 0, "files_deleted": bool(deleted),
        "reason": str(p.get("deleteReason") or ""),
        "renamed": [{"from": f.get("previousRelativePath") or f.get("previousPath") or "",
                     "to": f.get("relativePath") or f.get("path") or ""} for f in renamed],
        "status": str(p.get("downloadStatus") or ""), "problems": problems,
        "check": ev.health_type or "", "message": str(p.get("message") or ""),
        "level": str(p.get("level") or "").lower(), "wiki": str(p.get("wikiUrl") or ""),
        "previous_version": str(p.get("previousVersion") or ""), "new_version": str(p.get("newVersion") or ""),
    }


def event_ctx(ev: WebhookEvent) -> dict[str, Any]:
    """The template variables for `ev`'s card: exactly the keys `VARIABLES` documents for its kind."""
    facts = _facts(ev)
    return {name: facts[name] for name in VARIABLES[kind_for(ev)]}


def event_embed(ev: WebhookEvent, lab_name: str | None) -> discord.Embed:
    """The feed card for a parsed event, as the bot posts it (before any customisation)."""
    e = lab_embed(ev.title, truncate(ev.description, 2000) or None, severity=ev.severity, lab_name=lab_name)
    if ev.poster:
        e.set_thumbnail(url=ev.poster)
    for k, v in ev.fields.items():
        e.add_field(name=k, value=truncate(str(v), 1024), inline=True)
    return e


class Webhooks(commands.Cog):
    """One POST /<app> route per *arr app on the shared webhook server; events go out through the service
    that owns the app (its feed channel, its alert router)."""

    def __init__(self, bot):
        self.bot = bot
        self.hub = bot.media_hub
        self.hub.webhooks_cog = self
        self.routes: set[str] = set()
        if bot.webhook is None:
            raise RuntimeError("Webhooks cog requires the webhook server (LabBot(webhook=True))")
        for app in self.hub.webhook_apps():
            self.ensure_route(app)

    def ensure_route(self, app: str) -> None:
        """Register POST /<app> once; the hub calls this again for apps that join later (v2)."""
        if app not in APPS or app in self.routes:
            return
        self.bot.webhook.add_route("POST", f"/{app}", self._make_handler(app))
        self.routes.add(app)

    def _make_handler(self, app: str):
        async def handler(request: web.Request) -> web.Response:
            try:
                payload = await request.json()
            except Exception:
                return web.json_response({"error": "invalid json"}, status=400)
            if not isinstance(payload, dict):
                return web.json_response({"error": "expected object"}, status=400)
            ev = parse_event(app, payload)
            log.info("webhook %s %s", app, ev.event_type)
            await self.handle(ev)
            return web.json_response({"ok": True})

        return handler

    async def handle(self, ev: WebhookEvent) -> None:
        alerts = self.hub.alerts_for(ev.app)
        if ev.event_type == "HealthIssue":
            await alerts.fire(Alert(fingerprint=ev.health_fingerprint, title=ev.title,
                                    description=ev.description, severity=ev.severity, fields=ev.fields))
            return
        if ev.event_type == "HealthRestored":
            if not await alerts.resolve(ev.health_fingerprint, note="Reported healthy by the app"):
                await self._post(ev)
            return
        await self._post(ev)

    async def _post(self, ev: WebhookEvent) -> None:
        owner = self.hub.bot_for(ev.app)
        cid = self.hub.media_channel_for(ev.app)
        if not cid:
            log.warning("MEDIA_CHANNEL_ID / ALERT_CHANNEL_ID not set; dropping %s event", ev.event_type)
            return
        # the owning service's customisation of this kind of card (Messages page)
        e = owner.messages.apply(kind_for(ev), event_embed(ev, owner.lab_name), event_ctx(ev))
        if e is None:                       # switched off on the Messages page
            return
        ch = await owner.get_channel_safe(cid)
        if ch is None:
            return
        await ch.send(embed=e)
        # the log lives on whichever service owns this app; a bot built by hand has none, so fall back
        getattr(owner, "history", NO_LOG).record(
            service="arr", kind=kind_for(ev).split(".")[-1], key=ev.app, severity=ev.severity.value,
            title=truncate(f"{ev.title} {media_title(ev.app, ev.payload)[0]}".strip(), 200),
            server=owner.lab_name, payload={"event": ev.event_type})


async def setup(bot):
    await bot.add_cog(Webhooks(bot))

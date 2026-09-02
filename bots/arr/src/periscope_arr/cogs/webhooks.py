"""Inbound webhooks from Sonarr / Radarr / Lidarr / Prowlarr → rich embeds + health alerts."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field

from aiohttp import web
from discord.ext import commands

from periscope import Alert, Severity, human_bytes, lab_embed, truncate

log = logging.getLogger(__name__)

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
    ev = WebhookEvent(app=app, event_type=et, title=f"{app.capitalize()}: {label}", severity=sev)

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
        ev.fields["Deleted files"] = str(len(p["deletedFiles"]))
    if p.get("deleteReason"):
        ev.fields["Reason"] = str(p["deleteReason"])
    return ev


class Webhooks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        if bot.webhook is None:
            raise RuntimeError("Webhooks cog requires the webhook server (LabBot(webhook=True))")
        for app in APPS:
            bot.webhook.add_route("POST", f"/{app}", self._make_handler(app))

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
        if ev.event_type == "HealthIssue":
            await self.bot.alerts.fire(Alert(fingerprint=ev.health_fingerprint, title=ev.title,
                                             description=ev.description, severity=ev.severity, fields=ev.fields))
            return
        if ev.event_type == "HealthRestored":
            if not await self.bot.alerts.resolve(ev.health_fingerprint, note="Reported healthy by the app"):
                await self._post(ev)
            return
        await self._post(ev)

    async def _post(self, ev: WebhookEvent) -> None:
        cid = self.bot.cfg.media_channel_id or self.bot.settings.alert_channel_id
        if not cid:
            log.warning("MEDIA_CHANNEL_ID / ALERT_CHANNEL_ID not set; dropping %s event", ev.event_type)
            return
        ch = await self.bot.get_channel_safe(cid)
        if ch is None:
            return
        e = lab_embed(ev.title, truncate(ev.description, 2000) or None, severity=ev.severity, lab_name=self.bot.lab_name)
        if ev.poster:
            e.set_thumbnail(url=ev.poster)
        for k, v in ev.fields.items():
            e.add_field(name=k, value=truncate(str(v), 1024), inline=True)
        await ch.send(embed=e)


async def setup(bot):
    await bot.add_cog(Webhooks(bot))

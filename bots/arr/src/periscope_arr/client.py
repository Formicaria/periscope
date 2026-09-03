"""Async API clients for the *arr apps, download clients, and media servers."""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from periscope import Alert, HttpClient, Severity
from periscope.http import HttpError

from .config import ARR_API_VERSION, ARR_APPS, ArrSettings

log = logging.getLogger(__name__)

LOOKUP_PATH = {"sonarr": "series/lookup", "radarr": "movie/lookup", "lidarr": "artist/lookup"}
QUEUE_INCLUDE = {
    "sonarr": {"includeSeries": "true", "includeEpisode": "true"},
    "radarr": {"includeMovie": "true"},
    "lidarr": {"includeArtist": "true", "includeAlbum": "true"},
}


class ArrClient(HttpClient):
    """Generic Sonarr/Radarr/Lidarr/Prowlarr client (X-Api-Key auth)."""

    def __init__(self, app: str, base: str, api_key: str, version: str | None = None, *, verify_ssl: bool = True):
        self.app = app
        self.version = version or ARR_API_VERSION.get(app, "v3")
        super().__init__(base, headers={"X-Api-Key": api_key, "Accept": "application/json"}, verify_ssl=verify_ssl)

    def _p(self, path: str) -> str:
        return f"api/{self.version}/{path.lstrip('/')}"

    async def status(self) -> dict:
        return await self.get_json(self._p("system/status"))

    async def health(self) -> list[dict]:
        return await self.get_json(self._p("health")) or []

    async def diskspace(self) -> list[dict]:
        return await self.get_json(self._p("diskspace")) or []

    async def queue(self) -> list[dict]:
        params = {"pageSize": "100", "page": "1", **QUEUE_INCLUDE.get(self.app, {})}
        data = await self.get_json(self._p("queue"), params=params)
        return data.get("records", []) if isinstance(data, dict) else data or []

    async def calendar(self, start: dt.datetime, end: dt.datetime) -> list[dict]:
        params = {"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                  "unmonitored": "false"}
        if self.app == "sonarr":
            params["includeSeries"] = "true"
        if self.app == "lidarr":
            params["includeArtist"] = "true"
        return await self.get_json(self._p("calendar"), params=params) or []

    async def lookup(self, term: str) -> list[dict]:
        path = LOOKUP_PATH.get(self.app)
        if not path:
            return []
        return await self.get_json(self._p(path), params={"term": term}) or []

    async def command(self, name: str, **params: Any) -> Any:
        return await self.post_json(self._p("command"), {"name": name, **params})

    async def queue_delete(self, queue_id: int, *, remove_from_client: bool = True, blocklist: bool = False) -> int:
        params = {"removeFromClient": str(remove_from_client).lower(), "blocklist": str(blocklist).lower(),
                  "skipRedownload": "false"}
        return await self.delete(self._p(f"queue/{queue_id}"), params=params)

    async def indexer_status(self) -> list[dict]:
        """Prowlarr only: indexers currently disabled due to failures."""
        return await self.get_json(self._p("indexerstatus")) or []

    async def indexers(self) -> list[dict]:
        return await self.get_json(self._p("indexer")) or []


class QbitClient(HttpClient):
    """qBittorrent Web API v2. Prefers an API key (Bearer, qBittorrent ≥ 5.2); falls back to cookie login."""

    def __init__(self, base: str, user: str, password: str, *, api_key: str = "", verify_ssl: bool = True):
        headers = {"Referer": base}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        super().__init__(base, headers=headers, verify_ssl=verify_ssl)
        self._user = user
        self._pass = password
        self._api_key = api_key
        self._logged_in = bool(api_key)

    async def login(self) -> None:
        if self._api_key:
            raise RuntimeError("qBittorrent rejected QBIT_API_KEY (Options → Web UI → API keys, needs qBittorrent ≥ 5.2)")
        resp = await self.request("POST", "api/v2/auth/login", data={"username": self._user, "password": self._pass})
        async with resp:
            text = await resp.text()
        if text.strip() != "Ok.":
            raise RuntimeError("qBittorrent login failed (check QBIT_USER/QBIT_PASS)")
        self._logged_in = True

    async def _get(self, path: str, **kw) -> Any:
        if not self._logged_in:
            await self.login()
        try:
            return await self.get_json(path, **kw)
        except HttpError as e:
            if e.status in (401, 403):
                self._logged_in = bool(self._api_key)
                await self.login()
                return await self.get_json(path, **kw)
            raise

    async def torrents_info(self, filter: str = "downloading") -> list[dict]:
        return await self._get("api/v2/torrents/info", params={"filter": filter}) or []

    async def transfer_info(self) -> dict:
        return await self._get("api/v2/transfer/info") or {}


class SabClient(HttpClient):
    """SABnzbd JSON API."""

    def __init__(self, base: str, api_key: str, *, verify_ssl: bool = True):
        super().__init__(base, verify_ssl=verify_ssl)
        self._key = api_key

    async def queue(self) -> dict:
        data = await self.get_json("api", params={"mode": "queue", "output": "json", "apikey": self._key})
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"SABnzbd error: {data['error']}")
        return (data or {}).get("queue", {})


class PlexClient(HttpClient):
    def __init__(self, base: str, token: str, *, verify_ssl: bool = True):
        super().__init__(base, headers={"X-Plex-Token": token, "Accept": "application/json"}, verify_ssl=verify_ssl)

    async def sessions(self) -> list[dict]:
        data = await self.get_json("status/sessions")
        return ((data or {}).get("MediaContainer") or {}).get("Metadata") or []

    async def identity(self) -> dict:
        data = await self.get_json("identity")
        return (data or {}).get("MediaContainer") or {}


class JellyfinClient(HttpClient):
    def __init__(self, base: str, api_key: str, *, verify_ssl: bool = True):
        super().__init__(base, headers={"X-Emby-Token": api_key, "Accept": "application/json"}, verify_ssl=verify_ssl)

    async def sessions(self) -> list[dict]:
        return await self.get_json("Sessions") or []

    async def system_info(self) -> dict:
        return await self.get_json("System/Info") or {}


def build_client(name: str, cfg: ArrSettings) -> HttpClient | None:
    """The client for one media service, or None when its URL is not set in `cfg`."""
    v = cfg.verify_ssl
    if name in ARR_APPS:
        pair = cfg.arr.get(name)
        return ArrClient(name, pair[0], pair[1], verify_ssl=v) if pair else None
    if name == "qbittorrent":
        return QbitClient(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass, api_key=cfg.qbit_api_key, verify_ssl=v) if cfg.qbit_url else None
    if name == "sabnzbd":
        return SabClient(cfg.sabnzbd_url, cfg.sabnzbd_api_key, verify_ssl=v) if cfg.sabnzbd_url else None
    if name == "plex":
        return PlexClient(cfg.plex_url, cfg.plex_token, verify_ssl=v) if cfg.plex_url else None
    if name == "jellyfin":
        return JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key, verify_ssl=v) if cfg.jellyfin_url else None
    raise ValueError(f"unknown media service {name!r}")


class Services:
    """Every configured client, shared by every cog. v1 builds them all from settings at once; v2 services
    `add()` theirs one by one as they are built."""

    def __init__(self, cfg: ArrSettings):
        self.cfg = cfg
        self.arr: dict[str, ArrClient] = {}
        self.qbit: QbitClient | None = None
        self.sab: SabClient | None = None
        self.plex: PlexClient | None = None
        self.jellyfin: JellyfinClient | None = None
        self._fails: dict[str, int] = {}
        self._down: set[str] = set()
        for name in cfg.enabled_services():
            self.add(name, build_client(name, cfg))

    def add(self, name: str, client: HttpClient) -> None:
        if name in ARR_APPS:
            self.arr[name] = client  # type: ignore[assignment]
        elif name == "qbittorrent":
            self.qbit = client  # type: ignore[assignment]
        elif name == "sabnzbd":
            self.sab = client  # type: ignore[assignment]
        elif name == "plex":
            self.plex = client  # type: ignore[assignment]
        elif name == "jellyfin":
            self.jellyfin = client  # type: ignore[assignment]
        else:
            raise ValueError(f"unknown media service {name!r}")

    def get(self, name: str) -> HttpClient | None:
        if name in ARR_APPS:
            return self.arr.get(name)
        return {"qbittorrent": self.qbit, "sabnzbd": self.sab, "plex": self.plex, "jellyfin": self.jellyfin}.get(name)

    def names(self) -> list[str]:
        return [n for n in (*ARR_APPS, "qbittorrent", "sabnzbd", "plex", "jellyfin") if self.get(n) is not None]

    def all_clients(self) -> list[HttpClient]:
        return [*self.arr.values(), *[c for c in (self.qbit, self.sab, self.plex, self.jellyfin) if c]]

    async def close(self) -> None:
        for c in self.all_clients():
            await c.close()

    def record(self, name: str, ok: bool, threshold: int = 3) -> str | None:
        """Track consecutive failures. Returns 'down' on the 3rd straight failure, 'up' on recovery, else None."""
        if ok:
            self._fails[name] = 0
            if name in self._down:
                self._down.discard(name)
                return "up"
            return None
        self._fails[name] = self._fails.get(name, 0) + 1
        if self._fails[name] >= threshold and name not in self._down:
            self._down.add(name)
            return "down"
        return None

    def is_down(self, name: str) -> bool:
        return name in self._down


async def note_reachability(bot, name: str, ok: bool, error: str = "") -> None:
    """Call after every poll of `name`; fires/resolves the CRITICAL '<service> unreachable' alert.

    `bot` is any bot carrying the media hub; the alert goes out through the service that owns `name`.
    """
    hub = bot.media_hub
    transition = hub.svc.record(name, ok)
    fp = f"arr:{name}:unreachable"
    if transition == "down":
        log.error("%s unreachable after 3 consecutive failures: %s", name, error)
        await hub.alerts_for(name).fire(Alert(fingerprint=fp, title=f"{name} unreachable",
                                              description=f"3 consecutive API failures.\n`{error[:300]}`",
                                              severity=Severity.CRITICAL))
    elif transition == "up":
        log.info("%s reachable again", name)
        await hub.alerts_for(name).resolve(fp, note="API responding again")

"""Integration-specific settings for periscope-arr. A service is enabled iff its URL is set.

v1 runs the whole media stack as one bot (`from_env()` reads every key, at least one URL must be set).
v2 hosts each app as its own service: `from_env(only="sonarr")` reads that service's keys plus the shared
behaviour keys (VERIFY_SSL, MEDIA_CHANNEL_ID, ARR_QUEUE_STALL_MIN) and requires its URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from periscope import env, env_bool, env_int

ARR_APPS = ("sonarr", "radarr", "lidarr", "prowlarr")
ARR_API_VERSION = {"sonarr": "v3", "radarr": "v3", "lidarr": "v1", "prowlarr": "v1"}

# every media service and the env keys it owns (URL first, then its credential(s))
SERVICE_KEYS: dict[str, tuple[str, ...]] = {
    "sonarr": ("SONARR_URL", "SONARR_API_KEY"),
    "radarr": ("RADARR_URL", "RADARR_API_KEY"),
    "lidarr": ("LIDARR_URL", "LIDARR_API_KEY"),
    "prowlarr": ("PROWLARR_URL", "PROWLARR_API_KEY"),
    "qbittorrent": ("QBIT_URL", "QBIT_API_KEY", "QBIT_USER", "QBIT_PASS"),
    "sabnzbd": ("SABNZBD_URL", "SABNZBD_API_KEY"),
    "plex": ("PLEX_URL", "PLEX_TOKEN"),
    "jellyfin": ("JELLYFIN_URL", "JELLYFIN_API_KEY"),
}
MEDIA_SERVICES = tuple(SERVICE_KEYS)
SHARED_KEYS = ("VERIFY_SSL", "MEDIA_CHANNEL_ID", "ARR_QUEUE_STALL_MIN")


def _norm_url(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    return url


@dataclass
class ArrSettings:
    arr: dict[str, tuple[str, str]] = field(default_factory=dict)  # app -> (url, api_key)
    qbit_url: str | None = None
    qbit_user: str = ""
    qbit_pass: str = ""
    qbit_api_key: str = ""
    sabnzbd_url: str | None = None
    sabnzbd_api_key: str | None = None
    plex_url: str | None = None
    plex_token: str | None = None
    jellyfin_url: str | None = None
    jellyfin_api_key: str | None = None
    verify_ssl: bool = True
    media_channel_id: int | None = None
    queue_stall_min: int = 30

    @classmethod
    def from_env(cls, *, only: str | None = None) -> "ArrSettings":
        if only is not None and only not in SERVICE_KEYS:
            raise ValueError(f"unknown media service {only!r}")
        want = MEDIA_SERVICES if only is None else (only,)

        arr: dict[str, tuple[str, str]] = {}
        for app in ARR_APPS:
            if app not in want:
                continue
            url = _norm_url(env(f"{app.upper()}_URL"))
            key = env(f"{app.upper()}_API_KEY")
            if url and not key:
                raise RuntimeError(f"{app.upper()}_URL is set but {app.upper()}_API_KEY is missing")
            if url:
                arr[app] = (url, key)

        def pair(name: str, url_key: str, secret_key: str) -> tuple[str | None, str | None]:
            if name not in want:
                return None, None
            url, secret = _norm_url(env(url_key)), env(secret_key)
            if url and not secret:
                raise RuntimeError(f"{url_key} is set but {secret_key} is missing")
            return url, secret

        sab_url, sab_key = pair("sabnzbd", "SABNZBD_URL", "SABNZBD_API_KEY")
        plex_url, plex_token = pair("plex", "PLEX_URL", "PLEX_TOKEN")
        jf_url, jf_key = pair("jellyfin", "JELLYFIN_URL", "JELLYFIN_API_KEY")
        qbit = "qbittorrent" in want

        cfg = cls(
            arr=arr,
            qbit_url=_norm_url(env("QBIT_URL")) if qbit else None,
            qbit_user=(env("QBIT_USER", "") or "") if qbit else "",
            qbit_pass=(env("QBIT_PASS", "") or "") if qbit else "",
            qbit_api_key=(env("QBIT_API_KEY", "") or "") if qbit else "",
            sabnzbd_url=sab_url,
            sabnzbd_api_key=sab_key,
            plex_url=plex_url,
            plex_token=plex_token,
            jellyfin_url=jf_url,
            jellyfin_api_key=jf_key,
            verify_ssl=env_bool("VERIFY_SSL", True),
            media_channel_id=env_int("MEDIA_CHANNEL_ID"),
            queue_stall_min=env_int("ARR_QUEUE_STALL_MIN", 30),
        )
        if only is None:
            if not cfg.enabled_services():
                raise RuntimeError(
                    "No services configured. Set at least one of SONARR_URL, RADARR_URL, LIDARR_URL, "
                    "PROWLARR_URL, QBIT_URL, SABNZBD_URL, PLEX_URL, JELLYFIN_URL."
                )
        elif only not in cfg.enabled_services():
            raise RuntimeError(f"{SERVICE_KEYS[only][0]} is required")
        return cfg

    def enabled_services(self) -> list[str]:
        names = list(self.arr)
        for name, url in (("qbittorrent", self.qbit_url), ("sabnzbd", self.sabnzbd_url),
                          ("plex", self.plex_url), ("jellyfin", self.jellyfin_url)):
            if url:
                names.append(name)
        return names

    def shared_only(self) -> "ArrSettings":
        """Just the behaviour keys, no clients — what a MediaHub keeps as its own defaults."""
        return ArrSettings(verify_ssl=self.verify_ssl, media_channel_id=self.media_channel_id,
                           queue_stall_min=self.queue_stall_min)

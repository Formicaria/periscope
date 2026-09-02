"""Integration-specific settings for periscope-arr. A service is enabled iff its URL is set."""

from __future__ import annotations

from dataclasses import dataclass, field

from periscope import env, env_bool, env_int

ARR_APPS = ("sonarr", "radarr", "lidarr", "prowlarr")
ARR_API_VERSION = {"sonarr": "v3", "radarr": "v3", "lidarr": "v1", "prowlarr": "v1"}


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
    def from_env(cls) -> "ArrSettings":
        arr: dict[str, tuple[str, str]] = {}
        for app in ARR_APPS:
            url = _norm_url(env(f"{app.upper()}_URL"))
            key = env(f"{app.upper()}_API_KEY")
            if url and not key:
                raise RuntimeError(f"{app.upper()}_URL is set but {app.upper()}_API_KEY is missing")
            if url:
                arr[app] = (url, key)

        sab_url = _norm_url(env("SABNZBD_URL"))
        sab_key = env("SABNZBD_API_KEY")
        if sab_url and not sab_key:
            raise RuntimeError("SABNZBD_URL is set but SABNZBD_API_KEY is missing")
        plex_url = _norm_url(env("PLEX_URL"))
        plex_token = env("PLEX_TOKEN")
        if plex_url and not plex_token:
            raise RuntimeError("PLEX_URL is set but PLEX_TOKEN is missing")
        jf_url = _norm_url(env("JELLYFIN_URL"))
        jf_key = env("JELLYFIN_API_KEY")
        if jf_url and not jf_key:
            raise RuntimeError("JELLYFIN_URL is set but JELLYFIN_API_KEY is missing")

        cfg = cls(
            arr=arr,
            qbit_url=_norm_url(env("QBIT_URL")),
            qbit_user=env("QBIT_USER", "") or "",
            qbit_pass=env("QBIT_PASS", "") or "",
            qbit_api_key=env("QBIT_API_KEY", "") or "",
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
        if not cfg.enabled_services():
            raise RuntimeError(
                "No services configured. Set at least one of SONARR_URL, RADARR_URL, LIDARR_URL, "
                "PROWLARR_URL, QBIT_URL, SABNZBD_URL, PLEX_URL, JELLYFIN_URL."
            )
        return cfg

    def enabled_services(self) -> list[str]:
        names = list(self.arr)
        for name, url in (("qbittorrent", self.qbit_url), ("sabnzbd", self.sabnzbd_url),
                          ("plex", self.plex_url), ("jellyfin", self.jellyfin_url)):
            if url:
                names.append(name)
        return names

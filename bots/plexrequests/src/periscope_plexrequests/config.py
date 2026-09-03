"""Environment-driven configuration for the plexrequests service (same keys as the standalone bot it replaces)."""

from __future__ import annotations

from dataclasses import dataclass, field

from periscope import env, env_bool, env_int

BACKENDS = ("auto", "seerr", "arr")


def _channel_ref(raw: str | None) -> str:
    """A channel setting is a name or an id; '#name' is tolerated."""
    return (raw or "").strip().lstrip("#")


@dataclass
class PlexRequestsSettings:
    plex_url: str
    plex_token: str
    guild_id: int | None = None                 # PLEXREQ_GUILD_ID, else GUILD_ID
    channel_id: int | None = None               # invite channel
    channel_name: str = "join-plex"
    requests_channel_id: int | None = None
    role_name: str = "plex members"
    requests_role_name: str = ""                # empty = anyone may request (the .env.example suggests ROLE_NAME)
    libraries: str = "all"
    plex_link: str = ""
    server_name: str = ""
    request_backend: str = "auto"
    overseerr_url: str = ""
    overseerr_api_key: str = ""
    radarr_url: str = ""
    radarr_api_key: str = ""
    radarr_profile: str = ""
    radarr_root: str = ""
    sonarr_url: str = ""
    sonarr_api_key: str = ""
    sonarr_profile: str = ""
    sonarr_root: str = ""
    fallback_before_year: int = 2016
    radarr_fallback_profile: str = ""
    sonarr_fallback_profile: str = ""
    status_channel: str = ""
    new_channel: str = ""
    movies_channel: str = ""
    tv_channel: str = ""
    auto_revoke: bool = False
    announce_channel: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "PlexRequestsSettings":
        url = env("PLEX_URL", required=True).strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise RuntimeError("PLEX_URL must start with http:// or https:// (e.g. http://192.168.1.10:32400)")
        backend = (env("REQUEST_BACKEND", "auto") or "auto").strip().lower()
        if backend not in BACKENDS:
            raise RuntimeError(f"REQUEST_BACKEND must be one of {', '.join(BACKENDS)} (got {backend!r})")
        cfg = cls(
            plex_url=url,
            plex_token=(env("PLEX_TOKEN", required=True) or "").strip(),
            guild_id=env_int("PLEXREQ_GUILD_ID") or env_int("GUILD_ID"),
            channel_id=env_int("CHANNEL_ID"),
            channel_name=(env("CHANNEL_NAME", "join-plex") or "join-plex").strip().lstrip("#"),
            requests_channel_id=env_int("REQUESTS_CHANNEL_ID"),
            role_name=(env("ROLE_NAME", "plex members") or "").strip(),
            requests_role_name=(env("REQUESTS_ROLE_NAME", "") or "").strip(),
            libraries=(env("LIBRARIES", "all") or "all").strip(),
            plex_link=(env("PLEX_LINK", "") or "").strip(),
            server_name=(env("SERVER_NAME", "") or "").strip(),
            request_backend=backend,
            overseerr_url=(env("OVERSEERR_URL", "") or "").strip().rstrip("/"),
            overseerr_api_key=(env("OVERSEERR_API_KEY", "") or "").strip(),
            radarr_url=(env("RADARR_URL", "") or "").strip().rstrip("/"),
            radarr_api_key=(env("RADARR_API_KEY", "") or "").strip(),
            radarr_profile=(env("RADARR_PROFILE", "") or "").strip(),
            radarr_root=(env("RADARR_ROOT", "") or "").strip(),
            sonarr_url=(env("SONARR_URL", "") or "").strip().rstrip("/"),
            sonarr_api_key=(env("SONARR_API_KEY", "") or "").strip(),
            sonarr_profile=(env("SONARR_PROFILE", "") or "").strip(),
            sonarr_root=(env("SONARR_ROOT", "") or "").strip(),
            fallback_before_year=env_int("FALLBACK_BEFORE_YEAR", 2016) or 0,
            radarr_fallback_profile=(env("RADARR_FALLBACK_PROFILE", "") or "").strip(),
            sonarr_fallback_profile=(env("SONARR_FALLBACK_PROFILE", "") or "").strip(),
            status_channel=_channel_ref(env("STATUS_CHANNEL", "")),
            new_channel=_channel_ref(env("NEW_CHANNEL", "")),
            movies_channel=_channel_ref(env("MOVIES_CHANNEL", "")),
            tv_channel=_channel_ref(env("TV_CHANNEL", "")),
            auto_revoke=env_bool("AUTO_REVOKE", False),
        )
        cfg.announce_channel = {"movie": cfg.movies_channel, "tv": cfg.tv_channel}
        return cfg

    # ----- derived ------------------------------------------------------------------------------
    @property
    def plex_name(self) -> str:
        """'yourdomain.com Plex' or just 'Plex'."""
        return f"{self.server_name} Plex".strip()

    @property
    def available_text(self) -> str:
        return f"Available to watch on {self.plex_link} now" if self.plex_link else "Available to watch on Plex now"

    @property
    def has_seerr(self) -> bool:
        return bool(self.overseerr_url and self.overseerr_api_key)

    @property
    def has_radarr(self) -> bool:
        return bool(self.radarr_url and self.radarr_api_key)

    @property
    def has_sonarr(self) -> bool:
        return bool(self.sonarr_url and self.sonarr_api_key)

    def invite_channel_where(self) -> str:
        return f"<#{self.channel_id}>" if self.channel_id else f"#{self.channel_name}"

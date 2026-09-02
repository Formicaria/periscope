"""UniFi-specific environment configuration."""

from __future__ import annotations

from dataclasses import dataclass

from periscope import env, env_bool, env_int, load_dotenv_if_present


@dataclass
class UnifiConfig:
    url: str
    user: str
    password: str
    site: str = "default"
    is_unifi_os: bool = True
    verify_ssl: bool = False
    alert_new_clients: bool = True
    wan_latency_warn_ms: int = 100
    device_cpu_warn: int = 80
    known_clients_ttl_days: int = 30

    @property
    def login_path(self) -> str:
        return "/api/auth/login" if self.is_unifi_os else "/api/login"

    @property
    def api_prefix(self) -> str:
        return "/proxy/network" if self.is_unifi_os else ""

    def site_path(self, path: str) -> str:
        """Path under the Network application for the configured site, e.g. `stat/sta`."""
        return f"{self.api_prefix}/api/s/{self.site}/{path.lstrip('/')}"

    @property
    def ttl_seconds(self) -> int:
        return self.known_clients_ttl_days * 86400

    @classmethod
    def from_env(cls) -> UnifiConfig:
        load_dotenv_if_present()
        url = str(env("UNIFI_URL", required=True)).rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise RuntimeError("UNIFI_URL must start with http:// or https://")
        return cls(
            url=url,
            user=env("UNIFI_USER", required=True),
            password=env("UNIFI_PASS", required=True),
            site=env("UNIFI_SITE", "default"),
            is_unifi_os=env_bool("UNIFI_IS_UNIFI_OS", True),
            verify_ssl=env_bool("VERIFY_SSL", False),
            alert_new_clients=env_bool("UNIFI_ALERT_NEW_CLIENTS", True),
            wan_latency_warn_ms=env_int("UNIFI_WAN_LATENCY_WARN_MS", 100),
            device_cpu_warn=env_int("UNIFI_DEVICE_CPU_WARN", 80),
            known_clients_ttl_days=env_int("UNIFI_KNOWN_CLIENTS_TTL_DAYS", 30),
        )

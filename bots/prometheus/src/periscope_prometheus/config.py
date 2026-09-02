"""Prometheus / Alertmanager / Grafana specific environment variables."""

from __future__ import annotations

from dataclasses import dataclass

from periscope import env, env_bool, env_int, load_dotenv_if_present


@dataclass
class PromSettings:
    prom_url: str
    alertmanager_url: str
    grafana_url: str | None = None
    grafana_token: str | None = None
    grafana_org_id: int = 1
    prom_basic_user: str | None = None
    prom_basic_pass: str | None = None
    verify_ssl: bool = True
    target_watch: bool = True
    render_width: int = 1000
    render_height: int = 500
    default_dashboard_uid: str | None = None

    @property
    def grafana_enabled(self) -> bool:
        return bool(self.grafana_url)

    @classmethod
    def from_env(cls) -> "PromSettings":
        load_dotenv_if_present()
        grafana_url = env("GRAFANA_URL")
        grafana_token = env("GRAFANA_TOKEN")
        if grafana_url and not grafana_token:
            raise RuntimeError("GRAFANA_TOKEN is required when GRAFANA_URL is set "
                               "(create a service account token with the Viewer role)")
        user, pw = env("PROM_BASIC_USER"), env("PROM_BASIC_PASS")
        if bool(user) != bool(pw):
            raise RuntimeError("PROM_BASIC_USER and PROM_BASIC_PASS must be set together")
        s = cls(
            prom_url=str(env("PROM_URL", required=True)).rstrip("/"),
            alertmanager_url=str(env("ALERTMANAGER_URL", required=True)).rstrip("/"),
            grafana_url=grafana_url.rstrip("/") if grafana_url else None,
            grafana_token=grafana_token,
            grafana_org_id=env_int("GRAFANA_ORG_ID", 1),
            prom_basic_user=user,
            prom_basic_pass=pw,
            verify_ssl=env_bool("VERIFY_SSL", True),
            target_watch=env_bool("PROM_TARGET_WATCH", True),
            render_width=env_int("GRAFANA_RENDER_WIDTH", 1000),
            render_height=env_int("GRAFANA_RENDER_HEIGHT", 500),
            default_dashboard_uid=env("GRAFANA_DEFAULT_DASHBOARD_UID"),
        )
        for name, url in (("PROM_URL", s.prom_url), ("ALERTMANAGER_URL", s.alertmanager_url),
                          ("GRAFANA_URL", s.grafana_url or "http://x")):
            if not url.startswith(("http://", "https://")):
                raise RuntimeError(f"{name} must start with http:// or https:// (got {url!r})")
        if s.render_width < 100 or s.render_height < 100:
            raise RuntimeError("GRAFANA_RENDER_WIDTH/HEIGHT must be >= 100")
        return s

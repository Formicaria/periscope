"""v2 service definitions: the v1 Prometheus bot split into `prometheus`, `alertmanager` and `grafana`.

Each service owns only its own keys and clients; they share the `/prom` slash group when they run on the same
presence (`prom_group()` keys it on the presence tree), and the status board of `prometheus` picks up the
Alertmanager / Grafana clients of its sibling services so one board still covers the whole stack.
"""

from __future__ import annotations

import base64
from pathlib import Path

from periscope import ServiceBot, ServiceSpec, Setting, env_scope, settings_from_example
from periscope.http import HttpClient, HttpError

from .client import AlertmanagerClient, GrafanaClient, PrometheusClient
from .config import PromSettings

EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

PROM_KEYS = ("PROM_URL", "PROM_BASIC_USER", "PROM_BASIC_PASS", "PROM_TARGET_WATCH", "VERIFY_SSL")
AM_KEYS = ("ALERTMANAGER_URL", "PROM_BASIC_USER", "PROM_BASIC_PASS", "VERIFY_SSL")
GRAFANA_KEYS = ("GRAFANA_URL", "GRAFANA_TOKEN", "GRAFANA_ORG_ID", "GRAFANA_RENDER_WIDTH", "GRAFANA_RENDER_HEIGHT",
                "GRAFANA_DEFAULT_DASHBOARD_UID", "VERIFY_SSL")


def _settings(keys: tuple[str, ...], required: tuple[str, ...]) -> list[Setting]:
    """The subset of the v1 `.env.example` one service owns, in the example's order."""
    by_key = {s.key: s for s in settings_from_example(EXAMPLE, required=required)}
    return [by_key[k] for k in keys if k in by_key]


# ----- build --------------------------------------------------------------------------------------

async def build_prometheus(bot: ServiceBot) -> None:
    with env_scope(bot.env):
        cfg = PromSettings.from_env(require=("PROM_URL",))
    bot.cfg = cfg
    bot.prom = PrometheusClient(cfg)
    # only present when this service's own settings carry them; otherwise the status cog looks at siblings
    bot.am = AlertmanagerClient(cfg) if cfg.alertmanager_enabled else None
    bot.grafana = GrafanaClient(cfg) if cfg.grafana_enabled else None
    for path in ("periscope_prometheus.cogs.query", "periscope_prometheus.cogs.status"):
        await bot.load_extension(path)


async def build_alertmanager(bot: ServiceBot) -> None:
    with env_scope(bot.env):
        cfg = PromSettings.from_env(require=("ALERTMANAGER_URL",))
    bot.cfg = cfg
    bot.am = AlertmanagerClient(cfg)
    await bot.load_extension("periscope_prometheus.cogs.alertmanager")


async def build_grafana(bot: ServiceBot) -> None:
    with env_scope(bot.env):
        cfg = PromSettings.from_env(require=("GRAFANA_URL", "GRAFANA_TOKEN"))
    bot.cfg = cfg
    bot.grafana = GrafanaClient(cfg)
    await bot.load_extension("periscope_prometheus.cogs.grafana")


# ----- check ---------------------------------------------------------------------------------------

def _basic(env: dict[str, str]) -> dict[str, str]:
    """Optional HTTP basic auth header (Prometheus and Alertmanager behind the same reverse proxy)."""
    user, pw = env.get("PROM_BASIC_USER", ""), env.get("PROM_BASIC_PASS", "")
    if not (user and pw):
        return {}
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def _verify(env: dict[str, str]) -> bool:
    return env.get("VERIFY_SSL", "true").strip().lower() not in ("false", "0", "no", "off")


async def _ready(env: dict[str, str], key: str, label: str) -> tuple[bool, str]:
    url = env.get(key, "").strip().rstrip("/")
    if not url:
        return False, f"{key} is required"
    if not url.startswith(("http://", "https://")):
        return False, f"{key} must start with http:// or https://"
    client = HttpClient(url, headers=_basic(env), verify_ssl=_verify(env), timeout_s=10)
    try:
        body = (await client.get_bytes("/-/ready")).decode(errors="replace").strip()
        return True, f"{label} ready" + (f" — {body[:80]}" if body else "")
    except HttpError as e:
        hint = "check PROM_BASIC_USER / PROM_BASIC_PASS" if e.status in (401, 403) else "not ready"
        return False, f"{label} answered {e.status}: {hint}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    finally:
        await client.close()


async def check_prometheus(env: dict[str, str]) -> tuple[bool, str]:
    return await _ready(env, "PROM_URL", "Prometheus")


async def check_alertmanager(env: dict[str, str]) -> tuple[bool, str]:
    return await _ready(env, "ALERTMANAGER_URL", "Alertmanager")


async def check_grafana(env: dict[str, str]) -> tuple[bool, str]:
    url, token = env.get("GRAFANA_URL", "").strip().rstrip("/"), env.get("GRAFANA_TOKEN", "").strip()
    if not (url and token):
        return False, "GRAFANA_URL and GRAFANA_TOKEN are required"
    if not url.startswith(("http://", "https://")):
        return False, "GRAFANA_URL must start with http:// or https://"
    client = HttpClient(url, headers={"Authorization": f"Bearer {token}"}, verify_ssl=_verify(env), timeout_s=10)
    try:
        health = await client.get_json("/api/health")
        health = health if isinstance(health, dict) else {}
        # /api/health is unauthenticated; one tiny search proves the service-account token works
        try:
            await client.get_json("/api/search", params={"limit": 1})
        except HttpError as e:
            if e.status in (401, 403):
                return False, f"Grafana {health.get('version', '?')} reachable but rejected the token ({e.status})"
            raise
        db = health.get("database", "?")
        return db == "ok", f"Grafana {health.get('version', '?')} answered (database {db})"
    except HttpError as e:
        return False, f"Grafana answered {e.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    finally:
        await client.close()


SERVICES = [
    ServiceSpec(
        name="prometheus",
        title="Prometheus",
        description="Monitoring status board, scrape-target watcher, /prom query and targets.",
        group="infra",
        settings=_settings(PROM_KEYS, required=("PROM_URL",)),
        build=build_prometheus,
        check=check_prometheus,
        slash="/prom",
    ),
    ServiceSpec(
        name="alertmanager",
        title="Alertmanager",
        description="Every Alertmanager alert in #lab-alerts (resolve in place, Silence buttons), "
                    "/prom alerts and silences.",
        group="infra",
        settings=_settings(AM_KEYS, required=("ALERTMANAGER_URL",)),
        build=build_alertmanager,
        check=check_alertmanager,
        slash="/prom",
        webhook_paths=["/alertmanager"],
        needs_webhook=True,
    ),
    ServiceSpec(
        name="grafana",
        title="Grafana",
        description="Dashboard lookup and panel screenshots: /prom dashboards, panel, grafana.",
        group="infra",
        settings=_settings(GRAFANA_KEYS, required=("GRAFANA_URL", "GRAFANA_TOKEN")),
        build=build_grafana,
        check=check_grafana,
        slash="/prom",
    ),
]

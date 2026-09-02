"""Async API clients for Prometheus, Alertmanager and Grafana built on periscope.HttpClient."""

from __future__ import annotations

import datetime as dt
from typing import Any

import aiohttp

from periscope import HttpClient

from .config import PromSettings


def _basic_auth(cfg: PromSettings) -> aiohttp.BasicAuth | None:
    if cfg.prom_basic_user and cfg.prom_basic_pass:
        return aiohttp.BasicAuth(cfg.prom_basic_user, cfg.prom_basic_pass)
    return None


def _utc_iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class PrometheusClient:
    """Prometheus HTTP API v1 (read-only)."""

    def __init__(self, cfg: PromSettings):
        self.base_url = cfg.prom_url
        self.http = HttpClient(cfg.prom_url, auth=_basic_auth(cfg), verify_ssl=cfg.verify_ssl, timeout_s=20)

    async def _data(self, path: str, params: dict[str, Any] | None = None) -> Any:
        body = await self.http.get_json(path, params=params)
        if not isinstance(body, dict) or body.get("status") != "success":
            err = body.get("error", "unknown error") if isinstance(body, dict) else body
            raise RuntimeError(f"Prometheus error: {err}")
        return body.get("data")

    async def query(self, expr: str, time: float | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"query": expr}
        if time is not None:
            params["time"] = time
        return await self._data("api/v1/query", params)

    async def query_range(self, expr: str, start: float, end: float, step: str | float) -> dict[str, Any]:
        return await self._data("api/v1/query_range", {"query": expr, "start": start, "end": end, "step": step})

    async def targets(self, state: str = "active") -> list[dict[str, Any]]:
        data = await self._data("api/v1/targets", {"state": state})
        return list(data.get("activeTargets", []))

    async def alerts(self) -> list[dict[str, Any]]:
        data = await self._data("api/v1/alerts")
        return list(data.get("alerts", []))

    async def rules(self, kind: str | None = None) -> list[dict[str, Any]]:
        data = await self._data("api/v1/rules", {"type": kind} if kind else None)
        return list(data.get("groups", []))

    async def healthy(self) -> bool:
        try:
            await self.http.get_bytes("-/healthy")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self.http.close()


class AlertmanagerClient:
    """Alertmanager API v2: alerts + silences."""

    def __init__(self, cfg: PromSettings):
        self.base_url = cfg.alertmanager_url
        self.http = HttpClient(cfg.alertmanager_url, auth=_basic_auth(cfg), verify_ssl=cfg.verify_ssl, timeout_s=20)

    async def alerts(self, *, active: bool = True, silenced: bool = False, inhibited: bool = False,
                     unprocessed: bool = False) -> list[dict[str, Any]]:
        params = {
            "active": str(active).lower(),
            "silenced": str(silenced).lower(),
            "inhibited": str(inhibited).lower(),
            "unprocessed": str(unprocessed).lower(),
        }
        return list(await self.http.get_json("api/v2/alerts", params=params) or [])

    async def silences(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        items = list(await self.http.get_json("api/v2/silences") or [])
        if active_only:
            items = [s for s in items if (s.get("status") or {}).get("state") in ("active", "pending")]
        return items

    async def create_silence(self, labels: dict[str, str], duration_s: int, created_by: str,
                             comment: str) -> str:
        now = dt.datetime.now(dt.timezone.utc)
        payload = {
            "matchers": [{"name": k, "value": v, "isRegex": False, "isEqual": True} for k, v in labels.items()],
            "startsAt": _utc_iso(now),
            "endsAt": _utc_iso(now + dt.timedelta(seconds=duration_s)),
            "createdBy": created_by,
            "comment": comment,
        }
        body = await self.http.post_json("api/v2/silences", json=payload)
        if isinstance(body, dict) and body.get("silenceID"):
            return str(body["silenceID"])
        raise RuntimeError(f"unexpected Alertmanager response: {body!r}")

    async def delete_silence(self, silence_id: str) -> None:
        # Note the singular path: DELETE /api/v2/silence/{id} (GET/POST use /silences).
        await self.http.delete(f"api/v2/silence/{silence_id}")

    async def healthy(self) -> bool:
        try:
            await self.http.get_bytes("-/healthy")
            return True
        except Exception:
            return False

    async def close(self) -> None:
        await self.http.close()


class GrafanaClient:
    """Grafana HTTP API with a service-account bearer token.

    Panel rendering (`render_panel`) hits `render/d-solo/...`, which requires the
    `grafana-image-renderer` plugin (or a remote renderer) on the Grafana side.
    """

    def __init__(self, cfg: PromSettings):
        self.base_url = cfg.grafana_url or ""
        self.org_id = cfg.grafana_org_id
        self.width = cfg.render_width
        self.height = cfg.render_height
        headers = {"Authorization": f"Bearer {cfg.grafana_token}"} if cfg.grafana_token else {}
        self.http = HttpClient(self.base_url, headers=headers, verify_ssl=cfg.verify_ssl, timeout_s=60)

    async def search_dashboards(self, query: str = "", limit: int = 100) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"type": "dash-db", "limit": limit}
        if query:
            params["query"] = query
        return list(await self.http.get_json("api/search", params=params) or [])

    async def dashboard(self, uid: str) -> dict[str, Any]:
        return await self.http.get_json(f"api/dashboards/uid/{uid}")

    def dashboard_url(self, uid: str, slug: str = "") -> str:
        return f"{self.base_url}/d/{uid}/{slug}".rstrip("/")

    async def render_panel(self, uid: str, slug: str, panel_id: int, *, range_: str = "6h",
                           width: int | None = None, height: int | None = None) -> bytes:
        params = {
            "panelId": panel_id,
            "width": width or self.width,
            "height": height or self.height,
            "from": f"now-{range_}",
            "to": "now",
            "orgId": self.org_id,
            "tz": "UTC",
        }
        return await self.http.get_bytes(f"render/d-solo/{uid}/{slug or 'd'}", params=params)

    async def health(self) -> dict[str, Any]:
        body = await self.http.get_json("api/health")
        return body if isinstance(body, dict) else {}

    async def healthy(self) -> bool:
        try:
            return (await self.health()).get("database") == "ok"
        except Exception:
            return False

    async def close(self) -> None:
        await self.http.close()

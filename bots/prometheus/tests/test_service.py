"""v2: prometheus / alertmanager / grafana as three services sharing one presence and one /prom group."""

import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer
from discord import app_commands
from periscope import Store
from periscope.http import HttpClient, HttpError
from periscope.runtime import Runtime

from periscope_prometheus.client import AlertmanagerClient, GrafanaClient, PrometheusClient
from periscope_prometheus.config import PromSettings
from periscope_prometheus.service import SERVICES

PROM = {"PROM_URL": "http://prom:9090", "PROM_TARGET_WATCH": "false"}
AM = {"ALERTMANAGER_URL": "http://am:9093"}
GRAFANA = {"GRAFANA_URL": "http://grafana:3000", "GRAFANA_TOKEN": "glsa_x", "GRAFANA_DEFAULT_DASHBOARD_UID": "abc"}
EXPECTED = {"alerts", "silences", "unsilence", "query", "targets", "dashboards", "panel", "grafana", "status"}


def make_runtime(tmp_path, services: dict[str, dict]) -> Runtime:
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.lab.update({"alert_channel_id": "2", "status_channel_id": "1"})
    s.webhook["secret"] = "s3cret"
    for name, env in services.items():
        s.services[name] = {"enabled": True, "presence": "default", "env": env}
    rt = Runtime(s, tmp_path)
    rt.assemble()
    assert not rt.skipped, rt.skipped
    return rt


async def build_all(rt: Runtime, *names: str):
    pres = rt.presences["default"]

    async def never_ready():  # the presence never logs in; loops stay parked in before_loop until unload
        await asyncio.Event().wait()

    pres.wait_until_ready = never_ready
    for n in names:
        await rt.services[n].spec.build(rt.services[n])
    return pres


async def teardown(rt: Runtime, *names: str):
    for n in names:
        sb = rt.services[n]
        await sb.unload()
        for attr in ("prom", "am", "grafana"):
            client = getattr(sb, attr, None)
            if client is not None:
                await client.close()


def test_specs():
    specs = {s.name: s for s in SERVICES}
    assert list(specs) == ["prometheus", "alertmanager", "grafana"]
    assert [x.key for x in specs["prometheus"].settings] == ["PROM_URL", "PROM_BASIC_USER", "PROM_BASIC_PASS", "PROM_TARGET_WATCH", "VERIFY_SSL"]
    assert [x.key for x in specs["alertmanager"].settings] == ["ALERTMANAGER_URL", "PROM_BASIC_USER", "PROM_BASIC_PASS", "VERIFY_SSL"]
    assert [x.key for x in specs["grafana"].settings] == ["GRAFANA_URL", "GRAFANA_TOKEN", "GRAFANA_ORG_ID", "GRAFANA_RENDER_WIDTH",
                                                          "GRAFANA_RENDER_HEIGHT", "GRAFANA_DEFAULT_DASHBOARD_UID", "VERIFY_SSL"]
    assert specs["prometheus"].required_missing({}) == ["PROM_URL"]
    assert specs["alertmanager"].required_missing({}) == ["ALERTMANAGER_URL"]
    assert specs["grafana"].required_missing({"GRAFANA_URL": "http://g"}) == ["GRAFANA_TOKEN"]
    assert specs["alertmanager"].needs_webhook and specs["alertmanager"].webhook_paths == ["/alertmanager"]
    assert not specs["prometheus"].needs_webhook and not specs["grafana"].needs_webhook
    assert all(s.slash == "/prom" and s.group == "infra" for s in SERVICES)
    assert specs["prometheus"].setting("PROM_BASIC_PASS").type == "secret" and specs["grafana"].setting("GRAFANA_TOKEN").type == "secret"


def test_settings_per_service(monkeypatch):
    for k in ("PROM_URL", "ALERTMANAGER_URL", "GRAFANA_URL", "GRAFANA_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("ALERTMANAGER_URL", "http://am:9093/")
    s = PromSettings.from_env(require=("ALERTMANAGER_URL",))             # no PROM_URL needed for the alertmanager service
    assert s.alertmanager_url == "http://am:9093" and s.prom_url is None and not s.prom_enabled and s.alertmanager_enabled
    with pytest.raises(RuntimeError, match="PROM_URL"):
        PromSettings.from_env()                                            # v1 still wants both
    with pytest.raises(RuntimeError, match="GRAFANA_URL"):
        PromSettings.from_env(require=("GRAFANA_URL", "GRAFANA_TOKEN"))


@pytest.mark.asyncio
async def test_three_services_share_prom_group(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, {"prometheus": PROM, "alertmanager": AM, "grafana": GRAFANA})
    pres = await build_all(rt, "prometheus", "alertmanager", "grafana")
    prom, am, gf = rt.services["prometheus"], rt.services["alertmanager"], rt.services["grafana"]
    group = pres.tree.get_command("prom")
    assert isinstance(group, app_commands.Group) and {c.name for c in group.commands} == EXPECTED
    assert [c.name for c in pres.tree.get_commands()] == ["prom"]        # one shared group, not three
    names = {c.qualified_name for c in pres.cogs.values()}
    assert names == {"prometheus:QueryCog", "prometheus:StatusCog", "alertmanager:AlertmanagerCog", "grafana:GrafanaCog"}
    assert prom.cfg.prom_url == "http://prom:9090" and prom.am is None and prom.grafana is None    # own keys only
    assert am.cfg.alertmanager_url == "http://am:9093" and gf.cfg.grafana_url == "http://grafana:3000"
    assert ("POST", "/alertmanager") in {(r.method, r.resource.canonical) for r in rt.webhook.app.router.routes()}
    # the status board of `prometheus` sees the sibling services' clients
    status = prom.get_cog("StatusCog")
    assert status.owner("am") is am and status.client("am") is am.am and status.client("grafana") is gf.grafana

    async def healthy(self):
        return True

    async def targets(self, state="active"):
        return [{"labels": {"job": "node", "instance": "a:9100"}, "health": "up"}]

    async def alerts(self, **kw):
        return [{"labels": {"alertname": "X", "severity": "warning"}}]

    async def silences(self, **kw):
        return []

    monkeypatch.setattr(PrometheusClient, "healthy", healthy)
    monkeypatch.setattr(PrometheusClient, "targets", targets)
    monkeypatch.setattr(AlertmanagerClient, "healthy", healthy)
    monkeypatch.setattr(AlertmanagerClient, "alerts", alerts)
    monkeypatch.setattr(AlertmanagerClient, "silences", silences)
    monkeypatch.setattr(GrafanaClient, "healthy", healthy)
    e = await status.build_embed()
    fields = {f.name: f.value for f in e.fields}
    assert fields["Prometheus"].endswith("up") and fields["Alertmanager"].endswith("up") and fields["Grafana"].endswith("up")
    assert "1 warning" in fields["Firing alerts"] and "1 up" in fields["Scrape targets"] and fields["Active silences"] == "🔕 0"
    assert "[Dashboard](http://grafana:3000/d/abc)" in fields["Links"]     # from the grafana service's own settings
    await teardown(rt, "prometheus", "alertmanager", "grafana")


@pytest.mark.asyncio
async def test_prometheus_alone(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, {"prometheus": PROM})
    pres = await build_all(rt, "prometheus")
    assert {c.name for c in pres.tree.get_command("prom").commands} == {"query", "targets", "status"}
    status = rt.services["prometheus"].get_cog("StatusCog")
    assert status.owner("am") is None and status.client("grafana") is None

    async def healthy(self):
        return True

    async def targets(self, state="active"):
        return []

    monkeypatch.setattr(PrometheusClient, "healthy", healthy)
    monkeypatch.setattr(PrometheusClient, "targets", targets)
    e = await status.build_embed()
    fields = {f.name: f.value for f in e.fields}
    assert fields["Alertmanager"].endswith("not configured") and fields["Grafana"].endswith("not configured")
    assert fields["Prometheus"].endswith("up") and fields["Links"] == "[Prometheus](http://prom:9090)"
    await teardown(rt, "prometheus")


@pytest.mark.asyncio
async def test_alertmanager_webhook_on_shared_server(tmp_path, monkeypatch):
    rt = make_runtime(tmp_path, {"alertmanager": AM})
    await build_all(rt, "alertmanager")
    sb = rt.services["alertmanager"]
    fired, resolved = [], []

    async def fake_fire(alert, force=False):
        fired.append((alert.fingerprint, force))

    async def fake_resolve(fp, note=None):
        resolved.append(fp)

    monkeypatch.setattr(sb.alerts, "fire", fake_fire)
    monkeypatch.setattr(sb.alerts, "resolve", fake_resolve)
    payload = {"receiver": "discord", "alerts": [
        {"status": "firing", "fingerprint": "f1", "labels": {"alertname": "A", "severity": "critical"}, "annotations": {}},
        {"status": "resolved", "fingerprint": "f2", "labels": {"alertname": "B"}, "annotations": {}}]}
    async with TestClient(TestServer(rt.webhook.app)) as client:
        assert (await client.post("/alertmanager", data=json.dumps(payload))).status == 401
        r = await client.post("/alertmanager?token=s3cret", data=json.dumps(payload))
        assert r.status == 200 and await r.json() == {"ok": True, "fired": 1, "resolved": 1}
    assert fired == [("am:f1", True)] and resolved == ["am:f2"]
    await teardown(rt, "alertmanager")


@pytest.mark.asyncio
async def test_checks(monkeypatch):
    seen = []

    async def get_bytes(self, path, **kw):
        auth = self._headers.get("Authorization")
        seen.append((self.base_url, path, auth))
        if self.base_url.startswith("http://down"):
            raise OSError("connection refused")
        if self.base_url.startswith("http://locked") and auth != "Basic dTpw":      # u:p
            raise HttpError(401, self.base_url + path, "Unauthorized")
        return b"Prometheus Server is Ready.\n" if "prom" in self.base_url else b"OK"

    async def get_json(self, path, **kw):
        seen.append((self.base_url, path, self._headers.get("Authorization")))
        if path == "/api/health":
            return {"database": "ok", "version": "11.2.0"}
        if path == "/api/search":
            if self._headers.get("Authorization") != "Bearer good":
                raise HttpError(401, self.base_url + path, "Unauthorized")
            return []
        return {}

    monkeypatch.setattr(HttpClient, "get_bytes", get_bytes)
    monkeypatch.setattr(HttpClient, "get_json", get_json)
    checks = {s.name: s.check for s in SERVICES}
    assert await checks["prometheus"]({"PROM_URL": "http://prom:9090/"}) == (True, "Prometheus ready — Prometheus Server is Ready.")
    assert await checks["alertmanager"]({"ALERTMANAGER_URL": "http://am:9093"}) == (True, "Alertmanager ready — OK")
    assert (await checks["prometheus"]({}))[1] == "PROM_URL is required"
    ok, msg = await checks["prometheus"]({"PROM_URL": "http://down:9090"})
    assert not ok and "unreachable" in msg
    ok, msg = await checks["alertmanager"]({"ALERTMANAGER_URL": "http://locked:9093"})
    assert not ok and "401" in msg and "PROM_BASIC_USER" in msg
    assert (await checks["alertmanager"]({"ALERTMANAGER_URL": "http://locked:9093", "PROM_BASIC_USER": "u", "PROM_BASIC_PASS": "p"}))[0]
    assert seen[-1][2] is not None                                          # basic auth was sent
    assert await checks["grafana"]({"GRAFANA_URL": "http://g:3000", "GRAFANA_TOKEN": "good"}) == (True, "Grafana 11.2.0 answered (database ok)")
    ok, msg = await checks["grafana"]({"GRAFANA_URL": "http://g:3000", "GRAFANA_TOKEN": "bad"})
    assert not ok and "rejected the token" in msg
    assert (await checks["grafana"]({"GRAFANA_URL": "http://g:3000"}))[1] == "GRAFANA_URL and GRAFANA_TOKEN are required"
    assert (await checks["prometheus"]({"PROM_URL": "prom:9090"}))[1].startswith("PROM_URL must start")

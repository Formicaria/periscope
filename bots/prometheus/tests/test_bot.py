"""Loads every cog into a real (never-connected) PromBot and checks the /prom command tree + webhook route."""

import asyncio
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from discord import app_commands

from periscope import Settings
from periscope_prometheus.bot import COGS, PromBot
from periscope_prometheus.config import PromSettings

EXPECTED = {"alerts", "silences", "unsilence", "query", "targets", "dashboards", "panel", "grafana", "status"}


@pytest.fixture
async def bot(tmp_path):
    settings = Settings(discord_token="x", data_dir=tmp_path, status_interval_s=60, webhook_secret="s3cret")
    cfg = PromSettings(prom_url="http://127.0.0.1:1", alertmanager_url="http://127.0.0.1:1",
                       grafana_url="http://127.0.0.1:1", grafana_token="t", target_watch=False)
    b = PromBot(settings, cfg)

    async def never_ready():  # the bot never logs in; keep loops parked in before_loop until unload
        await asyncio.Event().wait()

    b.wait_until_ready = never_ready
    for path in COGS:
        await b.load_extension(path)
    yield b
    for cog in list(b.cogs):
        await b.remove_cog(cog)
    await b.close()


async def test_prom_group_has_all_commands(bot):
    group = bot.tree.get_command("prom")
    assert isinstance(group, app_commands.Group)
    names = {c.name for c in group.commands}
    assert names == EXPECTED
    assert [c.name for c in bot.tree.get_commands()] == ["prom"]
    panel = group.get_command("panel")
    assert "dashboard" in panel._params and panel._params["dashboard"].autocomplete is not None


async def test_webhook_route(bot, monkeypatch):
    fired, resolved = [], []

    async def fake_fire(alert, force=False):
        fired.append((alert.fingerprint, alert.severity.value, force))

    async def fake_resolve(fp, note=None):
        resolved.append(fp)

    monkeypatch.setattr(bot.alerts, "fire", fake_fire)
    monkeypatch.setattr(bot.alerts, "resolve", fake_resolve)

    payload = {"version": "4", "receiver": "discord", "alerts": [
        {"status": "firing", "fingerprint": "f1", "labels": {"alertname": "A", "severity": "critical"},
         "annotations": {"summary": "s"}},
        {"status": "resolved", "fingerprint": "f2", "labels": {"alertname": "B"}, "annotations": {}},
    ]}
    async with TestClient(TestServer(bot.webhook.app)) as client:
        r = await client.post("/alertmanager", data=json.dumps(payload))
        assert r.status == 401
        r = await client.post("/alertmanager?token=s3cret", data=json.dumps(payload))
        assert r.status == 200 and (await r.json()) == {"ok": True, "fired": 1, "resolved": 1}
        r = await client.post("/alertmanager", data="nope", headers={"X-Webhook-Secret": "s3cret"})
        assert r.status == 400
    assert fired == [("am:f1", "critical", True)]
    assert resolved == ["am:f2"]

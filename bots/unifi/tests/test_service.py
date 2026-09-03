"""v2: the unifi service built on a shared presence, plus its login check."""

import asyncio

import pytest
from discord import app_commands
from periscope import Store
from periscope.http import HttpClient, HttpError
from periscope.runtime import Runtime

from periscope_unifi.cogs import unifi
from periscope_unifi.service import SERVICES

EXPECTED = {"clients", "client", "kick", "block", "unblock", "devices", "device", "restart", "wan", "events", "alarms"}
ENV = {"UNIFI_URL": "https://192.168.1.1/", "UNIFI_USER": "periscope", "UNIFI_PASS": "pw", "UNIFI_SITE": "home"}


def test_spec():
    (spec,) = SERVICES
    assert spec.name == "unifi" and spec.slash == "/unifi" and spec.group == "infra" and not spec.needs_webhook
    keys = [s.key for s in spec.settings]
    assert keys == ["UNIFI_URL", "UNIFI_USER", "UNIFI_PASS", "UNIFI_SITE", "UNIFI_IS_UNIFI_OS", "VERIFY_SSL",
                    "UNIFI_ALERT_NEW_CLIENTS", "UNIFI_WAN_LATENCY_WARN_MS", "UNIFI_DEVICE_CPU_WARN", "UNIFI_KNOWN_CLIENTS_TTL_DAYS"]
    assert spec.required_missing({"UNIFI_URL": "https://x"}) == ["UNIFI_USER", "UNIFI_PASS"]
    assert spec.setting("UNIFI_PASS").type == "secret" and spec.setting("UNIFI_IS_UNIFI_OS").type == "bool"
    assert all(s.help for s in spec.settings)                       # every field explains itself in the UI


@pytest.mark.asyncio
async def test_build_on_presence(tmp_path):
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.lab.update({"alert_channel_id": "2", "status_channel_id": "1"})
    s.services["unifi"] = {"enabled": True, "presence": "default", "env": ENV}
    rt = Runtime(s, tmp_path)
    rt.assemble()
    assert not rt.skipped
    pres, sb = rt.presences["default"], rt.services["unifi"]

    async def never_ready():
        await asyncio.Event().wait()

    pres.wait_until_ready = never_ready
    await sb.spec.build(sb)
    assert sb.cfg.url == "https://192.168.1.1" and sb.cfg.site == "home" and sb.unifi.cfg is sb.cfg
    group = pres.tree.get_command("unifi")
    assert isinstance(group, app_commands.Group) and group is unifi and {c.name for c in group.commands} == EXPECTED
    names = {c.qualified_name for c in pres.cogs.values()}
    assert names == {"unifi:StatusCog", "unifi:ClientsCog", "unifi:DevicesCog"}
    assert group.get_command("kick").binding is sb.get_cog("ClientsCog")      # bound to this presence's cog
    assert group.get_command("restart").binding is sb.get_cog("DevicesCog")
    assert sb.get_cog("StatusCog").board.channel_id == 1 and sb.alerts.active() == []
    assert rt.status()["services"]["unifi"]["presence"] == "default"
    await sb.unload()
    await sb.unifi.close()


class FakeResp:
    def __init__(self, cookies):
        self.cookies = cookies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def read(self):
        return b"{}"


@pytest.mark.asyncio
async def test_check(monkeypatch):
    calls = []

    async def request(self, method, path, **kw):
        calls.append((method, self.base_url, path, kw.get("json"), self._verify_ssl))
        if self.base_url.startswith("https://down"):
            raise OSError("no route to host")
        if kw["json"]["password"] != "pw":
            raise HttpError(401, self.base_url + path, "Unauthorized")
        return FakeResp({"TOKEN": "x"} if path == "/api/auth/login" else {})

    monkeypatch.setattr(HttpClient, "request", request)
    (check,) = [s.check for s in SERVICES]
    assert await check(ENV) == (True, "UniFi login works (UniFi OS console)")
    assert calls[-1][:4] == ("POST", "https://192.168.1.1", "/api/auth/login", {"username": "periscope", "password": "pw"})
    assert calls[-1][4] is False                                            # VERIFY_SSL defaults to false for UniFi
    ok, msg = await check({**ENV, "UNIFI_IS_UNIFI_OS": "false", "VERIFY_SSL": "true"})
    assert ok and "self-hosted" in msg and "no session cookie" in msg and calls[-1][2] == "/api/login" and calls[-1][4] is True
    ok, msg = await check({**ENV, "UNIFI_PASS": "wrong"})
    assert not ok and "401" in msg and "UNIFI_PASS" in msg
    ok, msg = await check({**ENV, "UNIFI_URL": "https://down"})
    assert not ok and "unreachable" in msg
    assert (await check({"UNIFI_URL": "https://x"}))[1].endswith("are required")
    assert (await check({**ENV, "UNIFI_URL": "192.168.1.1"}))[1].startswith("UNIFI_URL must start")

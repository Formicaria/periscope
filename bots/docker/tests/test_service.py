"""v2: the docker service built on a shared presence, plus what its Test button reports."""

import asyncio

import pytest
from discord import app_commands
from periscope import Store
from periscope.http import HttpClient, HttpError
from periscope.runtime import Runtime

from periscope_docker import samples
from periscope_docker.cogs import docker as docker_group
from periscope_docker.service import SERVICES

EXPECTED = {"ps", "restart", "start", "stop", "logs", "stats", "updates"}
ADMIN = ("restart", "start", "stop")
ENV = {"DOCKER_HOST": "tcp://10.0.0.5:2375", "DOCKER_IGNORE": "buildx_*", "DOCKER_POLL_S": "30"}


def test_spec():
    (spec,) = SERVICES
    assert spec.name == "docker" and spec.slash == "/docker" and spec.group == "infra"
    assert not spec.needs_webhook and not spec.intents and spec.title == "Docker"
    keys = [s.key for s in spec.settings]
    assert keys == ["DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CA_PATH", "DOCKER_CERT_PATH", "DOCKER_KEY_PATH",
                    "PORTAINER_URL", "PORTAINER_API_KEY", "PORTAINER_ENDPOINT_ID", "DOCKER_INCLUDE", "DOCKER_IGNORE",
                    "DOCKER_POLL_S", "DOCKER_RESTART_LOOP_N", "DOCKER_ALERT_ON_STOP", "DOCKER_CHECK_UPDATES",
                    "DOCKER_UPDATE_CHECK_H"]
    # nothing is required: a socket-mounted install works with no settings at all
    assert spec.required_missing({}) == []
    assert spec.setting("DOCKER_HOST").default == "/var/run/docker.sock"
    assert spec.setting("PORTAINER_API_KEY").type == "secret" and spec.setting("PORTAINER_URL").type == "url"
    assert spec.setting("DOCKER_ALERT_ON_STOP").type == "bool" and spec.setting("DOCKER_POLL_S").type == "int"
    assert spec.setting("DOCKER_INCLUDE").type == "list" and spec.setting("DOCKER_IGNORE").type == "list"
    assert {s.group for s in spec.settings} == {"Docker daemon", "Portainer (instead of the socket)", "What to watch"}
    assert all(s.help for s in spec.settings)                       # every field explains itself in the UI


@pytest.mark.asyncio
async def test_build_on_presence(tmp_path):
    s = Store(tmp_path / "config" / "periscope.yaml")
    s.presences["default"]["token"] = "T"
    s.lab.update({"alert_channel_id": "2", "status_channel_id": "1"})
    s.services["docker"] = {"enabled": True, "presence": "default", "env": ENV}
    rt = Runtime(s, tmp_path)
    rt.assemble()
    assert not rt.skipped, rt.skipped
    pres, sb = rt.presences["default"], rt.services["docker"]

    async def never_ready():
        await asyncio.Event().wait()

    pres.wait_until_ready = never_ready
    await sb.spec.build(sb)
    assert sb.cfg.base_url == "http://10.0.0.5:2375" and sb.cfg.mode == "tcp" and sb.docker.cfg is sb.cfg
    assert sb.cfg.ignore == ["buildx_*"] and not sb.cfg.watches("buildx_buildkit_default")

    group = pres.tree.get_command("docker")
    assert isinstance(group, app_commands.Group) and group is docker_group
    assert {c.name for c in group.commands} == EXPECTED
    names = {c.qualified_name for c in pres.cogs.values()}
    assert names == {"docker:StatusCog", "docker:ContainersCog"}
    containers_cog = sb.get_cog("ContainersCog")
    assert group.get_command("restart").binding is containers_cog
    assert group.get_command("logs")._params["container"].autocomplete.pass_command_binding is True
    for name in ADMIN:
        assert group.get_command(name).checks, f"/docker {name} must be admin-gated"
    assert not group.get_command("ps").checks

    status = sb.get_cog("StatusCog")
    assert status.board.channel_id == 1 and status.board.kind == "docker.board"
    assert status.tick.seconds == 30 and sb.alerts.active() == []      # DOCKER_POLL_S drives the loop
    assert status.view in pres.persistent_views
    await sb.unload()
    await sb.docker.close()


class FakeResp:
    def __init__(self, payload):
        self.payload = payload
        self.status, self.content_length = 200, 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self.payload


class Wrapped(OSError):
    """What aiohttp raises when a connection fails: the real reason hangs off `os_error`."""

    def __init__(self, os_error):
        super().__init__(f"Cannot connect to host: {os_error}")
        self.os_error = os_error


@pytest.mark.asyncio
async def test_check(monkeypatch):
    seen = []
    fail: list[Exception] = []

    async def request(self, method, path, **kw):
        seen.append((self._url(path), dict(self._headers), getattr(self, "socket_path", "")))
        if fail:
            raise fail[0]
        return FakeResp(samples.VERSION if path == "/version" else samples.CONTAINERS)

    monkeypatch.setattr(HttpClient, "request", request)
    (check,) = [s.check for s in SERVICES]

    assert await check({}) == (True, "Docker 27.1.1 · 7 containers")
    assert seen[0][0] == "http://docker/version" and seen[0][2] == "/var/run/docker.sock"
    ok, msg = await check({"PORTAINER_URL": "https://portainer.lan", "PORTAINER_API_KEY": "k"})
    assert ok and msg.endswith("7 containers")
    assert seen[-1][0] == "https://portainer.lan/api/endpoints/1/docker/containers/json"
    assert seen[-1][1] == {"X-API-Key": "k"}

    fail.append(Wrapped(PermissionError(13, "Permission denied")))
    ok, msg = await check({})
    assert not ok and msg.startswith("cannot reach the Docker socket at /var/run/docker.sock")
    assert "docker group" in msg
    fail[0] = FileNotFoundError(2, "No such file or directory")
    assert (await check({}))[1].startswith("there is no Docker socket at /var/run/docker.sock")
    fail[0] = ConnectionRefusedError(111, "Connection refused")
    ok, msg = await check({"DOCKER_HOST": "tcp://10.0.0.5:2375"})
    assert not ok and msg.startswith("cannot reach the Docker daemon at http://10.0.0.5:2375")

    portainer = {"PORTAINER_URL": "https://portainer.lan", "PORTAINER_API_KEY": "k", "PORTAINER_ENDPOINT_ID": "3"}
    fail[0] = HttpError(401, "https://portainer.lan/api/endpoints/3/docker/version", "invalid token")
    assert (await check(portainer))[1] == "Portainer rejected the API key (401) — check PORTAINER_API_KEY"
    fail[0] = HttpError(404, "https://portainer.lan/api/endpoints/3/docker/version", "endpoint not found")
    assert "PORTAINER_ENDPOINT_ID" in (await check(portainer))[1]
    fail[0] = HttpError(400, "http://10.0.0.5:2375/version",
                        "Client sent an HTTP request to an HTTPS server.")
    assert "wants TLS" in (await check({"DOCKER_HOST": "tcp://10.0.0.5:2375"}))[1]

    # bad settings never reach the daemon: the message names the setting to fix
    before = len(seen)
    assert (await check({"DOCKER_HOST": "10.0.0.5:2375"}))[1].startswith("DOCKER_HOST must be")
    assert (await check({"PORTAINER_URL": "https://portainer.lan"}))[1].startswith("PORTAINER_API_KEY is required")
    assert len(seen) == before

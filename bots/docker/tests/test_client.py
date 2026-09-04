"""The client: one set of paths, three ways in (unix socket, tcp, Portainer's proxy), aiohttp mocked out."""

import ssl

import aiohttp
import pytest
from periscope import env_scope
from periscope.http import HttpClient, HttpError

from periscope_docker import samples
from periscope_docker.client import DockerClient, DockerHttp, tls_context
from periscope_docker.config import DockerConfig

SOCKET = {}
TCP = {"DOCKER_HOST": "tcp://10.0.0.5:2375"}
PORTAINER = {"PORTAINER_URL": "https://portainer.lan:9443", "PORTAINER_API_KEY": "ptr_abc",
             "PORTAINER_ENDPOINT_ID": "3"}


def cfg(env) -> DockerConfig:
    with env_scope(env):
        return DockerConfig.from_env()


class FakeResp:
    def __init__(self, payload=None, body=b""):
        self.payload, self.body = payload, body
        self.status, self.content_length = 200, len(body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self, content_type=None):
        return self.payload

    async def read(self):
        return self.body


@pytest.fixture
def calls(monkeypatch):
    """Record every request the client makes and answer it from the sample payloads."""
    seen = []

    async def request(self, method, path, **kw):
        seen.append({"method": method, "url": self._url(path), "params": kw.get("params"),
                     "headers": dict(self._headers), "socket": getattr(self, "socket_path", "")})
        if path == "/version":
            return FakeResp(samples.VERSION)
        if path == "/containers/json":
            return FakeResp(samples.CONTAINERS)
        if path == "/images/json":
            return FakeResp(samples.IMAGES)
        if path.startswith("/distribution/"):
            ref = path[len("/distribution/"):-len("/json")]
            if ref not in samples.REGISTRY_DIGESTS:
                raise HttpError(401, self._url(path), "unauthorized: authentication required")
            return FakeResp({"Descriptor": {"digest": samples.REGISTRY_DIGESTS[ref]}})
        if path.endswith("/stats"):
            return FakeResp(samples.STATS["jellyfin"])
        if path.endswith("/logs"):
            return FakeResp(body=samples.log_stream())
        return FakeResp({})

    monkeypatch.setattr(HttpClient, "request", request)
    return seen


@pytest.mark.asyncio
async def test_every_transport_asks_for_the_same_paths(calls):
    for env, base in ((SOCKET, "http://docker"), (TCP, "http://10.0.0.5:2375"),
                      (PORTAINER, "https://portainer.lan:9443/api/endpoints/3/docker")):
        client = DockerClient(cfg(env))
        await client.version()
        await client.containers()
        await client.logs("abc123", 20)
        await client.action("abc123", "restart")
        assert [c["url"] for c in calls[-4:]] == [f"{base}/version", f"{base}/containers/json",
                                                  f"{base}/containers/abc123/logs",
                                                  f"{base}/containers/abc123/restart"]
        assert calls[-1]["method"] == "POST" and calls[-2]["params"] == {"stdout": "1", "stderr": "1", "tail": "20"}
        assert calls[-3]["params"] == {"all": "1"}
        await client.close()
    # only Portainer needs a credential, and only the socket transport dials a socket
    assert calls[3]["headers"] == {} and calls[3]["socket"] == "/var/run/docker.sock"
    assert calls[-1]["headers"] == {"X-API-Key": "ptr_abc"} and calls[-1]["socket"] == ""


@pytest.mark.asyncio
async def test_the_connector_follows_the_transport():
    unix = DockerHttp("http://docker", socket_path="/var/run/docker.sock").connector()
    assert isinstance(unix, aiohttp.UnixConnector) and unix.path == "/var/run/docker.sock"
    await unix.close()
    plain = DockerHttp("http://10.0.0.5:2375").connector()
    assert isinstance(plain, aiohttp.TCPConnector)
    await plain.close()
    # TLS is only built for an https endpoint, and DOCKER_TLS_VERIFY decides whether the certificate is checked
    assert tls_context(cfg(SOCKET)) is None and tls_context(cfg(TCP)) is None
    checked = tls_context(cfg({"DOCKER_HOST": "https://docker.lan:2376", "DOCKER_TLS_VERIFY": "true"}))
    assert checked.verify_mode is ssl.CERT_REQUIRED and checked.check_hostname
    unchecked = tls_context(cfg({"DOCKER_HOST": "https://docker.lan:2376"}))
    assert unchecked.verify_mode is ssl.CERT_NONE and not unchecked.check_hostname


@pytest.mark.asyncio
async def test_reads_cache_what_autocomplete_needs(calls):
    client = DockerClient(cfg(SOCKET))
    containers = await client.containers()
    assert [c.name for c in containers] == [c.name for c in client.cached] and len(containers) == 7
    assert client.find("sonarr").name == "sonarr" and client.find("SONARR").name == "sonarr"
    assert client.find("3f0a1c9d").name == "jellyfin"                    # an id prefix works too
    assert client.find("arr").name == "sonarr" and client.find("nothing here") is None
    assert client.find("") is None
    # the log tail is capped whatever is asked for, and comes back unframed
    text = await client.logs("abc", 5000)
    assert calls[-1]["params"]["tail"] == "200" and text.startswith("[2026-09-02 21:14:03] Starting Sonarr")
    with pytest.raises(ValueError, match="unsupported action"):
        await client.action("abc", "destroy")
    await client.close()


@pytest.mark.asyncio
async def test_stats_are_sampled_for_running_containers_only(calls):
    client = DockerClient(cfg(SOCKET))
    containers = await client.containers()
    before = len(calls)
    await client.sample(containers)
    assert len(calls) - before == 4                                      # four running, the rest are not sampled
    running = {c.name: c for c in containers if c.running}
    assert running["jellyfin"].cpu_pct == 24.0 and running["jellyfin"].mem_used == 1_207_959_552
    assert all(c.cpu_pct is None for c in containers if not c.running)
    await client.sample([c for c in containers if not c.running])        # nothing to do, nothing asked
    assert len(calls) - before == 4
    await client.close()


@pytest.mark.asyncio
async def test_updates_are_what_the_registry_contradicts(calls):
    client = DockerClient(cfg(SOCKET))
    refs = ["linuxserver/jellyfin:10.9.11", "traefik:v3.1", "linuxserver/sonarr:4.0.9",
            "prodrigestivill/postgres-backup-local:16"]
    found = await client.updates(refs)
    assert [u["ref"] for u in found] == ["traefik:v3.1", "linuxserver/sonarr:4.0.9"]
    assert found[0]["remote"] == samples.REGISTRY_DIGESTS["traefik:v3.1"]
    # the last ref has no local image at all, and a registry that answers 401 is quietly skipped
    assert await client.registry_digest("private.lan/thing:1") == ""
    await client.close()

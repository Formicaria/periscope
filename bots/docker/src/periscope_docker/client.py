"""Async Docker Engine API client, over a unix socket, a TCP endpoint, or Portainer's proxy.

All three speak the same API: only the connector and the path prefix differ, so `DockerHttp` picks the
connector (`aiohttp.UnixConnector` for a socket, a TLS-aware `TCPConnector` otherwise) and the config's
`base_url` carries Portainer's `/api/endpoints/<id>/docker` prefix when that is the way in. Nothing here
needs the `docker` package — the Engine API is plain HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Any

import aiohttp
from periscope import HttpClient
from periscope.http import HttpError

from .config import DockerConfig
from .util import Container, cpu_percent, demux_logs, has_update, image_ref, memory, parse_containers

log = logging.getLogger(__name__)

ACTIONS = ("start", "stop", "restart")
# how many containers the status board will sample stats for; each sample costs the daemon about a second
STATS_LIMIT = 12
LOG_TAIL_MAX = 200


class DockerError(RuntimeError):
    """The daemon (or Portainer) answered, but not with what was asked for."""


def tls_context(cfg: DockerConfig) -> ssl.SSLContext | None:
    """The TLS settings for a daemon reached over https, or None when plain http is enough."""
    if cfg.socket_path or not cfg.base_url.startswith("https://"):
        return None
    ctx = ssl.create_default_context(cafile=cfg.ca_path or None)
    if cfg.cert_path and cfg.key_path:
        ctx.load_cert_chain(cfg.cert_path, cfg.key_path)
    if not cfg.tls_verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


class DockerHttp(HttpClient):
    """The core HTTP client, taught to dial a unix socket and to present a client certificate."""

    def __init__(self, base_url: str = "", *, socket_path: str = "", ssl_context: ssl.SSLContext | None = None,
                 **kw: Any):
        super().__init__(base_url, **kw)
        self.socket_path = socket_path
        self.ssl_context = ssl_context

    def connector(self) -> aiohttp.BaseConnector:
        if self.socket_path:
            return aiohttp.UnixConnector(path=self.socket_path)
        if self.ssl_context is not None:
            return aiohttp.TCPConnector(ssl=self.ssl_context)
        return aiohttp.TCPConnector(ssl=False if not self._verify_ssl else None)

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(headers=self._headers, auth=self._auth, timeout=self._timeout,
                                                  connector=self.connector())
        return self._session


def build_http(cfg: DockerConfig, *, timeout_s: float = 20) -> DockerHttp:
    return DockerHttp(cfg.base_url, socket_path=cfg.socket_path, ssl_context=tls_context(cfg),
                      headers=cfg.headers, verify_ssl=cfg.tls_verify, timeout_s=timeout_s)


class DockerClient:
    """Thin wrapper over the endpoints this bot needs. Reads are cached only where autocomplete wants them."""

    def __init__(self, cfg: DockerConfig):
        self.cfg = cfg
        self.http = build_http(cfg)
        # last successful container list, so slash-command autocomplete answers without hitting the daemon
        self.cached: list[Container] = []

    async def close(self) -> None:
        await self.http.close()

    # ----- reads --------------------------------------------------------

    async def version(self) -> dict[str, Any]:
        data = await self.http.get_json("/version")
        if not isinstance(data, dict):
            raise DockerError(f"{self.cfg.endpoint} did not answer with a version")
        return data

    async def raw_containers(self, all_containers: bool = True) -> list[dict[str, Any]]:
        data = await self.http.get_json("/containers/json", params={"all": "1" if all_containers else "0"})
        if not isinstance(data, list):
            raise DockerError("the container list came back in an unexpected shape")
        return data

    async def containers(self, all_containers: bool = True) -> list[Container]:
        self.cached = parse_containers(await self.raw_containers(all_containers))
        return self.cached

    async def inspect(self, cid: str) -> dict[str, Any]:
        return await self.http.get_json(f"/containers/{cid}/json") or {}

    async def stats(self, cid: str) -> dict[str, Any]:
        """One sample (`stream=false`), which is what `docker stats --no-stream` reads: the daemon takes about
        a second over it because the CPU figure needs two readings."""
        return await self.http.get_json(f"/containers/{cid}/stats", params={"stream": "false"}) or {}

    async def logs(self, cid: str, lines: int = 50) -> str:
        raw = await self.http.get_bytes(f"/containers/{cid}/logs", params={
            "stdout": "1", "stderr": "1", "tail": str(max(1, min(int(lines), LOG_TAIL_MAX)))})
        return demux_logs(raw)

    async def images(self) -> list[dict[str, Any]]:
        data = await self.http.get_json("/images/json")
        return data if isinstance(data, list) else []

    async def registry_digest(self, ref: str) -> str:
        """What the registry currently serves for this tag, or "" when it will not say (private, rate-limited)."""
        try:
            data = await self.http.get_json(f"/distribution/{ref}/json")
        except (HttpError, DockerError, OSError, aiohttp.ClientError) as e:
            log.debug("no registry digest for %s: %s", ref, e)
            return ""
        return str(((data or {}).get("Descriptor") or {}).get("digest") or "")

    # ----- derived reads ------------------------------------------------

    async def sample(self, containers: list[Container], limit: int = STATS_LIMIT) -> None:
        """Fill in cpu / memory for up to `limit` running containers, in parallel. Best effort: a container
        that stops mid-sample simply keeps its empty figures and the board leaves them off its line."""
        running = [c for c in containers if c.running][:limit]
        if not running:
            return
        results = await asyncio.gather(*(self.stats(c.id) for c in running), return_exceptions=True)
        for container, result in zip(running, results, strict=True):
            if isinstance(result, BaseException):
                log.debug("stats for %s failed: %s", container.name, result)
                continue
            container.cpu_pct = cpu_percent(result)
            container.mem_used, container.mem_limit = memory(result)

    async def updates(self, refs: list[str]) -> list[dict[str, str]]:
        """Which of these image tags the registry has a newer digest for: [{ref, local, remote}, …]."""
        images = {image_ref(img): img for img in await self.images() if image_ref(img)}
        out: list[dict[str, str]] = []
        for ref in refs:
            image = images.get(ref)
            if image is None:
                continue
            remote = await self.registry_digest(ref)
            if has_update(image, remote, ref):
                out.append({"ref": ref, "local": image.get("Id", ""), "remote": remote})
        return out

    def find(self, query: str) -> Container | None:
        """Resolve what someone typed against the last container list: exact name, then id prefix, then substring."""
        q = (query or "").strip().lstrip("/").lower()
        if not q:
            return None
        for c in self.cached:
            if c.name.lower() == q:
                return c
        for c in self.cached:
            if c.id.lower().startswith(q):
                return c
        return next((c for c in self.cached if q in c.name.lower()), None)

    # ----- writes -------------------------------------------------------

    async def action(self, cid: str, action: str) -> None:
        if action not in ACTIONS:
            raise ValueError(f"unsupported action {action!r}")
        resp = await self.http.request("POST", f"/containers/{cid}/{action}")
        async with resp:
            await resp.read()

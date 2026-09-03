"""Inbound webhook + health server (aiohttp) that runs inside the bot process."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Awaitable, Callable

from aiohttp import web

log = logging.getLogger(__name__)

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class WebhookServer:
    """
    Usage:
        srv = WebhookServer(host, port, secret="...")
        srv.add_route("POST", "/alertmanager", handler)
        await srv.start()

    Routes are looked up in a table at request time, so `add_route` works before *and* after `start()` —
    services build after the listener is up, and can be reloaded later without touching the socket.

    Auth: if `secret` is set, requests must carry either
      - header  X-Webhook-Secret: <secret>          (simple shared secret), or
      - query   ?token=<secret>, or
      - header  X-Hub-Signature-256: sha256=<hmac>  (GitHub-style HMAC over the raw body).
    """

    def __init__(self, host: str = "0.0.0.0", port: int = 8080, secret: str | None = None):
        self.host = host
        self.port = port
        self.secret = secret
        self.extra_secrets: set[str] = set()  # v2: per-service WEBHOOK_SECRET overrides are accepted too
        self._routes: dict[tuple[str, str], Handler] = {}
        self.app = web.Application(client_max_size=4 * 1024 * 1024)
        self.app.router.add_get("/health", self._health)
        self.app.router.add_route("*", "/{tail:.*}", self._dispatch)
        self._runner: web.AppRunner | None = None
        self._healthy: Callable[[], bool] = lambda: True

    def set_health_check(self, fn: Callable[[], bool]) -> None:
        self._healthy = fn

    async def _health(self, _: web.Request) -> web.Response:
        ok = self._healthy()
        return web.json_response({"ok": ok}, status=200 if ok else 503)

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        key = (request.method.upper(), request.path)
        handler = self._routes.get(key) or self._routes.get((key[0], key[1].rstrip("/") or "/"))
        if handler is None:
            return web.json_response({"error": "no such webhook", "paths": sorted(self.paths)}, status=404)
        return await handler(request)

    @property
    def paths(self) -> list[str]:
        return sorted({p for _, p in self._routes})

    @property
    def secrets(self) -> set[str]:
        return ({self.secret} if self.secret else set()) | {s for s in self.extra_secrets if s}

    def accept_secret(self, secret: str | None) -> None:
        if secret:
            self.extra_secrets.add(secret)

    async def authorized(self, request: web.Request, body: bytes | None = None) -> bool:
        secrets = self.secrets
        if not secrets:
            return True
        if request.headers.get("X-Webhook-Secret") in secrets:
            return True
        if request.query.get("token") in secrets:
            return True
        sig = request.headers.get("X-Hub-Signature-256")
        if sig and sig.startswith("sha256="):
            body = body if body is not None else await request.read()
            for s in secrets:
                expected = hmac.new(s.encode(), body, hashlib.sha256).hexdigest()
                if hmac.compare_digest(sig[7:], expected):
                    return True
        return False

    def add_route(self, method: str, path: str, handler: Handler, *, auth: bool = True) -> None:
        async def wrapped(request: web.Request) -> web.StreamResponse:
            if auth and not await self.authorized(request):
                log.warning("unauthorized webhook %s %s from %s", method, path, request.remote)
                return web.json_response({"error": "unauthorized"}, status=401)
            try:
                return await handler(request)
            except Exception:
                log.exception("webhook handler error on %s %s", method, path)
                return web.json_response({"error": "handler failed"}, status=500)

        self._routes[(method.upper(), path)] = wrapped

    def remove_route(self, method: str, path: str) -> None:
        self._routes.pop((method.upper(), path), None)

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        log.info("webhook server listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

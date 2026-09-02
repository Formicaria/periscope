"""Async UniFi Network API client (UniFi OS consoles and self-hosted controllers)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
from periscope import HttpClient
from periscope.http import HttpError

from .config import UnifiConfig

log = logging.getLogger(__name__)


class UnifiError(RuntimeError):
    """The controller answered but reported an error in `meta.rc`."""


class UnifiClient:
    """Cookie-session client. Logs in lazily, re-logs in once on 401/403.

    Cookies are tracked by hand instead of aiohttp's cookie jar because the default jar
    drops cookies from bare-IP hosts (the usual `https://192.168.1.1`).
    """

    def __init__(self, cfg: UnifiConfig):
        self.cfg = cfg
        self.http = HttpClient(cfg.url, verify_ssl=cfg.verify_ssl, timeout_s=20)
        self._cookies: dict[str, str] = {}
        self._csrf: str | None = None
        self._login_lock = asyncio.Lock()
        # Last successful fetches; used by slash-command autocomplete without hitting the API.
        self.cached_clients: list[dict[str, Any]] = []
        self.cached_devices: list[dict[str, Any]] = []

    async def close(self) -> None:
        await self.http.close()

    # ----- session handling ---------------------------------------------

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {}
        if self._cookies:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in self._cookies.items())
        if self._csrf:
            h["X-CSRF-Token"] = self._csrf
        return h

    def _absorb(self, resp: aiohttp.ClientResponse) -> None:
        for name, morsel in resp.cookies.items():
            self._cookies[name] = morsel.value
        token = resp.headers.get("X-Updated-CSRF-Token") or resp.headers.get("X-CSRF-Token")
        if token:
            self._csrf = token
        elif "csrf_token" in resp.cookies and not self._csrf:  # self-hosted controller
            self._csrf = resp.cookies["csrf_token"].value

    async def login(self) -> None:
        async with self._login_lock:
            self._cookies, self._csrf = {}, None
            payload: dict[str, Any] = {"username": self.cfg.user, "password": self.cfg.password}
            if self.cfg.is_unifi_os:
                payload["rememberMe"] = True
            else:
                payload["remember"] = True
            resp = await self.http.request("POST", self.cfg.login_path, json=payload)
            async with resp:
                self._absorb(resp)
                await resp.read()
            if not self._cookies:
                raise UnifiError("login succeeded but no session cookie was returned")
            log.info("logged in to UniFi at %s as %s (site=%s)", self.cfg.url, self.cfg.user, self.cfg.site)

    async def _call(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                    _retry: bool = True) -> list[dict[str, Any]]:
        if not self._cookies:
            await self.login()
        try:
            resp = await self.http.request(method, self.cfg.site_path(path), json=json, params=params,
                                           headers=self._headers())
        except HttpError as e:
            if e.status in (401, 403) and _retry:
                log.info("UniFi session rejected (%s); re-authenticating", e.status)
                await self.login()
                return await self._call(method, path, json=json, params=params, _retry=False)
            raise
        async with resp:
            self._absorb(resp)
            data = await resp.json(content_type=None)
        if isinstance(data, dict):
            meta = data.get("meta") or {}
            if meta.get("rc") not in (None, "ok"):
                raise UnifiError(meta.get("msg") or "unknown controller error")
            return data.get("data") or []
        return data or []

    # ----- read endpoints -----------------------------------------------

    async def active_clients(self) -> list[dict[str, Any]]:
        self.cached_clients = await self._call("GET", "stat/sta")
        return self.cached_clients

    async def devices(self) -> list[dict[str, Any]]:
        self.cached_devices = await self._call("GET", "stat/device")
        return self.cached_devices

    async def health(self) -> list[dict[str, Any]]:
        return await self._call("GET", "stat/health")

    async def alarms(self, archived: bool = False) -> list[dict[str, Any]]:
        return await self._call("GET", "stat/alarm", params={"archived": "true" if archived else "false"})

    async def events(self, limit: int = 50) -> list[dict[str, Any]]:
        evs = await self._call("GET", "stat/event", params={"_limit": str(limit), "_sort": "-time"})
        return sorted(evs, key=lambda e: e.get("time", 0), reverse=True)[:limit]

    async def known_users(self) -> list[dict[str, Any]]:
        """Every client the controller has ever seen (names, hostnames, OUI vendor)."""
        return await self._call("GET", "rest/user")

    # ----- admin endpoints ----------------------------------------------

    async def restart_device(self, mac: str) -> None:
        await self._call("POST", "cmd/devmgr", json={"cmd": "restart", "mac": mac, "reboot_type": "soft"})

    async def kick_client(self, mac: str) -> None:
        await self._call("POST", "cmd/stamgr", json={"cmd": "kick-sta", "mac": mac})

    async def block_client(self, mac: str) -> None:
        await self._call("POST", "cmd/stamgr", json={"cmd": "block-sta", "mac": mac})

    async def unblock_client(self, mac: str) -> None:
        await self._call("POST", "cmd/stamgr", json={"cmd": "unblock-sta", "mac": mac})

"""Thin aiohttp wrapper with sane timeouts, auth, and optional self-signed TLS."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} from {url}: {body[:200]}")
        self.status = status
        self.url = url
        self.body = body


class HttpClient:
    def __init__(
        self,
        base_url: str = "",
        *,
        headers: dict[str, str] | None = None,
        auth: aiohttp.BasicAuth | None = None,
        verify_ssl: bool = True,
        timeout_s: float = 15,
        cookie_jar: aiohttp.abc.AbstractCookieJar | None = None,
        unsafe_cookies: bool = False,
    ):
        """`unsafe_cookies=True` keeps cookies for bare-IP hosts (UniFi, Proxmox, etc.)."""
        self.base_url = base_url.rstrip("/")
        self._headers = headers or {}
        self._auth = auth
        self._verify_ssl = verify_ssl
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._cookie_jar = cookie_jar
        self._unsafe_cookies = unsafe_cookies
        self._session: aiohttp.ClientSession | None = None

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=False if not self._verify_ssl else None)
            jar = self._cookie_jar or (aiohttp.CookieJar(unsafe=True) if self._unsafe_cookies else None)
            self._session = aiohttp.ClientSession(
                headers=self._headers, auth=self._auth, timeout=self._timeout, connector=connector,
                cookie_jar=jar,
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    async def request(self, method: str, path: str, **kw) -> aiohttp.ClientResponse:
        s = await self.session()
        url = self._url(path)
        resp = await s.request(method, url, **kw)
        if resp.status >= 400:
            body = await resp.text()
            resp.release()
            raise HttpError(resp.status, url, body)
        return resp

    async def get_json(self, path: str, **kw) -> Any:
        resp = await self.request("GET", path, **kw)
        async with resp:
            return await resp.json(content_type=None)

    async def post_json(self, path: str, json: Any = None, **kw) -> Any:
        resp = await self.request("POST", path, json=json, **kw)
        async with resp:
            if resp.content_length == 0:
                return None
            try:
                return await resp.json(content_type=None)
            except Exception:
                return await resp.text()

    async def get_bytes(self, path: str, **kw) -> bytes:
        resp = await self.request("GET", path, **kw)
        async with resp:
            return await resp.read()

    async def delete(self, path: str, **kw) -> Any:
        resp = await self.request("DELETE", path, **kw)
        async with resp:
            return resp.status

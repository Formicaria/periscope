"""The few Discord REST calls the UI makes itself (httpx): token checks, guild lists, OAuth2, channel/role
listings when no presence is connected. Nothing here logs a token."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import httpx

API = "https://discord.com/api/v10"
# View Channels, Send Messages, Embed Links, Attach Files, Read Message History, Manage Messages, Mention
# Everyone (224256) + Manage Channels (16) + Manage Roles (268435456): enough to create the channel layout.
INVITE_PERMS = 268659728
OAUTH_SCOPES = "identify guilds.members.read"


class DiscordError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(f"Discord answered {status}: {message}" if message else f"Discord answered {status}")
        self.status = status
        self.message = message


def invite_url(app_id: str | int) -> str:
    return f"https://discord.com/oauth2/authorize?client_id={app_id}&scope=bot%20applications.commands&permissions={INVITE_PERMS}"


def authorize_url(client_id: str, redirect_uri: str, state: str) -> str:
    q = urlencode({"client_id": client_id, "response_type": "code", "redirect_uri": redirect_uri,
                   "scope": OAUTH_SCOPES, "state": state, "prompt": "none"})
    return f"https://discord.com/oauth2/authorize?{q}"


def avatar_url(user: dict[str, Any]) -> str:
    uid, av = user.get("id"), user.get("avatar")
    if uid and av:
        return f"https://cdn.discordapp.com/avatars/{uid}/{av}.png?size=64"
    return ""


class DiscordAPI:
    """Thin async wrapper. `transport` lets tests plug an httpx.MockTransport in."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 10.0):
        self._transport = transport
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=API, timeout=self._timeout, transport=self._transport,
                                             headers={"User-Agent": "periscope-web (https://github.com/formicaria/periscope)"})
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(self, method: str, path: str, *, auth: str, data: dict | None = None, params: dict | None = None) -> Any:
        try:
            r = await self.client().request(method, path, headers={"Authorization": auth}, data=data, params=params)
        except httpx.HTTPError as e:
            raise DiscordError(0, f"unreachable ({type(e).__name__})") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("message") or r.json().get("error_description") or ""
            except Exception:  # noqa: BLE001
                msg = ""
            raise DiscordError(r.status_code, msg)
        if not r.content:
            return {}
        return r.json()

    # ----- bot token ----------------------------------------------------------------------------
    async def me(self, token: str) -> dict[str, Any]:
        """GET /users/@me with a bot token → the bot user (its id is the application id)."""
        return await self._request("GET", "/users/@me", auth=f"Bot {token}")

    async def guilds(self, token: str) -> list[dict[str, Any]]:
        out = await self._request("GET", "/users/@me/guilds", auth=f"Bot {token}")
        return out if isinstance(out, list) else []

    async def guild(self, token: str, guild_id: str | int) -> dict[str, Any]:
        return await self._request("GET", f"/guilds/{guild_id}", auth=f"Bot {token}")

    async def guild_channels(self, token: str, guild_id: str | int) -> list[dict[str, Any]]:
        out = await self._request("GET", f"/guilds/{guild_id}/channels", auth=f"Bot {token}")
        return out if isinstance(out, list) else []

    async def guild_roles(self, token: str, guild_id: str | int) -> list[dict[str, Any]]:
        out = await self._request("GET", f"/guilds/{guild_id}/roles", auth=f"Bot {token}")
        return out if isinstance(out, list) else []

    # ----- OAuth2 (user sign-in) ------------------------------------------------------------------
    async def exchange_code(self, client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict[str, Any]:
        try:
            r = await self.client().post("/oauth2/token", data={"grant_type": "authorization_code", "code": code,
                                                                "redirect_uri": redirect_uri},
                                         auth=(client_id, client_secret))
        except httpx.HTTPError as e:
            raise DiscordError(0, f"unreachable ({type(e).__name__})") from e
        if r.status_code >= 400:
            try:
                msg = r.json().get("error_description") or r.json().get("error") or ""
            except Exception:  # noqa: BLE001
                msg = ""
            raise DiscordError(r.status_code, msg)
        return r.json()

    async def oauth_me(self, access_token: str) -> dict[str, Any]:
        return await self._request("GET", "/users/@me", auth=f"Bearer {access_token}")

    async def oauth_member(self, access_token: str, guild_id: str | int) -> dict[str, Any]:
        """GET /users/@me/guilds/{id}/member (scope guilds.members.read) → {roles: [...], user: {...}}."""
        return await self._request("GET", f"/users/@me/guilds/{guild_id}/member", auth=f"Bearer {access_token}")

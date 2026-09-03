"""Guild lookups for pickers and layout actions.

Channels/roles come from a connected presence's cache when there is one, else from the REST API with any
presence's token (cached 60 s). Layout actions need a `discord.Guild`: a connected presence's, else a temporary
REST-only discord.py client (login + fetch_guild, no gateway session) so the first-run flow works before any
service is running.
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import discord

log = logging.getLogger(__name__)

TEXT_TYPES = (0, 5)  # GUILD_TEXT, GUILD_ANNOUNCEMENT
CACHE_TTL = 60.0


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    category: str = ""

    @property
    def label(self) -> str:
        return f"#{self.name}" + (f"  ·  {self.category}" if self.category else "")


@dataclass(frozen=True)
class Role:
    id: str
    name: str
    color: int = 0

    @property
    def label(self) -> str:
        return f"@{self.name}"


class GuildDirectory:
    def __init__(self, runtime, api):
        self.runtime = runtime
        self.api = api
        self._cache: dict[str, tuple[float, Any]] = {}

    # ----- basics ----------------------------------------------------------------------------------
    @property
    def store(self):
        return self.runtime.store

    def guild_id(self) -> int | None:
        raw = str(self.store.lab.get("guild_id") or "").strip()
        return int(raw) if raw.isdigit() else None

    def any_token(self) -> str:
        for p in self.store.presences.values():
            if p.get("token"):
                return str(p["token"])
        return ""

    def connected_presence(self):
        for pres in self.runtime.presences.values():
            if getattr(pres, "connected", False):
                return pres
        return None

    def connected_guild(self):
        gid = self.guild_id()
        pres = self.connected_presence()
        if gid is None or pres is None:
            return None
        try:
            return pres.get_guild(gid)
        except Exception:  # noqa: BLE001
            return None

    def invalidate(self) -> None:
        self._cache.clear()

    # ----- pickers ----------------------------------------------------------------------------------
    async def channels(self) -> list[Channel]:
        g = self.connected_guild()
        if g is not None:
            out = []
            for ch in getattr(g, "text_channels", []) or []:
                cat = getattr(getattr(ch, "category", None), "name", "") or ""
                out.append(Channel(str(ch.id), str(ch.name), str(cat)))
            return sorted(out, key=lambda c: (c.category, c.name))
        raw = await self._rest("channels", self.api.guild_channels)
        cats = {c["id"]: c.get("name", "") for c in raw if c.get("type") == 4}
        out = [Channel(str(c["id"]), str(c.get("name", "")), str(cats.get(c.get("parent_id"), "") or ""))
               for c in raw if c.get("type") in TEXT_TYPES]
        return sorted(out, key=lambda c: (c.category, c.name))

    async def roles(self) -> list[Role]:
        g = self.connected_guild()
        if g is not None:
            out = [Role(str(r.id), str(r.name), int(getattr(getattr(r, "colour", None), "value", 0) or 0))
                   for r in getattr(g, "roles", []) or [] if str(r.name) != "@everyone"]
            return sorted(out, key=lambda r: r.name.lower())
        raw = await self._rest("roles", self.api.guild_roles)
        out = [Role(str(r["id"]), str(r.get("name", "")), int(r.get("color") or 0)) for r in raw if r.get("name") != "@everyone"]
        return sorted(out, key=lambda r: r.name.lower())

    async def _rest(self, kind: str, fn) -> list[dict[str, Any]]:
        gid, token = self.guild_id(), self.any_token()
        if gid is None or not token:
            return []
        hit = self._cache.get(kind)
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            data = await fn(token, gid)
        except Exception as e:  # noqa: BLE001
            log.warning("guild %s lookup failed: %s", kind, e)
            data = []
        self._cache[kind] = (time.time() + CACHE_TTL, data)
        return data

    # ----- layout actions -----------------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[tuple[Any, int | None]]:
        """Yield (guild, acting bot user id). Prefers a connected presence; otherwise logs a REST-only client in."""
        gid = self.guild_id()
        if gid is None:
            raise RuntimeError("no Discord server configured (set the server id on the Discord page first)")
        pres = self.connected_presence()
        g = self.connected_guild()
        if g is not None:
            me = getattr(getattr(pres, "user", None), "id", None)
            yield g, me
            return
        token = self.any_token()
        if not token:
            raise RuntimeError("no presence has a bot token yet")
        client = discord.Client(intents=discord.Intents.default())
        try:
            await client.login(token)
            guild = await client.fetch_guild(gid)
            me = getattr(getattr(client, "user", None), "id", None)
            yield guild, me
        finally:
            await client.close()
            self.invalidate()

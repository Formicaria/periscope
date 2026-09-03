"""Guild lookups for pickers and layout actions, for any of the configured Discord servers.

One server's channels/roles — and its own name in Discord — come from a connected presence that can see it (any
of `runtime.presences`, by `get_guild`), else from the REST API with any presence's token. Everything is cached
per server id for 60 s.
Layout actions need a `discord.Guild`: a connected presence's, else a temporary REST-only discord.py client
(login + fetch_guild, no gateway session) so the first-run flow works before any service is running.
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
        self._cache: dict[tuple[str, int], tuple[float, Any]] = {}

    # ----- basics ----------------------------------------------------------------------------------
    @property
    def store(self):
        return self.runtime.store

    def guild_id(self, key: str | None = None) -> int | None:
        """The Discord server id of one configured server (the default one when no key is given)."""
        raw = str(self.store.server(key).get("guild_id") or "").strip()
        return int(raw) if raw.isdigit() else None

    def any_token(self) -> str:
        for p in self.store.presences.values():
            if p.get("token"):
                return str(p["token"])
        return ""

    def connected_presence(self, gid: int | None = None):
        """A connected presence — one that can see `gid` when a server id is given."""
        for pres in self.runtime.presences.values():
            if not getattr(pres, "connected", False):
                continue
            if gid is None:
                return pres
            try:
                if pres.get_guild(gid) is not None:
                    return pres
            except Exception:  # noqa: BLE001
                continue
        return None

    def guild_for(self, gid: int | None):
        """The live `discord.Guild` object for a server id, from whichever presence is in that server."""
        if gid is None:
            return None
        for pres in self.runtime.presences.values():
            if not getattr(pres, "connected", False):
                continue
            try:
                g = pres.get_guild(gid)
            except Exception:  # noqa: BLE001
                continue
            if g is not None:
                return g
        return None

    def connected_guild(self, key: str | None = None):
        return self.guild_for(self.guild_id(key))

    def invite_for(self, gid: int | None = None) -> str:
        """An invite link to hand out when the bot is not in a server: the link of the bot that says it is
        missing this one, else any bot's link."""
        presences = (self.runtime.status().get("presences") or {}).values()
        fallback = ""
        for p in presences:
            url = str(p.get("invite") or "")
            if not url:
                continue
            if gid is not None and str(gid) in (p.get("missing_guilds") or {}):
                return url
            fallback = fallback or url
        return fallback

    def invalidate(self) -> None:
        self._cache.clear()

    # ----- pickers ----------------------------------------------------------------------------------
    async def channels(self, key: str | None = None) -> list[Channel]:
        return await self.channels_for(self.guild_id(key))

    async def roles(self, key: str | None = None) -> list[Role]:
        return await self.roles_for(self.guild_id(key))

    async def channels_for(self, gid: int | None) -> list[Channel]:
        g = self.guild_for(gid)
        if g is not None:
            out = []
            for ch in getattr(g, "text_channels", []) or []:
                cat = getattr(getattr(ch, "category", None), "name", "") or ""
                out.append(Channel(str(ch.id), str(ch.name), str(cat)))
            return sorted(out, key=lambda c: (c.category, c.name))
        raw = await self._rest("channels", gid, self.api.guild_channels)
        cats = {c["id"]: c.get("name", "") for c in raw if c.get("type") == 4}
        out = [Channel(str(c["id"]), str(c.get("name", "")), str(cats.get(c.get("parent_id"), "") or ""))
               for c in raw if c.get("type") in TEXT_TYPES]
        return sorted(out, key=lambda c: (c.category, c.name))

    async def roles_for(self, gid: int | None) -> list[Role]:
        g = self.guild_for(gid)
        if g is not None:
            out = [Role(str(r.id), str(r.name), int(getattr(getattr(r, "colour", None), "value", 0) or 0))
                   for r in getattr(g, "roles", []) or [] if str(r.name) != "@everyone"]
            return sorted(out, key=lambda r: r.name.lower())
        raw = await self._rest("roles", gid, self.api.guild_roles)
        out = [Role(str(r["id"]), str(r.get("name", "")), int(r.get("color") or 0)) for r in raw if r.get("name") != "@everyone"]
        return sorted(out, key=lambda r: r.name.lower())

    async def guild_name(self, gid: int | None) -> str:
        """A server's own name in Discord — what the cards head with, so two servers that share a display name
        can still be told apart. From a connected presence's guild object, else GET /guilds/{id}; "" when no bot
        is in that server, or when none has a token to ask with."""
        g = self.guild_for(gid)
        if g is not None:
            return str(getattr(g, "name", "") or "")
        token = self.any_token()
        if gid is None or not token:
            return ""
        hit = self._cache.get(("name", gid))
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            name = str(((await self.api.guild(token, gid)) or {}).get("name") or "")
        except Exception as e:  # noqa: BLE001
            log.warning("guild name lookup for server %s failed: %s", gid, e)
            name = ""
        self._cache[("name", gid)] = (time.time() + CACHE_TTL, name)
        return name

    async def names(self) -> dict[str, str]:
        """{Discord server id: its real name} for the configured servers — the one lookup a request makes so
        every label can name the Discord server behind a display name without asking again."""
        out: dict[str, str] = {}
        for gid in dict.fromkeys(self.store.guild_ids().values()):
            name = await self.guild_name(int(gid)) if gid.isdigit() else ""
            if name:
                out[gid] = name
        return out

    async def available_guilds(self) -> list[dict[str, str]]:
        """The Discord servers a bot of ours is in — what the "add a server" picker offers. From a connected
        presence's own list when there is one, else GET /users/@me/guilds with any token."""
        out: dict[str, str] = {}
        for pres in self.runtime.presences.values():
            if not getattr(pres, "connected", False):
                continue
            for g in getattr(pres, "guilds", []) or []:
                out[str(g.id)] = str(getattr(g, "name", "") or g.id)
        if not out:
            token = self.any_token()
            if not token:
                return []
            hit = self._cache.get(("guilds", 0))
            if hit and hit[0] > time.time():
                raw = hit[1]
            else:
                try:
                    raw = await self.api.guilds(token)
                except Exception as e:  # noqa: BLE001
                    log.warning("could not list the bot's servers: %s", e)
                    raw = []
                self._cache[("guilds", 0)] = (time.time() + CACHE_TTL, raw)
            out = {str(g.get("id")): str(g.get("name") or g.get("id")) for g in raw if g.get("id")}
        return [{"id": gid, "name": name} for gid, name in sorted(out.items(), key=lambda kv: kv[1].lower())]

    async def _rest(self, kind: str, gid: int | None, fn) -> list[dict[str, Any]]:
        token = self.any_token()
        if gid is None or not token:
            return []
        hit = self._cache.get((kind, gid))
        if hit and hit[0] > time.time():
            return hit[1]
        try:
            data = await fn(token, gid)
        except Exception as e:  # noqa: BLE001
            log.warning("guild %s lookup for server %s failed: %s", kind, gid, e)
            data = []
        self._cache[(kind, gid)] = (time.time() + CACHE_TTL, data)
        return data

    # ----- layout actions -----------------------------------------------------------------------------
    @contextlib.asynccontextmanager
    async def acquire(self, key: str | None = None) -> AsyncIterator[tuple[Any, int | None]]:
        """Yield (guild, acting bot user id). Prefers a connected presence; otherwise logs a REST-only client in."""
        gid = self.guild_id(key)
        if gid is None:
            raise RuntimeError("this server has no Discord server id yet (set it on the Discord page first)")
        pres = self.connected_presence(gid)
        g = self.guild_for(gid)
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

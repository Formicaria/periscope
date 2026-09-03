"""Request backend selection: Overseerr/Jellyseerr, or native Radarr + Sonarr, or nothing configured."""

from __future__ import annotations

import asyncio
from itertools import zip_longest
from typing import Any

from .arr import ArrClient
from .seerr import SeerrClient


def select_backend(mode: str, *, has_seerr: bool, has_radarr: bool, has_sonarr: bool) -> str:
    """'seerr', 'arr', or '' when nothing usable is configured for the requested mode."""
    mode = (mode or "auto").strip().lower()
    has_arr = has_radarr or has_sonarr
    if mode == "seerr":
        return "seerr" if has_seerr else ""
    if mode == "arr":
        return "arr" if has_arr else ""
    if has_seerr:
        return "seerr"
    return "arr" if has_arr else ""


class RequestBackend:
    """Routes search / request / availability to whichever clients are configured."""

    def __init__(self, mode: str, seerr: SeerrClient | None, radarr: ArrClient | None, sonarr: ArrClient | None):
        self.mode = mode
        self.seerr = seerr
        self.radarr = radarr
        self.sonarr = sonarr

    @property
    def active(self) -> str:
        return select_backend(self.mode, has_seerr=self.seerr is not None, has_radarr=self.radarr is not None,
                              has_sonarr=self.sonarr is not None)

    def describe(self) -> str:
        active = self.active
        if not active:
            return "none"
        if active == "seerr":
            return "seerr"
        return f"arr (radarr={'on' if self.radarr else 'off'}, sonarr={'on' if self.sonarr else 'off'})"

    def arr_for(self, media_type: str) -> ArrClient | None:
        return self.radarr if media_type == "movie" else self.sonarr

    async def search(self, query: str) -> list[dict[str, Any]]:
        """Search via the active backend; arr mode interleaves Radarr + Sonarr results."""
        if self.active == "seerr" and self.seerr is not None:
            return await self.seerr.search(query)
        lookups = [c.lookup(query, limit=4) for c in (self.radarr, self.sonarr) if c]
        groups = await asyncio.gather(*lookups, return_exceptions=True)
        lists = [g for g in groups if isinstance(g, list)]
        errors = [g for g in groups if isinstance(g, BaseException)]
        if not lists:
            raise errors[0] if errors else RuntimeError("no request backend configured")
        out: list[dict[str, Any]] = []
        for pair in zip_longest(*lists):                 # interleave movies & tv
            out.extend(x for x in pair if x)
        return out[:8]

    async def request(self, pick: dict[str, Any]) -> tuple[bool, str, dict[str, Any] | None]:
        """Submit to the right backend. Returns (ok, message, watch_info)."""
        if pick.get("backend") == "arr":
            client = self.arr_for(pick["media_type"])
            if client is None:
                return (False, f"no {'Radarr' if pick['media_type'] == 'movie' else 'Sonarr'} configured", None)
            ok, msg, arr_id = await client.add(pick["arr_raw"])
            info = {"backend": "arr", "kind": client.kind, "arr_id": arr_id} if ok and arr_id else None
            return (ok, msg, info)
        if self.seerr is None:
            return (False, "no Seerr configured", None)
        ok, msg, media_id = await self.seerr.request(pick["media_type"], pick["tmdb_id"])
        info = {"backend": "seerr", "media_id": media_id} if ok and media_id else None
        return (ok, msg, info)

    async def watch_status(self, watch: dict[str, Any]) -> int | None:
        """Current availability code of a watched request, None when unknown."""
        if watch.get("backend") == "arr":
            client = self.radarr if watch.get("kind") == "radarr" else self.sonarr
            return await client.is_available(watch["arr_id"]) if client else None
        if self.seerr is None or "media_id" not in watch:
            return None
        return await self.seerr.media_status(watch["media_id"])

    async def close(self) -> None:
        for c in (self.seerr, self.radarr, self.sonarr):
            if c is not None:
                await c.close()

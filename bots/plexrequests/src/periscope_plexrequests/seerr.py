"""Tiny async client for the Overseerr / Jellyseerr API (search, request, media status)."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

from .common import STATUS_LABEL  # noqa: F401  (re-exported for callers that only import this module)

log = logging.getLogger(__name__)


class SeerrClient:
    def __init__(self, base_url: str, api_key: str):
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self._session: aiohttp.ClientSession | None = None

    async def _sess(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"X-Api-Key": self.api_key, "Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Simplified results: title, year, media_type, tmdb_id, status, poster, overview."""
        s = await self._sess()
        # Encode exactly like the Seerr web UI (%20 for spaces, never '+') — some builds reject '+'-encoded
        # queries with a 400.
        url = URL(f"{self.base}/api/v1/search?query={quote(query, safe='')}&page=1", encoded=True)
        async with s.get(url) as r:
            if r.status != 200:
                body = await self._safe_message(r)
                log.warning("Seerr search HTTP %s for %r: %s", r.status, query, body)
                raise RuntimeError(f"Seerr answered HTTP {r.status}: {body}")
            data = await r.json()
        return parse_search(data, limit)

    async def request(self, media_type: str, tmdb_id: int) -> tuple[bool, str, int | None]:
        """Submit a request. Returns (ok, message, seerr_media_id)."""
        payload: dict[str, Any] = {"mediaType": media_type, "mediaId": int(tmdb_id)}
        if media_type == "tv":
            # An explicit season list works on every Overseerr/Jellyseerr version; "all" is the fallback
            # if the season lookup fails.
            seasons = await self._season_numbers(tmdb_id)
            payload["seasons"] = seasons or "all"

        status, data, body = await self._post_request(payload)
        if status in (200, 201):
            return (True, "requested", (data.get("media") or {}).get("id"))

        if media_type == "tv":
            # Retry once with the other seasons form (API differences between versions)
            alt = "all" if isinstance(payload["seasons"], list) else await self._season_numbers(tmdb_id)
            if alt and alt != payload["seasons"]:
                payload["seasons"] = alt
                status, data, body = await self._post_request(payload)
                if status in (200, 201):
                    return (True, "requested", (data.get("media") or {}).get("id"))

        log.warning("Seerr request failed: %s %s -> HTTP %s: %s", media_type, tmdb_id, status, body)
        if status == 409:
            return (False, "already exists", None)
        return (False, f"{body} (HTTP {status})", None)

    async def media_status(self, media_id: int) -> int | None:
        """Current status of a Seerr media item (see STATUS_LABEL); None on error."""
        s = await self._sess()
        async with s.get(f"{self.base}/api/v1/media/{media_id}") as r:
            if r.status != 200:
                return None
            data = await r.json()
            return data.get("status")

    async def version(self) -> str:
        s = await self._sess()
        async with s.get(f"{self.base}/api/v1/status") as r:
            if r.status != 200:
                raise RuntimeError(f"Seerr status HTTP {r.status}")
            data = await r.json()
            return str(data.get("version", "?"))

    async def _post_request(self, payload: dict[str, Any]) -> tuple[int, dict[str, Any], str]:
        s = await self._sess()
        async with s.post(f"{self.base}/api/v1/request", json=payload) as r:
            if r.status in (200, 201):
                try:
                    data = await r.json()
                except Exception:  # noqa: BLE001
                    data = {}
                return (r.status, data or {}, "")
            return (r.status, {}, await self._safe_message(r))

    async def _season_numbers(self, tmdb_id: int) -> list[int]:
        try:
            s = await self._sess()
            async with s.get(f"{self.base}/api/v1/tv/{tmdb_id}") as r:
                if r.status != 200:
                    return []
                data = await r.json()
            return [x["seasonNumber"] for x in data.get("seasons", []) if x.get("seasonNumber", 0) > 0]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    async def _safe_message(resp: aiohttp.ClientResponse) -> str:
        try:
            data = await resp.json()
            return str(data.get("message") or data)[:200]
        except Exception:  # noqa: BLE001
            return f"HTTP {resp.status}"


def parse_search(data: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    """Seerr /search payload → the result shape the request flows use."""
    out: list[dict[str, Any]] = []
    for item in data.get("results", []):
        mtype = item.get("mediaType")
        if mtype not in ("movie", "tv"):
            continue
        title = item.get("title") or item.get("name") or "?"
        date = item.get("releaseDate") or item.get("firstAirDate") or ""
        media_info = item.get("mediaInfo") or {}
        poster = item.get("posterPath")
        overview = (item.get("overview") or "").strip()
        if len(overview) > 350:
            overview = overview[:347].rstrip() + "…"
        out.append({
            "tmdb_id": item.get("id"),
            "media_type": mtype,
            "title": title,
            "year": date[:4] if date else "",
            "status": media_info.get("status") or 1,
            "poster": f"https://image.tmdb.org/t/p/w342{poster}" if poster else None,
            "overview": overview,
            "backend": "seerr",
        })
        if len(out) >= limit:
            break
    return out

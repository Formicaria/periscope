"""Tiny async clients for the native Radarr/Sonarr v3+ APIs.

Used when REQUEST_BACKEND=arr (or auto without Seerr): the service talks straight to Radarr for movies and
Sonarr for TV — no Overseerr/Jellyseerr required. Quality profile and root folder are picked by name, and
titles released before FALLBACK_BEFORE_YEAR go to the fallback profile (old titles rarely exist in 4K).
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

import aiohttp
from yarl import URL

log = logging.getLogger(__name__)

UHD_HINTS = ("ultra", "2160", "4k", "uhd")


# ----- pure profile logic (unit-tested on its own) ---------------------------------------------------

def is_uhd_profile(p: dict[str, Any]) -> bool:
    """A profile is 4K-only when its name says so or every allowed quality is a 2160p one."""
    name = (p.get("name") or "").lower()
    if any(k in name for k in UHD_HINTS):
        return True
    allowed = [i for i in p.get("items") or [] if i.get("allowed")]
    quals = [(i.get("quality") or {}).get("name", "") for i in allowed] + \
            [q.get("quality", {}).get("name", "") for i in allowed for q in i.get("items") or []]
    quals = [q for q in quals if q]
    return bool(quals) and all("2160" in q for q in quals)


def find_profile(profiles: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    want = (name or "").strip().lower()
    if not want:
        return None
    return next((p for p in profiles if (p.get("name") or "").lower() == want), None)


def pick_fallback(profiles: list[dict[str, Any]], chosen: dict[str, Any], explicit: str = "",
                  kind: str = "arr") -> int | None:
    """Fallback profile id: the explicitly named one, else (only when the main profile is 4K-only) the first
    1080p profile that is not itself 4K-only, else None."""
    if explicit:
        m = find_profile(profiles, explicit)
        if m is None:
            log.warning("%s: fallback profile %r not found — ignoring", kind, explicit)
        return m["id"] if m else None
    if not is_uhd_profile(chosen):
        return None
    m = next((p for p in profiles if "1080" in (p.get("name") or "") and not is_uhd_profile(p)), None)
    if m:
        log.info("%s: %r is a 4K profile; older titles will use %r", kind, chosen.get("name"), m.get("name"))
    return m["id"] if m else None


def profile_for_year(year: Any, main_id: int | None, fallback_id: int | None, before_year: int) -> int | None:
    """Main profile, or the fallback for titles released before the cutoff year."""
    try:
        y = int(year or 0)
    except (TypeError, ValueError):
        y = 0
    if fallback_id and before_year and 0 < y < before_year:
        return fallback_id
    return main_id


def status_of(kind: str, it: dict[str, Any]) -> int:
    """Map an arr lookup item to the Seerr-style status codes used by the request flows."""
    if not it.get("id"):                            # not in the library yet
        return 1
    if kind == "radarr":
        return 5 if it.get("hasFile") else 2
    stats = it.get("statistics") or {}
    have = stats.get("episodeFileCount") or 0
    if not have:
        return 2
    return 5 if (stats.get("percentOfEpisodes") or 0) >= 100 else 4


def parse_lookup(kind: str, items: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    media_type = "movie" if kind == "radarr" else "tv"
    out: list[dict[str, Any]] = []
    for it in items[:limit * 2]:
        ext_id = it.get("tmdbId") if kind == "radarr" else it.get("tvdbId")
        if not ext_id:
            continue
        overview = (it.get("overview") or "").strip()
        if len(overview) > 350:
            overview = overview[:347].rstrip() + "…"
        out.append({
            "tmdb_id": ext_id,                      # tmdb (radarr) / tvdb (sonarr)
            "media_type": media_type,
            "title": it.get("title") or "?",
            "year": str(it.get("year") or "") if it.get("year") else "",
            "status": status_of(kind, it),
            "poster": it.get("remotePoster"),
            "overview": overview,
            "backend": "arr",
            "arr_raw": it,                          # payload for add()
        })
        if len(out) >= limit:
            break
    return out


# ----- client ---------------------------------------------------------------------------------------

class ArrClient:
    """One client per app. kind: 'radarr' (movies) or 'sonarr' (tv)."""

    def __init__(self, kind: str, base_url: str, api_key: str, profile_name: str = "", root_folder: str = "",
                 fallback_profile: str = "", fallback_before_year: int = 0):
        assert kind in ("radarr", "sonarr")
        self.kind = kind
        self.media_type = "movie" if kind == "radarr" else "tv"
        self.base = base_url.rstrip("/")
        self.api_key = api_key
        self.profile_name = profile_name.strip()
        self.root_folder = root_folder.strip()
        self.fallback_profile = fallback_profile.strip()
        self.fallback_before_year = int(fallback_before_year or 0)
        self._session: aiohttp.ClientSession | None = None
        self._profile_id: int | None = None
        self._fallback_id: int | None = None
        self._root_path: str | None = None

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

    async def _get(self, path: str) -> Any:
        s = await self._sess()
        async with s.get(f"{self.base}/api/v3{path}") as r:
            if r.status != 200:
                raise RuntimeError(f"{self.kind} GET {path} -> HTTP {r.status}")
            return await r.json()

    # ----- lookup -----

    async def lookup(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search new + existing items. Returns dicts in the shared result shape."""
        ep = "/movie/lookup" if self.kind == "radarr" else "/series/lookup"
        s = await self._sess()
        url = URL(f"{self.base}/api/v3{ep}?term={quote(query, safe='')}", encoded=True)
        async with s.get(url) as r:
            if r.status != 200:
                raise RuntimeError(f"{self.kind} lookup HTTP {r.status}")
            items = await r.json()
        return parse_lookup(self.kind, items, limit)

    # ----- add -----

    async def _resolve_defaults(self) -> None:
        if self._profile_id is None:
            profiles = await self._get("/qualityprofile")
            match = find_profile(profiles, self.profile_name)
            if self.profile_name and match is None:
                log.warning("%s: profile %r not found — using %r", self.kind, self.profile_name,
                            profiles[0].get("name") if profiles else "?")
            chosen = match or (profiles[0] if profiles else None)
            if chosen is None:
                raise RuntimeError(f"{self.kind} has no quality profiles")
            self._profile_id = chosen["id"]
            self._fallback_id = pick_fallback(profiles, chosen, self.fallback_profile, self.kind)
        if self._root_path is None:
            if self.root_folder:
                self._root_path = self.root_folder
            else:
                roots = await self._get("/rootfolder")
                if not roots:
                    raise RuntimeError(f"{self.kind} has no root folders")
                self._root_path = roots[0]["path"]

    def profile_for(self, raw: dict[str, Any]) -> int | None:
        return profile_for_year(raw.get("year"), self._profile_id, self._fallback_id, self.fallback_before_year)

    async def add(self, raw: dict[str, Any]) -> tuple[bool, str, int | None]:
        """Add + start searching. Returns (ok, message, arr_id)."""
        if raw.get("id"):
            return (False, "already exists", raw["id"])
        await self._resolve_defaults()
        body = dict(raw)
        body.update({
            "qualityProfileId": self.profile_for(raw),
            "rootFolderPath": self._root_path,
            "monitored": True,
        })
        if self.kind == "radarr":
            ep = "/movie"
            body.setdefault("minimumAvailability", "released")
            body["addOptions"] = {"searchForMovie": True}
        else:
            ep = "/series"
            body["seasonFolder"] = True
            body["addOptions"] = {"searchForMissingEpisodes": True}
        s = await self._sess()
        async with s.post(f"{self.base}/api/v3{ep}", json=body) as r:
            if r.status in (200, 201):
                data = await r.json()
                return (True, "added", data.get("id"))
            text = (await r.text())[:300]
            low = text.lower()
            log.warning("%s add failed HTTP %s: %s", self.kind, r.status, text)
            if r.status == 400 and ("already" in low or "exists" in low):
                return (False, "already exists", None)
            return (False, f"HTTP {r.status}: {text[:150]}", None)

    # ----- watch / status board -----

    async def is_available(self, arr_id: int) -> int | None:
        """Availability of a library item → status code, or None on error."""
        try:
            ep = f"/movie/{arr_id}" if self.kind == "radarr" else f"/series/{arr_id}"
            it = await self._get(ep)
            it.setdefault("id", arr_id)
            return status_of(self.kind, it)
        except Exception:  # noqa: BLE001
            return None

    async def queue_summary(self, top: int = 3) -> tuple[int, list[str]]:
        """(count, ['Title — timeleft', ...]) for the status board."""
        data = await self._get("/queue?page=1&pageSize=20")
        records = data.get("records", data if isinstance(data, list) else [])
        total = data.get("totalRecords", len(records)) if isinstance(data, dict) else len(records)
        lines = []
        for rec in records[:top]:
            title = rec.get("title") or rec.get("movie", {}).get("title") or rec.get("series", {}).get("title") or "?"
            left = rec.get("timeleft") or ""
            lines.append(f"{title[:60]}" + (f" — {left}" if left else ""))
        return (total, lines)

    async def disk_space(self) -> list[tuple[str, int, int]]:
        """[(path, free_bytes, total_bytes), ...]"""
        data = await self._get("/diskspace")
        return [(d.get("path", "?"), d.get("freeSpace", 0), d.get("totalSpace", 0)) for d in data]

    async def version(self) -> str:
        data = await self._get("/system/status")
        return data.get("version", "?")

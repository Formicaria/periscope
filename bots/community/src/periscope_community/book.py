"""The maintenance book: every window an admin scheduled with /psa maint, and where its posts are.

Entries live in the service's state (data/state.json, svc:community → "maint"). Each one also opens a one-off
window in config/maintenance.yaml (id psa-<id>) so periscope's own alerts stay quiet for the duration — the
window is removed/rewritten when the entry is cancelled, extended or finished early.

    {"id": "m0007", "start": 1793400000, "end": 1793407200, "reason": "Moving Plex to pve2",
     "affects": ["Plex"], "by": "chronus", "phase": "scheduled", "mid": 1234, "finished": null}
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any

log = logging.getLogger(__name__)
LIVE = ("live", "overrun")
OPEN = ("scheduled", "live", "overrun")


class MaintBook:
    def __init__(self, state: Any, windows: Any, tz: dt.tzinfo, *, slugify=None):
        self.state = state
        self.windows = windows
        self.tz = tz
        self.slugify = slugify or (lambda s: s.lower())

    # ----- storage -------------------------------------------------------------------------------------
    def all(self) -> list[dict[str, Any]]:
        return sorted((self.state.get("maint") or {}).values(), key=lambda e: float(e["start"]))

    def open(self) -> list[dict[str, Any]]:
        return [e for e in self.all() if e.get("phase") in OPEN]

    def get(self, mid: str) -> dict[str, Any] | None:
        return (self.state.get("maint") or {}).get(mid.strip().lower())

    def save(self, entry: dict[str, Any]) -> None:
        book = dict(self.state.get("maint") or {})
        book[entry["id"]] = entry
        # keep the book small: finished/cancelled entries older than 30 days go
        cutoff = time.time() - 30 * 86400
        book = {k: v for k, v in book.items() if v.get("phase") in OPEN or float(v.get("end", 0)) > cutoff}
        self.state.set("maint", book)

    def _next_id(self) -> str:
        n = int(self.state.get("maint_seq") or 0) + 1
        self.state.set("maint_seq", n)
        return f"m{n:04d}"

    # ----- windows (quiet periscope's alerts while it's happening) ---------------------------------------
    def _iso(self, epoch: float) -> str:
        return dt.datetime.fromtimestamp(epoch, self.tz).isoformat(timespec="minutes")

    def _sync_window(self, entry: dict[str, Any]) -> None:
        add, remove = getattr(self.windows, "add", None), getattr(self.windows, "remove", None)
        if add is None or remove is None:
            return                            # maintenance windows aren't installed: posts still work
        wid = f"psa-{entry['id']}"
        try:
            remove(wid)
            if entry.get("phase") not in OPEN:
                return
            affects = entry.get("affects") or []
            keys = [f"community:{self.slugify(a)}*" for a in affects]
            add(reason=f"[PSA {entry['id']}] {entry.get('reason') or 'maintenance'}", start=self._iso(entry["start"]),
                end=self._iso(entry["end"]), services=["community"] if affects else None, keys=keys or None, wid=wid)
        except Exception as e:  # noqa: BLE001 — the announcement matters more than the alert mute
            log.warning("community: could not write maintenance window %s: %s", wid, e)

    # ----- lifecycle -----------------------------------------------------------------------------------
    def create(self, *, start: float, end: float, reason: str, affects: list[str], by: str) -> dict[str, Any]:
        if end <= start:
            raise ValueError("it has to end after it starts")
        for other in self.open():
            if start < float(other["end"]) and float(other["start"]) < end and \
                    (not other.get("affects") or not affects or set(other["affects"]) & set(affects)):
                raise ValueError(f"that overlaps {other['id']} ({other.get('reason') or 'maintenance'}) — "
                                 f"extend or cancel that one instead")
        entry = {"id": self._next_id(), "start": float(start), "end": float(end), "reason": reason.strip(),
                 "affects": list(affects), "by": by, "phase": "scheduled", "mid": None, "finished": None,
                 "created": time.time()}
        self.save(entry)
        self._sync_window(entry)
        return entry

    def set_phase(self, entry: dict[str, Any], phase: str, **extra: Any) -> dict[str, Any]:
        entry = {**entry, "phase": phase, **extra}
        self.save(entry)
        self._sync_window(entry)
        return entry

    def extend(self, entry: dict[str, Any], seconds: int) -> dict[str, Any]:
        base = max(float(entry["end"]), time.time())
        entry = {**entry, "end": base + seconds}
        if entry.get("phase") == "overrun":
            entry["phase"] = "live"
        self.save(entry)
        self._sync_window(entry)
        return entry

    def covering(self, name: str, now: float | None = None) -> dict[str, Any] | None:
        """The live entry that covers this resource right now (affects [] = everything)."""
        now = now if now is not None else time.time()
        for e in self.open():
            if e.get("phase") in LIVE or float(e["start"]) <= now:
                affects = [a.lower() for a in (e.get("affects") or [])]
                if (not affects or name.lower() in affects) and float(e["start"]) <= now and \
                        (now < float(e["end"]) or e.get("phase") == "overrun"):
                    return e
        return None

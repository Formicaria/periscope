"""Typed accessors over the service's namespaced state: sticky message ids, invitee emails, availability
watches, per-user request history and the new-on-Plex baseline (what the standalone bot kept in state.json)."""

from __future__ import annotations

import time
from typing import Any

HISTORY_PER_USER = 15


class Records:
    def __init__(self, state: Any):
        self._s = state

    # ----- sticky / board messages -----
    def message_id(self, key: str) -> int | None:
        v = self._s.get(key)
        return int(v) if v else None

    def set_message_id(self, key: str, message_id: int | None) -> None:
        self._s.set(key, message_id)

    # ----- invitee emails (for auto-revoke) -----
    def email_for(self, user_id: int) -> str | None:
        return (self._s.get("emails") or {}).get(str(user_id))

    def remember_email(self, user_id: int, email: str) -> None:
        emails = dict(self._s.get("emails") or {})
        emails[str(user_id)] = email
        self._s.set("emails", emails)

    def forget_email(self, user_id: int) -> None:
        emails = dict(self._s.get("emails") or {})
        if emails.pop(str(user_id), None) is not None:
            self._s.set("emails", emails)

    # ----- availability watches -----
    def watches(self) -> list[dict[str, Any]]:
        return list(self._s.get("watches") or [])

    def add_watch(self, info: dict[str, Any], channel_id: int, message_id: int, requester: str,
                  requester_id: int = 0, title: str = "") -> None:
        watches = self.watches()
        watches.append({**info, "channel_id": channel_id, "message_id": message_id, "requester": requester,
                        "requester_id": requester_id, "title": title, "added": time.time()})
        self._s.set("watches", watches)

    def drop_watches(self, message_ids: set[int]) -> None:
        self._s.set("watches", [w for w in self.watches() if w.get("message_id") not in message_ids])

    # ----- per-user request history (/requests mystatus) -----
    def history(self, user_id: int) -> list[dict[str, Any]]:
        return list((self._s.get("requests") or {}).get(str(user_id), []))

    def track_request(self, user_id: int, pick: dict[str, Any], message_id: int) -> None:
        all_hist = dict(self._s.get("requests") or {})
        hist = list(all_hist.get(str(user_id), []))
        hist.append({"title": pick["title"], "year": pick.get("year", ""), "type": pick["media_type"],
                     "ts": time.time(), "status": "queued", "msg": message_id})
        all_hist[str(user_id)] = hist[-HISTORY_PER_USER:]
        self._s.set("requests", all_hist)

    def mark_history_available(self, user_id: int, message_id: int) -> None:
        all_hist = dict(self._s.get("requests") or {})
        hist = all_hist.get(str(user_id))
        if not hist:
            return
        changed = False
        for entry in hist:
            if entry.get("msg") == message_id and entry.get("status") != "available":
                entry["status"] = "available"
                changed = True
        if changed:
            self._s.set("requests", all_hist)

    # ----- new-on-plex baseline -----
    def plex_seen(self) -> list[str] | None:
        v = self._s.get("plex_seen")
        return list(v) if v is not None else None

    def set_plex_seen(self, keys: list[str]) -> None:
        self._s.set("plex_seen", list(keys))

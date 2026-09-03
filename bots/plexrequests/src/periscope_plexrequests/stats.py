"""Usage counters — every interaction counted, per user and in total, kept in the service state and shown by
`/requests plexstats` (admin only)."""

from __future__ import annotations

import time
from typing import Any

# event key -> label shown in the report (order = report order)
EVENT_LABELS = {
    "invite_button":     "🎟  Get Plex Access button",
    "request_button":    "🔎  Search & Request button",
    "cmd_plexinvite":    "⌨️  /plexinvite command",
    "cmd_request":       "⌨️  /requests request command",
    "typed_email":       "📧  Emails typed in channel",
    "typed_request":     "💬  Titles typed in channel",
    "search":            "🔍  Searches run",
    "pick":              "👆  Titles picked from menu",
    "request_ok":        "📥  Requests sent to the backend",
    "request_fail":      "❌  Requests failed",
    "already_on_plex":   "✅  Already on Plex",
    "already_requested": "⏳  Already in the queue",
    "invite_sent":       "📬  Plex invites sent",
    "invite_pending":    "⏳  Invites already pending",
    "invite_updated":    "🔄  Library shares refreshed",
    "invite_error":      "❌  Invite errors",
    "became_available":  "🟢  Cards flipped to available",
    "cmd_mystatus":      "📈  /requests mystatus checks",
    "cmd_plexstats":     "📊  /requests plexstats reports",
    "new_on_plex":       "🆕  New-on-Plex announcements",
    "revoked":           "🔐  Plex shares auto-revoked",
}


def _ago(ts: float, now: float | None = None) -> str:
    d = max(0, int((time.time() if now is None else now) - ts))
    for unit, secs in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= secs:
            return f"{d // secs}{unit} ago"
    return "just now"


def user_columns(ev: dict[str, int]) -> tuple[int, int, int, int, int]:
    buttons = ev.get("invite_button", 0) + ev.get("request_button", 0)
    searches = ev.get("search", 0)
    picks = ev.get("pick", 0)
    requests = ev.get("request_ok", 0)
    invites = ev.get("invite_sent", 0) + ev.get("invite_updated", 0)
    return (buttons, searches, picks, requests, invites)


class Stats:
    """Counters live under the `stats` key of the service state: {since, totals: {event: n},
    users: {id: {name, last, events: {event: n}}}} — the same shape the standalone bot's stats.json had."""

    KEY = "stats"

    def __init__(self, state: Any):
        self._s = state

    def data(self) -> dict[str, Any]:
        return dict(self._s.get(self.KEY) or {})

    def bump(self, event: str, user: Any = None) -> None:
        """Count one occurrence of `event`, optionally attributed to a Discord user. Never raises."""
        try:
            data = self.data()
            data.setdefault("since", time.time())
            totals = dict(data.get("totals") or {})
            totals[event] = totals.get(event, 0) + 1
            data["totals"] = totals
            if user is not None:
                users = dict(data.get("users") or {})
                u = dict(users.get(str(user.id)) or {"events": {}})
                u["name"] = getattr(user, "display_name", None) or str(user)
                u["last"] = time.time()
                events = dict(u.get("events") or {})
                events[event] = events.get(event, 0) + 1
                u["events"] = events
                users[str(user.id)] = u
                data["users"] = users
            self._s.set(self.KEY, data)
        except Exception:  # noqa: BLE001 - stats must never break a flow
            pass

    def report(self, now: float | None = None, max_users: int = 15) -> str:
        """Plain-text usage report (totals table + per-user table)."""
        data = self.data()
        now = time.time() if now is None else now
        if not data.get("totals"):
            return "Nothing counted yet — go push some buttons."
        out: list[str] = []
        since = data.get("since")
        if since:
            days = max(1, round((now - since) / 86400))
            out.append(f"since {time.strftime('%Y-%m-%d', time.localtime(since))} ({days}d)")
            out.append("")
        width = max(len(label) for label in EVENT_LABELS.values()) + 2
        out.append(f"{'EVENT':<{width}}COUNT")
        out.append(f"{'─' * width}─────")
        totals = data["totals"]
        for key, label in EVENT_LABELS.items():
            if totals.get(key):
                out.append(f"{label:<{width}}{totals[key]:>5}")
        for key, n in sorted(totals.items()):          # anything not in the label map
            if key not in EVENT_LABELS:
                out.append(f"{key:<{width}}{n:>5}")
        users = data.get("users") or {}
        if users:
            out.append("")
            name_w = max(12, max(len(u.get("name", "?")) for u in users.values()) + 2)
            out.append(f"{'USER':<{name_w}}{'BUTTONS':>8}{'SEARCHES':>10}{'PICKS':>7}{'REQUESTS':>10}{'INVITES':>9}   LAST SEEN")
            out.append(f"{'─' * (name_w + 44 + 11)}")
            ranked = sorted(users.values(), key=lambda u: -sum(user_columns(u.get("events", {}))))
            for u in ranked[:max_users]:
                b, s, p, r, i = user_columns(u.get("events", {}))
                out.append(f"{u.get('name', '?'):<{name_w}}{b:>8}{s:>10}{p:>7}{r:>10}{i:>9}   {_ago(u.get('last', 0), now)}")
        return "\n".join(out)

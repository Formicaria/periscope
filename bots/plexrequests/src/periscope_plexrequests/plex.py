"""Plex side: account invites / revokes, sessions and recently-added items via plexapi.

plexapi is synchronous; every public method here blocks and is meant to be called with `asyncio.to_thread`.
Connections are opened lazily and reused, so a Plex outage at start-up never blocks the service from building.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

InviteResult = tuple[str, str]   # status: sent | pending | updated | error, and a human detail


def wanted_sections(sections: list[Any], libraries: str) -> list[Any]:
    """Filter library sections by the LIBRARIES setting ('all' or comma-separated titles, case-insensitive)."""
    if (libraries or "all").strip().lower() == "all":
        return list(sections)
    wanted = {n.strip().lower() for n in libraries.split(",") if n.strip()}
    return [s for s in sections if s.title.lower() in wanted]


def classify_invite_error(message: str) -> InviteResult:
    """plexapi raises plain exceptions; 'already shared/invited' style messages mean a pending invite."""
    low = (message or "").lower()
    if "already" in low and any(k in low for k in ("shar", "invit", "friend", "exist")):
        return ("pending", "Looks like that address was already invited — check your email and accept it.")
    return ("error", f"Plex said no: {message[:300]}")


class PlexGateway:
    def __init__(self, url: str, token: str, libraries: str = "all", timeout: int = 20):
        self.url = url.rstrip("/")
        self.token = token
        self.libraries = libraries
        self.timeout = timeout
        self._account: Any = None
        self._server: Any = None

    # ----- connections -----

    def account(self) -> Any:
        if self._account is None:
            from plexapi.myplex import MyPlexAccount
            self._account = MyPlexAccount(token=self.token)
        return self._account

    def server(self) -> Any:
        if self._server is None:
            from plexapi.server import PlexServer
            self._server = PlexServer(self.url, self.token, timeout=self.timeout)
        return self._server

    def sections(self) -> list[Any]:
        return wanted_sections(self.server().library.sections(), self.libraries)

    # ----- invites -----

    def invite(self, email: str) -> InviteResult:
        """Share the configured libraries with `email`. Returns (status, detail)."""
        if not self.token:
            return ("error", "Plex is not configured yet (missing PLEX_TOKEN). Tell the server admin.")
        email_l = email.lower()
        try:
            acct = self.account()
            plex = self.server()
            secs = self.sections()

            try:
                for inv in acct.pendingInvites(includeSent=True, includeReceived=False):
                    if (getattr(inv, "email", "") or "").lower() == email_l or \
                       (getattr(inv, "username", "") or "").lower() == email_l:
                        return ("pending", "An invite for that address is already waiting — "
                                           "check your email (including spam) and accept it.")
            except Exception:  # noqa: BLE001 - pending-invite lookup is best effort
                pass

            friend = None
            for u in acct.users():
                if (u.email or "").lower() == email_l or (u.username or "").lower() == email_l:
                    friend = u
                    break
            if friend is not None:
                try:
                    acct.updateFriend(friend, plex, sections=secs)
                    return ("updated", "That account already has access — library share refreshed.")
                except Exception as e:  # noqa: BLE001
                    log.warning("updateFriend failed for %s: %s", email, e)
                    return ("updated", "That account already has access to the server.")

            acct.inviteFriend(email, plex, sections=secs)
            return ("sent", "Invite sent!")
        except Exception as e:  # noqa: BLE001
            status, detail = classify_invite_error(str(e))
            if status == "error":
                log.exception("Plex invite failed for %s", email)
            return (status, detail)

    def revoke(self, email: str) -> bool:
        """Remove the share (or cancel the pending invite) for `email`. True when something was removed."""
        acct = self.account()
        email_l = email.lower()
        for u in acct.users():
            if (u.email or "").lower() == email_l or (u.username or "").lower() == email_l:
                acct.removeFriend(u)
                return True
        try:
            for inv in acct.pendingInvites(includeSent=True, includeReceived=False):
                if (getattr(inv, "email", "") or "").lower() == email_l:
                    acct.cancelInvite(inv)
                    return True
        except Exception:  # noqa: BLE001
            pass
        return False

    # ----- status board / feed -----

    def sessions(self) -> list[str]:
        """One line per active stream: **user** — title (pct%)."""
        lines = []
        for s in self.server().sessions():
            user = (s.usernames or ["?"])[0]
            if s.type == "episode":
                title = f"{s.grandparentTitle} · S{s.parentIndex:02}E{s.index:02}"
            else:
                title = s.title
            pct = int(100 * (s.viewOffset or 0) / s.duration) if s.duration else 0
            lines.append(f"**{user}** — {title[:55]} ({pct}%)")
        return lines

    def recently_added(self, limit: int = 30) -> list[dict[str, Any]]:
        out = []
        for it in self.server().library.recentlyAdded()[:limit]:
            kind = getattr(it, "type", "?")
            if kind == "episode":
                title = f"{it.grandparentTitle} — S{it.parentIndex:02}E{it.index:02} · {it.title}"
            elif kind == "season":
                title = f"{it.parentTitle} — {it.title}"
            else:
                title = getattr(it, "title", "?")
            out.append({"key": str(it.ratingKey), "kind": kind, "title": title,
                        "year": getattr(it, "year", None),
                        "summary": (getattr(it, "summary", "") or "")[:280]})
        return out

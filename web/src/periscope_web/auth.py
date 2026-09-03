"""Sign-in model.

* Session = signed cookie (itsdangerous, `web.session_secret`), 14 days, httponly, SameSite=Lax.
* Discord OAuth2 (`identify guilds.members.read`): allowed when the member's roles in the default server include any of
  `web.allowed_role_ids` (default: the default server's `admin_role_ids`); when both are empty, the guild owner (GET /guilds/{id}
  with any presence's bot token) is allowed.
* Bootstrap: while `web.oauth_client_id` is empty, the one-time setup token logged at startup signs in a
  "bootstrap admin" and stores the OAuth application details.
* PERISCOPE_WEB_NOAUTH=1: every request is the local admin (development only, warned loudly at startup).
* CSRF: every session carries a token; state-changing requests must echo it (X-CSRF-Token header — HTMX sends it
  via hx-headers on <body> — or a `csrf` form field).
"""

from __future__ import annotations

import hmac
import os
import secrets
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

SESSION_COOKIE = "periscope_session"
STATE_COOKIE = "periscope_oauth"
FLASH_COOKIE = "periscope_flash"
SESSION_MAX_AGE = 14 * 86400
PUBLIC_PREFIXES = ("/login", "/auth/", "/healthz", "/logout")


def noauth() -> bool:
    return os.environ.get("PERISCOPE_WEB_NOAUTH", "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class User:
    id: str
    name: str
    avatar: str = ""
    via: str = "discord"  # discord | bootstrap | noauth
    csrf: str = field(default_factory=lambda: secrets.token_hex(16))

    def to_session(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "avatar": self.avatar, "via": self.via, "csrf": self.csrf}

    @classmethod
    def from_session(cls, d: dict[str, Any]) -> User | None:
        if not isinstance(d, dict) or not d.get("id") or not d.get("csrf"):
            return None
        return cls(str(d["id"]), str(d.get("name") or "?"), str(d.get("avatar") or ""), str(d.get("via") or "discord"), str(d["csrf"]))


class NotLoggedIn(Exception):
    pass


class CsrfFailed(Exception):
    pass


class Sessions:
    def __init__(self, secret: str, *, secure: bool = False):
        self._s = URLSafeTimedSerializer(secret, salt="periscope-web-session")
        self._state = URLSafeTimedSerializer(secret, salt="periscope-web-oauth-state")
        self._flash = URLSafeTimedSerializer(secret, salt="periscope-web-flash")
        self.secure = secure

    # ----- session ------------------------------------------------------------------------------
    def load(self, request: Request) -> User | None:
        raw = request.cookies.get(SESSION_COOKIE)
        if not raw:
            return None
        try:
            return User.from_session(self._s.loads(raw, max_age=SESSION_MAX_AGE))
        except BadSignature:
            return None

    def set(self, response, user: User) -> None:
        response.set_cookie(SESSION_COOKIE, self._s.dumps(user.to_session()), max_age=SESSION_MAX_AGE, httponly=True,
                            samesite="lax", secure=self.secure, path="/")

    def clear(self, response) -> None:
        response.delete_cookie(SESSION_COOKIE, path="/")

    def cookie_value(self, user: User) -> str:  # tests
        return self._s.dumps(user.to_session())

    # ----- OAuth state ----------------------------------------------------------------------------
    def new_state(self, response, next_url: str = "/") -> str:
        state = secrets.token_urlsafe(24)
        response.set_cookie(STATE_COOKIE, self._state.dumps({"s": state, "n": next_url}), max_age=600, httponly=True,
                            samesite="lax", secure=self.secure, path="/auth/")
        return state

    def check_state(self, request: Request, state: str) -> str | None:
        """Return the `next` url when `state` matches the cookie, else None."""
        raw = request.cookies.get(STATE_COOKIE)
        if not raw or not state:
            return None
        try:
            d = self._state.loads(raw, max_age=600)
        except BadSignature:
            return None
        if not hmac.compare_digest(str(d.get("s", "")), state):
            return None
        return str(d.get("n") or "/")

    # ----- flash (one render) ---------------------------------------------------------------------
    def flash_dumps(self, items: list[tuple[str, str]]) -> str:
        return self._flash.dumps(items)

    def flash_loads(self, raw: str) -> list[tuple[str, str]]:
        try:
            items = self._flash.loads(raw, max_age=300)
            return [(str(a), str(b)) for a, b in items]
        except (BadSignature, TypeError, ValueError):
            return []


def allowed_role_ids(store) -> list[str]:
    ids = [str(x).strip() for x in (store.web.get("allowed_role_ids") or []) if str(x).strip()]
    if not ids:
        ids = [str(x).strip() for x in (store.server().get("admin_role_ids") or []) if str(x).strip()]
    return ids


async def is_allowed(app_state, user_id: str, member_roles: list[str]) -> tuple[bool, str]:
    """Role gate, or guild-owner gate when no role is configured. Returns (allowed, reason)."""
    store = app_state.runtime.store
    guild_id = str(store.server().get("guild_id") or "").strip()
    if not guild_id:
        return False, "no Discord server is configured yet — sign in with the setup token first, then set one on the Discord page"
    wanted = allowed_role_ids(store)
    if wanted:
        if any(r in wanted for r in member_roles):
            return True, ""
        return False, "you are not holding one of the allowed roles in that server"
    token = next((str(p.get("token") or "") for p in store.presences.values() if p.get("token")), "")
    if not token:
        return False, "no allowed roles configured and no presence token to look up the server owner"
    try:
        g = await app_state.discord.guild(token, guild_id)
    except Exception as e:  # noqa: BLE001
        return False, f"could not look up the server owner: {e}"
    if str(g.get("owner_id")) == str(user_id):
        return True, ""
    return False, "no allowed roles are configured, so only the server owner may sign in"


def csrf_ok(user: User | None, provided: str | None) -> bool:
    return bool(user and provided and hmac.compare_digest(user.csrf, provided))

"""Sign-in: Discord OAuth2, the first-run bootstrap (setup token), sign-out."""

from __future__ import annotations

import hmac
import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from ..app import clear_setup_token, site_url
from ..auth import User, is_allowed
from ..discordapi import DiscordError, authorize_url, avatar_url
from ..render import flash, redirect, render
from . import save

log = logging.getLogger(__name__)
router = APIRouter()


def _safe_next(raw: str | None) -> str:
    if not raw or not raw.startswith("/") or raw.startswith("//") or urlparse(raw).netloc:
        return "/"
    return raw


def _redirect_uri(request: Request) -> str:
    return site_url(request) + "/auth/callback"


def _oauth_configured(store) -> bool:
    return bool(str(store.web.get("oauth_client_id") or "").strip() and str(store.web.get("oauth_client_secret") or "").strip())


def _bootstrap_session(request: Request, nxt: str):
    """Sign the browser in as the bootstrap admin and burn the one-time token."""
    st = request.app.state
    clear_setup_token(request.app)
    resp = redirect(request, _safe_next(nxt), 303)
    st.sessions.set(resp, User("bootstrap", "bootstrap admin", "", "bootstrap"))
    log.info("web sign-in: bootstrap admin (setup token) from %s", request.client.host if request.client else "?")
    return resp


@router.get("/login")
async def login(request: Request, next: str | None = None, error: str | None = None, token: str | None = None):
    st = request.app.state
    if st.noauth or getattr(request.state, "user", None):
        return RedirectResponse(_safe_next(next), status_code=302)
    store = st.runtime.store
    if token:
        # the one-time link `periscope web` prints: /login?token=…
        if st.setup_token and hmac.compare_digest(st.setup_token, token.strip()):
            if not _oauth_configured(store):
                flash(request, "Signed in. To let others sign in with Discord, add the OAuth application on the Discord page.", "info")
            return _bootstrap_session(request, next or "/")
        log.warning("web bootstrap: wrong or used setup token in link from %s", request.client.host if request.client else "?")
        error = error or "That sign-in link is not valid any more — run `periscope web` on the box for a fresh one"
    return render(request, "login.html", {
        "oauth": _oauth_configured(store),
        "client_id": store.web.get("oauth_client_id") or "",
        "base_url": store.web.get("base_url") or "",
        "redirect_uri": _redirect_uri(request),
        "secret_set": bool(store.web.get("oauth_client_secret")),
        "token_available": bool(st.setup_token),
        "next": _safe_next(next),
        "error": error,
    })


@router.get("/auth/discord")
async def auth_discord(request: Request, next: str | None = None):
    st = request.app.state
    store = st.runtime.store
    if not _oauth_configured(store):
        return RedirectResponse("/login", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    state = st.sessions.new_state(resp, _safe_next(next))
    resp.headers["location"] = authorize_url(str(store.web["oauth_client_id"]), _redirect_uri(request), state)
    return resp


@router.get("/auth/callback")
async def auth_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None):
    st = request.app.state
    store = st.runtime.store
    if error or not code:
        flash(request, f"Discord sign-in failed: {error or 'no code'}", "error")
        return redirect(request, "/login", 302)
    nxt = st.sessions.check_state(request, state or "")
    if nxt is None:
        flash(request, "Sign-in state did not match — try again", "error")
        return redirect(request, "/login", 302)
    api = st.discord
    try:
        tok = await api.exchange_code(str(store.web["oauth_client_id"]), str(store.web["oauth_client_secret"]), code, _redirect_uri(request))
        access = str(tok.get("access_token") or "")
        me = await api.oauth_me(access)
        roles: list[str] = []
        gid = str(store.lab.get("guild_id") or "").strip()
        if gid:
            try:
                member = await api.oauth_member(access, gid)
                roles = [str(r) for r in member.get("roles") or []]
            except DiscordError as e:
                if e.status != 404:
                    raise
                return render(request, "login.html", {"oauth": True, "denied": "you are not a member of the lab server",
                                                      "token_available": bool(st.setup_token), "next": nxt}, status=403)
    except DiscordError as e:
        log.warning("OAuth sign-in failed: %s", e)
        flash(request, f"Discord sign-in failed: {e}", "error")
        return redirect(request, "/login", 302)
    ok, why = await is_allowed(st, str(me.get("id")), roles)
    if not ok:
        log.warning("web sign-in denied for %s: %s", me.get("username"), why)
        return render(request, "login.html", {"oauth": True, "denied": why, "token_available": bool(st.setup_token), "next": nxt},
                      status=403)
    user = User(str(me["id"]), str(me.get("global_name") or me.get("username") or "?"), avatar_url(me), "discord")
    resp = RedirectResponse(nxt, status_code=302)
    st.sessions.set(resp, user)
    resp.delete_cookie("periscope_oauth", path="/auth/")
    log.info("web sign-in: %s", user.name)
    return resp


@router.post("/auth/bootstrap")
async def auth_bootstrap(request: Request):
    """First run: the setup token from the log signs in a bootstrap admin and stores the OAuth application."""
    st = request.app.state
    store = st.runtime.store
    form = await request.form()
    token = str(form.get("token") or "").strip()
    if not st.setup_token or not token or not hmac.compare_digest(st.setup_token, token):
        log.warning("web bootstrap: wrong setup token from %s", request.client.host if request.client else "?")
        why = ("That setup token was already used — run `periscope web` on the box for a fresh link (a restart makes a new one)"
               if not st.setup_token else "That is not the current setup token — run `periscope web` on the box and use the link it prints")
        return render(request, "login.html", {"oauth": _oauth_configured(store), "token_available": bool(st.setup_token),
                                              "error": why, "next": "/",
                                              "client_id": form.get("client_id") or "", "base_url": form.get("base_url") or "",
                                              "redirect_uri": _redirect_uri(request)}, status=403)
    client_id = str(form.get("client_id") or "").strip()
    client_secret = str(form.get("client_secret") or "").strip()
    base_url = str(form.get("base_url") or "").strip().rstrip("/")
    changed = False
    if client_id:
        store.web["oauth_client_id"] = client_id
        changed = True
    if client_secret:
        store.web["oauth_client_secret"] = client_secret
        changed = True
    if base_url:
        store.web["base_url"] = base_url
        changed = True
    if changed:
        save(request)
    if _oauth_configured(store):
        flash(request, "Discord sign-in saved — next time, sign in with Discord", "success")
    else:
        flash(request, "Signed in. To let others sign in with Discord, add the OAuth application on the Discord page.", "info")
    return _bootstrap_session(request, str(form.get("next") or "/"))


@router.api_route("/logout", methods=["GET", "POST"])
async def logout(request: Request):
    resp = RedirectResponse("/login", status_code=302)
    request.app.state.sessions.clear(resp)
    return resp

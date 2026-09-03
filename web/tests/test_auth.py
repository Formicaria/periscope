"""Auth gate, CSRF, Discord OAuth callback, bootstrap with the setup token, NOAUTH mode."""

from __future__ import annotations

import httpx
from periscope_web.auth import SESSION_COOKIE
from periscope_web.discordapi import OAUTH_SCOPES


async def test_logged_out_redirects_pages_and_401s_api(anon):
    r = await anon.get("/")
    assert r.status_code == 302 and r.headers["location"].startswith("/login")
    r = await anon.get("/api/status")
    assert r.status_code == 401 and r.json() == {"error": "unauthorized"}
    r = await anon.get("/services/pve", headers={"HX-Request": "true"})
    assert r.status_code == 401 and r.headers["HX-Redirect"] == "/login"
    r = await anon.get("/healthz")  # public
    assert r.status_code == 200 and r.json()["ok"] is True


async def test_session_cookie_allows(client):
    r = await client.get("/")
    assert r.status_code == 200 and "Proxmox VE" in r.text and "Alice" in r.text


async def test_noauth_env(runtime, logbuf, monkeypatch):
    monkeypatch.setenv("PERISCOPE_WEB_NOAUTH", "1")
    from periscope_web.app import create_app

    app = create_app(runtime, log_buffer=logbuf)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/")
        assert r.status_code == 200 and "NOAUTH mode" in r.text
        r = await c.get("/login")
        assert r.status_code == 302 and r.headers["location"] == "/"


async def test_csrf_required_on_posts(app, user):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                                 cookies={SESSION_COOKIE: app.state.sessions.cookie_value(user)}) as c:
        r = await c.post("/services/pve/disable")
        assert r.status_code == 403
        r = await c.post("/services/pve/disable", data={"csrf": user.csrf})  # form field works too
        assert r.status_code == 303
        r = await c.post("/services/pve/disable", headers={"X-CSRF-Token": "wrong", "HX-Request": "true"})
        assert r.status_code == 403 and "Session expired" in r.text and r.headers["HX-Reswap"] == "none"


async def test_login_page_and_authorize_redirect(anon):
    r = await anon.get("/login")
    assert r.status_code == 200 and "Sign in with Discord" in r.text
    r = await anon.get("/auth/discord?next=/logs")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://discord.com/oauth2/authorize?") and "client_id=cid" in loc
    assert "redirect_uri=http%3A%2F%2Ftest%2Fauth%2Fcallback" in loc and OAUTH_SCOPES.replace(" ", "+") in loc
    assert "periscope_oauth" in r.headers.get("set-cookie", "")


async def test_oauth_callback_allowed_by_role(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/auth/discord?next=/logs")
        state = r.headers["location"].split("state=")[1].split("&")[0]
        r = await c.get(f"/auth/callback?code=goodcode&state={state}")
        assert r.status_code == 302 and r.headers["location"] == "/logs"
        assert SESSION_COOKIE in c.cookies
        r = await c.get("/")
        assert r.status_code == 200 and "Alice" in r.text


async def test_oauth_callback_denied_without_role(app, store):
    store.lab["admin_role_ids"] = ["9999"]  # alice holds 2001 only
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/auth/discord")
        state = r.headers["location"].split("state=")[1].split("&")[0]
        r = await c.get(f"/auth/callback?code=goodcode&state={state}")
        assert r.status_code == 403 and "not holding one of the allowed roles" in r.text
        assert SESSION_COOKIE not in c.cookies
        # bad state → back to login
        r = await c.get("/auth/callback?code=goodcode&state=nope")
        assert r.status_code == 302 and r.headers["location"] == "/login"


async def test_owner_fallback_when_no_roles_configured(app, store):
    store.lab["admin_role_ids"] = []
    store.web["allowed_role_ids"] = []
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/auth/discord")
        state = r.headers["location"].split("state=")[1].split("&")[0]
        r = await c.get(f"/auth/callback?code=goodcode&state={state}")
        assert r.status_code == 302  # 555 is the guild owner in the mocked API


async def test_bootstrap_setup_page_and_token(make_app, store, reload):
    """No OAuth app yet → /login is the setup page; the setup token signs in and stores the OAuth details once."""
    store.web.update({"oauth_client_id": "", "oauth_client_secret": ""})
    app = make_app(setup_token="tok-123")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/login")
        assert r.status_code == 200 and "First run" in r.text and "/auth/callback" in r.text
        r = await c.post("/auth/bootstrap", data={"token": "wrong", "client_id": "x"})
        assert r.status_code == 403 and SESSION_COOKIE not in c.cookies
        r = await c.post("/auth/bootstrap", data={"token": "tok-123", "client_id": "app1", "client_secret": "sec1", "base_url": "https://p.example/"})
        assert r.status_code == 303 and SESSION_COOKIE in c.cookies
        saved = reload()
        assert saved.web["oauth_client_id"] == "app1" and saved.web["oauth_client_secret"] == "sec1" and saved.web["base_url"] == "https://p.example"
        assert app.state.setup_token is None  # one-time
        r = await c.get("/")
        assert r.status_code == 200 and "bootstrap admin" in r.text
        r = await c.get("/logout")
        assert r.status_code == 302
        r = await c.get("/")
        assert r.status_code == 302  # signed out

"""Jinja rendering, HTMX helpers and flash toasts.

Full pages: `render()`. HTMX partial swaps: `partial()` — flashes queued on the request are appended as an
out-of-band swap into #toasts. Redirects: `redirect()` carries queued flashes over in a short-lived signed cookie.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from periscope.embeds import human_duration

from . import __version__
from .auth import FLASH_COOKIE

TEMPLATES = Path(__file__).parent / "templates"

NAV = [
    ("/", "Overview", "grid"),
    ("/presences", "Bots", "bot"),
    ("/discord", "Discord", "hash"),
    ("/routing", "Routing", "route"),
    ("/logs", "Logs", "terminal"),
]

# the runtime's plain-language states (periscope.runtime) plus the two only the UI knows about
STATE_BADGE = {
    "running": "badge-success",
    "starting": "badge-warning",
    "error": "badge-error",
    "needs setup": "badge-warning",
    "on after restart": "badge-info",
    "off": "badge-ghost",
    "not installed": "badge-ghost",
}

# where a problem gets fixed: the runtime names a page, the UI knows the URL
FIX_HREF = {"settings": "/services/{name}", "bots": "/presences", "logs": "/logs?q={name}", "discord": "/discord"}
FIX_LABEL = {"settings": "open settings", "bots": "open Bots", "logs": "open the log", "discord": "open Discord settings"}


def fix_link(fix: str | None, name: str) -> tuple[str, str] | None:
    if not fix or fix not in FIX_HREF:
        return None
    return FIX_HREF[fix].format(name=name), FIX_LABEL[fix]


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    env.filters["duration"] = human_duration
    env.globals["version"] = __version__
    env.globals["nav"] = NAV
    env.globals["state_badge"] = lambda s: STATE_BADGE.get(s, "badge-ghost")
    env.globals["fix_link"] = fix_link
    env.globals["now"] = time.time
    return env


ENV = _env()


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request", "").lower() == "true"


def flash(request: Request, message: str, level: str = "info") -> None:
    """Queue a toast for this response (partial → OOB swap; page → rendered; redirect → cookie)."""
    items = getattr(request.state, "flashes", None)
    if items is None:
        items = []
        request.state.flashes = items
    items.append((level, message))


def _base_ctx(request: Request) -> dict[str, Any]:
    app = request.app
    st = app.state
    runtime = st.runtime
    store = runtime.store
    user = getattr(request.state, "user", None)
    setup_needed = not any(p.get("token") for p in store.presences.values())
    return {
        "request": request,
        "user": user,
        "csrf": user.csrf if user else "",
        "path": request.url.path,
        "lab": store.lab,
        "noauth": st.noauth,
        "dirty": st.dirty(),
        "setup_needed": setup_needed,
        "uptime": human_duration(time.time() - runtime.started),
        "web_port": store.web.get("port"),
        "webhook_port": store.webhook.get("port"),
        "flashes": list(getattr(request.state, "flashes", []) or []),
    }


def render(request: Request, name: str, ctx: dict[str, Any] | None = None, status: int = 200,
           headers: dict[str, str] | None = None) -> HTMLResponse:
    base = _base_ctx(request)
    raw = request.cookies.get(FLASH_COOKIE)
    if raw:
        base["flashes"] = request.app.state.sessions.flash_loads(raw) + base["flashes"]
    base.update(ctx or {})
    html = ENV.get_template(name).render(**base)
    resp = HTMLResponse(html, status_code=status, headers=headers)
    if raw:
        resp.delete_cookie(FLASH_COOKIE, path="/")
    return resp


def partial(request: Request, name: str, ctx: dict[str, Any] | None = None, status: int = 200,
            headers: dict[str, str] | None = None) -> HTMLResponse:
    """A fragment for an HTMX swap, plus any queued toasts as an out-of-band swap."""
    base = _base_ctx(request)
    base.update(ctx or {})
    html = ENV.get_template(name).render(**base)
    if base["flashes"]:
        html += ENV.get_template("partials/toasts.html").render(flashes=base["flashes"], oob=True)
    return HTMLResponse(html, status_code=status, headers=headers)


def toasts(request: Request, status: int = 200) -> HTMLResponse:
    """Only the queued toasts: HX-Reswap: none keeps the caller's target untouched, the OOB toast still lands."""
    base = _base_ctx(request)
    html = ENV.get_template("partials/toasts.html").render(flashes=base["flashes"], oob=True)
    return HTMLResponse(html, status_code=status, headers={"HX-Reswap": "none"})


def redirect(request: Request, url: str, status: int = 303) -> RedirectResponse:
    """Redirect and carry queued flashes over. For HTMX callers answer with HX-Redirect instead."""
    items = list(getattr(request.state, "flashes", []) or [])
    if is_htmx(request):
        resp = HTMLResponse("", status_code=200, headers={"HX-Redirect": url})
    else:
        resp = RedirectResponse(url, status_code=status)
    if items:
        resp.set_cookie(FLASH_COOKIE, request.app.state.sessions.flash_dumps(items), max_age=300, httponly=True,
                        samesite="lax", path="/")
    return resp

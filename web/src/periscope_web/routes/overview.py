"""Overview: every service as a card with a plain-language state, a "needs attention" list with a fix link per
problem, on/off + Test per card, one Restart for the whole process (header) when config changed."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import restart
from ..forms import merged_env, parse_form
from ..render import fix_link, flash, is_htmx, partial, redirect, render
from . import save
from .servers import server_label

log = logging.getLogger(__name__)
router = APIRouter()

GROUPS = [("infra", "Infrastructure"), ("media", "Media"), ("dev", "Dev")]

OFF, PENDING, NOT_INSTALLED = "off", "on after restart", "not installed"


def service_card(request: Request, name: str, status: dict[str, Any] | None = None,
                 names: dict[str, str] | None = None) -> dict[str, Any]:
    """Everything the card partial needs for one service (installed spec and/or configured entry). `names` is the
    {Discord server id: real name} map from `st.guild.names()`, so the card can say which server it means."""
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    spec = runtime.specs.get(name)
    status = status or runtime.status()
    svc = store.services.get(name) or {}
    enabled = bool(svc.get("enabled"))
    live = status.get("services", {}).get(name)
    presence = store.presence_for(name)
    pinfo = store.presences.get(presence) or {}
    server = store.server_for(name)
    # the server is only worth a word on the card when there is more than one to choose from
    label = server_label(server, store.servers[server], names) if len(store.servers) > 1 else ""
    problem: str | None = None
    fix: str | None = None
    if spec is None:
        state = NOT_INSTALLED
        problem = "the package for this service is not installed — run periscope update"
    elif not enabled:
        state = OFF
    elif live:
        state = str(live.get("state"))
        if state != "running":
            problem, fix = live.get("error"), live.get("fix")
    else:
        state = PENDING
        problem = "switched on — starts on the next restart"
    if enabled and spec is not None and state in (OFF, PENDING, "needs setup"):
        # the runtime only knows about the last start; say now what would block the next one
        missing = spec.required_missing(store.env_for(name))
        if missing:
            labels = [(spec.setting(k).label if spec.setting(k) else k) for k in missing]
            problem, fix, state = "needs " + ", ".join(labels), "settings", "needs setup"
        elif not pinfo.get("token"):
            problem, fix, state = f"no bot token yet (bot '{presence}')", "bots", "needs setup"
    link = fix_link(fix, name)
    presence_user = status.get("presences", {}).get(presence, {}).get("user")
    return {
        "name": name, "title": spec.title if spec else name, "description": spec.description if spec else "",
        "group": spec.group if spec else "infra", "slash": spec.slash if spec else "", "installed": spec is not None,
        "state": state, "enabled": enabled, "presence": presence, "presence_user": presence_user,
        "presence_label": pinfo.get("label") or presence, "presence_has_token": bool(pinfo.get("token")),
        "server": server, "server_label": label,
        "has_check": bool(spec and spec.check), "needs_webhook": bool(spec and spec.needs_webhook),
        "webhook_paths": list(spec.webhook_paths) if spec else [],
        "problem": problem, "fix_href": link[0] if link else None, "fix_label": link[1] if link else None,
        "starting": state == "starting",
    }


def _presence_chips(store, status: dict[str, Any]) -> list[dict[str, Any]]:
    chips = []
    for pname, p in status.get("presences", {}).items():
        chips.append({"name": pname, "label": store.presences.get(pname, {}).get("label") or pname, "user": p.get("user"),
                      "connected": bool(p.get("connected")), "error": p.get("error"), "invite": p.get("invite"),
                      "services": p.get("services") or []})
    return chips


@router.get("/")
async def overview(request: Request):
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    if not any(p.get("token") for p in store.presences.values()) and not is_htmx(request):
        return redirect(request, "/setup", 302)
    status = runtime.status()
    server_names = await st.guild.names()          # one lookup for the whole page, so every card can name its server
    names = list(runtime.specs) + [n for n in store.services if n not in runtime.specs]
    cards = [service_card(request, n, status, server_names) for n in names]
    groups = [(key, title, [c for c in cards if c["group"] == key]) for key, title in GROUPS]
    other = [c for c in cards if c["group"] not in {k for k, _ in GROUPS}]
    if other:
        groups.append(("other", "Other", other))
    chips = _presence_chips(store, status)
    bot_errors = {c["error"] for c in chips if not c["connected"] and c["error"]}
    # one line per bot that is down (not one per service riding on it), then every service-level problem
    attention = [{"name": c["name"], "title": f"bot {c['label']}", "problem": c["error"], "fix_href": "/presences",
                  "fix_label": "open Bots", "state": "error"} for c in chips if not c["connected"] and c["error"]]
    attention += [c for c in cards if c["enabled"] and c["problem"] and not c["starting"] and c["state"] != PENDING
                  and c["problem"] not in bot_errors]
    counts = {"running": sum(1 for c in cards if c["state"] == "running"), "enabled": sum(1 for c in cards if c["enabled"]),
              "problems": len(attention)}
    return render(request, "overview.html", {"groups": [g for g in groups if g[2]], "status": status, "counts": counts,
                                             "chips": chips, "attention": attention})


async def _card_response(request: Request, name: str):
    if is_htmx(request):
        card = service_card(request, name, names=await request.app.state.guild.names())
        return partial(request, "partials/service_card.html", {"card": card})
    return redirect(request, "/")


@router.post("/services/{name}/enable")
async def enable(request: Request, name: str):
    """Switch a service on. When its required settings are still empty, go to its settings page instead of
    switching on something that would only be skipped."""
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    spec = runtime.specs.get(name)
    if spec is None:
        raise HTTPException(404, f"unknown service {name}")
    svc = store.service(name)
    missing = spec.required_missing(store.env_for(name))
    if missing:
        labels = [(spec.setting(k).label if spec.setting(k) else k) for k in missing]
        flash(request, f"{spec.title} needs {', '.join(labels)} first — fill them in and save with the switch on", "info")
        return redirect(request, f"/services/{name}")
    if not store.token_for(name):
        flash(request, f"{spec.title} has no bot to post as yet — add a bot token first", "info")
        return redirect(request, "/presences")
    store.set_enabled(name, True)
    save(request)
    flash(request, f"{spec.title} is on — it starts on the next restart (button in the header)", "success")
    return await _card_response(request, name)


@router.post("/services/{name}/disable")
async def disable(request: Request, name: str):
    st = request.app.state
    store = st.runtime.store
    if name not in store.services and name not in st.runtime.specs:
        raise HTTPException(404, f"unknown service {name}")
    store.set_enabled(name, False)
    save(request)
    flash(request, f"{name} is off — it stops on the next restart", "info")
    return await _card_response(request, name)


@router.post("/services/{name}/check")
async def check(request: Request, name: str):
    """Run spec.check(): on the stored env, or on the submitted form values when the settings form is included."""
    st = request.app.state
    runtime = st.runtime
    spec = runtime.specs.get(name)
    if spec is None:
        raise HTTPException(404, f"unknown service {name}")
    env = runtime.store.env_for(name)
    form = await request.form()
    if any(k for k in form.keys() if k not in ("csrf",)):
        values, errors = parse_form(spec, form, env, require=False)
        if errors:
            return partial(request, "partials/check_result.html", {"ok": False, "message": "; ".join(errors), "name": name}, status=200)
        env = merged_env(env, values)
    if spec.check is None:
        return partial(request, "partials/check_result.html", {"ok": None, "message": "this service has no credential check", "name": name})
    try:
        ok, message = await asyncio.wait_for(spec.check(env), timeout=30)
    except asyncio.TimeoutError:
        ok, message = False, "check timed out after 30 s"
    except Exception as e:  # noqa: BLE001
        log.exception("check() of %s raised", name)
        ok, message = False, f"{type(e).__name__}: {e}"
    return partial(request, "partials/check_result.html", {"ok": ok, "message": message, "name": name})


@router.post("/restart")
async def restart_process(request: Request):
    """Re-exec the runtime in one second (config changes apply on restart)."""
    delay = float(getattr(request.app.state, "restart_delay", 1.0))
    restart.schedule(delay)
    log.warning("restart requested from the web UI by %s", getattr(request.state.user, "name", "?"))
    return render(request, "restarting.html", {"delay": delay})

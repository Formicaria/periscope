"""/services/{name}: the service's typed settings as a form. Save validates and writes the store; Test lives in
overview.py (`/services/{name}/check`) and runs on the submitted values without saving."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from ..forms import build_fields, parse_form
from ..render import fix_link, flash, is_htmx, partial, redirect, render
from . import save
from .servers import server_options

log = logging.getLogger(__name__)
router = APIRouter()


async def _ctx(request: Request, name: str, *, errors: list[str] | None = None) -> dict:
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    spec = runtime.specs.get(name)
    if spec is None:
        raise HTTPException(404, f"unknown service {name}")
    svc = store.services.get(name) or {"enabled": False, "presence": store.default_presence(), "env": {}}
    env = {str(k): ("" if v is None else str(v)) for k, v in (svc.get("env") or {}).items()}
    server = store.server_for(name)                 # the pickers show the server this service posts in
    channels, roles = await st.guild.channels(server), await st.guild.roles(server)
    live = runtime.status().get("services", {}).get(name) or {}
    enabled = bool(svc.get("enabled"))
    state = live.get("state") if enabled else "off"
    if enabled and not live:
        state = "on after restart"
    return {
        "spec": spec, "name": name, "svc": svc, "enabled": enabled, "state": state or "starting",
        "problem": live.get("error") if enabled and live.get("state") != "running" else None,
        "fix": fix_link(live.get("fix"), name) if enabled else None,
        "presence": store.presence_for(name),
        "presences": [(k, v.get("label") or k, bool(v.get("token"))) for k, v in store.presences.items()],
        "server": server, "servers": server_options(store, await st.guild.names()),
        "groups": build_fields(spec, env, channels=channels, roles=roles, store=store, server=server),
        "pickers": bool(channels), "errors": errors or [], "live": live,
        "webhook": {"port": store.webhook.get("port"), "paths": spec.webhook_paths, "needs": spec.needs_webhook},
    }


@router.get("/services/{name}")
async def service_page(request: Request, name: str):
    return render(request, "service.html", await _ctx(request, name))


@router.post("/services/{name}")
async def service_save(request: Request, name: str):
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    spec = runtime.specs.get(name)
    if spec is None:
        raise HTTPException(404, f"unknown service {name}")
    form = await request.form()
    svc = store.service(name)
    current = {str(k): ("" if v is None else str(v)) for k, v in (svc.get("env") or {}).items()}
    enabled = str(form.get("_enabled") or "").lower() in ("1", "true", "on", "yes")
    values, errors = parse_form(spec, form, current, require=enabled)
    presence = str(form.get("_presence") or svc.get("presence") or store.default_presence())
    if presence not in store.presences:
        errors.append(f"unknown bot {presence!r}")
    server = str(form.get("_server") or svc.get("server") or store.server_for(name))
    if server not in store.servers:
        errors.append(f"unknown server {server!r}")
    if errors:
        for e in errors:
            flash(request, e, "error")
        ctx = await _ctx(request, name, errors=errors)
        if is_htmx(request):
            return partial(request, "partials/service_form.html", ctx, status=422)
        return render(request, "service.html", ctx, status=422)
    store.update_service_env(name, values)
    svc["presence"] = presence
    svc["server"] = server
    svc["enabled"] = enabled
    save(request)
    missing = spec.required_missing(store.env_for(name))
    labels = [(spec.setting(k).label if spec.setting(k) else k) for k in missing]
    if missing and enabled:
        flash(request, f"saved — {spec.title} is on but still needs {', '.join(labels)}", "warning")
    elif missing:
        flash(request, f"saved — still needed before switching on: {', '.join(labels)}", "info")
    elif enabled:
        flash(request, "saved — applies on the next restart (button in the header)", "success")
    else:
        flash(request, "saved", "success")
    if is_htmx(request):
        return partial(request, "partials/service_form.html", await _ctx(request, name))
    return redirect(request, f"/services/{name}")

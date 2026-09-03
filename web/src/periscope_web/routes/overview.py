"""Overview: every service as a card, Enable/Disable/Test per card, whole-process Restart."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import restart
from ..forms import merged_env, parse_form
from ..render import flash, is_htmx, partial, redirect, render
from . import save

log = logging.getLogger(__name__)
router = APIRouter()

GROUPS = [("infra", "Infrastructure"), ("media", "Media"), ("dev", "Dev")]


def service_card(request: Request, name: str, status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the card partial needs for one service (installed spec and/or configured entry)."""
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    spec = runtime.specs.get(name)
    status = status or runtime.status()
    svc = store.services.get(name) or {}
    enabled = bool(svc.get("enabled"))
    live = status.get("services", {}).get(name)
    presence = svc.get("presence") or (spec.default_presence if spec else "default")
    if spec is None:
        state, detail = "missing", "package not installed"
    elif live:
        state, detail = str(live.get("state")), str(live.get("error") or "")
        if not enabled and state != "skipped":
            detail = detail or "disabled — stops on restart"
    elif enabled:
        state, detail = "pending", "enabled — starts on restart"
    else:
        state, detail = "disabled", ""
    presence_user = status.get("presences", {}).get(presence, {}).get("user")
    return {
        "name": name, "title": spec.title if spec else name, "description": spec.description if spec else "",
        "group": spec.group if spec else "infra", "slash": spec.slash if spec else "", "installed": spec is not None,
        "state": state, "detail": detail, "enabled": enabled, "presence": presence, "presence_user": presence_user,
        "presence_label": store.presences.get(presence, {}).get("label") or presence,
        "has_check": bool(spec and spec.check), "needs_webhook": bool(spec and spec.needs_webhook),
        "webhook_paths": list(spec.webhook_paths) if spec else [],
        "error": (live or {}).get("error") if live and live.get("state") == "error" else None,
    }


@router.get("/")
async def overview(request: Request):
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    if not any(p.get("token") for p in store.presences.values()) and not is_htmx(request):
        return redirect(request, "/setup", 302)
    status = runtime.status()
    names = list(runtime.specs) + [n for n in store.services if n not in runtime.specs]
    cards = [service_card(request, n, status) for n in names]
    groups = [(key, title, [c for c in cards if c["group"] == key]) for key, title in GROUPS]
    other = [c for c in cards if c["group"] not in {k for k, _ in GROUPS}]
    if other:
        groups.append(("other", "Other", other))
    counts = {"running": sum(1 for c in cards if c["state"] == "running"), "enabled": sum(1 for c in cards if c["enabled"]),
              "problems": sum(1 for c in cards if c["state"] in ("error", "skipped"))}
    return render(request, "overview.html", {"groups": [g for g in groups if g[2]], "status": status, "counts": counts,
                                             "presences": status.get("presences", {})})


def _card_response(request: Request, name: str):
    if is_htmx(request):
        return partial(request, "partials/service_card.html", {"card": service_card(request, name)})
    return redirect(request, "/")


@router.post("/services/{name}/enable")
async def enable(request: Request, name: str):
    st = request.app.state
    runtime = st.runtime
    spec = runtime.specs.get(name)
    if spec is None:
        raise HTTPException(404, f"unknown service {name}")
    svc = runtime.store.service(name)
    if not svc.get("presence"):
        svc["presence"] = spec.default_presence
    missing = spec.required_missing(runtime.store.env_for(name))
    runtime.store.set_enabled(name, True)
    save(request)
    if missing:
        flash(request, f"{spec.title} enabled — still missing {', '.join(missing)}; it will be skipped until set", "warning")
    else:
        flash(request, f"{spec.title} enabled — restart to apply", "success")
    return _card_response(request, name)


@router.post("/services/{name}/disable")
async def disable(request: Request, name: str):
    st = request.app.state
    store = st.runtime.store
    if name not in store.services and name not in st.runtime.specs:
        raise HTTPException(404, f"unknown service {name}")
    store.set_enabled(name, False)
    save(request)
    flash(request, f"{name} disabled — restart to apply", "info")
    return _card_response(request, name)


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

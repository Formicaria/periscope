"""/alerts: what is firing right now, and when periscope is meant to stay quiet.

Two halves. The top is every alert the running services currently hold open, with its state — firing, acked by
someone, snoozed until a time, or held back by a maintenance window — and a button to ack or close it from the
browser instead of from Discord. Those buttons call the very same `AlertRouter` the cards call, so the Discord
card is edited at the same moment.

The bottom is the maintenance-window editor: the quiet times, written to `config/maintenance.yaml`. A window
covers a set of servers and services (empty means all of them), either on chosen weekdays between two clock
times or once between two dates, and it carries a reason so the log and the card can say why nobody was paged.
There is also one switch that quiets everything until a moment in time, for the jobs that touch the whole rack.

The file is re-read whenever it changes, so a save here is live on the next poll — no restart needed, which is
why nothing on this page raises the "restart to apply" flag.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from periscope.embeds import human_duration
from periscope.maintenance import DAY_LABELS, DAY_NAMES, Windows

from ..render import flash, is_htmx, partial, redirect, render, toasts

log = logging.getLogger(__name__)
router = APIRouter()

SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2, "ok": 3, "unknown": 4}
SEVERITY_BADGE = {"critical": "badge-error", "warning": "badge-warning", "info": "badge-info",
                  "ok": "badge-success"}
STATE_BADGE = {"firing": "badge-error", "acked": "badge-info", "snoozed": "badge-ghost",
               "held back": "badge-ghost"}
SNOOZE_CHOICES = (1, 8, 24)
WINDOW_FILE = "maintenance.yaml"


# ----- where the live objects come from ---------------------------------------------------------------------
def windows_of(request: Request) -> Windows:
    """The running maintenance windows, or a reader over the same file when nothing is running yet."""
    st = request.app.state
    live = getattr(st.runtime, "windows", None)
    if isinstance(live, Windows):
        live.reload()
        return live
    path = Path(st.runtime.store.path).parent / WINDOW_FILE
    cached = getattr(st, "windows", None)
    if isinstance(cached, Windows) and cached.path == path:
        cached.reload()
        return cached
    st.windows = Windows(path)
    return st.windows


def routers_of(request: Request) -> list[tuple[str, Any]]:
    """(service name, its AlertRouter) for every service the runtime actually built."""
    services = getattr(request.app.state.runtime, "services", None) or {}
    out = []
    for name, bot in services.items():
        alerts = getattr(bot, "alerts", None)
        if alerts is not None and hasattr(alerts, "snapshot"):
            out.append((name, alerts))
    return out


def router_for(request: Request, service: str) -> Any:
    for name, alerts in routers_of(request):
        if name == service:
            return alerts
    raise HTTPException(404, f"{service} is not running, so its alerts cannot be changed from here")


# ----- reading -----------------------------------------------------------------------------------------------
def since_words(ts: float | None, now: float | None = None) -> str:
    """'4m' / '2h 5m' — how long this alert has been open."""
    if not ts:
        return ""
    return human_duration(max(0, (now or time.time()) - float(ts)))


def firing(request: Request) -> list[dict[str, Any]]:
    now = time.time()
    rows: list[dict[str, Any]] = []
    for name, alerts in routers_of(request):
        try:
            found = alerts.snapshot()
        except Exception:  # noqa: BLE001 - one broken service must not empty the page
            log.exception("could not read the open alerts of %s", name)
            continue
        for row in found:
            row = dict(row)
            row["service"] = row.get("service") or name
            row["owner"] = name                       # which router to call back for ack / resolve
            row["age"] = since_words(row.get("since"), now)
            row["snoozed_words"] = clock_words(row.get("snoozed_until"))
            row["severity_badge"] = SEVERITY_BADGE.get(str(row.get("severity")), "badge-ghost")
            row["state_badge"] = STATE_BADGE.get(str(row.get("state")), "badge-ghost")
            rows.append(row)
    rows.sort(key=lambda r: (SEVERITY_ORDER.get(str(r.get("severity")), 5), -(r.get("since") or 0)))
    return rows


def clock_words(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (ValueError, OSError, OverflowError):
        return ""


def server_options(request: Request) -> list[str]:
    """The server names a window can name — what a bot calls its own server, lowercased."""
    store = request.app.state.runtime.store
    names = [str(s.get("name") or key).strip().lower() for key, s in store.servers.items()]
    return sorted({n for n in names if n})


def page_ctx(request: Request) -> dict[str, Any]:
    windows = windows_of(request)
    rows = firing(request)
    raw_until, why, until_ts = windows.quiet_until()
    return {
        "rows": rows,
        "windows_rows": windows.rows(),
        "problems": list(windows.errors),
        "quiet": {"until": raw_until, "reason": why, "ends": clock_words(until_ts), "on": bool(until_ts)},
        "live": bool(routers_of(request)),
        "servers": server_options(request),
        "services": sorted(request.app.state.runtime.store.services),
        "days": [(d, DAY_LABELS[d]) for d in DAY_NAMES],
        "snooze_choices": SNOOZE_CHOICES,
        "file": str(windows.path),
    }


def alerts_partial(request: Request):
    ctx = {"rows": firing(request), "live": bool(routers_of(request)), "snooze_choices": SNOOZE_CHOICES}
    return partial(request, "partials/alert_card.html", ctx)


def windows_partial(request: Request):
    ctx = page_ctx(request)
    keys = ("windows_rows", "servers", "services", "problems", "quiet", "days", "file")
    return partial(request, "partials/window_row.html", {k: ctx[k] for k in keys})


@router.get("/alerts")
async def alerts_page(request: Request):
    return render(request, "alerts.html", page_ctx(request))


# ----- acting on one alert -----------------------------------------------------------------------------------
async def _form(request: Request) -> tuple[Any, str]:
    form = await request.form()
    service = str(form.get("service") or "").strip()
    fingerprint = str(form.get("fingerprint") or "").strip()
    if not fingerprint:
        raise HTTPException(422, "which alert? no fingerprint was sent")
    return router_for(request, service), fingerprint


def _who(request: Request) -> str:
    user = getattr(request.state, "user", None)
    return str(getattr(user, "name", "") or "someone on the web UI")


@router.post("/alerts/ack")
async def alert_ack(request: Request):
    alerts, fingerprint = await _form(request)
    if await alerts.ack(fingerprint, who=_who(request)):
        flash(request, "acked — the pings stop, and the card in Discord says who did it", "success")
    else:
        flash(request, "that alert has already closed", "info")
    return alerts_partial(request) if is_htmx(request) else redirect(request, "/alerts")


@router.post("/alerts/snooze")
async def alert_snooze(request: Request):
    alerts, fingerprint = await _form(request)
    form = await request.form()
    try:
        hours = max(1, min(72, int(str(form.get("hours") or 1))))
    except ValueError:
        hours = 1
    if await alerts.snooze(fingerprint, hours, who=_who(request)):
        flash(request, f"snoozed for {hours}h — it speaks up again after that", "success")
    else:
        flash(request, "that alert has already closed", "info")
    return alerts_partial(request) if is_htmx(request) else redirect(request, "/alerts")


@router.post("/alerts/resolve")
async def alert_resolve(request: Request):
    alerts, fingerprint = await _form(request)
    who = _who(request)
    if await alerts.resolve(fingerprint, note=f"Closed by hand by {who}", by=who):
        flash(request, "closed — the card in Discord went green", "success")
    else:
        flash(request, "that alert has already closed", "info")
    return alerts_partial(request) if is_htmx(request) else redirect(request, "/alerts")


# ----- the quiet times ---------------------------------------------------------------------------------------
@router.post("/alerts/windows")
async def window_add(request: Request):
    form = await request.form()
    windows = windows_of(request)
    once = str(form.get("kind") or "repeat").strip() == "once"
    fields = {
        "reason": str(form.get("reason") or "").strip(),
        "start": str((form.get("start_at") if once else form.get("start")) or "").strip(),
        "end": str((form.get("end_at") if once else form.get("end")) or "").strip(),
        "days": [] if once else form.getlist("days"),
        "servers": [s for s in form.getlist("servers") if s],
        "services": [s for s in form.getlist("services") if s],
        "keys": str(form.get("keys") or "").strip(),
    }
    if not fields["reason"]:
        flash(request, "give the window a reason — it is what the log and the alert card will say", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/alerts")
    try:
        win = windows.add(**fields)
    except ValueError as e:
        flash(request, str(e), "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/alerts")
    flash(request, f"quiet time added: {win.reason} ({win.when()}) — it applies from the next check", "success")
    return windows_partial(request) if is_htmx(request) else redirect(request, "/alerts")


@router.post("/alerts/windows/{wid}/delete")
async def window_delete(request: Request, wid: str):
    windows = windows_of(request)
    if not windows.remove(wid):
        raise HTTPException(404, f"there is no quiet time called {wid}")
    flash(request, "quiet time removed", "info")
    return windows_partial(request) if is_htmx(request) else redirect(request, "/alerts")


@router.post("/alerts/windows/{wid}/toggle")
async def window_toggle(request: Request, wid: str):
    windows = windows_of(request)
    win = windows.get(wid)
    if win is None:
        raise HTTPException(404, f"there is no quiet time called {wid}")
    wanted = not win.enabled
    windows.set_enabled(wid, wanted)
    flash(request, f"{win.reason or wid} is now {'on' if wanted else 'off'}", "success")
    return windows_partial(request) if is_htmx(request) else redirect(request, "/alerts")


@router.post("/alerts/quiet")
async def quiet_everything(request: Request):
    form = await request.form()
    windows = windows_of(request)
    until = str(form.get("until") or "").strip()
    reason = str(form.get("reason") or "").strip()
    if until and not reason:
        reason = "everything is switched off for now"
    try:
        windows.set_quiet_until(until, reason)
    except ValueError as e:
        flash(request, str(e), "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/alerts")
    flash(request, f"everything is quiet until {until}" if until else "the quiet switch is off again", "success")
    return windows_partial(request) if is_htmx(request) else redirect(request, "/alerts")

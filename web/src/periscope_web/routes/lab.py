"""/discord: lab settings, web sign-in settings, and the channel layout (create missing, apply git/op permissions)."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Request
from periscope.layout import apply_git_layout, ensure_layout, git_env_lines, layout_status

from ..app import site_url
from ..render import flash, is_htmx, partial, redirect, render
from . import save

log = logging.getLogger(__name__)
router = APIRouter()
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
ID_RE = re.compile(r"^\d{1,25}$")


async def _layout(request: Request) -> dict:
    st = request.app.state
    channels, roles = await st.guild.channels(), await st.guild.roles()
    github = "github" in st.runtime.store.services
    status = layout_status([c.name for c in channels], [r.name for r in roles], github=github)
    status["available"] = bool(channels) or bool(roles)
    status["can_act"] = st.guild.guild_id() is not None and bool(st.guild.any_token())
    status["connected"] = st.guild.connected_guild() is not None
    return status


async def _ctx(request: Request) -> dict:
    st = request.app.state
    store = st.runtime.store
    channels, roles = await st.guild.channels(), await st.guild.roles()
    # ids as strings so the pickers preselect even when the YAML holds bare integers
    lab = {**store.lab, **{k: str(store.lab.get(k) or "") for k in ("guild_id", "status_channel_id", "alert_channel_id", "alert_role_id")}}
    return {
        "lab": lab, "web": store.web, "channels": channels, "roles": roles, "levels": LOG_LEVELS,
        "admin_ids": [str(x) for x in (store.lab.get("admin_role_ids") or [])],
        "allowed_ids": [str(x) for x in (store.web.get("allowed_role_ids") or [])],
        "redirect_uri": site_url(request) + "/auth/callback", "layout": await _layout(request),
        "presence_tokens": [k for k, p in store.presences.items() if p.get("token")],
    }


@router.get("/discord")
async def lab_page(request: Request):
    return render(request, "lab.html", await _ctx(request))


@router.post("/discord")
async def lab_save(request: Request):
    store = request.app.state.runtime.store
    form = await request.form()
    lab = store.lab
    errors = []
    name = str(form.get("name") or "").strip()
    color = str(form.get("color") or "").strip().lstrip("#")
    guild_id = str(form.get("guild_id") or "").strip()
    if name:
        lab["name"] = name
    if color and not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        errors.append("color must be 6 hex digits")
    elif color:
        lab["color"] = color.upper()
    if guild_id and not ID_RE.match(guild_id):
        errors.append("server id must be a Discord id")
    else:
        lab["guild_id"] = guild_id
    for key in ("status_channel_id", "alert_channel_id", "alert_role_id"):
        v = str(form.get(key) or "").strip()
        if v and not ID_RE.match(v):
            errors.append(f"{key} must be a Discord id")
        else:
            lab[key] = v
    ids = [str(x).strip() for x in form.getlist("admin_role_ids") if str(x).strip()]
    if len(ids) == 1 and "," in ids[0]:
        ids = [x.strip() for x in ids[0].split(",") if x.strip()]
    lab["admin_role_ids"] = ids
    level = str(form.get("log_level") or "INFO").upper()
    lab["log_level"] = level if level in LOG_LEVELS else "INFO"
    interval = str(form.get("status_interval_s") or "").strip()
    if interval and not interval.isdigit():
        errors.append("board refresh must be a whole number of seconds")
    elif interval:
        lab["status_interval_s"] = int(interval)
    if errors:
        for e in errors:
            flash(request, e, "error")
    else:
        save(request)
        flash(request, "lab settings saved — restart to apply", "success")
    if is_htmx(request):
        return partial(request, "partials/lab_form.html", await _ctx(request), status=422 if errors else 200)
    return redirect(request, "/discord")


@router.post("/discord/web")
async def web_save(request: Request):
    store = request.app.state.runtime.store
    form = await request.form()
    web = store.web
    base = str(form.get("base_url") or "").strip().rstrip("/")
    if base and not re.match(r"^https?://", base):
        flash(request, "base URL must start with http:// or https://", "error")
        return partial(request, "partials/web_form.html", await _ctx(request), status=422) if is_htmx(request) else redirect(request, "/discord")
    web["base_url"] = base
    web["oauth_client_id"] = str(form.get("oauth_client_id") or "").strip()
    secret = str(form.get("oauth_client_secret") or "").strip()
    if str(form.get("clear_oauth_client_secret") or "").lower() in ("1", "true", "on"):
        web["oauth_client_secret"] = ""
    elif secret:
        web["oauth_client_secret"] = secret
    ids = [str(x).strip() for x in form.getlist("allowed_role_ids") if str(x).strip()]
    if len(ids) == 1 and "," in ids[0]:
        ids = [x.strip() for x in ids[0].split(",") if x.strip()]
    web["allowed_role_ids"] = ids
    port = str(form.get("port") or "").strip()
    if port.isdigit() and 0 < int(port) < 65536:
        web["port"] = int(port)
    save(request)
    flash(request, "sign-in settings saved — they apply to the next sign-in (port: on restart)", "success")
    if is_htmx(request):
        return partial(request, "partials/web_form.html", await _ctx(request))
    return redirect(request, "/discord")


@router.get("/discord/layout")
async def layout_panel(request: Request):
    return partial(request, "partials/layout.html", {"layout": await _layout(request)})


@router.post("/discord/layout/create")
async def layout_create(request: Request):
    """Create the missing convention roles/categories/channels through a connected presence (or a REST-only login)."""
    st = request.app.state
    github = "github" in st.runtime.store.services
    try:
        async with st.guild.acquire() as (guild, _me):
            rep = await ensure_layout(guild, github=github, say=lambda s: log.info("[layout] %s", s))
    except Exception as e:  # noqa: BLE001
        log.warning("layout create failed: %s", e)
        flash(request, f"could not create the layout: {e}", "error")
        return partial(request, "partials/layout.html", {"layout": await _layout(request), "report": [f"!! {e}"]}, status=200)
    st.guild.invalidate()
    if rep.errors:
        flash(request, f"layout: {len(rep.errors)} error(s)", "warning")
    elif rep.changed:
        flash(request, f"created {len(rep.created_roles)} role(s), {len(rep.created_channels)} channel(s)", "success")
    else:
        flash(request, "layout already complete", "info")
    return partial(request, "partials/layout.html", {"layout": await _layout(request), "report": rep.lines})


@router.post("/discord/layout/git")
async def layout_git(request: Request):
    """Same logic as `periscope layout`: #git-* feeds (humans read-only, @bots post), #op-* mute bots."""
    st = request.app.state
    form = await request.form()
    dry = str(form.get("dry") or "").lower() in ("1", "true", "on")
    try:
        async with st.guild.acquire() as (guild, me):
            res = await apply_git_layout(guild, me_id=me, dry=dry, say=lambda s: log.info("[layout] %s", s))
    except Exception as e:  # noqa: BLE001
        log.warning("git layout failed: %s", e)
        flash(request, f"could not apply permissions: {e}", "error")
        return partial(request, "partials/layout.html", {"layout": await _layout(request), "report": [f"!! {e}"]})
    lines = list(res.lines)
    if not res.aborted:
        lines += ["", "# env hints for the github service:", *git_env_lines(res)]
    if res.errors:
        flash(request, f"permissions: {len(res.errors)} error(s)", "warning")
    else:
        flash(request, ("dry run — " if dry else "") + f"{len(res.channel_ids)} channel(s) processed", "success")
    return partial(request, "partials/layout.html", {"layout": await _layout(request), "report": lines})

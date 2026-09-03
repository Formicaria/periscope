"""/setup: the first-run flow — token → invite → pick server → channel layout → add services."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from periscope.layout import ensure_layout, layout_status

from ..discordapi import DiscordError, invite_url
from ..render import flash, is_htmx, partial, redirect, render, toasts
from . import save

log = logging.getLogger(__name__)
router = APIRouter()


async def _ctx(request: Request) -> dict:
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    tokens = {k: p for k, p in store.presences.items() if p.get("token")}
    first = next(iter(tokens), None)
    app_id = st.app_ids.get(first) if first else None
    guild_id = str(store.lab.get("guild_id") or "").strip()
    guilds: list[dict] = []
    guild_error = ""
    if first and not guild_id:
        try:
            guilds = [{"id": str(g.get("id")), "name": str(g.get("name")), "owner": bool(g.get("owner"))}
                      for g in await st.discord.guilds(str(tokens[first]["token"]))]
        except DiscordError as e:
            guild_error = f"could not list the bot's servers ({e.status or 'unreachable'})"
    layout = None
    if guild_id and tokens:
        channels, roles = await st.guild.channels(), await st.guild.roles()
        layout = layout_status([c.name for c in channels], [r.name for r in roles])
        layout["available"] = bool(channels) or bool(roles)
    enabled = store.enabled_services()
    steps = {"token": bool(tokens), "guild": bool(guild_id), "layout": bool(layout and not layout["missing_channels"]),
             "services": bool(enabled)}
    return {
        "steps": steps, "presence": first, "app_id": app_id, "invite": invite_url(app_id) if app_id else None,
        "guilds": guilds, "guild_error": guild_error, "guild_id": guild_id, "layout": layout,
        "specs": sorted(runtime.specs.values(), key=lambda s: (s.group, s.name)), "enabled": enabled,
        "current": next((k for k, v in steps.items() if not v), "done"),
    }


@router.get("/setup")
async def setup_page(request: Request):
    return render(request, "setup.html", await _ctx(request))


@router.post("/setup/token")
async def setup_token(request: Request):
    st = request.app.state
    store = st.runtime.store
    form = await request.form()
    token = str(form.get("token") or "").strip()
    name = str(form.get("presence") or "default").strip().lower() or "default"
    if not token:
        flash(request, "paste the bot token first", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/setup")
    try:
        me = await st.discord.me(token)
    except DiscordError as e:
        flash(request, f"Discord rejected that token ({e.status or 'unreachable'})", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/setup")
    p = store.presences.setdefault(name, {"token": "", "label": name if name != "default" else "periscope"})
    p["token"] = token
    if name != "default" or not p.get("label"):
        p["label"] = p.get("label") or str(me.get("username") or name)
    st.app_ids[name] = str(me.get("id"))
    save(request)
    flash(request, f"token works — signed in as {me.get('username')}", "success")
    if is_htmx(request):
        return partial(request, "partials/setup_steps.html", await _ctx(request))
    return redirect(request, "/setup")


@router.post("/setup/guild")
async def setup_guild(request: Request):
    st = request.app.state
    store = st.runtime.store
    form = await request.form()
    gid = str(form.get("guild_id") or "").strip()
    gname = str(form.get("guild_name") or "").strip()
    if not gid.isdigit():
        flash(request, "pick a server (or paste its id)", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/setup")
    store.lab["guild_id"] = gid
    if gname and str(store.lab.get("name") or "lab") in ("lab", "my-lab", ""):
        store.lab["name"] = gname
    save(request)
    flash(request, f"server saved{f' — {gname}' if gname else ''}", "success")
    if is_htmx(request):
        return partial(request, "partials/setup_steps.html", await _ctx(request))
    return redirect(request, "/setup")


@router.post("/setup/layout")
async def setup_layout(request: Request):
    st = request.app.state
    try:
        async with st.guild.acquire() as (guild, _me):
            rep = await ensure_layout(guild, say=lambda s: log.info("[layout] %s", s))
    except Exception as e:  # noqa: BLE001
        log.warning("setup layout failed: %s", e)
        flash(request, f"could not create the layout: {e}", "error")
        return partial(request, "partials/setup_steps.html", await _ctx(request)) if is_htmx(request) else redirect(request, "/setup")
    st.guild.invalidate()
    store = st.runtime.store
    # point the lab defaults at the convention channels/roles when they are still empty
    channels, roles = await st.guild.channels(), await st.guild.roles()
    by_name = {c.name.lower(): c.id for c in channels}
    role_by = {r.name.lower(): r.id for r in roles}
    changed = False
    for key, chan in (("status_channel_id", "lab-status"), ("alert_channel_id", "lab-alerts")):
        if not store.lab.get(key) and by_name.get(chan):
            store.lab[key] = by_name[chan]
            changed = True
    if not store.lab.get("alert_role_id") and role_by.get("lab-oncall"):
        store.lab["alert_role_id"] = role_by["lab-oncall"]
        changed = True
    if not store.lab.get("admin_role_ids") and role_by.get("lab-admin"):
        store.lab["admin_role_ids"] = [role_by["lab-admin"]]
        changed = True
    if changed:
        save(request)
    if rep.errors:
        flash(request, f"layout: {len(rep.errors)} error(s) — {rep.errors[0]}", "warning")
    else:
        flash(request, "channel layout ready" + (" — lab defaults filled in" if changed else ""), "success")
    ctx = await _ctx(request)
    ctx["report"] = rep.lines
    if is_htmx(request):
        return partial(request, "partials/setup_steps.html", ctx)
    return redirect(request, "/setup")

"""/presences ("Bots" in the UI): the Discord identities services post as — tokens (validated against Discord,
never rendered back), labels, invite links, which services use which bot, and why one is offline.

The page is laid out one section per Discord server: a bot belongs to a server when a service that posts in that
server posts as it, so a bot that serves two servers is listed in both. Bots no service uses yet get a last
section of their own, so a freshly added one is still visible."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Request

from ..discordapi import DiscordError, invite_url
from ..render import flash, is_htmx, partial, redirect, render, toasts
from . import save
from .servers import server_label

log = logging.getLogger(__name__)
router = APIRouter()
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _fallback_for(store, name: str) -> str | None:
    """Where a removed bot's services go: the shared default, else another bot that has a token. None when
    this is the only working bot — removing it would leave every service without an identity."""
    for cand in ["default", *store.presences]:
        if cand != name and cand in store.presences and store.presences[cand].get("token"):
            return cand
    if not store.presences.get(name, {}).get("token"):
        return next((c for c in store.presences if c != name), None)   # an empty row can always go
    return None


def _rows(request: Request) -> list[dict]:
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    status = runtime.status()
    rows = []
    for name, p in store.presences.items():
        live = status.get("presences", {}).get(name) or {}
        users = [s for s in store.services if store.presence_for(s) == name]
        fallback = _fallback_for(store, name)
        rows.append({"name": name, "label": p.get("label") or name, "has_token": bool(p.get("token")),
                     "services": users, "enabled": [s for s in users if store.services[s].get("enabled")],
                     "connected": bool(live.get("connected")), "user": live.get("user"), "live": bool(live),
                     "error": live.get("error"), "missing_guilds": live.get("missing_guilds") or {},
                     "app_id": st.app_ids.get(name) or live.get("app_id"), "invite": live.get("invite") or "",
                     "removable": fallback is not None, "fallback": fallback})
    return rows


def _in_server(row: dict, guild_id: str) -> bool | None:
    """Is this bot in that Discord server? False when the running bot names that server id as one it is missing,
    True when it is running and does not, None while nothing is known — no token, not started, or no id set."""
    if not guild_id:
        return None
    if guild_id in row["missing_guilds"]:
        return False
    return True if row["connected"] else None


def _scoped(store, row: dict, slug: str, guild_id: str, here: list[str], elsewhere: list[str]) -> dict:
    """One bot's row inside one section: only the services that post in that section's server, whether the bot is
    in that Discord server, and the other servers it posts in. `slug` keeps the row ids apart across sections."""
    return {**row, "id": f"{slug}-{row['name']}", "services": here, "also_in": elsewhere,
            "enabled": [s for s in here if store.services[s].get("enabled")],
            "in_server": _in_server(row, guild_id), "needed_by": row["missing_guilds"].get(guild_id, "")}


async def _groups(request: Request) -> list[dict]:
    """The page in sections: one per configured server (the default first) with the bots that post in it, then
    one for the bots no service uses yet."""
    st = request.app.state
    store = st.runtime.store
    names = await st.guild.names()
    labels = {key: server_label(key, srv, names) for key, srv in store.servers.items()}
    where = {name: store.server_for(name) for name in store.services}
    rows = _rows(request)
    default = store.default_server()
    groups, used = [], set()
    for key in [default, *(k for k in store.servers if k != default)]:
        guild_id = str(store.servers[key].get("guild_id") or "").strip()
        here = []
        for row in rows:
            mine = [s for s in row["services"] if where[s] == key]
            if not mine:
                continue
            used.add(row["name"])
            others = [labels[k] for k in dict.fromkeys(where[s] for s in row["services"]) if k != key]
            here.append(_scoped(store, row, key, guild_id, mine, others))
        groups.append({"slug": key, "label": labels[key], "guild_id": guild_id, "rows": here,
                       "is_default": key == default})
    spare = [_scoped(store, row, "unused", "", [], []) for row in rows if row["name"] not in used]
    if spare:
        groups.append({"slug": "unused", "unused": True, "rows": spare})
    return groups


async def _list(request: Request):
    """What every mutating endpoint answers with: the whole grouped list. One bot can sit in several sections
    now, so swapping a single row would leave its other rows showing the old values."""
    return partial(request, "partials/presence_groups.html", {"groups": await _groups(request)})


@router.get("/presences")
async def presences_page(request: Request):
    return render(request, "presences.html", {"groups": await _groups(request)})


@router.post("/presences")
async def presence_add(request: Request):
    store = request.app.state.runtime.store
    form = await request.form()
    name = str(form.get("name") or "").strip().lower()
    label = str(form.get("label") or "").strip() or name
    if not NAME_RE.match(name):
        flash(request, "bot name: lowercase letters, digits, - or _ (max 32)", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/presences")
    if name in store.presences:
        flash(request, f"a bot named {name} already exists", "error")
        return toasts(request, 409) if is_htmx(request) else redirect(request, "/presences")
    store.presences[name] = {"token": "", "label": label}
    save(request)
    flash(request, f"bot {name} added — now paste its token in the row", "success")
    if is_htmx(request):
        return await _list(request)
    return redirect(request, "/presences")


@router.post("/presences/{name}/token")
async def presence_token(request: Request, name: str):
    st = request.app.state
    store = st.runtime.store
    if name not in store.presences:
        raise HTTPException(404, f"unknown bot {name}")
    form = await request.form()
    token = str(form.get("token") or "").strip()
    if not token:
        flash(request, "paste a bot token first", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/presences")
    try:
        me = await st.discord.me(token)
    except DiscordError as e:
        flash(request, f"Discord rejected that token ({e.status or 'unreachable'}) — copy it again from the developer portal (Bot → Reset Token)", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/presences")
    store.presences[name]["token"] = token
    if not store.presences[name].get("label"):
        store.presences[name]["label"] = str(me.get("username") or name)
    st.app_ids[name] = str(me.get("id"))
    save(request)
    flash(request, f"token works — this is {me.get('username')} (app id {me.get('id')}); invite it to your server if you have not yet, then restart to apply", "success")
    if is_htmx(request):
        return await _list(request)
    return redirect(request, "/presences")


@router.post("/presences/{name}/label")
async def presence_label(request: Request, name: str):
    store = request.app.state.runtime.store
    if name not in store.presences:
        raise HTTPException(404, f"unknown bot {name}")
    form = await request.form()
    label = str(form.get("label") or "").strip()
    new_name = str(form.get("new_name") or "").strip().lower()
    if label:
        store.presences[name]["label"] = label
    if new_name and new_name != name:
        if not NAME_RE.match(new_name) or new_name in store.presences:
            flash(request, f"cannot rename to {new_name!r}", "error")
            return toasts(request, 422) if is_htmx(request) else redirect(request, "/presences")
        store.presences[new_name] = store.presences.pop(name)
        for svc in store.services.values():
            if svc.get("presence") == name:
                svc["presence"] = new_name
        request.app.state.app_ids.pop(name, None)
        name = new_name
    save(request)
    flash(request, "bot updated — restart to apply", "success")
    if is_htmx(request):
        return await _list(request)
    return redirect(request, "/presences")


@router.post("/presences/{name}/delete")
async def presence_delete(request: Request, name: str):
    store = request.app.state.runtime.store
    if name not in store.presences:
        raise HTTPException(404, f"unknown bot {name}")
    fallback = _fallback_for(store, name)
    if fallback is None:
        flash(request, "this is the only bot with a token — it cannot be removed; add another bot first", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/presences")
    store.presences.pop(name)
    moved = [s for s, svc in store.services.items() if svc.get("presence") == name]
    for s in moved:
        store.services[s]["presence"] = fallback
    request.app.state.app_ids.pop(name, None)
    save(request)
    flash(request, f"bot {name} removed" + (f" — {', '.join(moved)} now post as {fallback}" if moved else ""), "info")
    if is_htmx(request):
        return await _list(request)
    return redirect(request, "/presences")


@router.get("/presences/{name}/invite")
async def presence_invite(request: Request, name: str):
    """The invite link needs the application id: the connected presence knows it, else GET /users/@me."""
    st = request.app.state
    store = st.runtime.store
    p = store.presences.get(name)
    if p is None:
        raise HTTPException(404, f"unknown bot {name}")
    if not p.get("token"):
        return partial(request, "partials/invite.html", {"name": name, "url": None, "why": "no token"})
    app_id = st.app_ids.get(name)
    if not app_id:
        live = st.runtime.presences.get(name)
        live_id = getattr(live, "application_id", None) or getattr(getattr(live, "user", None), "id", None)
        if live_id:
            app_id = str(live_id)
        else:
            try:
                me = await st.discord.me(str(p["token"]))
                app_id = str(me.get("id") or "")
            except DiscordError as e:
                return partial(request, "partials/invite.html", {"name": name, "url": None, "why": f"token check failed ({e.status})"})
        if app_id:
            st.app_ids[name] = app_id
    return partial(request, "partials/invite.html", {"name": name, "url": invite_url(app_id) if app_id else None, "app_id": app_id})

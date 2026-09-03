"""/discord: the Discord servers periscope posts in (one card each), the settings that are not per-server,
web sign-in settings, and the channel layout (create missing, apply git/op permissions)."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Request
from periscope.layout import apply_git_layout, ensure_layout, git_env_lines, layout_status

from ..app import site_url
from ..render import flash, is_htmx, partial, redirect, render
from . import save

log = logging.getLogger(__name__)
router = APIRouter()
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
ID_RE = re.compile(r"^\d{1,25}$")
CHANNEL_KEYS = ("status_channel_id", "alert_channel_id")


def label_for(key: str, srv: dict) -> str:
    """A server's display name — the wording embed footers carry — or its key when it has none."""
    return str(srv.get("name") or "").strip() or key


def server_label(key: str, srv: dict, names: dict[str, str] | None = None) -> str:
    """What a server is called everywhere but its own card: the display name, and the real Discord name after it
    when that is known and different — "ztechnus.com (THE LAB)". Two servers may carry the same display name, so
    this is what keeps them apart. `names` is {Discord server id: real name}, fetched once per request with
    `st.guild.names()`; this looks nothing up itself."""
    label = label_for(key, srv)
    real = str((names or {}).get(str(srv.get("guild_id") or "").strip()) or "").strip()
    return f"{label} ({real})" if real and real != label else label


def server_options(store, names: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """(key, label) for every configured server — the "in server" pickers elsewhere use this too."""
    return [(k, server_label(k, v, names)) for k, v in store.servers.items()]


def _slug(text: str, taken) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", str(text).strip().lower()).strip("-")[:32] or "server"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}-{n}", n + 1
    return key


async def _card(request: Request, key: str) -> dict:
    """One server's card: its stored values as strings, plus the pickers and the real Discord name of that very
    server — the card heads with the name Discord knows it by, not with the display name."""
    st = request.app.state
    store = st.runtime.store
    srv = store.servers[key]
    gid = st.guild.guild_id(key)
    channels, roles = await st.guild.channels_for(gid), await st.guild.roles_for(gid)
    real_name = await st.guild.guild_name(gid)
    label = label_for(key, srv)
    full_label = server_label(key, srv, {str(gid): real_name} if real_name else {})
    listed = bool(channels or roles)
    return {
        "key": key, "label": label, "full_label": full_label, "name": str(srv.get("name") or ""),
        "color": str(srv.get("color") or ""), "real_name": real_name,
        "can_rename": full_label != label,      # Discord's name is known and is not the one the footers carry
        "guild_id": str(srv.get("guild_id") or ""), "is_default": key == store.default_server(),
        "channels": channels, "roles": roles, "listed": listed,
        "admin_ids": [str(x) for x in (srv.get("admin_role_ids") or [])],
        **{k: str(srv.get(k) or "") for k in (*CHANNEL_KEYS, "alert_role_id")},
        "invite": "" if listed or gid is None else st.guild.invite_for(gid),
        "unknown": gid is not None and not listed,      # a server id is set but the bot cannot see it
        "no_token": not st.guild.any_token(),           # …because no bot has a token yet, rather than a missing invite
        "services": [n for n in store.services if store.server_for(n) == key],
        "can_remove": len(store.servers) > 1,           # the last server stays: everything posts somewhere
    }


async def _cards(request: Request) -> list[dict]:
    return [await _card(request, key) for key in list(request.app.state.runtime.store.servers)]


async def _list_ctx(request: Request) -> dict:
    st = request.app.state
    store = st.runtime.store
    used = set(store.guild_ids().values())
    known = [g for g in await st.guild.available_guilds() if g["id"] not in used]   # only what is not here yet
    return {"servers": await _cards(request), "known": known}


async def _layout(request: Request, key: str | None = None) -> dict:
    st = request.app.state
    store = st.runtime.store
    key = key if key in store.servers else store.default_server()
    gid = st.guild.guild_id(key)
    channels, roles = await st.guild.channels_for(gid), await st.guild.roles_for(gid)
    github = "github" in store.services
    status = layout_status([c.name for c in channels], [r.name for r in roles], github=github)
    status["available"] = bool(channels) or bool(roles)
    status["can_act"] = gid is not None and bool(st.guild.any_token())
    status["connected"] = st.guild.guild_for(gid) is not None
    names = await st.guild.names()
    status["server"] = key
    status["server_label"] = server_label(key, store.servers[key], names)
    status["servers"] = server_options(store, names)
    return status


async def _ctx(request: Request, layout_key: str | None = None) -> dict:
    st = request.app.state
    store = st.runtime.store
    return {
        **await _list_ctx(request), "web": store.web, "globals": store.globals, "levels": LOG_LEVELS,
        "roles": await st.guild.roles(),                      # the sign-in gate reads the default server
        "admin_ids": [str(x) for x in (store.server().get("admin_role_ids") or [])],
        "allowed_ids": [str(x) for x in (store.web.get("allowed_role_ids") or [])],
        "redirect_uri": site_url(request) + "/auth/callback", "layout": await _layout(request, layout_key),
    }


@router.get("/discord")
async def servers_page(request: Request):
    return render(request, "servers.html", await _ctx(request))


@router.post("/discord/servers/{key}")
async def server_save(request: Request, key: str):
    """One server card: display name, accent colour, server id and the channels/roles it posts in."""
    st = request.app.state
    store = st.runtime.store
    if key not in store.servers:
        raise HTTPException(404, f"unknown server {key}")
    srv = store.servers[key]
    form = await request.form()
    errors = []
    name = str(form.get("name") or "").strip()
    color = str(form.get("color") or "").strip().lstrip("#")
    guild_id = str(form.get("guild_id") or "").strip()
    if name:
        srv["name"] = name
    if color and not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        errors.append("color must be 6 hex digits")
    elif color:
        srv["color"] = color.upper()
    if guild_id and not ID_RE.match(guild_id):
        errors.append("server id must be a Discord id")
    else:
        srv["guild_id"] = guild_id
    for field in (*CHANNEL_KEYS, "alert_role_id"):
        v = str(form.get(field) or "").strip()
        if v and not ID_RE.match(v):
            errors.append(f"{field} must be a Discord id")
        else:
            srv[field] = v
    ids = [str(x).strip() for x in form.getlist("admin_role_ids") if str(x).strip()]
    if len(ids) == 1 and "," in ids[0]:
        ids = [x.strip() for x in ids[0].split(",") if x.strip()]
    srv["admin_role_ids"] = ids
    if errors:
        for e in errors:
            flash(request, e, "error")
    else:
        save(request)
        flash(request, f"{server_label(key, srv, await st.guild.names())} saved — restart to apply", "success")
    if is_htmx(request):
        return partial(request, "partials/server_form.html", {"server": await _card(request, key)},
                       status=422 if errors else 200)
    return redirect(request, "/discord")


@router.post("/discord/servers")
async def server_add(request: Request):
    """Add a server: a name and an id, or one of the servers a connected bot is already in."""
    st = request.app.state
    store = st.runtime.store
    form = await request.form()
    name = str(form.get("name") or "").strip()
    guild_id = str(form.get("guild_id") or "").strip() or str(form.get("pick") or "").strip()
    problem = ""
    if not guild_id and not name:
        problem = "give the server a name, or pick one the bot is in"
    elif guild_id and not ID_RE.match(guild_id):
        problem = "server id must be a Discord id"
    elif guild_id and guild_id in store.guild_ids().values():
        problem = "that Discord server is already on this page"
    if problem:
        flash(request, problem, "error")
    else:
        if not name and guild_id:
            known = {g["id"]: g["name"] for g in await st.guild.available_guilds()}
            name = known.get(guild_id, "")
        key = _slug(name or guild_id, store.servers)
        srv = store.add_server(key, name or key)
        srv["guild_id"] = guild_id
        save(request)
        label = server_label(key, srv, await st.guild.names())
        flash(request, f"{label} added — set its channels below, then restart to apply", "success")
    if is_htmx(request):
        return partial(request, "partials/server_list.html", await _list_ctx(request), status=422 if problem else 200)
    return redirect(request, "/discord")


@router.post("/discord/servers/{key}/name")
async def server_use_discord_name(request: Request, key: str):
    """"Use the Discord name": copy the server's own name in Discord into the display name embed footers carry.
    One click — the card comes back with the field already filled in and saved."""
    st = request.app.state
    store = st.runtime.store
    if key not in store.servers:
        raise HTTPException(404, f"unknown server {key}")
    srv = store.servers[key]
    real = await st.guild.guild_name(st.guild.guild_id(key))
    if not real:
        flash(request, "this server's Discord name is not known — the bot has to be in it first", "error")
        if is_htmx(request):
            return partial(request, "partials/server_form.html", {"server": await _card(request, key)}, status=422)
        return redirect(request, "/discord")
    srv["name"] = real
    save(request)
    flash(request, f"embed footers now say {real} — restart to apply", "success")
    if is_htmx(request):
        return partial(request, "partials/server_form.html", {"server": await _card(request, key)})
    return redirect(request, "/discord")


@router.post("/discord/servers/{key}/delete")
async def server_delete(request: Request, key: str):
    st = request.app.state
    store = st.runtime.store
    if key not in store.servers:
        raise HTTPException(404, f"unknown server {key}")
    names = await st.guild.names()
    label = server_label(key, store.servers[key], names)
    if len(store.servers) <= 1:
        flash(request, "this is the only server — add another one before removing it", "error")
        return partial(request, "partials/server_list.html", await _list_ctx(request), status=422) if is_htmx(request) \
            else redirect(request, "/discord")
    moved = store.remove_server(key)
    fallback = server_label(store.default_server(), store.server(), names)
    save(request)
    flash(request, f"{label} removed" + (f" — {', '.join(moved)} now post in {fallback}" if moved else ""), "info")
    if is_htmx(request):
        return partial(request, "partials/server_list.html", await _list_ctx(request))
    return redirect(request, "/discord")


@router.post("/discord/servers/{key}/default")
async def server_default(request: Request, key: str):
    """Make one server the default: new services post there, and it is the one the sign-in gate checks.

    The store reads the default off the order of the servers block, and loading a config always puts the `main`
    entry first — so the two blocks swap places instead of being reordered. Every service is first pinned to the
    server it posts in today, so only *new* ones follow the change."""
    st = request.app.state
    store = st.runtime.store
    if key not in store.servers:
        raise HTTPException(404, f"unknown server {key}")
    srv = store.servers[key]
    if not str(srv.get("guild_id") or "").strip():
        flash(request, "give this server its Discord server id first", "error")
        return partial(request, "partials/server_list.html", await _list_ctx(request), status=422) if is_htmx(request) \
            else redirect(request, "/discord")
    label = server_label(key, srv, await st.guild.names())
    current = store.default_server()
    if current != key:
        pinned = {name: store.server_for(name) for name in store.services}
        store.servers[current], store.servers[key] = store.servers[key], store.servers[current]
        swapped = {current: key, key: current}
        for name, where in pinned.items():
            store.services[name]["server"] = swapped.get(where, where)
    save(request)
    flash(request, f"{label} is now the default — restart to apply", "success")
    if is_htmx(request):
        return partial(request, "partials/server_list.html", await _list_ctx(request))
    return redirect(request, "/discord")


@router.post("/discord/globals")
async def globals_save(request: Request):
    """The settings that are not per-server: how much is logged, how often the status board is redrawn."""
    store = request.app.state.runtime.store
    form = await request.form()
    errors = []
    level = str(form.get("log_level") or "INFO").upper()
    store.globals["log_level"] = level if level in LOG_LEVELS else "INFO"
    interval = str(form.get("status_interval_s") or "").strip()
    if interval and not interval.isdigit():
        errors.append("board refresh must be a whole number of seconds")
    elif interval:
        store.globals["status_interval_s"] = int(interval)
    if errors:
        for e in errors:
            flash(request, e, "error")
    else:
        save(request)
        flash(request, "settings saved — restart to apply", "success")
    if is_htmx(request):
        return partial(request, "partials/globals_form.html", await _ctx(request), status=422 if errors else 200)
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


# ----- channel layout ------------------------------------------------------------------------------------
def _layout_key(request: Request, form=None) -> str:
    raw = str((form or {}).get("server") or request.query_params.get("server") or "")
    return raw if raw in request.app.state.runtime.store.servers else request.app.state.runtime.store.default_server()


@router.get("/discord/layout")
async def layout_panel(request: Request):
    return partial(request, "partials/layout.html", {"layout": await _layout(request, _layout_key(request))})


@router.post("/discord/layout/create")
async def layout_create(request: Request):
    """Create the missing convention roles/categories/channels through a connected presence (or a REST-only login)."""
    st = request.app.state
    key = _layout_key(request, await request.form())
    github = "github" in st.runtime.store.services
    try:
        async with st.guild.acquire(key) as (guild, _me):
            rep = await ensure_layout(guild, github=github, say=lambda s: log.info("[layout] %s", s))
    except Exception as e:  # noqa: BLE001
        log.warning("layout create failed: %s", e)
        flash(request, f"could not create the layout: {e}", "error")
        return partial(request, "partials/layout.html", {"layout": await _layout(request, key), "report": [f"!! {e}"]}, status=200)
    st.guild.invalidate()
    if rep.errors:
        flash(request, f"layout: {len(rep.errors)} error(s)", "warning")
    elif rep.changed:
        flash(request, f"created {len(rep.created_roles)} role(s), {len(rep.created_channels)} channel(s)", "success")
    else:
        flash(request, "layout already complete", "info")
    return partial(request, "partials/layout.html", {"layout": await _layout(request, key), "report": rep.lines})


@router.post("/discord/layout/git")
async def layout_git(request: Request):
    """Same logic as `periscope layout`: #git-* feeds (humans read-only, @bots post), #op-* mute bots."""
    st = request.app.state
    form = await request.form()
    key = _layout_key(request, form)
    dry = str(form.get("dry") or "").lower() in ("1", "true", "on")
    try:
        async with st.guild.acquire(key) as (guild, me):
            res = await apply_git_layout(guild, me_id=me, dry=dry, say=lambda s: log.info("[layout] %s", s))
    except Exception as e:  # noqa: BLE001
        log.warning("git layout failed: %s", e)
        flash(request, f"could not apply permissions: {e}", "error")
        return partial(request, "partials/layout.html", {"layout": await _layout(request, key), "report": [f"!! {e}"]})
    lines = list(res.lines)
    if not res.aborted:
        lines += ["", "# env hints for the github service:", *git_env_lines(res)]
    if res.errors:
        flash(request, f"permissions: {len(res.errors)} error(s)", "warning")
    else:
        flash(request, ("dry run — " if dry else "") + f"{len(res.channel_ids)} channel(s) processed", "success")
    return partial(request, "partials/layout.html", {"layout": await _layout(request, key), "report": lines})

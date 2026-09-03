"""/routing: the GitHub repo→channel map (+ feed/CI catch-alls, mirror flag) and per-service alert routing."""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter, HTTPException, Request

from ..render import flash, is_htmx, partial, redirect, render
from . import save
from .servers import server_label

log = logging.getLogger(__name__)
router = APIRouter()
ID_RE = re.compile(r"^\d{1,25}$")
ROUTE_KEYS = ("ALERT_CHANNEL_ID", "STATUS_CHANNEL_ID", "ALERT_ROLE_ID")


def parse_map(raw: str) -> list[tuple[str, str]]:
    """'Anthill=123,micromound=123' → [(pattern, channel id)], tolerant of stray spaces/blank items."""
    out: list[tuple[str, str]] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item or "=" not in item:
            continue
        repo, _, cid = item.partition("=")
        if repo.strip() and cid.strip():
            out.append((repo.strip(), cid.strip()))
    return out


def format_map(rows: list[tuple[str, str]]) -> str:
    return ",".join(f"{r}={c}" for r, c in rows)


async def _ctx(request: Request) -> dict:
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    # the repo map belongs to the github service, so its channel picker shows that service's server
    channels = await st.guild.channels(store.server_for("github"))
    gh = (store.services.get("github") or {}).get("env") or {}
    rows = parse_map(str(gh.get("GITHUB_REPO_CHANNEL_MAP") or ""))
    return {
        "channels": channels, "rows": rows,
        "feed": str(gh.get("GITHUB_FEED_CHANNEL_ID") or ""), "ci": str(gh.get("GITHUB_CI_CHANNEL_ID") or ""),
        "mirror": str(gh.get("GITHUB_MIRROR_TO_FEED") or "false").lower() in ("1", "true", "yes", "on"),
        "github_installed": "github" in runtime.specs, "alerts": await _alert_rows(request),
        "many_servers": len(store.servers) > 1,
    }


def _default_labels(store, key: str, channels, roles) -> dict[str, str]:
    """Human labels for one server's own defaults ('#lab-alerts', '@lab-oncall', or the bare id)."""
    srv = store.server(key)
    out = {}
    for env_key, field in (("ALERT_CHANNEL_ID", "alert_channel_id"), ("STATUS_CHANNEL_ID", "status_channel_id"),
                           ("ALERT_ROLE_ID", "alert_role_id")):
        v = str(srv.get(field) or "")
        if not v:
            out[env_key] = ""
            continue
        pool = roles if env_key.endswith("_ROLE_ID") else channels
        hit = next((x for x in pool if x.id == v), None)
        out[env_key] = hit.label if hit else v
    return out


async def _alert_rows(request: Request) -> list[dict]:
    """One row per service, with the pickers of the server that service posts in."""
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    names = list(runtime.specs) + [n for n in store.services if n not in runtime.specs]
    server_names = await st.guild.names()           # one lookup for the table: the badges name the Discord server
    per_server: dict[str, tuple[list, list, dict[str, str]]] = {}
    out = []
    for name in names:
        spec = runtime.specs.get(name)
        env = (store.services.get(name) or {}).get("env") or {}
        key = store.server_for(name)
        if key not in per_server:
            channels, roles = await st.guild.channels(key), await st.guild.roles(key)
            per_server[key] = (channels, roles, _default_labels(store, key, channels, roles))
        channels, roles, defaults = per_server[key]
        srv = store.server(key)
        out.append({"name": name, "title": spec.title if spec else name, "group": spec.group if spec else "other",
                    "enabled": bool((store.services.get(name) or {}).get("enabled")),
                    "server": key, "server_label": server_label(key, srv, server_names),
                    "channels": channels, "roles": roles, "defaults": defaults,
                    "values": {k: str(env.get(k) or "") for k in ROUTE_KEYS}})
    return out


@router.get("/routing")
async def routing_page(request: Request):
    return render(request, "routing.html", await _ctx(request))


@router.post("/routing")
async def routing_save(request: Request):
    store = request.app.state.runtime.store
    form = await request.form()
    repos = [str(x).strip() for x in form.getlist("repo")]
    chans = [str(x).strip() for x in form.getlist("channel")]
    rows, errors = [], []
    for repo, cid in zip(repos, chans, strict=False):
        if not repo and not cid:
            continue
        if not repo or not cid:
            errors.append(f"row {repo or '?'}: both a repo pattern and a channel are needed")
            continue
        if not ID_RE.match(cid):
            errors.append(f"{repo}: channel must be a Discord id")
            continue
        if "," in repo or "=" in repo:
            errors.append(f"{repo}: pattern may not contain ',' or '='")
            continue
        rows.append((repo, cid))
    values = {"GITHUB_REPO_CHANNEL_MAP": format_map(rows)}
    for key in ("GITHUB_FEED_CHANNEL_ID", "GITHUB_CI_CHANNEL_ID"):
        v = str(form.get(key) or "").strip()
        if v and not ID_RE.match(v):
            errors.append(f"{key} must be a Discord id")
        values[key] = v
    values["GITHUB_MIRROR_TO_FEED"] = "true" if str(form.get("GITHUB_MIRROR_TO_FEED") or "").lower() in ("1", "true", "on") else "false"
    if errors:
        for e in errors:
            flash(request, e, "error")
        return partial(request, "partials/repo_map.html", await _ctx(request), status=422) if is_htmx(request) else redirect(request, "/routing")
    store.update_service_env("github", values)
    save(request)
    flash(request, f"routing saved ({len(rows)} rule(s)) — restart to apply", "success")
    if is_htmx(request):
        return partial(request, "partials/repo_map.html", await _ctx(request))
    return redirect(request, "/routing")


@router.get("/routing/row")
async def routing_row(request: Request):
    st = request.app.state
    channels = await st.guild.channels(st.runtime.store.server_for("github"))
    return partial(request, "partials/repo_row.html", {"repo": "", "cid": "", "channels": channels})


@router.post("/routing/alerts/{name}")
async def alert_route_save(request: Request, name: str):
    st = request.app.state
    store = st.runtime.store
    if name not in st.runtime.specs and name not in store.services:
        raise HTTPException(404, f"unknown service {name}")
    form = await request.form()
    values = {}
    errors = []
    for key in ROUTE_KEYS:
        v = str(form.get(key) or "").strip()
        if v and not ID_RE.match(v):
            errors.append(f"{key} must be a Discord id")
        values[key] = v
    if errors:
        for e in errors:
            flash(request, e, "error")
    else:
        store.update_service_env(name, values)
        save(request)
        flash(request, f"{name}: alert routing saved — restart to apply", "success")
    ctx = await _ctx(request)
    row = next(r for r in ctx["alerts"] if r["name"] == name)
    return partial(request, "partials/alert_row.html", {"row": row, "many_servers": ctx["many_servers"]},
                   status=422 if errors else 200)

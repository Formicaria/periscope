"""/messages: every post a bot makes, shown the way Discord draws it and editable in words.

The gallery lists every registered kind, grouped by service (alerts registered under the pseudo-service `core` head
"Every service") and inside a service by the kind's own group. Being in the registry *is* being installed: a kind
is only there because the package that registers it was imported by service discovery. A service name need not be
a service — the media stack registers `media.*` for the eight apps that post the same cards through one hub — so
headings fall back from a spec's title to the label of the group its services sit in.

The editor puts two tabs over one template: Simple builds the template dict from plain fields, Code edits the same
dict as JSON — a hidden `mode` says which one the server should take. Saves go through the runtime's MessageStore,
which writes config/messages.yaml itself and is re-read on the next post, so nothing here asks for a restart.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from periscope.messages import (
    LIMITS,
    MAX_FIELDS,
    REGISTRY,
    SEVERITY_COLORS,
    STANDARD_VARIABLES,
    MessageKind,
    embed_to_dict,
    get_kind,
    parse_template_json,
    preview,
    template_json,
    validate_template,
)

from ..render import flash, is_htmx, partial, redirect, render, toasts
from . import messages_saved
from .overview import GROUPS

log = logging.getLogger(__name__)
router = APIRouter()

AUTO = "auto"
MODES = ("simple", "code")
CORE = "core"                       # the pseudo-service the alert kinds are registered under
CORE_TITLE = "Every service"
# what the Simple tab does not show but must not lose when the form is saved
PASS_KEYS = ("url", "thumbnail", "image", "timestamp", "author", "if")
# "keep the bot's own fields" — one entry per field the bot built
REPEAT_FIELD = {"repeat": "fields", "name": "{{ item.name }}", "value": "{{ item.value }}", "inline": "{{ item.inline }}"}
HEX = "0123456789abcdefABCDEF"


# ----- small helpers ----------------------------------------------------------------------------------------
def _on(value: Any) -> bool:
    return str("" if value is None else value).strip().lower() in ("1", "true", "on", "yes")


def _hex(value: str) -> bool:
    return len(value) == 6 and all(c in HEX for c in value)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _slug(key: str) -> str:
    """A key like `github.push` as something safe to use in an id/selector."""
    return key.replace(".", "-")


def _kind(key: str) -> MessageKind:
    kind = get_kind(key)
    if kind is None:
        raise HTTPException(404, f"unknown message {key}")
    return kind


def _server_name(runtime) -> str:
    """The name embed footers carry — the `lab` variable a message template can use."""
    return str(runtime.store.server().get("name") or "my server")


def _service_title(request: Request, service: str) -> str:
    """The heading a service's kinds sit under: its own title, else the label of the group whose services post
    them (the media stack registers everything as `media.*`), else the bare name."""
    if service == CORE:
        return CORE_TITLE
    specs = request.app.state.runtime.specs
    spec = specs.get(service)
    if spec is not None:
        return spec.title
    if any(s.group == service for s in specs.values()):
        return dict(GROUPS).get(service, service)
    return service


# ----- form <-> template ------------------------------------------------------------------------------------
def _passthrough(form) -> dict[str, Any]:
    """The keys the Simple tab carries through untouched, out of its hidden field."""
    try:
        data = json.loads(_text(form.get("passthrough")) or "{}")
    except ValueError:
        return {}
    return {k: v for k, v in data.items() if k in PASS_KEYS} if isinstance(data, dict) else {}


def _colour(form) -> tuple[str, list[str]]:
    mode = _text(form.get("color_mode")).strip().lower() or AUTO
    if mode != "custom":
        return (mode if mode in SEVERITY_COLORS else AUTO), []
    raw = _text(form.get("color_hex")).strip().lstrip("#")
    if not _hex(raw):
        return AUTO, [f"colour {raw or '(empty)'!r}: pick a colour or type six hex digits like 5865F2"]
    return f"#{raw.upper()}", []


def _fields(form) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if _on(form.get("keep_fields")):
        out.append(dict(REPEAT_FIELD))
    rows = [_text(t) for t in form.getlist("field_row")]
    names = [_text(n) for n in form.getlist("field_name")]
    values = [_text(v) for v in form.getlist("field_value")]
    for row, name, value in zip(rows, names, values, strict=False):
        if not name.strip() and not value.strip():
            continue
        out.append({"name": name, "value": value, "inline": _on(form.get(f"field_inline_{row}"))})
    return out


def simple_template(form) -> tuple[dict[str, Any], list[str]]:
    """The Simple tab's controls as a template dict, in the shipped default's key order."""
    keep = _passthrough(form)
    colour, problems = _colour(form)
    tpl: dict[str, Any] = {"title": _text(form.get("title")), "description": _text(form.get("description"))}
    if "url" in keep:
        tpl["url"] = keep["url"]
    tpl["color"] = colour
    tpl["fields"] = _fields(form)
    tpl["footer"] = _text(form.get("footer"))
    for key in ("thumbnail", "image", "timestamp", "author", "if"):
        if key in keep:
            tpl[key] = keep[key]
    return tpl, problems


def template_from_form(form) -> tuple[dict[str, Any] | None, list[str]]:
    """Whichever tab was active → (template, problems). None with problems when it cannot be built."""
    if _text(form.get("mode")).strip().lower() == "code":
        try:
            return parse_template_json(_text(form.get("code"))), []
        except ValueError as e:
            return None, [str(e)]
    tpl, problems = simple_template(form)
    problems += validate_template(tpl)
    return (None, problems) if problems else (tpl, [])


def simple_view(template: dict[str, Any]) -> dict[str, Any]:
    """A template as the Simple tab's controls, plus what that tab cannot show (the Code tab can)."""
    fields = [f for f in (template.get("fields") or []) if isinstance(f, dict)]
    colour = _text(template.get("color")).strip() or AUTO
    mode, picked = AUTO, "#5865F2"
    if colour.lower() in SEVERITY_COLORS:
        mode = colour.lower()
    elif _hex(colour.lstrip("#")):
        mode, picked = "custom", "#" + colour.lstrip("#").upper()
    beyond = []      # only what a save from this tab would really drop — the rest rides along in `passthrough`
    if not isinstance(template.get("footer", ""), str):
        beyond.append("an icon next to the footer")
    if any(f.get("if") for f in fields):
        beyond.append("a field with a condition")
    if any(f.get("repeat") and dict(f) != REPEAT_FIELD for f in fields):
        beyond.append("a field repeated over something other than the bot's own fields")
    if colour.lower() != AUTO and mode == AUTO:
        beyond.append(f"a colour written as {colour!r}")
    return {
        "title": _text(template.get("title")), "description": _text(template.get("description")),
        "footer": _text(template.get("footer")) if isinstance(template.get("footer", ""), str) else "",
        "color_mode": mode, "color_hex": picked,
        "keep_fields": any(f.get("repeat") for f in fields),
        "fields": [{"name": _text(f.get("name")), "value": _text(f.get("value")), "inline": _on(f.get("inline"))}
                   for f in fields if not f.get("repeat")],
        "passthrough": json.dumps({k: template[k] for k in PASS_KEYS if k in template}),
        "beyond": beyond,
    }


# ----- previews ---------------------------------------------------------------------------------------------
def _rendered(kind: MessageKind, template: dict[str, Any] | None, lab: str) -> dict[str, Any]:
    """The kind's sample through a template: what the preview partial draws, or why it could not."""
    embed, _ctx, error = preview(kind, template, lab=lab)
    return {"embed": embed_to_dict(embed), "error": error}


def _variables(kind: MessageKind) -> list[dict[str, str]]:
    """What a template can say here: the kind's own variables first, then the ones every kind has."""
    pairs = list(kind.variables.items()) + [(k, v) for k, v in STANDARD_VARIABLES.items() if k not in kind.variables]
    return [{"name": n, "meaning": m, "token": "{{ " + n + " }}"} for n, m in pairs]


# ----- gallery ----------------------------------------------------------------------------------------------
def _card(request: Request, kind: MessageKind) -> dict[str, Any]:
    runtime = request.app.state.runtime
    store = runtime.messages
    saved = store.template_for(kind.key)
    shown = _rendered(kind, saved, _server_name(runtime))
    return {
        "key": kind.key, "slug": _slug(kind.key), "title": kind.title, "description": kind.description,
        "where": kind.where, "group": kind.group or "general", "service": kind.service,
        "enabled": store.enabled(kind.key), "customised": store.customised(kind.key),
        "embed": shown["embed"], "error": shown["error"],
        "search": " ".join([kind.key, kind.title, kind.description, kind.where, kind.group]).lower(),
    }


def _gallery(request: Request) -> list[dict[str, Any]]:
    """Every registered kind as cards, by service and then by the kind's group, both in registration order —
    which follows service discovery, so the shared alert kinds come first and each package's kinds stay together."""
    services: list[str] = []
    for kind in REGISTRY.values():
        if kind.service not in services:
            services.append(kind.service)
    services = [s for s in services if s == CORE] + [s for s in services if s != CORE]
    out = []
    for service in services:
        groups: list[dict[str, Any]] = []
        for kind in REGISTRY.values():
            if kind.service != service:
                continue
            card = _card(request, kind)
            hit = next((g for g in groups if g["name"] == card["group"]), None)
            if hit is None:
                hit = {"name": card["group"], "cards": []}
                groups.append(hit)
            hit["cards"].append(card)
        out.append({"service": service, "title": _service_title(request, service), "groups": groups,
                    "count": sum(len(g["cards"]) for g in groups)})
    return out


@router.get("/messages")
async def gallery(request: Request):
    items = _gallery(request)
    return render(request, "messages.html", {"items": items, "total": sum(i["count"] for i in items)})


def _card_response(request: Request, kind: MessageKind):
    if is_htmx(request):
        return partial(request, "partials/message_card.html", {"card": _card(request, kind)})
    return redirect(request, "/messages")


@router.post("/messages/{key}/toggle")
async def toggle(request: Request, key: str):
    """Switch a kind off (the bot posts nothing) or back on."""
    kind = _kind(key)
    store = request.app.state.runtime.messages
    on = not store.enabled(key)
    store.set(key, store.template_for(key), enabled=on)
    messages_saved(request)
    flash(request, f"{kind.title} is {'on again' if on else 'off — the bot will not post it'}", "success" if on else "info")
    return _card_response(request, kind)


@router.post("/messages/{key}/reset")
async def reset(request: Request, key: str):
    """Drop the customisation: back to what the service ships."""
    kind = _kind(key)
    request.app.state.runtime.messages.reset(key)
    messages_saved(request)
    flash(request, f"{kind.title} is back to the wording the service ships", "success")
    form = await request.form()
    if _text(form.get("scope")) == "editor":
        return redirect(request, f"/messages/{key}")
    return _card_response(request, kind)


# ----- editor -----------------------------------------------------------------------------------------------
def _editor(request: Request, kind: MessageKind, template: dict[str, Any], *, mode: str = "simple",
            errors: list[str] | None = None, code: str | None = None) -> dict[str, Any]:
    runtime = request.app.state.runtime
    store = runtime.messages
    lab = _server_name(runtime)
    return {
        "kind": kind, "key": kind.key, "slug": _slug(kind.key), "service_title": _service_title(request, kind.service),
        "template": template, "code": code if code is not None else template_json(template),
        "simple": simple_view(template), "mode": mode if mode in MODES else "simple", "errors": errors or [],
        "variables": _variables(kind), "severities": list(SEVERITY_COLORS), "limits": LIMITS, "max_fields": MAX_FIELDS,
        "enabled": store.enabled(kind.key), "customised": store.customised(kind.key),
        "default": _rendered(kind, None, lab), "yours": _rendered(kind, template, lab),
        "where_env": kind.where_env,
    }


@router.get("/messages/{key}")
async def editor(request: Request, key: str):
    kind = _kind(key)
    template = request.app.state.runtime.messages.template_for(key) or kind.default_template()
    return render(request, "message.html", _editor(request, kind, template))


@router.post("/messages/{key}")
async def save(request: Request, key: str):
    kind = _kind(key)
    st = request.app.state
    store = st.runtime.messages
    form = await request.form()
    mode = _text(form.get("mode")).strip().lower()
    template, problems = template_from_form(form)
    if template is not None:
        _embed, _ctx, error = preview(kind, template, lab=_server_name(st.runtime))
        if error:
            problems.append(error)
    if problems:
        shown = template if template is not None else simple_template(form)[0]
        ctx = _editor(request, kind, shown, mode=mode, errors=problems,
                      code=_text(form.get("code")) if mode == "code" else None)
        for p in problems:
            flash(request, p, "error")
        if is_htmx(request):
            return partial(request, "partials/message_form.html", ctx, status=422)
        return render(request, "message.html", ctx, status=422)
    store.set(key, template, enabled=store.enabled(key))
    messages_saved(request)
    flash(request, "saved — it applies to the next post", "success")
    if is_htmx(request):
        return partial(request, "partials/message_form.html", _editor(request, kind, template, mode=mode))
    return redirect(request, f"/messages/{key}")


@router.post("/messages/{key}/preview")
async def live_preview(request: Request, key: str):
    """What the form holds right now, rendered next to the shipped default — nothing is saved."""
    kind = _kind(key)
    lab = _server_name(request.app.state.runtime)
    template, problems = template_from_form(await request.form())
    yours = {"embed": None, "error": "; ".join(problems)} if template is None else _rendered(kind, template, lab)
    return partial(request, "partials/message_preview.html", {"default": _rendered(kind, None, lab), "yours": yours})


# ----- test post --------------------------------------------------------------------------------------------
def _channel_id(runtime, kind: MessageKind) -> str:
    """Where a test goes: the setting the kind names, else the alert channel of the server that service posts
    in. Read by hand rather than through `env_for`, which would add an entry for a service that is only a name
    here (`core`, `media`)."""
    store = runtime.store
    own = (store.services.get(kind.service) or {}).get("env") or {}
    server = store.server_for(kind.service)
    env = {**store.server_env(server), **{str(k): str(v) for k, v in own.items() if v is not None}}
    cid = str(env.get(kind.where_env) or "").strip() if kind.where_env else ""
    return cid or str(store.server(server).get("alert_channel_id") or "").strip()


def _presence(runtime, service: str):
    """The bot the service posts as when it is running, else any bot that is connected."""
    running = getattr(runtime.services.get(service), "presence", None)
    if running is not None and getattr(running, "connected", False):
        return running
    return next((p for p in runtime.presences.values() if getattr(p, "connected", False)), None)


def _reply(request: Request, key: str, status: int = 200):
    return toasts(request, status) if is_htmx(request) else redirect(request, f"/messages/{key}")


@router.post("/messages/{key}/test")
async def test_post(request: Request, key: str):
    """Post the previewed embed to the channel this kind normally uses, through a connected bot."""
    kind = _kind(key)
    runtime = request.app.state.runtime
    form = await request.form()
    if form.get("mode") is not None:
        template, problems = template_from_form(form)
    else:
        template, problems = runtime.messages.template_for(key), []
    if problems:
        flash(request, "fix the template first: " + "; ".join(problems), "error")
        return _reply(request, key, 422)
    embed, _ctx, error = preview(kind, template, lab=_server_name(runtime))
    if error or embed is None:
        flash(request, error or "this renders to nothing — there would be no post at all", "error")
        return _reply(request, key, 422)
    cid = _channel_id(runtime, kind)
    if not cid.isdigit():
        where = kind.where_env or "the alert channel"
        flash(request, f"no channel to post to yet — set {where} on the service's settings page (or the alert "
                       f"channel on the Discord page)", "error")
        return _reply(request, key, 422)
    presence = _presence(runtime, kind.service)
    if presence is None:
        flash(request, "no bot is connected right now — start periscope, then try again", "error")
        return _reply(request, key, 422)
    try:
        channel = await presence.get_channel_safe(int(cid))
    except Exception as e:  # noqa: BLE001
        log.warning("test post: channel %s lookup failed: %s", cid, e)
        channel = None
    if channel is None:
        flash(request, f"the bot cannot see channel {cid} — invite it to the server and give it access there", "error")
        return _reply(request, key, 422)
    try:
        await channel.send(embed=embed)
    except Exception as e:  # noqa: BLE001
        log.warning("test post to %s failed: %s", cid, e)
        flash(request, f"Discord refused the post: {e}", "error")
        return _reply(request, key, 422)
    name = getattr(channel, "name", "")
    flash(request, f"posted a test to {('#' + str(name)) if name else 'channel ' + cid}", "success")
    return _reply(request, key)

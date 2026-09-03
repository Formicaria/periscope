"""Message templates: every post a bot makes is a *message kind* that can be previewed and edited.

A kind is registered by the service that posts it (`MessageKind`), with a `sample()` that builds the embed the
way the bot would from representative data. At the send site the service calls `bot.messages.apply(key, embed,
ctx)`: with no customisation the embed goes out untouched; with one, the user's template is rendered over the
embed's parts (`title`, `description`, `fields`, …) plus whatever the service put in `ctx`, so the wording,
colour, footer and fields can be changed — or the whole thing rewritten — without touching code. A kind can
also be switched off. Customisations live in `config/messages.yaml` and apply immediately.

Template = a dict shaped like a Discord embed whose strings are Jinja2 (sandboxed):

    {"title": "🚀 {{ title }}", "description": "{{ description }}", "color": "auto",
     "fields": [{"repeat": "fields", "if": "item.name != 'Source'", "name": "{{ item.name }}",
                 "value": "{{ item.value }}", "inline": "{{ item.inline }}"},
                {"name": "Lab", "value": "{{ lab }}", "inline": true}],
     "footer": "{{ footer }}", "url": "{{ url }}", "timestamp": true}

`color`: "auto" (keep the bot's), a hex "#5865F2", a severity name (ok · info · warning · critical), or an
expression. A field with `repeat` is emitted once per item of that list (`item`, `loop` available); `if` on a
field or the whole template drops it when false.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import discord
import yaml
from jinja2 import ChainableUndefined, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from .embeds import Severity, human_bytes, human_duration, progress_bar, status_dot, truncate

log = logging.getLogger(__name__)

LIMITS = {"title": 256, "description": 4096, "field_name": 256, "field_value": 1024, "footer": 2048, "author": 256}
MAX_FIELDS = 25
SEVERITY_COLORS = {s.value: s.color for s in Severity}
AUTO = "auto"

SampleFn = Callable[[], "tuple[discord.Embed | None, dict[str, Any]]"]


@dataclass
class MessageKind:
    key: str                          # "github.push" — <service>.<name>
    title: str                        # "Push"
    description: str                  # when the bot posts it
    where: str = ""                   # where it goes, in words ("the alert channel")
    where_env: str = ""               # the setting that names that channel, for test posts (ALERT_CHANNEL_ID)
    sample: SampleFn | None = None    # () -> (embed as the bot would build it, ctx) from representative data
    variables: dict[str, str] = field(default_factory=dict)   # extra ctx the service passes, name → meaning
    template: dict[str, Any] | None = None   # explicit default template; None = identity over the bot's embed
    group: str = ""                   # UI grouping inside the service ("boards", "feed", "alerts", …)

    @property
    def service(self) -> str:
        return self.key.split(".", 1)[0]

    def default_template(self) -> dict[str, Any]:
        return copy.deepcopy(self.template) if self.template else identity_template()


# ----- registry -------------------------------------------------------------------------------------------
REGISTRY: dict[str, MessageKind] = {}


def register(*kinds: MessageKind) -> None:
    for k in kinds:
        REGISTRY[k.key] = k


def kinds_for(service: str) -> list[MessageKind]:
    return [k for k in REGISTRY.values() if k.service == service]


def get_kind(key: str) -> MessageKind | None:
    return REGISTRY.get(key)


# ----- embed <-> plain data ---------------------------------------------------------------------------------
STANDARD_VARIABLES = {
    "title": "the title the bot wrote", "description": "the body text", "url": "the title's link",
    "color": "the colour as #hex", "fields": "the bot's fields: item.name · item.value · item.inline",
    "footer": "the footer text", "thumbnail": "thumbnail image url", "image": "large image url",
    "author": "the author line", "timestamp": "true when the embed carries a time",
    "lab": "the lab name", "service": "the service posting this",
}


def embed_ctx(embed: discord.Embed | None) -> dict[str, Any]:
    """The parts of an embed as plain values — the variables every template can use."""
    if embed is None:
        return {"title": "", "description": "", "url": "", "color": "", "fields": [], "footer": "", "thumbnail": "",
                "image": "", "author": "", "timestamp": False}
    color = f"#{embed.color.value:06X}" if embed.color is not None else ""
    return {
        "title": embed.title or "", "description": embed.description or "", "url": embed.url or "", "color": color,
        "fields": [{"name": f.name or "", "value": f.value or "", "inline": bool(f.inline)} for f in embed.fields],
        "footer": (embed.footer.text or "") if embed.footer else "",
        "thumbnail": (embed.thumbnail.url or "") if embed.thumbnail else "",
        "image": (embed.image.url or "") if embed.image else "",
        "author": (embed.author.name or "") if embed.author else "",
        "timestamp": embed.timestamp is not None,
    }


def identity_template() -> dict[str, Any]:
    """Reproduces the bot's embed unchanged — the starting point for editing a kind without its own template."""
    return {
        "title": "{{ title }}", "description": "{{ description }}", "url": "{{ url }}", "color": AUTO,
        "fields": [{"repeat": "fields", "name": "{{ item.name }}", "value": "{{ item.value }}", "inline": "{{ item.inline }}"}],
        "footer": "{{ footer }}", "thumbnail": "{{ thumbnail }}", "image": "{{ image }}", "timestamp": AUTO,
    }


def embed_to_dict(embed: discord.Embed | None) -> dict[str, Any] | None:
    """What the web preview renders (discord.py's wire format, plus nothing)."""
    return embed.to_dict() if embed is not None else None


# ----- rendering --------------------------------------------------------------------------------------------
def _env() -> SandboxedEnvironment:
    env = SandboxedEnvironment(autoescape=False, undefined=ChainableUndefined, trim_blocks=True, lstrip_blocks=True)
    env.filters.update({
        "bytes": human_bytes, "duration": human_duration, "bar": progress_bar, "dot": status_dot,
        "cut": lambda s, n=1024: truncate(str(s), int(n)),
    })
    return env


ENV = _env()


def _render_str(value: Any, ctx: dict[str, Any]) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(value)
    if "{{" not in value and "{%" not in value:
        return value
    return ENV.from_string(value).render(**ctx)


def _truthy(value: Any, ctx: dict[str, Any]) -> bool:
    """An `if` clause: a Jinja expression (string) or a plain bool."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip()
    if not text:
        return True
    try:
        return bool(ENV.compile_expression(text)(**ctx))
    except TemplateError as e:
        raise TemplateError(f"in condition {text!r}: {e}") from e


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _color(spec: Any, ctx: dict[str, Any]) -> int | None:
    text = _render_str(spec, ctx).strip()
    if not text or text.lower() == AUTO:
        base = str(ctx.get("color") or "").strip()
        text = base
        if not text:
            return None
    low = text.lower()
    if low in SEVERITY_COLORS:
        return SEVERITY_COLORS[low]
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", text)
    if m:
        return int(m.group(1), 16)
    if text.isdigit():
        return int(text) & 0xFFFFFF
    raise ValueError(f"colour {text!r}: use auto, #RRGGBB or ok/info/warning/critical")


def render_template(template: dict[str, Any], ctx: dict[str, Any]) -> discord.Embed | None:
    """Template + variables → embed (None when the template's own `if` is false or nothing is left)."""
    if not isinstance(template, dict):
        raise ValueError("the template must be an object")
    if not _truthy(template.get("if"), ctx):
        return None
    e = discord.Embed()
    title = truncate(_render_str(template.get("title"), ctx).strip(), LIMITS["title"])
    desc = truncate(_render_str(template.get("description"), ctx).strip(), LIMITS["description"])
    if title:
        e.title = title
    if desc:
        e.description = desc
    url = _render_str(template.get("url"), ctx).strip()
    if url:
        e.url = url
    color = _color(template.get("color", AUTO), ctx)
    if color is not None:
        e.color = color
    fields = template.get("fields") or []
    if not isinstance(fields, list):
        raise ValueError("fields must be a list")
    out_fields: list[tuple[str, str, bool]] = []
    for spec in fields:
        if not isinstance(spec, dict):
            raise ValueError("each field must be an object with name and value")
        repeat = spec.get("repeat")
        if repeat:
            items = ctx.get(str(repeat).strip())
            if items is None:
                items = ENV.compile_expression(str(repeat))(**ctx)
            items = list(items or [])
            for i, item in enumerate(items):
                sub = {**ctx, "item": item, "loop": {"index": i + 1, "index0": i, "first": i == 0, "last": i == len(items) - 1,
                                                    "length": len(items)}}
                if not _truthy(spec.get("if"), sub):
                    continue
                out_fields.append((_render_str(spec.get("name"), sub), _render_str(spec.get("value"), sub),
                                   _as_bool(_render_str(spec.get("inline", False), sub))))
        else:
            if not _truthy(spec.get("if"), ctx):
                continue
            out_fields.append((_render_str(spec.get("name"), ctx), _render_str(spec.get("value"), ctx),
                               _as_bool(_render_str(spec.get("inline", False), ctx))))
    for name, value, inline in out_fields[:MAX_FIELDS]:
        name, value = name.strip(), value.strip()
        if not name and not value:
            continue
        e.add_field(name=truncate(name or "\u200b", LIMITS["field_name"]), value=truncate(value or "\u200b", LIMITS["field_value"]),
                    inline=inline)
    footer = template.get("footer")
    if isinstance(footer, dict):
        ftext = truncate(_render_str(footer.get("text"), ctx).strip(), LIMITS["footer"])
        ficon = _render_str(footer.get("icon_url"), ctx).strip() or None
    else:
        ftext, ficon = truncate(_render_str(footer, ctx).strip(), LIMITS["footer"]), None
    if ftext:
        e.set_footer(text=ftext, icon_url=ficon)
    thumb = _render_str(template.get("thumbnail"), ctx).strip()
    if thumb:
        e.set_thumbnail(url=thumb)
    image = _render_str(template.get("image"), ctx).strip()
    if image:
        e.set_image(url=image)
    author = template.get("author")
    if isinstance(author, dict):
        aname = truncate(_render_str(author.get("name"), ctx).strip(), LIMITS["author"])
        if aname:
            e.set_author(name=aname, url=_render_str(author.get("url"), ctx).strip() or None,
                         icon_url=_render_str(author.get("icon_url"), ctx).strip() or None)
    elif author:
        aname = truncate(_render_str(author, ctx).strip(), LIMITS["author"])
        if aname:
            e.set_author(name=aname)
    # timestamp: "auto" (the default) keeps what the bot did; true always stamps, false never
    ts = template.get("timestamp", AUTO)
    if isinstance(ts, str):
        rendered = _render_str(ts, ctx).strip().lower()
        ts = bool(ctx.get("timestamp", True)) if rendered in ("", AUTO) else _as_bool(rendered)
    if ts:
        e.timestamp = discord.utils.utcnow()
    if not (e.title or e.description or e.fields or e.image or e.thumbnail):
        return None
    return e


def validate_template(template: Any) -> list[str]:
    """Structural problems a template has, in plain words (empty = fine)."""
    problems: list[str] = []
    if not isinstance(template, dict):
        return ["the template must be an object like {\"title\": ..., \"description\": ...}"]
    known = {"if", "title", "description", "url", "color", "fields", "footer", "thumbnail", "image", "author", "timestamp"}
    for k in template:
        if k not in known:
            problems.append(f"unknown key {k!r} (known: {', '.join(sorted(known))})")
    fields = template.get("fields", [])
    if not isinstance(fields, list):
        problems.append("fields must be a list")
    else:
        for i, f in enumerate(fields, 1):
            if not isinstance(f, dict):
                problems.append(f"field {i} must be an object with name and value")
                continue
            for k in f:
                if k not in {"name", "value", "inline", "if", "repeat"}:
                    problems.append(f"field {i}: unknown key {k!r}")
    for k in ("title", "description", "url", "thumbnail", "image"):
        v = template.get(k)
        if v is not None and not isinstance(v, str):
            problems.append(f"{k} must be text")
    for k, v in (("title", template.get("title")), ("description", template.get("description")), ("url", template.get("url")),
                 ("color", template.get("color"))):
        if isinstance(v, str):
            try:
                ENV.from_string(v)
            except TemplateError as e:
                problems.append(f"{k}: {e}")
    for i, f in enumerate([f for f in fields if isinstance(f, dict)] if isinstance(fields, list) else [], 1):
        for k in ("name", "value"):
            v = f.get(k)
            if isinstance(v, str):
                try:
                    ENV.from_string(v)
                except TemplateError as e:
                    problems.append(f"field {i} {k}: {e}")
    return problems


# ----- customisations on disk -------------------------------------------------------------------------------
class MessageStore:
    """config/messages.yaml: {key: {template: {...}, enabled: bool}}. Re-read when the file changes, so a save in
    the web UI is live on the next post — no restart."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._data: dict[str, dict[str, Any]] = {}
        self._mtime: float = -1
        self.reload()

    def reload(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            self._data, self._mtime = {}, -1
            return
        if mtime == self._mtime:
            return
        try:
            raw = yaml.safe_load(self.path.read_text()) or {}
            self._data = {str(k): dict(v) for k, v in raw.items() if isinstance(v, dict)}
        except (OSError, yaml.YAMLError) as e:
            log.error("messages.yaml unreadable (%s) — customisations ignored until fixed", e)
            self._data = {}
        self._mtime = mtime

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".messages-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(self._data, f, sort_keys=True, allow_unicode=True)
        os.replace(tmp, self.path)
        try:
            self._mtime = self.path.stat().st_mtime
        except OSError:
            self._mtime = time.time()

    def get(self, key: str) -> dict[str, Any] | None:
        self.reload()
        return self._data.get(key)

    def template_for(self, key: str) -> dict[str, Any] | None:
        entry = self.get(key)
        return entry.get("template") if entry else None

    def enabled(self, key: str) -> bool:
        entry = self.get(key)
        return True if entry is None else bool(entry.get("enabled", True))

    def customised(self, key: str) -> bool:
        entry = self.get(key)
        return bool(entry and (entry.get("template") or not entry.get("enabled", True)))

    def set(self, key: str, template: dict[str, Any] | None, enabled: bool = True) -> None:
        self.reload()
        if template is None and enabled:
            self._data.pop(key, None)
        else:
            self._data[key] = {"template": template, "enabled": enabled}
        self.save()

    def reset(self, key: str) -> None:
        self.reload()
        if self._data.pop(key, None) is not None:
            self.save()

    def keys(self) -> list[str]:
        self.reload()
        return list(self._data)


# ----- the per-bot facade -----------------------------------------------------------------------------------
class Messages:
    """`bot.messages`: apply the user's customisation of a kind (if any) at the moment a post is built."""

    def __init__(self, store: MessageStore | None = None, *, service: str = "", lab: str = ""):
        self.store = store
        self.service = service
        self.lab = lab

    def enabled(self, key: str) -> bool:
        return self.store.enabled(key) if self.store else True

    def context(self, embed: discord.Embed | None, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        base = embed_ctx(embed)
        base.update({"lab": self.lab, "service": self.service})
        if ctx:
            base.update(ctx)
        return base

    def apply(self, key: str, embed: discord.Embed | None, ctx: dict[str, Any] | None = None) -> discord.Embed | None:
        """The embed the bot built → the embed to post. Untouched without a customisation; None when the kind is
        switched off. A broken template never blocks a post: it is logged and the bot's own embed goes out."""
        if self.store is None:
            return embed
        if not self.store.enabled(key):
            return None
        template = self.store.template_for(key)
        if not template:
            return embed
        try:
            return render_template(template, self.context(embed, ctx))
        except Exception as e:  # noqa: BLE001
            log.error("message template %s failed (%s) — posting the default", key, e)
            return embed

    def render(self, key: str, ctx: dict[str, Any] | None = None) -> discord.Embed | None:
        """For kinds with an explicit default template and no code-built embed: render default or customisation."""
        kind = REGISTRY.get(key)
        if kind is None or kind.template is None:
            return None
        if self.store is not None and not self.store.enabled(key):
            return None
        template = (self.store.template_for(key) if self.store else None) or kind.template
        full = self.context(None, ctx)
        full.update(default_ctx(kind, full))     # "auto" in a customisation means "as the default has it"
        try:
            return render_template(template, full)
        except Exception as e:  # noqa: BLE001
            log.error("message template %s failed (%s) — rendering the default", key, e)
            return render_template(kind.template, full)


def default_ctx(kind: MessageKind, ctx: dict[str, Any]) -> dict[str, Any]:
    """What the kind's own default template produces, as variables — so a customisation of a template-only kind
    can still say "auto" (colour) or reuse `{{ title }}` / `{{ description }}` from the shipped wording."""
    if kind.template is None:
        return {}
    try:
        base = render_template(kind.template, ctx)
    except Exception:  # noqa: BLE001
        return {}
    out = embed_ctx(base)
    return {k: v for k, v in out.items() if v not in ("", [], None)}


def preview(kind: MessageKind, template: dict[str, Any] | None, *, lab: str = "lab") -> tuple[discord.Embed | None, dict[str, Any], str | None]:
    """(embed, ctx, error) for the editor: the kind's sample run through `template` (default when None)."""
    embed, ctx = (None, {})
    if kind.sample is not None:
        try:
            embed, ctx = kind.sample()
        except Exception as e:  # noqa: BLE001
            log.exception("sample for %s failed", kind.key)
            return None, {}, f"sample data failed: {type(e).__name__}: {e}"
    full = embed_ctx(embed)
    full.update({"lab": lab, "service": kind.service})
    full.update(ctx or {})
    if kind.template is not None:
        full.update(default_ctx(kind, full))
    tpl = template if template else kind.default_template()
    try:
        return render_template(tpl, full), full, None
    except Exception as e:  # noqa: BLE001
        return None, full, f"{type(e).__name__}: {e}"


def template_json(template: dict[str, Any]) -> str:
    return json.dumps(template, indent=2, ensure_ascii=False)


def parse_template_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"not valid JSON: {e.msg} (line {e.lineno}, column {e.colno})") from e
    problems = validate_template(data)
    if problems:
        raise ValueError("; ".join(problems))
    return data

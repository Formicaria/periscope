"""Typed `Setting` lists → form fields, and submitted forms → validated env values."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from periscope.service import SHARED_SETTINGS, ServiceSpec, Setting

from .guild import Channel, Role

SHARED_GROUP = "Discord routing"
TRUE = ("1", "true", "yes", "on")

# lab keys behind the shared settings, for the "lab default" hint
LAB_FIELD = {"STATUS_CHANNEL_ID": "status_channel_id", "ALERT_CHANNEL_ID": "alert_channel_id",
             "ALERT_ROLE_ID": "alert_role_id", "STATUS_INTERVAL_S": "status_interval_s"}


@dataclass
class Field:
    key: str
    label: str
    type: str
    value: str = ""
    placeholder: str = ""
    required: bool = False
    help: str = ""
    group: str = ""
    is_set: bool = False                      # secrets: a value exists (never rendered)
    options: list[tuple[str, str]] = field(default_factory=list)   # (value, label) for selects
    shared: bool = False
    lab_default: str = ""                     # human hint for shared keys
    unknown_option: str = ""                  # current value not in options → kept as an extra option

    @property
    def input_type(self) -> str:
        return {"secret": "password", "int": "number", "url": "url"}.get(self.type, "text")

    @property
    def is_select(self) -> bool:
        return bool(self.options) and self.type in ("channel", "role", "choice")

    @property
    def checked(self) -> bool:
        return self.value.strip().lower() in TRUE


def _lab_hint(store, key: str, channels: list[Channel], roles: list[Role]) -> str:
    lab_key = LAB_FIELD.get(key)
    if not lab_key:
        return ""
    v = str(store.lab.get(lab_key) or "").strip()
    if not v:
        return "no lab default"
    if key.endswith("_CHANNEL_ID"):
        name = next((c.name for c in channels if c.id == v), "")
        return f"lab default: #{name}" if name else f"lab default: {v}"
    if key.endswith("_ROLE_ID"):
        name = next((r.name for r in roles if r.id == v), "")
        return f"lab default: @{name}" if name else f"lab default: {v}"
    return f"lab default: {v}"


def build_field(s: Setting, env: dict[str, str], *, channels: list[Channel], roles: list[Role], shared: bool = False,
                store=None) -> Field:
    raw = str(env.get(s.key, "") or "")
    f = Field(s.key, s.label, s.type, required=s.required, help=s.help, group=s.group, shared=shared)
    if s.type == "secret":
        f.is_set = bool(raw)
        f.placeholder = "•••• set — leave blank to keep" if raw else "not set"
    elif s.type == "bool":
        f.value = raw if raw else (s.default or "false")
    elif s.type in ("int", "choice"):
        f.value = raw if raw else (s.default or "")
        if s.type == "choice":
            f.options = [(c, c) for c in s.choices]
    elif s.type == "channel":
        f.value = raw
        f.options = [(c.id, c.label) for c in channels]
    elif s.type == "role":
        f.value = raw
        f.options = [(r.id, r.label) for r in roles]
    else:
        f.value = raw
        f.placeholder = s.default or ""
        if s.type == "list" and not f.help:
            f.help = "comma-separated"
    if f.options and f.value and f.value not in {v for v, _ in f.options}:
        f.unknown_option = f.value
    if shared and store is not None:
        f.lab_default = _lab_hint(store, s.key, channels, roles)
    return f


def build_fields(spec: ServiceSpec, env: dict[str, str], *, channels: list[Channel], roles: list[Role], store=None
                 ) -> list[tuple[str, list[Field]]]:
    """Fields grouped as [(group title, [Field, ...]), ...] in first-seen order; shared settings last."""
    groups: dict[str, list[Field]] = {}
    for s in spec.settings:
        groups.setdefault(s.group or spec.title, []).append(build_field(s, env, channels=channels, roles=roles))
    groups[SHARED_GROUP] = [build_field(s, env, channels=channels, roles=roles, shared=True, store=store) for s in SHARED_SETTINGS]
    return list(groups.items())


def parse_form(spec: ServiceSpec, form: Any, current_env: dict[str, str], *, require: bool = True
               ) -> tuple[dict[str, str], list[str]]:
    """Submitted form → {KEY: value} for `Store.update_service_env` (blank = remove the override; a secret left
    blank is *absent* so the stored one survives) + a list of validation errors. `require=False` skips the
    required-field check (a disabled service may be configured incrementally; Test reports what is missing)."""
    values: dict[str, str] = {}
    errors: list[str] = []
    for s in [*spec.settings, *SHARED_SETTINGS]:
        if s.type == "secret":
            if str(form.get(f"clear_{s.key}") or "").lower() in TRUE:
                values[s.key] = ""
                continue
            raw = str(form.get(s.key) or "").strip()
            if raw:
                values[s.key] = raw
            continue
        if s.type == "bool":
            vals = [str(v) for v in form.getlist(s.key)] if hasattr(form, "getlist") else [str(form.get(s.key) or "")]
            raw = vals[-1] if vals else "false"
            values[s.key] = "true" if raw.strip().lower() in TRUE else "false"
            continue
        raw = str(form.get(s.key) or "").strip()
        if s.type == "int" and raw and not re.fullmatch(r"-?\d+", raw):
            errors.append(f"{s.label} must be a whole number")
        if s.type in ("channel", "role") and raw and not re.fullmatch(r"\d{1,25}", raw):
            errors.append(f"{s.label} must be a Discord id")
        if s.type == "choice" and raw and s.choices and raw not in s.choices:
            errors.append(f"{s.label}: {raw!r} is not one of {', '.join(s.choices)}")
        values[s.key] = raw
    for s in spec.settings:
        effective = values[s.key] if s.key in values else current_env.get(s.key, "")
        if require and s.required and not effective:
            errors.append(f"{s.label} is required")
    return values, errors


def merged_env(current_env: dict[str, str], values: dict[str, str]) -> dict[str, str]:
    """The env a Test run sees: stored values overlaid with the submitted ones (blank = unset)."""
    env = dict(current_env)
    for k, v in values.items():
        if v == "":
            env.pop(k, None)
        else:
            env[k] = v
    return env


def mask_secrets(obj: Any) -> Any:
    """Recursively mask dict values under secret-looking keys (for any JSON the API emits)."""
    from periscope.store import is_secret_key

    if isinstance(obj, dict):
        return {k: ("••••••••" if isinstance(k, str) and is_secret_key(k) and v else mask_secrets(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_secrets(x) for x in obj]
    return obj

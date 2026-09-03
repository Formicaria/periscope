"""v2 config store: one YAML file for the whole install (config/periscope.yaml, mode 0600).

    version: 2
    lab:        {name, color, guild_id, status_channel_id, alert_channel_id, alert_role_id, admin_role_ids, log_level}
    webhook:    {host, port, secret}            # one inbound listener shared by every service
    web:        {host, port, base_url, oauth_client_id, oauth_client_secret, session_secret, allowed_role_ids}
    presences:  {default: {token, label}, arr: {token, label}}
    services:   {proxmox: {enabled, presence, env: {PVE_URL: ..., ALERT_CHANNEL_ID: ...}}}

`env_for(service)` flattens lab + webhook + presence token + the service's own keys into the KEY=VALUE mapping
every service's `Settings.from_env()` already understands, so v1 bot code runs unchanged inside v2.
"""

from __future__ import annotations

import copy
import os
import secrets as _secrets
import tempfile
from pathlib import Path
from typing import Any

import yaml

DEFAULT_PATH = Path("config/periscope.yaml")

LAB_KEYS = {
    "name": "LAB_NAME", "color": "LAB_COLOR", "guild_id": "GUILD_ID", "status_channel_id": "STATUS_CHANNEL_ID",
    "alert_channel_id": "ALERT_CHANNEL_ID", "alert_role_id": "ALERT_ROLE_ID", "admin_role_ids": "ADMIN_ROLE_IDS",
    "log_level": "LOG_LEVEL", "status_interval_s": "STATUS_INTERVAL_S",
}


def _blank() -> dict[str, Any]:
    return {
        "version": 2,
        "lab": {"name": "lab", "color": "5865F2", "guild_id": "", "status_channel_id": "", "alert_channel_id": "",
                "alert_role_id": "", "admin_role_ids": [], "log_level": "INFO", "status_interval_s": 60},
        "webhook": {"host": "0.0.0.0", "port": 8080, "secret": _secrets.token_hex(24)},
        "web": {"host": "0.0.0.0", "port": 8090, "base_url": "", "oauth_client_id": "", "oauth_client_secret": "",
                "session_secret": _secrets.token_hex(32), "allowed_role_ids": []},
        "presences": {"default": {"token": "", "label": "periscope"}},
        "services": {},
    }


class Store:
    def __init__(self, path: str | os.PathLike = DEFAULT_PATH, data: dict[str, Any] | None = None):
        self.path = Path(path)
        self.data: dict[str, Any] = data if data is not None else _blank()

    # ----- io ------------------------------------------------------------------------------
    @classmethod
    def load(cls, path: str | os.PathLike = DEFAULT_PATH) -> "Store":
        p = Path(path)
        if not p.exists():
            return cls(p)
        raw = yaml.safe_load(p.read_text()) or {}
        data = _blank()
        for k, v in raw.items():
            if isinstance(v, dict) and isinstance(data.get(k), dict) and k != "services" and k != "presences":
                data[k].update(v)
            else:
                data[k] = v
        data.setdefault("presences", {})
        data.setdefault("services", {})
        return cls(p, data)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".periscope-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(self.data, f, sort_keys=False, allow_unicode=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def exists(self) -> bool:
        return self.path.exists()

    # ----- sections -----------------------------------------------------------------------
    @property
    def lab(self) -> dict[str, Any]:
        return self.data["lab"]

    @property
    def webhook(self) -> dict[str, Any]:
        return self.data["webhook"]

    @property
    def web(self) -> dict[str, Any]:
        return self.data["web"]

    @property
    def presences(self) -> dict[str, dict[str, Any]]:
        return self.data["presences"]

    @property
    def services(self) -> dict[str, dict[str, Any]]:
        return self.data["services"]

    def service(self, name: str) -> dict[str, Any]:
        return self.services.setdefault(name, {"enabled": False, "presence": self.default_presence(), "env": {}})

    def enabled_services(self) -> list[str]:
        return [n for n, s in self.services.items() if s.get("enabled")]

    def set_enabled(self, name: str, on: bool) -> None:
        self.service(name)["enabled"] = bool(on)

    def default_presence(self) -> str:
        """The bot identity a service uses when it does not name one: `default` when that has a token, else
        the first identity that does (a migrated install has one per old bot and no shared one)."""
        if self.presences.get("default", {}).get("token"):
            return "default"
        for name, p in self.presences.items():
            if p.get("token"):
                return name
        return "default" if "default" in self.presences else next(iter(self.presences), "default")

    def presence_for(self, name: str) -> str:
        p = str((self.services.get(name) or {}).get("presence") or "")
        return p if p and p in self.presences else self.default_presence()

    def token_for(self, name: str) -> str:
        return str(self.presences.get(self.presence_for(name), {}).get("token") or "")

    def tidy(self) -> bool:
        """Drop the empty `default` identity once real ones exist and nothing points at it (a migrated install
        would otherwise show a bot with a 'missing token' forever). Returns True when something changed."""
        d = self.presences.get("default")
        if d is None or d.get("token"):
            return False
        others = [n for n, p in self.presences.items() if n != "default" and p.get("token")]
        used = any((s.get("presence") or "") == "default" for s in self.services.values())
        if not others or used:
            return False
        self.presences.pop("default")
        return True

    # ----- flattening -----------------------------------------------------------------------
    def lab_env(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, key in LAB_KEYS.items():
            v = self.lab.get(k, "")
            if isinstance(v, list):
                v = ",".join(str(x) for x in v)
            out[key] = "" if v is None else str(v)
        wh = self.webhook
        out["WEBHOOK_HOST"] = str(wh.get("host", "0.0.0.0"))
        out["WEBHOOK_PORT"] = str(wh.get("port", 8080))
        out["WEBHOOK_SECRET"] = str(wh.get("secret", ""))
        out["DATA_DIR"] = str(self.path.parent.parent / "data")
        return out

    def env_for(self, name: str) -> dict[str, str]:
        """lab defaults + presence token + the service's own env (which may override any lab key)."""
        env = self.lab_env()
        env["DISCORD_TOKEN"] = self.token_for(name)
        for k, v in (self.service(name).get("env") or {}).items():
            if v is None:
                continue
            env[str(k)] = "" if v is None else str(v)
        return env

    def update_service_env(self, name: str, values: dict[str, Any]) -> None:
        env = self.service(name).setdefault("env", {})
        for k, v in values.items():
            if v in ("", None) and k in env:
                env.pop(k)
            elif v not in ("", None):
                env[k] = v

    # ----- copies for the UI ----------------------------------------------------------------
    def redacted(self) -> dict[str, Any]:
        d = copy.deepcopy(self.data)
        for p in d.get("presences", {}).values():
            if p.get("token"):
                p["token"] = "••••••••"
        for s in d.get("services", {}).values():
            for k in list((s.get("env") or {}).keys()):
                if is_secret_key(k) and s["env"][k]:
                    s["env"][k] = "••••••••"
        for k in ("oauth_client_secret", "session_secret"):
            if d.get("web", {}).get(k):
                d["web"][k] = "••••••••"
        if d.get("webhook", {}).get("secret"):
            d["webhook"]["secret"] = "••••••••"
        return d


SECRET_HINTS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "PASS", "API")


def is_secret_key(key: str) -> bool:
    k = key.upper()
    return any(h in k for h in SECRET_HINTS) and not k.endswith(("_ID", "_IDS", "_URL", "_ENABLED", "_NAME"))

"""v2 config store: one YAML file for the whole install (config/periscope.yaml, mode 0600).

    version: 2
    servers:    {main: {name, color, guild_id, status_channel_id, alert_channel_id, alert_role_id, admin_role_ids},
                 plex: {…}}                     # every Discord server periscope posts in
    lab:        {log_level, status_interval_s}  # settings that are not per-server (legacy name, kept for v1 code)
    webhook:    {host, port, secret}            # one inbound listener shared by every service
    web:        {host, port, base_url, oauth_client_id, oauth_client_secret, session_secret, allowed_role_ids}
    presences:  {default: {token, label}, arr: {token, label}}
    services:   {proxmox: {enabled, presence, server, env: {PVE_URL: ..., ALERT_CHANNEL_ID: ...}}}

A service names the server it posts in; a bot (presence) serves every server its services use, so one bot can
post in several servers at once. `env_for(service)` flattens that server + webhook + presence token + the
service's own keys into the KEY=VALUE mapping every service's `Settings.from_env()` already understands, so
v1 bot code runs unchanged inside v2.
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

# what a server's fields are called in the KEY=VALUE mapping the bots read
SERVER_KEYS = {
    "name": "LAB_NAME", "color": "LAB_COLOR", "guild_id": "GUILD_ID", "status_channel_id": "STATUS_CHANNEL_ID",
    "alert_channel_id": "ALERT_CHANNEL_ID", "alert_role_id": "ALERT_ROLE_ID", "admin_role_ids": "ADMIN_ROLE_IDS",
}
GLOBAL_KEYS = {"log_level": "LOG_LEVEL", "status_interval_s": "STATUS_INTERVAL_S"}
LAB_KEYS = {**SERVER_KEYS, **GLOBAL_KEYS}      # v1 name, kept so older code and tests keep working
MAIN = "main"                                   # the first server's key


def blank_server(name: str = "my server") -> dict[str, Any]:
    return {"name": name, "color": "5865F2", "guild_id": "", "status_channel_id": "", "alert_channel_id": "",
            "alert_role_id": "", "admin_role_ids": []}


def _blank() -> dict[str, Any]:
    return {
        "version": 2,
        "servers": {MAIN: blank_server()},
        "lab": {"log_level": "INFO", "status_interval_s": 60},
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
        _upgrade_servers(data, had_servers=isinstance(raw.get("servers"), dict) and bool(raw["servers"]))
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
    def servers(self) -> dict[str, dict[str, Any]]:
        """Every Discord server periscope posts in, by key. The first one is the default for new services."""
        return self.data.setdefault("servers", {MAIN: blank_server()})

    @property
    def lab(self) -> dict[str, Any]:
        """The default server plus the settings that are not per-server — what v1 code and `LAB_*` keys mean."""
        return _LabView(self)

    @property
    def globals(self) -> dict[str, Any]:
        return self.data.setdefault("lab", {"log_level": "INFO", "status_interval_s": 60})

    def default_server(self) -> str:
        """The server new services post in: the first one that names a Discord server, else the first."""
        for key, srv in self.servers.items():
            if str(srv.get("guild_id") or "").strip():
                return key
        return next(iter(self.servers), MAIN)

    def server(self, key: str | None = None) -> dict[str, Any]:
        servers = self.servers
        if key and key in servers:
            return servers[key]
        return servers.setdefault(self.default_server(), blank_server())

    def server_for(self, service: str) -> str:
        """Which server a service posts in (its own choice, else the default)."""
        pick = str((self.services.get(service) or {}).get("server") or "")
        return pick if pick in self.servers else self.default_server()

    def add_server(self, key: str, name: str = "") -> dict[str, Any]:
        srv = self.servers.setdefault(key, blank_server(name or key))
        if name:
            srv["name"] = name
        return srv

    def remove_server(self, key: str) -> list[str]:
        """Forget a server; services that used it fall back to the default. Returns those services."""
        if key not in self.servers or len(self.servers) <= 1:
            return []
        self.servers.pop(key)
        fallback = self.default_server()
        moved = []
        for name, svc in self.services.items():
            if svc.get("server") == key:
                svc["server"] = fallback
                moved.append(name)
        return moved

    def guild_ids(self) -> dict[str, str]:
        """server key → Discord server id, for the servers that name one."""
        return {k: str(v.get("guild_id") or "").strip() for k, v in self.servers.items() if str(v.get("guild_id") or "").strip()}

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
        return self.services.setdefault(name, {"enabled": False, "presence": self.default_presence(),
                                               "server": self.default_server(), "env": {}})

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
    def server_env(self, key: str | None = None) -> dict[str, str]:
        """One server's settings plus the global ones, as the KEY=VALUE mapping the bots read."""
        srv, out = self.server(key), {}
        for field, env_key in SERVER_KEYS.items():
            v = srv.get(field, "")
            if isinstance(v, list):
                v = ",".join(str(x) for x in v)
            out[env_key] = "" if v is None else str(v)
        for field, env_key in GLOBAL_KEYS.items():
            v = self.globals.get(field, "")
            out[env_key] = "" if v is None else str(v)
        wh = self.webhook
        out["WEBHOOK_HOST"] = str(wh.get("host", "0.0.0.0"))
        out["WEBHOOK_PORT"] = str(wh.get("port", 8080))
        out["WEBHOOK_SECRET"] = str(wh.get("secret", ""))
        out["DATA_DIR"] = str(self.path.parent.parent / "data")
        return out

    def lab_env(self) -> dict[str, str]:
        """The default server's settings (v1 name)."""
        return self.server_env()

    def env_for(self, name: str) -> dict[str, str]:
        """the service's server + presence token + the service's own env (which overrides any server default)."""
        env = self.server_env(self.server_for(name))
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


class _LabView(dict):
    """`store.lab`: the default server's fields plus the global ones, writable — v1 code, the CLI and the
    migration all reach for `store.lab[...]`, and a write must land on the right block."""

    def __init__(self, store: "Store"):
        self._store = store
        super().__init__({**store.server(), **store.globals})

    def __setitem__(self, key: str, value: Any) -> None:
        super().__setitem__(key, value)
        target = self._store.globals if key in GLOBAL_KEYS else self._store.server()
        target[key] = value

    def update(self, other=(), **kw) -> None:  # type: ignore[override]
        for k, v in dict(other, **kw).items():
            self[k] = v

    def setdefault(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key: str, *default: Any) -> Any:  # type: ignore[override]
        (self._store.globals if key in GLOBAL_KEYS else self._store.server()).pop(key, None)
        return super().pop(key, *default)


def _upgrade_servers(data: dict[str, Any], had_servers: bool = False) -> None:
    """A config written before multiple servers: its `lab` block becomes the first server."""
    lab = data.get("lab") or {}
    if had_servers:
        data["lab"] = {k: v for k, v in lab.items() if k in GLOBAL_KEYS} or {"log_level": "INFO", "status_interval_s": 60}
        return
    srv = blank_server(str(lab.get("name") or "my server"))
    for field in SERVER_KEYS:
        if lab.get(field) not in (None, ""):
            srv[field] = lab[field]
    data["servers"] = {MAIN: srv}
    data["lab"] = {k: lab.get(k, v) for k, v in {"log_level": "INFO", "status_interval_s": 60}.items()}


SECRET_HINTS = ("TOKEN", "SECRET", "KEY", "PASSWORD", "PASS", "API")


def is_secret_key(key: str) -> bool:
    k = key.upper()
    return any(h in k for h in SECRET_HINTS) and not k.endswith(("_ID", "_IDS", "_URL", "_ENABLED", "_NAME"))

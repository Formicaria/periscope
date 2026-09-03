"""v2 service contract.

A *service* is one integration (proxmox, sonarr, github, …). It declares typed settings (rendered by the
web UI and validated once), an optional `check()` for the UI's Test button, and a `build()` that wires its
clients and cogs onto a `ServiceBot` — a per-service facade over the shared Discord *presence* that offers
exactly the surface v1 bots used (`settings`, `state`, `alerts`, `webhook`, `tree`, `get_channel_safe`,
`load_extension`, `add_cog`, …). v1 cogs therefore run unchanged; several services can share one presence.
"""

from __future__ import annotations

import importlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

import discord
from discord.ext import commands

from .alerts import AlertRouter
from .config import Settings
from .state import JsonState, NamespacedState

if TYPE_CHECKING:
    from .presence import Presence
    from .webhook import WebhookServer

log = logging.getLogger(__name__)

SettingType = str  # "str" | "secret" | "int" | "bool" | "url" | "channel" | "role" | "choice" | "list"


@dataclass
class Setting:
    key: str
    label: str = ""
    type: SettingType = "str"
    default: str = ""
    required: bool = False
    help: str = ""
    choices: list[str] = field(default_factory=list)
    group: str = ""  # section header in the UI

    def __post_init__(self) -> None:
        if not self.label:
            self.label = self.key.replace("_", " ").title()


# Shared keys every service understands (rendered once under "Discord" in the UI, overridable per service).
SHARED_SETTINGS: list[Setting] = [
    Setting("STATUS_CHANNEL_ID", "Status board channel", "channel", help="Where this service pins its live board"),
    Setting("ALERT_CHANNEL_ID", "Alert channel", "channel", help="Where this service posts alerts"),
    Setting("ALERT_ROLE_ID", "Role pinged on CRITICAL", "role"),
    Setting("STATUS_INTERVAL_S", "Board refresh (seconds)", "int", "60"),
]

CheckResult = tuple[bool, str]
CheckFn = Callable[[dict[str, str]], Awaitable[CheckResult]]
BuildFn = Callable[["ServiceBot"], Awaitable[None]]


@dataclass
class ServiceSpec:
    name: str                      # config key + CLI name, e.g. "sonarr"
    title: str                     # "Sonarr"
    description: str
    group: str                     # "infra" | "media" | "dev"
    settings: list[Setting]
    build: BuildFn                 # wire clients + cogs onto the ServiceBot
    check: CheckFn | None = None   # verify credentials for the UI's Test button
    slash: str = ""                # "/sonarr" — informational
    webhook_paths: list[str] = field(default_factory=list)
    default_presence: str = "default"
    needs_webhook: bool = False
    # gateway intents beyond discord.Intents.default() this service needs, by discord.Intents flag name
    # (e.g. ["members", "message_content"]); the runtime unions them per presence before it connects
    intents: list[str] = field(default_factory=list)

    def setting(self, key: str) -> Setting | None:
        return next((s for s in self.settings if s.key == key), None)

    def required_missing(self, env: dict[str, str]) -> list[str]:
        return [s.key for s in self.settings if s.required and not env.get(s.key)]


def settings_from_example(path: str | Path, *, required: tuple[str, ...] = (), skip: tuple[str, ...] = ()) -> list[Setting]:
    """Derive a Setting list from a v1 `.env.example` (KEY=default  # help). Sections (# ---- x ----) become groups.
    Comment lines directly above a key are its help when the line carries none inline (the web UI shows it)."""
    out: list[Setting] = []
    group = ""
    pending: list[str] = []
    shared = {s.key for s in SHARED_SETTINGS} | {"DISCORD_TOKEN", "LAB_NAME", "LAB_COLOR", "GUILD_ID", "ADMIN_ROLE_IDS",
                                                  "DATA_DIR", "LOG_LEVEL", "WEBHOOK_HOST", "WEBHOOK_PORT", "WEBHOOK_SECRET"}
    for line in Path(path).read_text().splitlines():
        m_group = re.match(r"^#\s*-{2,}\s*(.+?)\s*-{2,}\s*$", line)
        if m_group:
            group = m_group.group(1).strip()
            pending = []
            continue
        if line.startswith("#"):
            pending.append(line.lstrip("#").strip())
            continue
        m = re.match(r"^([A-Z0-9_]+)=(.*)$", line)
        if not m:
            pending = []
            continue
        above, pending = " ".join(x for x in pending if x), []
        key, rest = m.group(1), m.group(2)
        if key in shared or key in skip:
            continue
        value, _, comment = rest.partition("  #")
        value, comment = value.strip(), comment.strip() or above
        up = key.upper()
        if any(h in up for h in ("TOKEN", "SECRET", "PASS")) or up.endswith(("_KEY", "_API_KEY")):
            typ = "secret"
        elif value.lower() in ("true", "false"):
            typ = "bool"
        elif value.isdigit():
            typ = "int"
        elif up.endswith("_URL"):
            typ = "url"
        elif up.endswith("_CHANNEL_ID"):
            typ = "channel"
        elif up.endswith("_ROLE_ID"):
            typ = "role"
        elif up.endswith("_IDS") or up.endswith("_MAP") or up.endswith("_EVENTS"):
            typ = "list"
        else:
            typ = "str"
        out.append(Setting(key, type=typ, default=value if typ != "secret" else "", required=key in required,
                           help=comment, group=group))
    return out


class ServiceBot:
    """What a service's cogs see as `bot`. Same attribute surface as v1's LabBot, backed by a shared presence."""

    def __init__(self, spec: ServiceSpec, presence: "Presence", settings: Settings, env: dict[str, str],
                 state: JsonState, webhook: "WebhookServer | None"):
        self.spec = spec
        self.name = spec.name
        self.presence = presence
        self.settings = settings
        self.env = env
        self.lab_name = settings.lab_name
        self.state: NamespacedState = state.namespace(f"svc:{spec.name}")
        # one slot every service on this presence can see — for boards several services render together
        self.shared_state: NamespacedState = state.namespace(f"presence:{presence.name}")
        self.alerts = AlertRouter(self)
        self.webhook = webhook
        self.description = spec.description
        self._cogs: list[commands.Cog] = []
        self.healthy = True
        self.built = False
        self.last_error: str | None = None

    @property
    def guild_id(self) -> int | None:
        """The server this service works in: its own GUILD_ID (the lab's unless overridden, e.g. plexrequests)."""
        raw = str(self.env.get("GUILD_ID") or "").strip()
        if raw.isdigit():
            return int(raw)
        return self.presence.guild_id

    # ----- pass-throughs to the presence ---------------------------------------------------
    @property
    def tree(self):
        return self.presence.tree

    @property
    def user(self):
        return self.presence.user

    @property
    def loop(self):
        return self.presence.loop

    @property
    def guilds(self):
        return self.presence.guilds

    def get_channel(self, cid: int):
        return self.presence.get_channel(cid)

    def get_guild(self, gid: int):
        return self.presence.get_guild(gid)

    async def fetch_channel(self, cid: int):
        return await self.presence.fetch_channel(cid)

    async def fetch_guild(self, gid: int):
        return await self.presence.fetch_guild(gid)

    async def get_channel_safe(self, channel_id: int) -> discord.abc.Messageable | None:
        return await self.presence.get_channel_safe(channel_id)

    async def wait_until_ready(self) -> None:
        await self.presence.wait_until_ready()

    def is_ready(self) -> bool:
        return self.presence.is_ready()

    async def change_presence(self, **kw: Any) -> None:  # services must not fight over the shared status
        return None

    def is_admin(self, user: discord.abc.User | discord.Member) -> bool:
        if not self.settings.admin_role_ids:
            perms = getattr(user, "guild_permissions", None)
            return bool(perms and perms.administrator)
        roles = getattr(user, "roles", [])
        return any(r.id in self.settings.admin_role_ids for r in roles)

    # ----- cogs ---------------------------------------------------------------------------
    async def load_extension(self, path: str) -> None:
        module = importlib.import_module(path)
        setup = getattr(module, "setup", None)
        if setup is None:
            raise RuntimeError(f"{path} has no setup()")
        await setup(self)
        log.info("[%s] loaded %s", self.name, path)

    async def add_cog(self, cog: commands.Cog, **kw: Any) -> None:
        # several services may ship a cog with the same class name → namespace it on the presence
        cog.__cog_name__ = f"{self.name}:{cog.qualified_name}"  # type: ignore[attr-defined]
        await self.presence.add_cog(cog, override=True)
        self._cogs.append(cog)

    def get_cog(self, name: str) -> commands.Cog | None:
        return self.presence.get_cog(f"{self.name}:{name}")

    async def unload(self) -> None:
        for cog in self._cogs:
            try:
                await self.presence.remove_cog(cog.qualified_name)
            except Exception:  # noqa: BLE001
                log.debug("[%s] remove_cog failed", self.name, exc_info=True)
        self._cogs.clear()

    def __getattr__(self, item: str):
        # anything else a cog asks of "bot" (add_view, wait_for, http, application_id, …) is the presence's
        if item.startswith("_") or item in ("presence",):
            raise AttributeError(item)
        return getattr(self.presence, item)

    def __repr__(self) -> str:
        return f"<ServiceBot {self.name} via {self.presence.name}>"

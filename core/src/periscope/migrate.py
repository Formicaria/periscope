"""Import v1 configuration (bots/*/.env, /opt/displexia/.env) into the v2 store. Idempotent, non-destructive."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .state import JsonState
from .store import Store

log = logging.getLogger(__name__)

LAB_FROM_ENV = {"LAB_NAME": "name", "LAB_COLOR": "color", "GUILD_ID": "guild_id", "STATUS_CHANNEL_ID": "status_channel_id",
                "ALERT_CHANNEL_ID": "alert_channel_id", "ALERT_ROLE_ID": "alert_role_id", "ADMIN_ROLE_IDS": "admin_role_ids",
                "LOG_LEVEL": "log_level", "STATUS_INTERVAL_S": "status_interval_s"}
DROP = {"DISCORD_TOKEN", "DATA_DIR", "WEBHOOK_HOST", "WEBHOOK_PORT", "LAB_NAME", "LAB_COLOR", "GUILD_ID", "LOG_LEVEL",
        "STATUS_INTERVAL_S", "ADMIN_ROLE_IDS"}
# where the standalone v1 Plex bot (now the `plexrequests` service) kept its .env, state.json and stats.json;
# the service imports that state on its first build from here
PLEXREQUESTS_LEGACY_DIR = Path("/opt/displexia")

# v1 bot dir -> how its keys fan out into v2 services. A prefix tuple claims keys; "*" takes the rest.
SPLIT: dict[str, list[tuple[str, tuple[str, ...]]]] = {
    "proxmox": [("proxmox", ("*",))],
    "unifi": [("unifi", ("*",))],
    "github": [("github", ("*",))],
    "prometheus": [("prometheus", ("PROM_",)), ("alertmanager", ("ALERTMANAGER_", "AM_")), ("grafana", ("GRAFANA_",))],
    "arr": [("sonarr", ("SONARR_",)), ("radarr", ("RADARR_",)), ("lidarr", ("LIDARR_",)), ("prowlarr", ("PROWLARR_",)),
            ("qbittorrent", ("QBIT_",)), ("sabnzbd", ("SABNZBD_",)), ("plex", ("PLEX_",)), ("jellyfin", ("JELLYFIN_",))],
}
# keys every child of a split inherits (routing/behaviour shared by the old bot)
SPLIT_SHARED = {"arr": ("VERIFY_SSL", "MEDIA_CHANNEL_ID", "ARR_QUEUE_STALL_MIN", "ALERT_CHANNEL_ID", "STATUS_CHANNEL_ID", "ALERT_ROLE_ID"),
                "prometheus": ("ALERT_CHANNEL_ID", "STATUS_CHANNEL_ID", "ALERT_ROLE_ID", "PROM_BASIC_USER", "PROM_BASIC_PASS", "VERIFY_SSL")}


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        m = re.match(r"^\s*([A-Z0-9_]+)=(.*)$", line)
        if not m:
            continue
        v = m.group(2).split("  #", 1)[0].strip()
        if v.startswith("#"):
            v = ""
        out[m.group(1)] = v
    return out


def _apply_lab(store: Store, env: dict[str, str]) -> None:
    lab = store.lab
    for key, field in LAB_FROM_ENV.items():
        v = env.get(key, "")
        if not v:
            continue
        if field == "admin_role_ids":
            lab[field] = [x.strip() for x in v.split(",") if x.strip()]
        elif field == "status_interval_s":
            lab[field] = int(v) if v.isdigit() else 60
        elif not lab.get(field) or lab.get(field) in ("lab", "my-lab", "my server", "5865F2"):
            lab[field] = v


def _service_env(env: dict[str, str], prefixes: tuple[str, ...], shared: tuple[str, ...], store: Store) -> dict[str, str]:
    lab_env = store.lab_env()
    out: dict[str, str] = {}
    for k, v in env.items():
        if k in DROP or not v:
            continue
        take = "*" in prefixes or any(k.startswith(p) for p in prefixes) or k in shared
        if not take:
            continue
        if k in lab_env and lab_env[k] == v:
            continue  # same as the lab default → no override needed
        out[k] = v
    return out


def migrate_state(root: Path, bot: str, env: dict[str, str], presence: str) -> int:
    """Carry a v1 bot's runtime state (the pinned board's message id, open alerts) into the v2 state file so the
    boards are edited in place instead of posted again. Returns the number of keys copied."""
    src = Path(env.get("DATA_DIR") or "data")
    if not src.is_absolute():
        src = root / "bots" / bot / src
    old = JsonState(src / "state.json")
    if not old._data:
        return 0
    new = JsonState(root / "data" / "state.json")
    copied = 0
    for key, value in old._data.items():
        if bot == "arr":
            # the media board is shared by every media service of the presence; per-app alerts do not map
            if key.startswith("board:"):
                target = f"presence:{presence}:{key}"
            else:
                continue
        else:
            target = f"svc:{bot}:{key}"
        if new.get(target) is None:
            new._data[target] = value
            copied += 1
    if copied:
        new.save()
        log.info("carried %d state entries of the v1 %s bot over (boards keep their messages)", copied, bot)
    return copied


def migrate_v1(store: Store, root: Path) -> list[str]:
    """Populate `store` from v1 files under `root`. Returns the list of services created."""
    created: list[str] = []
    bots_dir = root / "bots"
    envs: dict[str, dict[str, str]] = {}
    for d in sorted(bots_dir.glob("*/")) if bots_dir.exists() else []:
        env = read_env(d / ".env")
        if env.get("DISCORD_TOKEN"):
            envs[d.name] = env
    displexia = read_env(PLEXREQUESTS_LEGACY_DIR / ".env")

    # lab defaults from the first bot that has them
    for env in envs.values():
        _apply_lab(store, env)
    if not envs and displexia:
        _apply_lab(store, {"GUILD_ID": displexia.get("GUILD_ID", ""), "LAB_NAME": displexia.get("SERVER_NAME", "")})

    # shared webhook secret: the arr bot's (Sonarr/Radarr already point at it), else github's
    for pick in ("arr", "github", "prometheus"):
        if envs.get(pick, {}).get("WEBHOOK_SECRET"):
            store.webhook["secret"] = envs[pick]["WEBHOOK_SECRET"]
            break

    # one presence per v1 bot so nothing changes in Discord
    for bot, env in envs.items():
        store.presences[bot] = {"token": env["DISCORD_TOKEN"], "label": bot}
        try:
            migrate_state(root, bot, env, presence=bot)
        except Exception:  # noqa: BLE001 - state is a convenience; config must still migrate
            log.warning("could not carry the v1 %s bot's state over", bot, exc_info=True)
        for svc, prefixes in SPLIT.get(bot, [(bot, ("*",))]):
            svc_env = _service_env(env, prefixes, SPLIT_SHARED.get(bot, ()), store)
            # a split child with no keys of its own (e.g. LIDARR_URL empty) stays disabled but present
            has_own = "*" in prefixes or any(k.startswith(p) for k in svc_env for p in prefixes)
            if svc_env.get("WEBHOOK_SECRET") == store.webhook.get("secret"):
                svc_env.pop("WEBHOOK_SECRET", None)
            store.services[svc] = {"enabled": bool(has_own), "presence": bot, "env": svc_env}
            if has_own:
                created.append(svc)

    if displexia.get("DISCORD_TOKEN"):
        store.presences["plex"] = {"token": displexia["DISCORD_TOKEN"], "label": "plex"}
        env = {k: v for k, v in displexia.items() if k not in ("DISCORD_TOKEN",) and v}
        env.setdefault("PLEXREQ_GUILD_ID", displexia.get("GUILD_ID", ""))
        # the standalone Plex bot usually lived in its own Discord server — keep it as a second server here
        server = store.default_server()
        gid = str(displexia.get("GUILD_ID") or "").strip()
        if gid and gid != str(store.server().get("guild_id") or "").strip():
            server = "plex"
            srv = store.add_server(server, str(displexia.get("SERVER_NAME") or "Plex").strip() or "Plex")
            srv["guild_id"] = gid
            log.info("the Plex bot's Discord server (%s) was added as a second server", gid)
        store.services["plexrequests"] = {"enabled": True, "presence": "plex", "server": server, "env": env}
        created.append("plexrequests")

    # one identity per old bot is the whole picture — an empty shared `default` would only show as "missing token"
    store.tidy()
    return created

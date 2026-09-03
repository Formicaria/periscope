"""v2 service definition for `plexrequests`: Plex invites + media requests + status board + new-on-Plex feed +
auto-revoke, replacing the standalone Plex Discord bot. Same env keys as that bot's .env, so the v1 migration
copies its configuration verbatim (plus PLEXREQ_GUILD_ID, because the Plex server may be a different Discord
server than the lab)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from periscope import ServiceBot, ServiceSpec, env_scope, settings_from_example
from periscope.http import HttpClient, HttpError
from periscope.migrate import PLEXREQUESTS_LEGACY_DIR

from . import messages  # noqa: F401  — importing messages registers the plexrequests.* message kinds
from .arr import ArrClient
from .backend import RequestBackend, select_backend
from .config import BACKENDS, PlexRequestsSettings
from .context import PlexRequests
from .plex import PlexGateway
from .seerr import SeerrClient

log = logging.getLogger(__name__)

EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
TOKEN_HELPER = "bots/plexrequests/scripts/plex_token.py"

COGS = [
    "periscope_plexrequests.cogs.invites",
    "periscope_plexrequests.cogs.requests",
    "periscope_plexrequests.cogs.board",
    "periscope_plexrequests.cogs.newonplex",
    "periscope_plexrequests.cogs.revoke",
    "periscope_plexrequests.cogs.stats",
]
INTENTS = ["members", "message_content"]   # auto-revoke / role-loss detection, typed emails and titles

# state keys the standalone bot kept in state.json, imported once into the service's namespaced state
LEGACY_STATE_KEYS = ("invite_message_id", "request_message_id", "status_message_id", "emails", "watches",
                     "requests", "plex_seen")


def make_settings():
    out = settings_from_example(EXAMPLE, required=("PLEX_URL", "PLEX_TOKEN", "CHANNEL_ID"))
    by_key = {s.key: s for s in out}
    by_key["REQUEST_BACKEND"].type = "choice"
    by_key["REQUEST_BACKEND"].choices = list(BACKENDS)
    by_key["CHANNEL_ID"].type = "channel"
    by_key["AUTO_REVOKE"].type = "bool"
    by_key["PLEXREQ_GUILD_ID"].label = "Plex Discord server id"
    return out


# ----- legacy state -----------------------------------------------------------------------------------

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def import_legacy_state(bot: ServiceBot, legacy_dir: Path | None = None) -> dict[str, Any]:
    """Copy the standalone bot's state.json (sticky message ids, invitee emails, availability watches, request
    history, new-on-Plex baseline) and stats.json (usage counters) into this service's state, once. Keys the
    service already has are left alone. Returns a summary of what was imported."""
    state = bot.state
    if state.get("legacy_imported"):
        return {"imported": False, "reason": "already imported"}
    root = legacy_dir or PLEXREQUESTS_LEGACY_DIR
    legacy = _read_json(root / "state.json")
    stats = _read_json(root / "stats.json")
    if legacy is None and stats is None:
        return {"imported": False, "reason": f"nothing to import in {root}"}
    copied: list[str] = []
    for key in LEGACY_STATE_KEYS:
        if legacy and legacy.get(key) not in (None, "", [], {}) and state.get(key) is None:
            state.set(key, legacy[key])
            copied.append(key)
    if stats and stats.get("totals") and state.get("stats") is None:
        state.set("stats", stats)
        copied.append("stats")
    state.set("legacy_imported", True)
    state.set("legacy_imported_from", str(root))
    log.info("[%s] imported v1 state from %s: %s", bot.name, root, ", ".join(copied) or "nothing new")
    return {"imported": True, "from": str(root), "keys": copied}


# ----- build -------------------------------------------------------------------------------------------

async def build(bot: ServiceBot) -> None:
    with env_scope(bot.env):
        cfg = PlexRequestsSettings.from_env()
    plex = PlexGateway(cfg.plex_url, cfg.plex_token, cfg.libraries)
    seerr = SeerrClient(cfg.overseerr_url, cfg.overseerr_api_key) if cfg.has_seerr else None
    radarr = ArrClient("radarr", cfg.radarr_url, cfg.radarr_api_key, cfg.radarr_profile, cfg.radarr_root,
                       cfg.radarr_fallback_profile, cfg.fallback_before_year) if cfg.has_radarr else None
    sonarr = ArrClient("sonarr", cfg.sonarr_url, cfg.sonarr_api_key, cfg.sonarr_profile, cfg.sonarr_root,
                       cfg.sonarr_fallback_profile, cfg.fallback_before_year) if cfg.has_sonarr else None
    backend = RequestBackend(cfg.request_backend, seerr, radarr, sonarr)
    ctx = PlexRequests(bot, cfg, plex, backend)
    bot.plexreq = ctx
    bot.plexreq_cfg = cfg
    import_legacy_state(bot)
    bot.tree.add_command(ctx.group, guild=ctx.command_guild, override=True)
    for path in COGS:
        await bot.load_extension(path)
    log.info("[%s] built: backend=%s guild=%s invite_channel=%s requests_channel=%s", bot.name, backend.describe(),
             cfg.guild_id, cfg.channel_id or cfg.channel_name, cfg.requests_channel_id or "off")
    log.info("[%s] needs the Server Members + Message Content privileged intents enabled for presence %r's application "
             "(Developer Portal → Bot → Privileged Gateway Intents) — the presence will not connect without them",
             bot.name, bot.presence.name)


# ----- check -------------------------------------------------------------------------------------------

async def _plex_check(url: str, token: str) -> tuple[bool, str]:
    client = HttpClient(url, headers={"X-Plex-Token": token, "Accept": "application/json"}, verify_ssl=False, timeout_s=10)
    try:
        d = await client.get_json(f"/identity?X-Plex-Token={quote(token)}")
        mc = (d.get("MediaContainer") or {}) if isinstance(d, dict) else {}
        try:                                  # /identity answers without a token; sessions prove it
            await client.get_json("/status/sessions")
        except HttpError as e:
            if e.status in (401, 403):
                return False, (f"Plex {mc.get('version', '?')} reachable but rejected the token ({e.status}): "
                               f"check PLEX_TOKEN (or fetch a new one with {TOKEN_HELPER})")
            raise
        return True, f"Plex {mc.get('version', '?')} answered"
    except HttpError as e:
        return False, f"Plex answered {e.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"Plex unreachable: {e}"
    finally:
        await client.close()


async def _arr_check(title: str, url: str, key: str) -> tuple[bool, str]:
    client = HttpClient(url, headers={"X-Api-Key": key, "Accept": "application/json"}, verify_ssl=False, timeout_s=10)
    try:
        d = await client.get_json("/api/v3/system/status")
        d = d if isinstance(d, dict) else {}
        return True, f"{title} {d.get('version', '?')}"
    except HttpError as e:
        if e.status in (401, 403):
            return False, f"{title} rejected the API key ({e.status})"
        return False, f"{title} answered {e.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"{title} unreachable: {e}"
    finally:
        await client.close()


async def _seerr_check(url: str, key: str) -> tuple[bool, str]:
    client = HttpClient(url, headers={"X-Api-Key": key, "Accept": "application/json"}, verify_ssl=False, timeout_s=10)
    try:
        d = await client.get_json("/api/v1/status")
        version = d.get("version", "?") if isinstance(d, dict) else "?"
        try:                                  # /status is public; /auth/me proves the key
            await client.get_json("/api/v1/auth/me")
        except HttpError as e:
            if e.status in (401, 403):
                return False, f"Seerr {version} rejected the API key ({e.status})"
            raise
        return True, f"Seerr {version}"
    except HttpError as e:
        return False, f"Seerr answered {e.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"Seerr unreachable: {e}"
    finally:
        await client.close()


async def check(env: dict[str, str]) -> tuple[bool, str]:
    url, token = env.get("PLEX_URL", "").strip().rstrip("/"), env.get("PLEX_TOKEN", "").strip()
    if not url:
        return False, "PLEX_URL is required"
    if not token:
        return False, f"PLEX_TOKEN is required — run {TOKEN_HELPER} to fetch one with the plex.tv/link flow"
    ok, msg = await _plex_check(url, token)
    if not ok:
        return False, msg
    parts, failed = [msg], []
    seerr_url, seerr_key = env.get("OVERSEERR_URL", "").strip().rstrip("/"), env.get("OVERSEERR_API_KEY", "").strip()
    if seerr_url and seerr_key:
        ok, m = await _seerr_check(seerr_url, seerr_key)
        (parts if ok else failed).append(m)
    for title, ukey, kkey in (("Radarr", "RADARR_URL", "RADARR_API_KEY"), ("Sonarr", "SONARR_URL", "SONARR_API_KEY")):
        u, k = env.get(ukey, "").strip().rstrip("/"), env.get(kkey, "").strip()
        if u and k:
            ok, m = await _arr_check(title, u, k)
            (parts if ok else failed).append(m)
    backend = select_backend(env.get("REQUEST_BACKEND", "auto"), has_seerr=bool(seerr_url and seerr_key),
                             has_radarr=bool(env.get("RADARR_URL") and env.get("RADARR_API_KEY")),
                             has_sonarr=bool(env.get("SONARR_URL") and env.get("SONARR_API_KEY")))
    parts.append(f"requests via {backend}" if backend else "requests off (no Seerr or Radarr/Sonarr configured)")
    if failed:
        return False, " · ".join(parts) + " · FAILED: " + "; ".join(failed)
    return True, " · ".join(parts)


SERVICES = [
    ServiceSpec(
        name="plexrequests",
        title="Plex requests",
        description="Plex library invites (button, typed email, /plexinvite), movie/TV requests via Overseerr or "
                    "Radarr/Sonarr with availability cards, live status board, new-on-Plex feed, auto-revoke, "
                    "usage stats.",
        group="media",
        settings=make_settings(),
        build=build,
        check=check,
        slash="/requests",
        intents=INTENTS,
    )
]

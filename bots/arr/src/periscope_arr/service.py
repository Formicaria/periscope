"""v2 service definitions: the v1 arr bot split into one service per app.

sonarr · radarr · lidarr · prowlarr · qbittorrent · sabnzbd · plex · jellyfin — each with its own settings,
credentials check, slash group and (for the *arr apps) webhook path. Services built on the same presence share
one MediaHub (`presence.media_hub`): one "Media stack" board, one queue/stall poller, one set of webhook routes.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from periscope import ServiceBot, ServiceSpec, Setting, env_scope, settings_from_example
from periscope.http import HttpClient, HttpError

from .config import ARR_API_VERSION, ARR_APPS, SERVICE_KEYS, ArrSettings, _norm_url
from .hub import TITLES, MediaHub

EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

DESCRIPTIONS = {
    "sonarr": "TV series: queue with progress, calendar, search, health and stalled-download alerts; Grab / Import / "
              "Upgrade / Health webhook feed.",
    "radarr": "Movies: queue with progress, calendar, search, health and stalled-download alerts; Grab / Import / "
              "Upgrade / Health webhook feed.",
    "lidarr": "Music: queue with progress, calendar, search, health and stalled-download alerts; webhook feed.",
    "prowlarr": "Indexer health and failing-indexer status; Health / ApplicationUpdate webhook feed.",
    "qbittorrent": "Transfer speeds and active torrents on the Media stack board, /qbittorrent status.",
    "sabnzbd": "Queue size and speed on the Media stack board, /sabnzbd status.",
    "plex": "Now playing (user, title, transcode) on the Media stack board, /plex nowplaying.",
    "jellyfin": "Now playing on the Media stack board, /jellyfin nowplaying.",
}
# shared behaviour keys each service also understands (the hub reads them from whichever service owns an app)
SHARED_FOR = {
    "sonarr": ("VERIFY_SSL", "MEDIA_CHANNEL_ID", "ARR_QUEUE_STALL_MIN"),
    "radarr": ("VERIFY_SSL", "MEDIA_CHANNEL_ID", "ARR_QUEUE_STALL_MIN"),
    "lidarr": ("VERIFY_SSL", "MEDIA_CHANNEL_ID", "ARR_QUEUE_STALL_MIN"),
    "prowlarr": ("VERIFY_SSL", "MEDIA_CHANNEL_ID"),
    "qbittorrent": ("VERIFY_SSL",),
    "sabnzbd": ("VERIFY_SSL",),
    "plex": ("VERIFY_SSL",),
    "jellyfin": ("VERIFY_SSL",),
}
REQUIRED = {name: keys[:1] if name == "qbittorrent" else keys[:2] for name, keys in SERVICE_KEYS.items()}


def _settings(name: str) -> list[Setting]:
    """This service's own keys from the v1 `.env.example` plus the shared behaviour keys it uses."""
    by_key = {s.key: s for s in settings_from_example(EXAMPLE, required=REQUIRED[name])}
    out: list[Setting] = []
    for key in (*SERVICE_KEYS[name], *SHARED_FOR[name]):
        s = by_key.get(key)
        if s is None:
            continue
        s.group = TITLES[name] if key in SERVICE_KEYS[name] else "Media stack (shared)"
        out.append(s)
    return out


def _build(name: str):
    async def build(bot: ServiceBot) -> None:
        with env_scope(bot.env):
            cfg = ArrSettings.from_env(only=name)
        hub = MediaHub.for_bot(bot, cfg)
        await hub.register(bot, name, cfg)

    build.__name__ = f"build_{name}"
    return build


# ----- checks ----------------------------------------------------------------------------------------

def _verify(env: dict[str, str]) -> bool:
    return env.get("VERIFY_SSL", "true").strip().lower() not in ("false", "0", "no", "off")


def _need(env: dict[str, str], *keys: str) -> tuple[str, ...] | None:
    vals = tuple(env.get(k, "").strip() for k in keys)
    return vals if all(vals) else None


def _check_arr(app: str):
    url_key, key_key = SERVICE_KEYS[app]
    title = TITLES[app]

    async def check(env: dict[str, str]) -> tuple[bool, str]:
        got = _need(env, url_key, key_key)
        if not got:
            return False, f"{url_key} and {key_key} are required"
        url, key = _norm_url(got[0]), got[1]
        client = HttpClient(url, headers={"X-Api-Key": key, "Accept": "application/json"},
                            verify_ssl=_verify(env), timeout_s=10)
        try:
            d = await client.get_json(f"/api/{ARR_API_VERSION[app]}/system/status")
            d = d if isinstance(d, dict) else {}
            return True, f"{d.get('appName') or title} {d.get('version', '?')} answered"
        except HttpError as e:
            if e.status in (401, 403):
                return False, f"{title} rejected the API key ({e.status}): check {key_key}"
            return False, f"{title} answered {e.status}"
        except Exception as e:  # noqa: BLE001
            return False, f"unreachable: {e}"
        finally:
            await client.close()

    check.__name__ = f"check_{app}"
    return check


async def check_qbittorrent(env: dict[str, str]) -> tuple[bool, str]:
    """API key (Bearer, qBittorrent >= 5.2) when set, else the cookie login the client falls back to."""
    got = _need(env, "QBIT_URL")
    if not got:
        return False, "QBIT_URL is required"
    url = _norm_url(got[0])
    key, user, pw = env.get("QBIT_API_KEY", "").strip(), env.get("QBIT_USER", ""), env.get("QBIT_PASS", "")
    headers = {"Referer": url}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    client = HttpClient(url, headers=headers, verify_ssl=_verify(env), timeout_s=10, unsafe_cookies=True)
    try:
        if not key:
            resp = await client.request("POST", "/api/v2/auth/login", data={"username": user, "password": pw})
            async with resp:
                text = (await resp.text()).strip()
            if text != "Ok.":
                return False, "qBittorrent login failed: check QBIT_USER / QBIT_PASS (or set QBIT_API_KEY on >= 5.2)"
        version = (await client.get_bytes("/api/v2/app/version")).decode(errors="replace").strip()
        return True, f"qBittorrent {version} answered ({'API key' if key else 'user/password'})"
    except HttpError as e:
        if e.status in (401, 403):
            what = ("QBIT_API_KEY (Options → Web UI → API keys, needs qBittorrent >= 5.2)" if key
                    else "QBIT_USER / QBIT_PASS")
            return False, f"qBittorrent rejected the credentials ({e.status}): check {what}"
        return False, f"qBittorrent answered {e.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    finally:
        await client.close()


async def check_sabnzbd(env: dict[str, str]) -> tuple[bool, str]:
    got = _need(env, "SABNZBD_URL", "SABNZBD_API_KEY")
    if not got:
        return False, "SABNZBD_URL and SABNZBD_API_KEY are required"
    url, key = _norm_url(got[0]), got[1]
    client = HttpClient(url, verify_ssl=_verify(env), timeout_s=10)
    try:
        v = await client.get_json("/api", params={"mode": "version", "output": "json", "apikey": key})
        version = v.get("version", "?") if isinstance(v, dict) else "?"
        # mode=version is open to everyone; the queue proves the key
        q = await client.get_json("/api", params={"mode": "queue", "output": "json", "apikey": key, "limit": "1"})
        if isinstance(q, dict) and q.get("error"):
            return False, f"SABnzbd {version} rejected the API key: {q['error']}"
        return True, f"SABnzbd {version} answered"
    except HttpError as e:
        return False, f"SABnzbd answered {e.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    finally:
        await client.close()


async def check_plex(env: dict[str, str]) -> tuple[bool, str]:
    got = _need(env, "PLEX_URL", "PLEX_TOKEN")
    if not got:
        return False, "PLEX_URL and PLEX_TOKEN are required"
    url, token = _norm_url(got[0]), got[1]
    client = HttpClient(url, headers={"X-Plex-Token": token, "Accept": "application/json"},
                        verify_ssl=_verify(env), timeout_s=10)
    try:
        d = await client.get_json(f"/identity?X-Plex-Token={quote(token)}")
        mc = (d.get("MediaContainer") or {}) if isinstance(d, dict) else {}
        # /identity answers without a token; the sessions call is what the board needs and proves the token
        try:
            await client.get_json("/status/sessions")
        except HttpError as e:
            if e.status in (401, 403):
                return False, (f"Plex {mc.get('version', '?')} reachable but rejected the token ({e.status}): "
                               "check PLEX_TOKEN")
            raise
        return True, f"Plex {mc.get('version', '?')} answered"
    except HttpError as e:
        return False, f"Plex answered {e.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    finally:
        await client.close()


async def check_jellyfin(env: dict[str, str]) -> tuple[bool, str]:
    got = _need(env, "JELLYFIN_URL", "JELLYFIN_API_KEY")
    if not got:
        return False, "JELLYFIN_URL and JELLYFIN_API_KEY are required"
    url, key = _norm_url(got[0]), got[1]
    client = HttpClient(url, headers={"X-Emby-Token": key, "Accept": "application/json"},
                        verify_ssl=_verify(env), timeout_s=10)
    try:
        d = await client.get_json("/System/Info")
        d = d if isinstance(d, dict) else {}
        name = f" ({d['ServerName']})" if d.get("ServerName") else ""
        return True, f"Jellyfin {d.get('Version', '?')}{name} answered"
    except HttpError as e:
        if e.status in (401, 403):
            return False, f"Jellyfin rejected the API key ({e.status}): check JELLYFIN_API_KEY"
        return False, f"Jellyfin answered {e.status}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    finally:
        await client.close()


CHECKS = {**{app: _check_arr(app) for app in ARR_APPS}, "qbittorrent": check_qbittorrent, "sabnzbd": check_sabnzbd,
          "plex": check_plex, "jellyfin": check_jellyfin}

SERVICES = [
    ServiceSpec(
        name=name,
        title=TITLES[name],
        description=DESCRIPTIONS[name],
        group="media",
        settings=_settings(name),
        build=_build(name),
        check=CHECKS[name],
        slash=f"/{name}",
        webhook_paths=[f"/{name}"] if name in ARR_APPS else [],
        needs_webhook=name in ARR_APPS,
    )
    for name in SERVICE_KEYS
]

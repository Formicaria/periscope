"""v2 service definition for UniFi Network: same wiring as UnifiBot.__init__, on a shared presence."""

from __future__ import annotations

from pathlib import Path

from periscope import ServiceBot, ServiceSpec, Setting, env_scope, settings_from_example
from periscope.http import HttpClient, HttpError

from .bot import COGS
from .client import UnifiClient
from .config import UnifiConfig

EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"

# the v1 example documents each key on the line above it, which settings_from_example does not read
HELP = {
    "UNIFI_URL": "UniFi OS console (UDM / UCG / Cloud Key): https://192.168.1.1 · self-hosted: https://unifi.lan:8443",
    "UNIFI_USER": "Local admin (not a Ubiquiti SSO account); read-only is enough for monitoring",
    "UNIFI_PASS": "Password of that local admin",
    "UNIFI_SITE": "Site name as it appears in the URL (…/network/site/<name>/…), almost always default",
    "UNIFI_IS_UNIFI_OS": "true = UniFi OS console (login /api/auth/login) · "
                         "false = self-hosted controller (/api/login)",
    "VERIFY_SSL": "Controllers almost always use a self-signed certificate; keep false unless you installed a real one",
    "UNIFI_ALERT_NEW_CLIENTS": "Post an INFO embed when a never-seen (or long-unseen) client joins",
    "UNIFI_WAN_LATENCY_WARN_MS": "WARNING alert when WAN latency exceeds this for 3 consecutive polls",
    "UNIFI_DEVICE_CPU_WARN": "WARNING alert when a device's CPU exceeds this % for 3 consecutive polls",
    "UNIFI_KNOWN_CLIENTS_TTL_DAYS": "A client not seen for this many days counts as new again when it reappears",
}


def _settings() -> list[Setting]:
    out = settings_from_example(EXAMPLE, required=("UNIFI_URL", "UNIFI_USER", "UNIFI_PASS"))
    for s in out:
        s.help = s.help or HELP.get(s.key, "")
    return out


async def build(bot: ServiceBot) -> None:
    with env_scope(bot.env):
        cfg = UnifiConfig.from_env()
    bot.cfg = cfg
    bot.unifi = UnifiClient(cfg)
    for path in COGS:
        await bot.load_extension(path)


async def check(env: dict[str, str]) -> tuple[bool, str]:
    """Log in the way the wizard does: POST /api/auth/login (UniFi OS) or /api/login (self-hosted)."""
    url, user, pw = env.get("UNIFI_URL", "").strip().rstrip("/"), env.get("UNIFI_USER", ""), env.get("UNIFI_PASS", "")
    if not (url and user and pw):
        return False, "UNIFI_URL, UNIFI_USER and UNIFI_PASS are required"
    if not url.startswith(("http://", "https://")):
        return False, "UNIFI_URL must start with http:// or https://"
    is_os = env.get("UNIFI_IS_UNIFI_OS", "true").strip().lower() in ("1", "true", "yes", "on")
    verify = env.get("VERIFY_SSL", "false").strip().lower() in ("1", "true", "yes", "on")
    path = "/api/auth/login" if is_os else "/api/login"
    client = HttpClient(url, verify_ssl=verify, timeout_s=10)
    try:
        resp = await client.request("POST", path, json={"username": user, "password": pw})
        async with resp:
            await resp.read()
            got_cookie = bool(resp.cookies)
        kind = "UniFi OS console" if is_os else "self-hosted controller"
        return True, f"UniFi login works ({kind})" + ("" if got_cookie else " — no session cookie returned")
    except HttpError as e:
        if e.status in (400, 401, 403):
            return False, f"UniFi rejected the login ({e.status}): check UNIFI_USER / UNIFI_PASS and UNIFI_IS_UNIFI_OS"
        return False, f"UniFi answered {e.status} on {path}"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    finally:
        await client.close()


SERVICES = [
    ServiceSpec(
        name="unifi",
        title="UniFi Network",
        description="Site status board (WAN, LAN/WLAN, clients, devices), offline / latency / CPU / new-client alerts, "
                    "/unifi clients, devices, kick, block, restart.",
        group="infra",
        settings=_settings(),
        build=build,
        check=check,
        slash="/unifi",
    )
]

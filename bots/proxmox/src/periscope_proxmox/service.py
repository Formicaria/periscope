"""v2 service definition for Proxmox VE — the reference for every other service module."""

from __future__ import annotations

from pathlib import Path

from discord import app_commands
from periscope import ServiceBot, ServiceSpec, env_scope, settings_from_example
from periscope.http import HttpClient, HttpError

from .bot import COGS
from .client import PveClient
from .config import PveSettings

EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"


async def build(bot: ServiceBot) -> None:
    """Same wiring ProxmoxBot.__init__ did, on the shared presence."""
    with env_scope(bot.env):
        cfg = PveSettings.from_env()
    bot.pve_cfg = cfg
    bot.pve = PveClient(cfg)
    group = app_commands.Group(name="pve", description="Proxmox VE monitoring and control")
    bot.pve_group = group
    bot.tree.add_command(group, override=True)

    def register_commands(*cmds: app_commands.Command) -> None:
        for cmd in cmds:
            group.add_command(cmd, override=True)

    def unregister_commands(*names: str) -> None:
        for n in names:
            group.remove_command(n)

    bot.register_commands = register_commands
    bot.unregister_commands = unregister_commands
    for path in COGS:
        await bot.load_extension(path)


async def check(env: dict[str, str]) -> tuple[bool, str]:
    url, tid, sec = env.get("PVE_URL", "").rstrip("/"), env.get("PVE_TOKEN_ID", ""), env.get("PVE_TOKEN_SECRET", "")
    if not (url and tid and sec):
        return False, "PVE_URL, PVE_TOKEN_ID and PVE_TOKEN_SECRET are required"
    verify = env.get("PVE_VERIFY_SSL", "false").lower() == "true"
    client = HttpClient(url, headers={"Authorization": f"PVEAPIToken={tid}={sec}"}, verify_ssl=verify, timeout_s=10)
    try:
        data = await client.get_json("/api2/json/version")
        return True, f"Proxmox VE {data.get('data', {}).get('version', '?')} answered"
    except HttpError as e:
        return False, f"PVE answered {e.status}: check the token id/secret"
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    finally:
        await client.close()


SERVICES = [
    ServiceSpec(
        name="proxmox",
        title="Proxmox VE",
        description="Live cluster board, threshold + offline alerts, backup watcher, VM/CT power control.",
        group="infra",
        settings=settings_from_example(EXAMPLE, required=("PVE_URL", "PVE_TOKEN_ID", "PVE_TOKEN_SECRET")),
        build=build,
        check=check,
        slash="/pve",
    )
]

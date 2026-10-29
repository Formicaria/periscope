"""Just enough Proxmox to answer "why is it down": is the node up, is the guest running.

Credentials are borrowed from another periscope service (PVE_FROM_SERVICE, normally `proxmox`) at the moment
they're needed, straight out of config/periscope.yaml — so there is one Proxmox token to rotate, not two, and
nobody copies a secret from one settings page into another.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from periscope import HttpClient, Store

log = logging.getLogger(__name__)


@dataclass
class GuestState:
    vmid: int
    kind: str          # lxc | qemu
    name: str
    node: str
    status: str        # running | stopped | paused | …


@dataclass
class PveView:
    nodes: dict[str, bool] = field(default_factory=dict)        # node → online
    guests: dict[int, GuestState] = field(default_factory=dict)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def guest(self, vmid: int | None) -> GuestState | None:
        return self.guests.get(vmid) if vmid is not None else None

    def node_online(self, node: str) -> bool | None:
        return self.nodes.get(node)


def parse_resources(data: list[dict]) -> PveView:
    view = PveView()
    for item in data or []:
        typ = item.get("type")
        if typ == "node":
            view.nodes[str(item.get("node"))] = item.get("status") == "online"
        elif typ in ("lxc", "qemu") and not item.get("template"):
            try:
                vmid = int(item.get("vmid"))
            except (TypeError, ValueError):
                continue
            view.guests[vmid] = GuestState(vmid=vmid, kind=typ, name=str(item.get("name") or vmid),
                                           node=str(item.get("node") or ""), status=str(item.get("status") or "unknown"))
    return view


def borrowed_env(service: str) -> dict[str, str]:
    """That service's settings from the install's config file, or {} when there is no such service."""
    if not service:
        return {}
    try:
        store = Store.load(os.environ.get("PERISCOPE_CONFIG") or "config/periscope.yaml")
        return store.env_for(service)
    except Exception as e:  # noqa: BLE001
        log.warning("community: could not read %s's settings for Proxmox access: %s", service, e)
        return {}


class PveLite:
    def __init__(self, env: dict[str, str]):
        self.url = (env.get("PVE_URL") or "").rstrip("/")
        tid, sec = env.get("PVE_TOKEN_ID", ""), env.get("PVE_TOKEN_SECRET", "")
        self.enabled = bool(self.url and tid and sec)
        verify = str(env.get("PVE_VERIFY_SSL", "false")).lower() == "true"
        self.http = HttpClient(self.url, headers={"Authorization": f"PVEAPIToken={tid}={sec}"} if self.enabled else {},
                               verify_ssl=verify, timeout_s=8)

    async def view(self) -> PveView:
        if not self.enabled:
            return PveView(error="no Proxmox access configured")
        try:
            data = await self.http.get_json("/api2/json/cluster/resources")
            return parse_resources(data.get("data") or [])
        except Exception as e:  # noqa: BLE001
            return PveView(error=f"Proxmox API unreachable ({type(e).__name__})")

    async def close(self) -> None:
        await self.http.close()

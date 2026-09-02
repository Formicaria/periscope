"""Async Proxmox VE API client (token auth) plus pure parsing helpers for its responses."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from periscope import HttpClient

from .config import PveSettings

log = logging.getLogger(__name__)

GUEST_TYPES = ("qemu", "lxc")
ACTIONS = ("start", "stop", "shutdown", "reboot")


# ----- models built from GET /cluster/resources ---------------------------------------


@dataclass
class Node:
    name: str
    online: bool
    cpu_pct: float
    mem_used: int
    mem_total: int
    uptime: int
    maxcpu: int

    @property
    def mem_pct(self) -> float:
        return 100.0 * self.mem_used / self.mem_total if self.mem_total else 0.0


@dataclass
class Guest:
    vmid: int
    name: str
    kind: str  # "qemu" | "lxc"
    node: str
    status: str  # "running" | "stopped" | "paused" | ...
    cpu_pct: float
    mem_used: int
    mem_total: int
    maxdisk: int
    uptime: int
    template: bool = False

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def label(self) -> str:
        return "VM" if self.kind == "qemu" else "CT"


@dataclass
class Storage:
    name: str
    node: str
    available: bool
    used: int
    total: int
    shared: bool

    @property
    def pct(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0

    @property
    def key(self) -> str:
        return self.name if self.shared else f"{self.node}/{self.name}"


@dataclass
class Snapshot:
    nodes: list[Node] = field(default_factory=list)
    guests: list[Guest] = field(default_factory=list)
    storages: list[Storage] = field(default_factory=list)
    fetched_at: float = 0.0

    def node(self, name: str) -> Node | None:
        return next((n for n in self.nodes if n.name == name), None)

    def guest(self, vmid: int) -> Guest | None:
        return next((g for g in self.guests if g.vmid == vmid), None)

    def guests_on(self, node: str) -> list[Guest]:
        return [g for g in self.guests if g.node == node]

    def unique_storages(self) -> list[Storage]:
        """Shared storages are reported once per node; collapse them to one entry."""
        seen: dict[str, Storage] = {}
        for s in self.storages:
            seen.setdefault(s.key, s)
        return sorted(seen.values(), key=lambda s: s.key)


def parse_resources(items: list[dict[str, Any]]) -> Snapshot:
    snap = Snapshot(fetched_at=time.time())
    for r in items:
        t = r.get("type")
        if t == "node":
            snap.nodes.append(Node(
                name=str(r.get("node", "?")),
                online=r.get("status") == "online",
                cpu_pct=100.0 * float(r.get("cpu") or 0.0),
                mem_used=int(r.get("mem") or 0),
                mem_total=int(r.get("maxmem") or 0),
                uptime=int(r.get("uptime") or 0),
                maxcpu=int(r.get("maxcpu") or 0),
            ))
        elif t in GUEST_TYPES:
            snap.guests.append(Guest(
                vmid=int(r.get("vmid", 0)),
                name=str(r.get("name") or f"{t}-{r.get('vmid')}"),
                kind=t,
                node=str(r.get("node", "?")),
                status=str(r.get("status") or "unknown"),
                cpu_pct=100.0 * float(r.get("cpu") or 0.0),
                mem_used=int(r.get("mem") or 0),
                mem_total=int(r.get("maxmem") or 0),
                maxdisk=int(r.get("maxdisk") or 0),
                uptime=int(r.get("uptime") or 0),
                template=bool(r.get("template")),
            ))
        elif t == "storage":
            snap.storages.append(Storage(
                name=str(r.get("storage", "?")),
                node=str(r.get("node", "?")),
                available=r.get("status") == "available",
                used=int(r.get("disk") or 0),
                total=int(r.get("maxdisk") or 0),
                shared=bool(r.get("shared")),
            ))
    snap.nodes.sort(key=lambda n: n.name)
    snap.guests.sort(key=lambda g: g.vmid)
    return snap


# ----- tasks ---------------------------------------------------------------------------

_UPID_RE = re.compile(
    r"^UPID:(?P<node>[^:]+):(?P<pid>[0-9A-Fa-f]+):(?P<pstart>[0-9A-Fa-f]+):(?P<starttime>[0-9A-Fa-f]+):"
    r"(?P<type>[^:]+):(?P<id>[^:]*):(?P<user>[^:]*):"
)


def parse_upid(upid: str) -> dict[str, Any]:
    """Decode a Proxmox UPID string into its parts (starttime as epoch seconds)."""
    m = _UPID_RE.match(upid or "")
    if not m:
        return {"node": "?", "type": "?", "id": "", "user": "", "starttime": 0}
    d = m.groupdict()
    d["starttime"] = int(d["starttime"], 16)
    d["pid"] = int(d["pid"], 16)
    return d


def task_ok(task: dict[str, Any]) -> bool | None:
    """True = finished OK, False = finished with error, None = still running."""
    if not task.get("endtime") and not task.get("status"):
        return None
    status = str(task.get("status") or "")
    return status == "OK"


_VZ_DONE = re.compile(r"Finished Backup of VM (\d+) \(([^)]+)\)")
_VZ_FAIL = re.compile(r"ERROR: Backup of VM (\d+) failed - (.*)")
_VZ_SIZE = re.compile(r"archive file size: (\S+)")


def summarize_vzdump_log(lines: list[str]) -> dict[str, Any]:
    """Extract per-guest results from a vzdump task log."""
    ok: list[dict[str, str]] = []
    failed: list[dict[str, str]] = []
    last_size = ""
    for line in lines:
        if m := _VZ_SIZE.search(line):
            last_size = m.group(1)
        elif m := _VZ_DONE.search(line):
            ok.append({"vmid": m.group(1), "duration": m.group(2), "size": last_size})
            last_size = ""
        elif m := _VZ_FAIL.search(line):
            failed.append({"vmid": m.group(1), "reason": m.group(2).strip()})
            last_size = ""
    return {"ok": ok, "failed": failed}


# ----- client --------------------------------------------------------------------------


class PveClient:
    """Thin async wrapper over the PVE REST API. Every response is unwrapped from its `data` key."""

    def __init__(self, cfg: PveSettings):
        self.cfg = cfg
        self.http = HttpClient(
            f"{cfg.url}/api2/json",
            headers={"Authorization": f"PVEAPIToken={cfg.token_id}={cfg.token_secret}"},
            verify_ssl=cfg.verify_ssl,
            timeout_s=20,
        )
        self._snapshot: Snapshot | None = None

    async def close(self) -> None:
        await self.http.close()

    async def get(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        body = await self.http.get_json(path, params=clean or None)
        return body.get("data") if isinstance(body, dict) else body

    async def post(self, path: str, **params: Any) -> Any:
        clean = {k: v for k, v in params.items() if v is not None}
        resp = await self.http.request("POST", path, data=clean or None)
        async with resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                return await resp.text()
        return body.get("data") if isinstance(body, dict) else body

    # --- cluster ---

    async def cluster_status(self) -> list[dict[str, Any]]:
        return await self.get("cluster/status") or []

    async def cluster_name(self) -> str | None:
        for item in await self.cluster_status():
            if item.get("type") == "cluster":
                return item.get("name")
        return None

    async def cluster_resources(self, rtype: str | None = None) -> list[dict[str, Any]]:
        return await self.get("cluster/resources", type=rtype) or []

    async def snapshot(self, *, max_age: float = 30) -> Snapshot:
        """Fetch + parse cluster/resources, cached for `max_age` seconds. Pass 0 to force."""
        if self._snapshot and time.time() - self._snapshot.fetched_at < max_age:
            return self._snapshot
        self._snapshot = parse_resources(await self.cluster_resources())
        return self._snapshot

    @property
    def cached(self) -> Snapshot | None:
        return self._snapshot

    def invalidate(self) -> None:
        self._snapshot = None

    async def resolve_guest(self, vmid: int) -> Guest | None:
        snap = self._snapshot or await self.snapshot()
        g = snap.guest(vmid)
        if g is None:
            g = (await self.snapshot(max_age=0)).guest(vmid)
        return g

    async def cluster_backup_jobs(self) -> list[dict[str, Any]]:
        return await self.get("cluster/backup") or []

    # --- nodes ---

    async def node_status(self, node: str) -> dict[str, Any]:
        return await self.get(f"nodes/{node}/status") or {}

    async def node_storage(self, node: str) -> list[dict[str, Any]]:
        return await self.get(f"nodes/{node}/storage") or []

    async def node_tasks(self, node: str, *, limit: int = 50, since: int | None = None,
                         typefilter: str | None = None, source: str = "all") -> list[dict[str, Any]]:
        return await self.get(f"nodes/{node}/tasks", limit=limit, since=since,
                              typefilter=typefilter, source=source) or []

    async def task_log(self, node: str, upid: str, limit: int = 1000) -> list[str]:
        rows = await self.get(f"nodes/{node}/tasks/{upid}/log", limit=limit, start=0) or []
        return [str(r.get("t", "")) for r in rows]

    # --- guests (qemu + lxc share the same URL shape) ---

    async def guest_current(self, node: str, kind: str, vmid: int) -> dict[str, Any]:
        return await self.get(f"nodes/{node}/{kind}/{vmid}/status/current") or {}

    async def guest_action(self, node: str, kind: str, vmid: int, action: str) -> str:
        if action not in ACTIONS:
            raise ValueError(f"unsupported action {action!r}")
        upid = await self.post(f"nodes/{node}/{kind}/{vmid}/status/{action}")
        return str(upid or "")

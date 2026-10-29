"""Why is it down? Turn probe results + what Proxmox says into one plain sentence per root cause.

Several things going down for the same reason become ONE cause with a list of what it affects — when the host
dies, the channel gets "pve3 is down: Plex, Requests, Minecraft…" once, not five separate posts.

Order of suspicion for a resource that failed its probe:
  1. the Proxmox node its guest lives on is offline           → node:<name>
  2. something it depends on is down                          → whatever took that down
  3. its container / VM isn't running                         → guest:<vmid>
  4. the guest runs but the app doesn't answer                → app:<key>
  5. no guest given / Proxmox not reachable                   → app:<key> (or pve:unreachable when everything
                                                                 that has a guest is down and the API is too)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Resource
from .probes import Probe
from .pve import PveView


@dataclass
class Cause:
    key: str
    title: str
    why: str
    affected: list[str] = field(default_factory=list)


def _kind_word(res: Resource) -> str:
    return "container" if res.guest_kind == "lxc" else "virtual machine" if res.guest_kind == "qemu" else "server"


def is_down(res: Resource, probe: Probe | None, pve: PveView) -> bool:
    """Down = its probe failed; for guest-only checks, the guest isn't running (or its node is offline)."""
    if res.check == "guest":
        g = pve.guest(res.vmid)
        if not pve.ok:
            return False                     # can't tell — don't cry wolf
        return g is None or g.status != "running" or pve.node_online(g.node) is False
    return bool(probe is not None and probe.ok is False)


def explain(res: Resource, down: dict[str, Resource], pve: PveView, probes: dict[str, Probe],
            _seen: frozenset[str] = frozenset()) -> Cause:
    probe = probes.get(res.key)
    detail = f" ({probe.detail})" if probe and probe.detail else ""
    g = pve.guest(res.vmid) if pve.ok else None
    # 1. the host it lives on
    if g is not None and pve.node_online(g.node) is False:
        return Cause(f"node:{g.node}", f"{g.node} is offline",
                     f"The server **{g.node}** that runs this is down, so everything on it is too. "
                     "It's usually power or hardware — someone has to go poke it.")
    # 2. what it leans on
    for dep_name in res.depends:
        dep = down.get(dep_name.lower())
        if dep is not None and dep.key not in _seen:
            inner = explain(dep, down, pve, probes, _seen | {res.key})
            return inner
    # 3. its own guest
    if res.vmid is not None and pve.ok:
        if g is None:
            return Cause(f"guest:{res.vmid}", f"{res.name} is missing",
                         f"Proxmox doesn't have {res.guest_label} any more — it may be mid-migration.")
        if g.status != "running":
            return Cause(f"guest:{res.vmid}", f"{res.name} is stopped",
                         f"Its {_kind_word(res)} ({res.guest_label} on {g.node}) is **{g.status}**.")
        return Cause(f"app:{res.key}", f"{res.name} isn't responding",
                     f"The {_kind_word(res)} is running but {res.name} itself isn't answering{detail}. "
                     "Usually it's restarting or stuck.")
    # 4/5. nothing more to go on
    if res.vmid is not None and not pve.ok:
        return Cause(f"app:{res.key}", f"{res.name} isn't responding",
                     f"{res.name} isn't answering{detail}, and the host can't be checked right now either.")
    return Cause(f"app:{res.key}", f"{res.name} isn't responding", f"{res.name} isn't answering{detail}.")


def group(resources: list[Resource], down_keys: set[str], pve: PveView, probes: dict[str, Probe]) -> list[Cause]:
    """Every down resource, folded into its root causes (order follows the resource list)."""
    down = {r.name.lower(): r for r in resources if r.key in down_keys}
    causes: dict[str, Cause] = {}
    for r in resources:
        if r.key not in down_keys:
            continue
        c = explain(r, down, pve, probes)
        have = causes.setdefault(c.key, c)
        if r.name not in have.affected:
            have.affected.append(r.name)
    # Proxmox itself unreachable and every guest-backed resource is down: say the host, not five apps
    guest_backed = [r for r in resources if r.vmid is not None and r.check != "guest"]
    if not pve.ok and guest_backed and all(r.key in down_keys for r in guest_backed) and len(guest_backed) > 1:
        names = [r.name for r in resources if r.key in down_keys]
        return [Cause("pve:unreachable", "The server is unreachable",
                      "Nothing on the host is answering and neither is Proxmox — probably power, network or the "
                      "host itself. Everything comes back when it does.", names)]
    return list(causes.values())

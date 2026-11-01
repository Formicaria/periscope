"""Settings for the community service, and the RESOURCES line it is mostly about.

RESOURCES is one string (the web UI edits it as a text field), entries separated by ";":

    Plex | plex | http://10.10.10.39:32400 | ct:139 | Media storage | plex members;
    Media storage | tcp | 10.10.10.140:2049 | vm:137 | | plex members;
    Minecraft: Horror | minecraft | 10.200.0.229:25565 | ct:127 | | horror.mc.ztechnus.com

    Name | check | target | proxmox guest | depends on | roles to ping (names or ids, comma separated)

Only the name and the check are required. Anything that does not parse is reported (and skipped), never fatal:
a typo in one entry must not take the whole board down.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from periscope import env, env_bool, env_int

CHECKS = ("plex", "http", "tcp", "minecraft", "guest")
GUEST_RE = re.compile(r"^(ct|lxc|vm|qemu)\s*[:/ ]?\s*(\d+)$", re.I)


def slug(text: str) -> str:
    """'Minecraft: Horror' → 'minecraft-horror' — the stable key state and alert fingerprints use."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "resource"


@dataclass
class Resource:
    name: str
    check: str
    target: str = ""
    guest_kind: str = ""          # "lxc" | "qemu" | ""
    vmid: int | None = None
    depends: list[str] = field(default_factory=list)
    ping: list[str] = field(default_factory=list)      # role names or ids: who hears about this one going down

    @property
    def key(self) -> str:
        return slug(self.name)

    @property
    def guest_label(self) -> str:
        if self.vmid is None:
            return ""
        return f"{'CT' if self.guest_kind == 'lxc' else 'VM'} {self.vmid}"


def parse_guest(raw: str) -> tuple[str, int | None]:
    m = GUEST_RE.match(raw.strip())
    if not m:
        return "", None
    kind = "lxc" if m.group(1).lower() in ("ct", "lxc") else "qemu"
    return kind, int(m.group(2))


def parse_resources(raw: str) -> tuple[list[Resource], list[str]]:
    """The RESOURCES string → (resources, problems in words). Duplicate names keep the first one."""
    out: list[Resource] = []
    problems: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[;\n]", raw or ""):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        parts = [p.strip() for p in chunk.split("|")]
        parts += [""] * (6 - len(parts))
        name, check, target, guest, depends, ping = parts[:6]
        check = check.lower()
        if not name:
            problems.append(f"an entry has no name: {chunk!r}")
            continue
        if check not in CHECKS:
            problems.append(f"{name}: check must be one of {', '.join(CHECKS)} (got {check or 'nothing'!r})")
            continue
        kind, vmid = parse_guest(guest) if guest else ("", None)
        if guest and vmid is None:
            problems.append(f"{name}: guest should look like ct:139 or vm:137 (got {guest!r}) — ignoring it")
        if check == "guest" and vmid is None:
            problems.append(f"{name}: a 'guest' check needs a guest like ct:105")
            continue
        if check in ("tcp", "minecraft") and not re.match(r"^[^:\s]+:\d+$", target):
            problems.append(f"{name}: {check} needs host:port (got {target!r})")
            continue
        if check in ("plex", "http") and not re.match(r"^https?://", target):
            problems.append(f"{name}: {check} needs a URL starting with http:// or https:// (got {target!r})")
            continue
        res = Resource(name=name, check=check, target=target, guest_kind=kind, vmid=vmid,
                       depends=[d.strip() for d in depends.split(",") if d.strip()],
                       ping=[x.strip().lstrip("@") for x in ping.split(",") if x.strip()])
        if res.key in seen:
            problems.append(f"{name}: listed twice — keeping the first")
            continue
        seen.add(res.key)
        out.append(res)
    names = {r.name.lower() for r in out}
    for r in out:
        for d in r.depends:
            if d.lower() not in names:
                problems.append(f"{r.name}: depends on {d!r}, which is not in the list")
        r.depends = [d for d in r.depends if d.lower() in names and d.lower() != r.name.lower()]
    return out, problems


@dataclass
class Bridge:
    """A Minecraft server's chat bridge: the Discord channel its in-game chat is relayed into, and the roles a
    player linked on that server gets (default: the resource's ping roles)."""
    resource: str
    channel_id: int
    roles: list[str] = field(default_factory=list)


def parse_bridges(raw: str, resources: list[Resource]) -> tuple[list[Bridge], list[str]]:
    """LINK_MINECRAFT → bridges. Entries separated by ";":  Resource name | chat channel id [| roles]"""
    out: list[Bridge] = []
    problems: list[str] = []
    by_name = {r.name.lower(): r for r in resources} | {r.key: r for r in resources}
    for chunk in re.split(r"[;\n]", raw or ""):
        chunk = chunk.strip()
        if not chunk or chunk.startswith("#"):
            continue
        parts = [p.strip() for p in chunk.split("|")] + ["", ""]
        name, chan, roles = parts[0], parts[1].lstrip("#<>"), parts[2]
        res = by_name.get(name.lower()) or by_name.get(slug(name))
        if res is None:
            problems.append(f"LINK_MINECRAFT: {name!r} is not one of the RESOURCES")
            continue
        if not chan.isdigit():
            problems.append(f"LINK_MINECRAFT: {name}: the chat channel should be a channel id (got {chan!r})")
            continue
        picked = [x.strip().lstrip("@") for x in roles.split(",") if x.strip()] or list(res.ping)
        if not picked:
            problems.append(f"LINK_MINECRAFT: {name}: no role to give — add one here or as its ping role in RESOURCES")
            continue
        out.append(Bridge(resource=res.name, channel_id=int(chan), roles=picked))
    return out, problems


@dataclass
class CommunitySettings:
    psa_channel_id: int | None
    psa_role_id: int | None
    resources: list[Resource]
    problems: list[str]
    grace_s: int = 180
    timezone: str = "America/Phoenix"
    pve_from: str = "proxmox"
    board: bool = True
    outbox: Path = Path("data/psa-outbox")
    pages: Path = Path("data/pages")
    bridges: list[Bridge] = field(default_factory=list)
    plex_role: str = ""               # the role a verified Plex user gets from /link plex ("" = /link plex is off)
    plex_from: str = "plexrequests"   # whose PLEX_URL / PLEX_TOKEN /link plex checks membership with
    announce_ping: str = "everyone"   # who /psa post and the outbox ping — announcements always go to everyone

    @classmethod
    def from_env(cls) -> "CommunitySettings":
        resources, problems = parse_resources(env("RESOURCES", "") or "")
        bridges, more = parse_bridges(env("LINK_MINECRAFT", "") or "", resources)
        return cls(
            psa_channel_id=env_int("PSA_CHANNEL_ID"),
            psa_role_id=env_int("PSA_ROLE_ID"),
            resources=resources,
            problems=problems + more,
            grace_s=max(30, env_int("OUTAGE_GRACE_S", 180) or 180),
            timezone=(env("TIMEZONE", "America/Phoenix") or "America/Phoenix").strip(),
            pve_from=(env("PVE_FROM_SERVICE", "proxmox") or "").strip(),
            board=env_bool("COMMUNITY_BOARD", True),
            outbox=Path(env("PSA_OUTBOX", "data/psa-outbox") or "data/psa-outbox"),
            pages=Path(env("PAGES_DIR", "data/pages") or "data/pages"),
            bridges=bridges,
            plex_role=(env("LINK_PLEX_ROLE", "") or "").strip().lstrip("@"),
            plex_from=(env("PLEX_FROM_SERVICE", "plexrequests") or "").strip(),
            announce_ping=(env("ANNOUNCE_PING", "everyone") or "everyone").strip(),
        )

    def bridge_for(self, channel_id: int) -> Bridge | None:
        return next((b for b in self.bridges if b.channel_id == channel_id), None)

    def by_name(self, name: str) -> Resource | None:
        low = name.strip().lower()
        return next((r for r in self.resources if r.name.lower() == low or r.key == slug(low)), None)

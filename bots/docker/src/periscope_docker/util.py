"""Pure helpers for interpreting Docker Engine API payloads (no network, unit-tested).

The Engine's container list says almost everything in two strings — `State` ("running", "exited", …) and the
human `Status` line ("Up 12 days (healthy)", "Exited (1) 5 minutes ago") — so most of this module is careful
reading of those, plus the arithmetic the docker CLI does on a stats sample and the framing its log stream uses.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any

from periscope import human_bytes, human_duration, truncate

# how long each word in a Status line lasts, the way docker's own humaniser writes them
UNIT_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800, "month": 2592000,
                "year": 31536000}
_UP_RE = re.compile(r"^Up\s+(?:(?P<about>About\s+an?)|(?P<less>Less than an?)|(?P<n>\d+))\s+"
                    r"(?P<unit>second|minute|hour|day|week|month|year)s?", re.IGNORECASE)
_EXIT_RE = re.compile(r"^(?:Exited|Restarting)\s+\((?P<code>-?\d+)\)")
_HEALTH_RE = re.compile(r"\((?P<health>healthy|unhealthy|health: starting)\)", re.IGNORECASE)

RUNNING, RESTARTING, PAUSED, EXITED = "running", "restarting", "paused", "exited"
# the dot each state gets on the board; a clean stop is grey, not red
STATE_DOTS = {RUNNING: "🟢", RESTARTING: "🟡", PAUSED: "🟡", "created": "⚪", "removing": "⚪", EXITED: "⚪",
              "dead": "🔴"}
NAME_LIMIT, TAG_LIMIT = 28, 34


# ----- containers --------------------------------------------------------


@dataclass
class Container:
    """One entry of `GET /containers/json?all=1`. `cpu_pct` / `mem_used` are filled in later from a stats
    sample when the poller could afford one — the list endpoint itself carries no resource usage."""

    id: str
    name: str
    image: str
    state: str
    status: str = ""
    created: int = 0
    labels: dict[str, str] = field(default_factory=dict)
    cpu_pct: float | None = None
    mem_used: int | None = None
    mem_limit: int | None = None

    @property
    def short_id(self) -> str:
        return self.id[:12]

    @property
    def running(self) -> bool:
        return self.state == RUNNING

    @property
    def health(self) -> str:
        """"healthy" · "unhealthy" · "starting" · "" (the container declares no health check)."""
        m = _HEALTH_RE.search(self.status)
        if not m:
            return ""
        found = m.group("health").lower()
        return "starting" if found.startswith("health:") else found

    @property
    def unhealthy(self) -> bool:
        return self.health == "unhealthy"

    @property
    def exit_code(self) -> int | None:
        m = _EXIT_RE.match(self.status.strip())
        return int(m.group("code")) if m else None

    @property
    def uptime_s(self) -> int | None:
        return parse_uptime(self.status)

    @property
    def tag(self) -> str:
        return image_tag(self.image)

    @property
    def trouble(self) -> str:
        """What is wrong with this container in one word, or "" when nothing is."""
        if self.state == RESTARTING:
            return RESTARTING
        if self.state in ("dead", "removing"):
            return self.state
        if not self.running:
            return "crashed" if (self.exit_code or 0) != 0 else "stopped"
        return "unhealthy" if self.unhealthy else ""

    @property
    def dot(self) -> str:
        if self.state == RESTARTING:
            return "🟡"                 # the exit code it carries is the one it is restarting from
        if not self.running and (self.exit_code or 0) != 0:
            return "🔴"
        if self.running and self.unhealthy:
            return "🟡"
        return STATE_DOTS.get(self.state, "⚪")


def parse_container(item: dict[str, Any]) -> Container:
    names = [str(n).lstrip("/") for n in (item.get("Names") or []) if n]
    return Container(
        id=str(item.get("Id") or ""),
        name=names[0] if names else str(item.get("Id") or "")[:12],
        image=str(item.get("Image") or ""),
        state=str(item.get("State") or "").lower(),
        status=str(item.get("Status") or ""),
        created=int(item.get("Created") or 0),
        labels={str(k): str(v) for k, v in (item.get("Labels") or {}).items()},
    )


def parse_containers(payload: list[dict[str, Any]] | None) -> list[Container]:
    return [parse_container(item) for item in payload or []]


def parse_uptime(status: str) -> int | None:
    """"Up 12 days (healthy)" → 1036800. None when the container is not up."""
    m = _UP_RE.match((status or "").strip())
    if not m:
        return None
    unit = UNIT_SECONDS[m.group("unit").lower()]
    if m.group("less"):
        return 0
    if m.group("about"):
        return unit
    return int(m.group("n")) * unit


def image_tag(image: str) -> str:
    """`linuxserver/sonarr:4.0.9` unchanged; a bare digest reference shortened to `repo@sha256:1234abcd`."""
    ref = (image or "").strip()
    if "@" in ref:
        repo, _, digest = ref.partition("@")
        return f"{repo}@{digest[:19]}" if digest else repo   # "sha256:" plus 12 hex, docker's own short form
    return ref or "?"


def repo_of(ref: str) -> str:
    """`ghcr.io/org/app:1.2` → `ghcr.io/org/app` (a port in the registry host is not a tag)."""
    ref = (ref or "").split("@", 1)[0]
    head, _, tail = ref.rpartition("/")
    if ":" in tail:
        tail = tail.rsplit(":", 1)[0]
    return f"{head}/{tail}" if head else tail


def watched(name: str, include: list[str], ignore: list[str]) -> bool:
    """DOCKER_INCLUDE decides what is in (empty = everything), DOCKER_IGNORE then takes names back out.
    Both are shell-style globs (`*arr`, `buildx_*`) matched against the container name, case-insensitively."""
    n = (name or "").lower()
    if include and not any(fnmatch.fnmatchcase(n, p.strip().lower()) for p in include if p.strip()):
        return False
    return not any(fnmatch.fnmatchcase(n, p.strip().lower()) for p in ignore if p.strip())


def sort_key(c: Container) -> tuple:
    """Board order: whatever needs attention first, then running containers, each group by name."""
    rank = 0 if c.trouble in ("crashed", "dead") else 1 if c.trouble else 2 if c.running else 3
    return (rank, c.name.lower())


def counts(containers: list[Container]) -> dict[str, int]:
    """running · stopped · unhealthy · restarting · total. An unhealthy container is still a running one."""
    running = sum(1 for c in containers if c.running)
    restarting = sum(1 for c in containers if c.state == RESTARTING)
    return {
        "running": running,
        "stopped": len(containers) - running - restarting,
        "unhealthy": sum(1 for c in containers if c.unhealthy),
        "restarting": restarting,
        "total": len(containers),
    }


def container_line(c: Container) -> str:
    """One board row: state dot, name, image tag, then whatever is worth knowing about the state it is in."""
    bits: list[str] = []
    if c.running:
        if c.uptime_s is not None:
            bits.append(f"up {human_duration(c.uptime_s)}")
        if c.unhealthy:
            bits.append("unhealthy")
        elif c.health == "starting":
            bits.append("starting")
        if c.cpu_pct is not None:
            bits.append(f"cpu {c.cpu_pct:.0f}%")
        if c.mem_used is not None:
            bits.append(human_bytes(c.mem_used))
    elif c.state == RESTARTING:
        bits.append("restarting")
        if (code := c.exit_code) is not None:
            bits.append(f"last exit {code}")
    elif c.state == PAUSED:
        bits.append("paused")
    else:
        code = c.exit_code
        bits.append(f"exited ({code})" if code else "stopped")
    name = truncate(c.name, NAME_LIMIT)
    return f"{c.dot} **{name}** `{truncate(c.tag, TAG_LIMIT)}`" + (" · " + " · ".join(bits) if bits else "")


def chunk_lines(lines: list[str], limit: int = 1000) -> list[str]:
    """Group board rows into embed-field-sized blocks (a field value stops at 1024 characters)."""
    blocks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + len(line) + 1 > limit:
            blocks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        blocks.append("\n".join(current))
    return blocks


# ----- stats (GET /containers/{id}/stats?stream=false) -------------------


def _usage(block: dict[str, Any]) -> int:
    return int(((block.get("cpu_usage") or {}).get("total_usage")) or 0)


def cpu_percent(stats: dict[str, Any] | None) -> float | None:
    """The number `docker stats` shows: the container's share of the machine, scaled by the number of CPUs.
    None when the sample carries no previous reading to compare against (the first one after a start)."""
    if not stats:
        return None
    cpu, pre = stats.get("cpu_stats") or {}, stats.get("precpu_stats") or {}
    system_delta = float(cpu.get("system_cpu_usage") or 0) - float(pre.get("system_cpu_usage") or 0)
    cpu_delta = float(_usage(cpu) - _usage(pre))
    if system_delta <= 0 or cpu_delta < 0:
        return None
    ncpu = int(cpu.get("online_cpus") or len((cpu.get("cpu_usage") or {}).get("percpu_usage") or []) or 1)
    return 100.0 * cpu_delta / system_delta * ncpu


def memory(stats: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """(used, limit) in bytes. Page cache is taken off the usage, the way the docker CLI reports it."""
    mem = (stats or {}).get("memory_stats") or {}
    if not mem.get("usage"):
        return None, None
    detail = mem.get("stats") or {}
    cache = detail.get("inactive_file", detail.get("total_inactive_file", detail.get("cache", 0)))
    used = max(0, int(mem["usage"]) - int(cache or 0))
    limit = int(mem.get("limit") or 0) or None
    return used, limit


def network(stats: dict[str, Any] | None) -> tuple[int, int]:
    """(received, sent) in bytes across every interface the container has."""
    nets = (stats or {}).get("networks") or {}
    rx = sum(int((n or {}).get("rx_bytes") or 0) for n in nets.values())
    tx = sum(int((n or {}).get("tx_bytes") or 0) for n in nets.values())
    return rx, tx


def block_io(stats: dict[str, Any] | None) -> tuple[int, int]:
    """(read, written) in bytes on the container's block devices."""
    entries = ((stats or {}).get("blkio_stats") or {}).get("io_service_bytes_recursive") or []
    read = sum(int(e.get("value") or 0) for e in entries if str(e.get("op", "")).lower() == "read")
    write = sum(int(e.get("value") or 0) for e in entries if str(e.get("op", "")).lower() == "write")
    return read, write


# ----- logs --------------------------------------------------------------


def demux_logs(raw: bytes) -> str:
    """Un-frame the log stream. Without a TTY the daemon prefixes every write with 8 bytes (stream id, then
    the payload length); with one it sends the bytes as they are, so fall back to decoding the lot."""
    out: list[bytes] = []
    i = 0
    while i + 8 <= len(raw):
        header = raw[i:i + 8]
        if header[0] not in (0, 1, 2) or header[1:4] != b"\x00\x00\x00":
            return raw.decode("utf-8", "replace")
        size = int.from_bytes(header[4:8], "big")
        out.append(raw[i + 8:i + 8 + size])
        i += 8 + size
    if i != len(raw):
        return raw.decode("utf-8", "replace")
    return b"".join(out).decode("utf-8", "replace")


def tail(text: str, lines: int, limit: int = 1800) -> str:
    """The last `lines` lines, trimmed from the front to `limit` characters so it fits in one code block."""
    kept = [ln for ln in (text or "").splitlines() if ln.strip()][-lines:]
    body = "\n".join(kept)
    if len(body) > limit:
        body = "…" + body[-(limit - 1):]
    return body


# ----- image updates -----------------------------------------------------


def image_ref(image: dict[str, Any]) -> str:
    """The tag an image is known by, or "" for a dangling / untagged one."""
    for tag in image.get("RepoTags") or []:
        if tag and not tag.endswith("<none>:<none>") and "<none>" not in tag:
            return str(tag)
    return ""


def local_digest(image: dict[str, Any], ref: str = "") -> str:
    """The registry digest the local image came from (`sha256:…`), for the repo of `ref` when there are several."""
    repo = repo_of(ref or image_ref(image))
    digests = [str(d) for d in image.get("RepoDigests") or [] if "@" in str(d)]
    for entry in digests:
        if repo_of(entry) == repo:
            return entry.split("@", 1)[1]
    return digests[0].split("@", 1)[1] if digests else ""


def has_update(image: dict[str, Any], remote_digest: str, ref: str = "") -> bool:
    """True when the registry's digest for this tag is not the one the local image was pulled from.
    An image with no digest at all (built locally, never pushed) can never be compared, so it never alerts."""
    local = local_digest(image, ref)
    return bool(local and remote_digest and local != remote_digest)


def images_in_use(containers: list[Container]) -> list[str]:
    """Every image tag the watched containers run, once each, in a stable order."""
    seen: dict[str, None] = {}
    for c in containers:
        ref = c.image.strip()
        if ref and "@" not in ref:
            seen.setdefault(ref, None)
    return sorted(seen)

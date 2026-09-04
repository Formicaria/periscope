"""Find the services you already run, so the settings pages start out filled in.

Three ways in, and an admin has to ask for every one of them:

* **A scan.** `default_hosts()` works out which subnets this box sits on, `probe_targets()` opens a bounded
  number of short-lived TCP connections across them, and `identify()` asks each open port one cheap,
  unauthenticated question whose answer names the product. Nothing is ever named from its port alone — a
  port only decides *which question to ask first*.
* **A compose file.** `from_compose()` reads image names, published ports and environment out of a
  docker-compose.yml. Pure text in, findings out; it opens no connections.
* **An *arr config.xml.** `from_arr_config()` reads Port, UrlBase and ApiKey out of a Sonarr/Radarr/Lidarr/
  Prowlarr config file. Also pure.

`suggestions()` turns findings into the settings periscope would write, using the keys the service specs
already declare (SONARR_URL, PLEX_URL, PVE_URL, …). It never overwrites a value somebody already set unless
it is asked to.

**Credentials.** A compose file or an *arr config can carry an API key. Findings keep the value — that is the
whole point, it has to be written to the store — but `Found.secret_keys` names those settings, `redacted()`
blanks them, and `__repr__` hides them, so a finding can go into a log line or a template without leaking.
Show that a key was found; never show the key.

Nothing in this module runs on a timer or at startup. The web UI calls it when an admin presses the button.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import re
import socket
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 1.5
READ_TIMEOUT_S = 4.0
IN_FLIGHT = 64
# A /22 is 1024 addresses — about as much as is polite to knock on. Bigger ranges are refused, not truncated,
# so nobody accidentally sweeps a /8 and wonders why it never finishes.
MAX_HOSTS = 1024


# ----- what lives where -------------------------------------------------------------------------------
# port -> the products worth asking about first. A hint only: identify() still has to get a product to name
# itself before anything is reported, and it will happily report a product found on an unexpected port.
PORT_HINTS: dict[int, tuple[str, ...]] = {
    8989: ("sonarr",),
    7878: ("radarr",),
    8686: ("lidarr",),
    9696: ("prowlarr",),
    8080: ("qbittorrent", "sabnzbd"),
    8081: ("sabnzbd", "qbittorrent"),
    32400: ("plex",),
    8096: ("jellyfin",),
    5055: ("overseerr",),
    9090: ("prometheus",),
    9093: ("alertmanager",),
    3000: ("grafana",),
    8006: ("proxmox",),
    443: ("unifi",),
    8443: ("unifi",),
    2375: ("docker",),
    2376: ("docker",),
    19999: ("netdata",),
    9000: ("portainer",),
    9443: ("portainer",),
}

DEFAULT_PORTS: tuple[int, ...] = tuple(sorted(PORT_HINTS))

# ports that only ever speak TLS
TLS_PORTS = frozenset({443, 8443, 8006, 2376, 9443})

ARR_APPS = ("sonarr", "radarr", "lidarr", "prowlarr")
ARR_API_VERSION = {"sonarr": "v3", "radarr": "v3", "lidarr": "v1", "prowlarr": "v1"}

TITLES = {
    "sonarr": "Sonarr", "radarr": "Radarr", "lidarr": "Lidarr", "prowlarr": "Prowlarr",
    "qbittorrent": "qBittorrent", "sabnzbd": "SABnzbd", "plex": "Plex", "jellyfin": "Jellyfin",
    "overseerr": "Overseerr", "prometheus": "Prometheus", "alertmanager": "Alertmanager",
    "grafana": "Grafana", "proxmox": "Proxmox VE", "unifi": "UniFi", "docker": "Docker",
    "portainer": "Portainer", "netdata": "Netdata",
}

# product -> (periscope service, the setting that takes its URL). Products periscope has no service for are
# still reported by a scan; they simply produce no suggestion.
SERVICE_FOR: dict[str, tuple[str, str]] = {
    "sonarr": ("sonarr", "SONARR_URL"),
    "radarr": ("radarr", "RADARR_URL"),
    "lidarr": ("lidarr", "LIDARR_URL"),
    "prowlarr": ("prowlarr", "PROWLARR_URL"),
    "qbittorrent": ("qbittorrent", "QBIT_URL"),
    "sabnzbd": ("sabnzbd", "SABNZBD_URL"),
    "plex": ("plex", "PLEX_URL"),
    "jellyfin": ("jellyfin", "JELLYFIN_URL"),
    "overseerr": ("plexrequests", "OVERSEERR_URL"),
    "prometheus": ("prometheus", "PROM_URL"),
    "alertmanager": ("alertmanager", "ALERTMANAGER_URL"),
    "grafana": ("grafana", "GRAFANA_URL"),
    "proxmox": ("proxmox", "PVE_URL"),
    "unifi": ("unifi", "UNIFI_URL"),
    "docker": ("docker", "DOCKER_HOST"),
    "portainer": ("docker", "PORTAINER_URL"),
}

# product -> the setting its API key/token belongs in, when a file hands us one
KEY_SETTING: dict[str, str] = {
    "sonarr": "SONARR_API_KEY", "radarr": "RADARR_API_KEY", "lidarr": "LIDARR_API_KEY",
    "prowlarr": "PROWLARR_API_KEY", "qbittorrent": "QBIT_API_KEY", "sabnzbd": "SABNZBD_API_KEY",
    "plex": "PLEX_TOKEN", "jellyfin": "JELLYFIN_API_KEY", "overseerr": "OVERSEERR_API_KEY",
    "grafana": "GRAFANA_TOKEN", "proxmox": "PVE_TOKEN_SECRET", "portainer": "PORTAINER_API_KEY",
}

# substrings in a compose image name -> product. Longest match wins, so "linuxserver/prowlarr" does not
# come back as "radarr" on account of the "arr".
IMAGE_HINTS: dict[str, str] = {
    "sonarr": "sonarr", "radarr": "radarr", "lidarr": "lidarr", "prowlarr": "prowlarr",
    "qbittorrent": "qbittorrent", "qbit": "qbittorrent", "sabnzbd": "sabnzbd",
    "plexinc/pms-docker": "plex", "plex-media-server": "plex", "plex": "plex",
    "jellyfin": "jellyfin", "overseerr": "overseerr", "jellyseerr": "overseerr",
    "prom/prometheus": "prometheus", "prometheus": "prometheus",
    "prom/alertmanager": "alertmanager", "alertmanager": "alertmanager",
    "grafana": "grafana", "portainer": "portainer", "netdata": "netdata",
}

# environment/label names a compose file uses for a product's key, per product
COMPOSE_KEY_NAMES: dict[str, tuple[str, ...]] = {
    "sonarr": ("SONARR_API_KEY", "API_KEY"), "radarr": ("RADARR_API_KEY", "API_KEY"),
    "lidarr": ("LIDARR_API_KEY", "API_KEY"), "prowlarr": ("PROWLARR_API_KEY", "API_KEY"),
    "sabnzbd": ("SABNZBD_API_KEY", "API_KEY"), "qbittorrent": ("QBIT_API_KEY",),
    "plex": ("PLEX_CLAIM", "PLEX_TOKEN"), "jellyfin": ("JELLYFIN_API_KEY",),
    "overseerr": ("OVERSEERR_API_KEY", "API_KEY"), "grafana": ("GF_SECURITY_ADMIN_PASSWORD", "GRAFANA_TOKEN"),
    "portainer": ("PORTAINER_API_KEY",),
}


# ----- findings ---------------------------------------------------------------------------------------
@dataclass
class Found:
    """One thing we are confident about: a product, where it answered, and whatever it volunteered."""

    service: str                                        # the product's own name (see TITLES)
    url: str = ""
    version: str = ""
    host: str = ""
    port: int = 0
    source: str = "scan"                                # scan | compose | config
    # named  — the product identified itself in its own response
    # family — the response proved the family (an *arr) and the port picked which one
    confidence: str = "named"
    settings: dict[str, str] = field(default_factory=dict)   # extra settings this finding can fill in
    secret_keys: tuple[str, ...] = ()                   # which of `settings` must never be shown
    note: str = ""

    @property
    def title(self) -> str:
        return TITLES.get(self.service, self.service)

    @property
    def has_secret(self) -> bool:
        """True when this finding carries a credential — all the UI is ever allowed to say about one."""
        return any(self.settings.get(k) for k in self.secret_keys)

    @property
    def where(self) -> str:
        return self.url or (f"{self.host}:{self.port}" if self.host else "")

    def redacted(self) -> Found:
        """A copy safe to render or log: every secret value replaced by an empty string."""
        clean = {k: ("" if k in self.secret_keys else v) for k, v in self.settings.items()}
        return replace(self, settings=clean)

    def __repr__(self) -> str:       # so an accidental log line cannot spill a key
        keys = f", key for {'+'.join(self.secret_keys)}" if self.has_secret else ""
        return f"<Found {self.service} {self.where} {self.version or '?'} via {self.source}{keys}>"


@dataclass
class Suggestion:
    """What periscope would write for one service, and what it is deliberately leaving alone."""

    service: str                                   # the periscope service name (SERVICE_FOR)
    title: str
    url: str
    settings: dict[str, str] = field(default_factory=dict)     # what `Use this` would write
    skipped: dict[str, str] = field(default_factory=dict)      # key -> why it was left alone
    secret_keys: tuple[str, ...] = ()
    already_configured: bool = False               # this service already has settings in the store
    enabled: bool = False
    found: Found | None = None

    @property
    def has_secret(self) -> bool:
        return any(self.settings.get(k) for k in self.secret_keys)

    @property
    def writes_nothing(self) -> bool:
        return not self.settings

    def redacted(self) -> Suggestion:
        clean = {k: ("" if k in self.secret_keys else v) for k, v in self.settings.items()}
        return replace(self, settings=clean, found=self.found.redacted() if self.found else None)

    def __repr__(self) -> str:
        return f"<Suggestion {self.service} {self.url} writes {sorted(self.settings)}>"


@dataclass
class Target:
    """A port that accepted a connection."""

    host: str
    port: int

    @property
    def url(self) -> str:
        scheme = "https" if self.port in TLS_PORTS else "http"
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{scheme}://{host}:{self.port}"

    def __str__(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class Reply:
    """The bit of an HTTP response identify() cares about. Tests hand these over directly."""

    status: int
    text: str = ""
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None

    def header(self, name: str) -> str:
        want = name.lower()
        return next((v for k, v in self.headers.items() if k.lower() == want), "")


Fetch = Callable[[str], Awaitable[Reply]]
Connect = Callable[[str, int, float], Awaitable[bool]]


# ----- which hosts ------------------------------------------------------------------------------------
def _own_addresses() -> list[str]:
    """Every non-loopback IPv4 address this box answers on, best effort and stdlib only."""
    out: list[str] = []
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("1.1.1.1", 53))       # nothing is sent; the kernel just picks a source address
            out.append(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            out.append(info[4][0])
    except (OSError, socket.gaierror):
        pass
    return [a for a in dict.fromkeys(out) if a and not a.startswith("127.")]


def default_hosts() -> list[str]:
    """The ranges a scan starts from: localhost, plus a /24 around each address this box holds.

    A /24 is a guess — it is the shape of nearly every home and lab network, and the field on the scan page
    is editable precisely because it is a guess. The real prefix length is not available from the standard
    library on every platform, so this does not pretend to know it.
    """
    hosts = ["127.0.0.1"]
    for addr in _own_addresses():
        try:
            net = ipaddress.ip_network(f"{addr}/24", strict=False)
        except ValueError:
            continue
        cidr = str(net)
        if cidr not in hosts:
            hosts.append(cidr)
    return hosts


def expand_hosts(hosts: str | Iterable[str], *, max_hosts: int = MAX_HOSTS) -> list[str]:
    """Turn what the caller typed — a host, a CIDR, or several of either — into a plain list of addresses.

    Accepts a comma/space separated string or any iterable. Names that are not addresses are passed through
    untouched, so `sonarr.lan` works. Raises ValueError when the ranges add up to more than `max_hosts`.
    """
    if isinstance(hosts, str):
        items = [p for p in re.split(r"[,\s]+", hosts) if p]
    else:
        items = [str(h).strip() for h in hosts if str(h).strip()]
    out: list[str] = []
    seen: set[str] = set()
    total = 0
    for item in items:
        addrs: list[str]
        if "/" in item:
            try:
                net = ipaddress.ip_network(item, strict=False)
            except ValueError as e:
                raise ValueError(f"{item!r} is not a network — try something like 192.168.1.0/24") from e
            size = net.num_addresses if net.num_addresses <= 2 else net.num_addresses - 2
            total += size
            if total > max_hosts:
                raise ValueError(f"that is {total} addresses — {max_hosts} is the most one scan will take; "
                                 "narrow the range (a /24 is 254)")
            addrs = [str(h) for h in (net.hosts() or [net.network_address])]
        else:
            total += 1
            if total > max_hosts:
                raise ValueError(f"that is more than {max_hosts} addresses — narrow the range")
            addrs = [item]
        for a in addrs:
            if a not in seen:
                seen.add(a)
                out.append(a)
    return out


# ----- the scan ---------------------------------------------------------------------------------------
async def _tcp_connect(host: str, port: int, timeout: float) -> bool:
    """Open a connection and drop it. Nothing is sent — this only asks whether something is listening."""
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    finally:
        if writer is not None:
            try:
                writer.close()
            except Exception:      # noqa: BLE001 — a closed transport is not worth a traceback
                pass


async def probe_targets(hosts: str | Iterable[str], ports: Sequence[int] | None = None, *,
                        in_flight: int = IN_FLIGHT, timeout: float = CONNECT_TIMEOUT_S,
                        connect: Connect | None = None, max_hosts: int = MAX_HOSTS) -> list[Target]:
    """Which of these host/port pairs has something listening.

    Runs at most `in_flight` connections at a time and gives each one `timeout` seconds, so a /24 across the
    usual ports finishes in seconds instead of hanging on every silent address. `connect` is swappable so
    tests never touch a real socket. Results come back sorted, host then port.
    """
    addrs = expand_hosts(hosts, max_hosts=max_hosts)
    want = tuple(ports) if ports is not None else DEFAULT_PORTS
    if not addrs or not want:
        return []
    dial = connect or _tcp_connect
    gate = asyncio.Semaphore(max(1, int(in_flight)))
    found: list[Target] = []

    async def one(host: str, port: int) -> None:
        async with gate:
            try:
                ok = await dial(host, port, timeout)
            except (OSError, asyncio.TimeoutError):
                ok = False
            except Exception:      # noqa: BLE001 — one bad address must not end the scan
                log.debug("probe %s:%s raised", host, port, exc_info=True)
                ok = False
        if ok:
            found.append(Target(host, port))

    await asyncio.gather(*(one(h, p) for h in addrs for p in want))
    found.sort(key=lambda t: (t.host, t.port))
    return found


# ----- identifying one candidate ----------------------------------------------------------------------
def _version_from(data: Any, *keys: str) -> str:
    if not isinstance(data, dict):
        return ""
    for k in keys:
        v = data.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
    return ""


def _probe_arr(url: str, reply: Reply, *, want: str = "") -> Found | None:
    """*arr apps answer /api/vN/system/status with 401 and a short JSON body when no key is sent.

    That proves the family but not which one — none of them put their own name in that response. When the
    port says which *arr to expect, the port names it (`confidence="family"`); the caller can also pass the
    title read off the login page, which names it outright.
    """
    if reply.status not in (401, 403):
        return None
    body = (reply.text or "").strip().lower()
    looks_arr = ("unauthorized" in body or not body) and len(body) < 400
    if not looks_arr and "sonarr" not in body and "radarr" not in body:
        return None
    named = next((a for a in ARR_APPS if a in body), "") or next((a for a in ARR_APPS if a in reply.header(
        "x-application-name").lower()), "")
    service = named or want
    if not service:
        return None
    version = reply.header("x-application-version")
    return Found(service, url=url, version=version, confidence="named" if named else "family",
                 note="answered 401 on its API, the way an *arr does" if not named else "")


def _probe_qbittorrent(url: str, reply: Reply) -> Found | None:
    text = (reply.text or "").strip()
    if reply.status == 200 and re.fullmatch(r"v?\d+\.\d+(\.\d+)*", text):
        return Found("qbittorrent", url=url, version=text.lstrip("v"))
    if reply.status == 403 and "forbidden" in text.lower():
        return Found("qbittorrent", url=url, note="reachable, asks you to sign in")
    return None


def _probe_sabnzbd(url: str, reply: Reply) -> Found | None:
    data = reply.json()
    if reply.status == 200 and isinstance(data, dict) and "version" in data:
        return Found("sabnzbd", url=url, version=str(data["version"]))
    return None


def _probe_plex(url: str, reply: Reply) -> Found | None:
    text = reply.text or ""
    if reply.status != 200 or "machineIdentifier" not in text:
        return None
    data = reply.json()
    if isinstance(data, dict):
        inner = data.get("MediaContainer") if isinstance(data.get("MediaContainer"), dict) else data
        return Found("plex", url=url, version=_version_from(inner, "version"))
    # /identity answers XML unless asked for JSON. Read the version off the MediaContainer tag itself — the
    # `<?xml version="1.0"?>` declaration above it would otherwise match first and report the XML version.
    m = re.search(r'<MediaContainer\b[^>]*?\sversion="([^"]+)"', text)
    return Found("plex", url=url, version=m.group(1) if m else "")


def _probe_jellyfin(url: str, reply: Reply) -> Found | None:
    data = reply.json()
    if reply.status != 200 or not isinstance(data, dict):
        return None
    product = str(data.get("ProductName") or "")
    if "jellyfin" not in product.lower() and not data.get("Id"):
        return None
    return Found("jellyfin", url=url, version=_version_from(data, "Version"),
                 note=str(data.get("ServerName") or ""))


def _probe_overseerr(url: str, reply: Reply) -> Found | None:
    data = reply.json()
    if reply.status != 200 or not isinstance(data, dict) or "version" not in data:
        return None
    if "commitTag" not in data and "updateAvailable" not in data:
        return None
    return Found("overseerr", url=url, version=_version_from(data, "version"))


def _probe_prometheus(url: str, reply: Reply) -> Found | None:
    if reply.status == 200 and "prometheus" in (reply.text or "").lower():
        return Found("prometheus", url=url, note="ready")
    return None


def _probe_alertmanager(url: str, reply: Reply) -> Found | None:
    data = reply.json()
    if reply.status != 200 or not isinstance(data, dict):
        return None
    info = data.get("versionInfo")
    if not isinstance(info, dict):
        return None
    return Found("alertmanager", url=url, version=_version_from(info, "version"))


def _probe_grafana(url: str, reply: Reply) -> Found | None:
    data = reply.json()
    if reply.status != 200 or not isinstance(data, dict) or "database" not in data:
        return None
    return Found("grafana", url=url, version=_version_from(data, "version"))


def _probe_proxmox(url: str, reply: Reply) -> Found | None:
    if reply.status == 401:
        return Found("proxmox", url=url, note="answered on its API, sign-in required")
    data = reply.json()
    if reply.status != 200 or not isinstance(data, dict) or "data" not in data:
        return None
    inner = data.get("data") if isinstance(data.get("data"), dict) else {}
    return Found("proxmox", url=url, version=_version_from(inner, "version", "release"))


def _probe_unifi(url: str, reply: Reply) -> Found | None:
    data = reply.json()
    if reply.status not in (200, 401) or not isinstance(data, dict):
        return None
    meta = data.get("meta")
    if not isinstance(meta, dict) or "rc" not in meta:
        return None
    return Found("unifi", url=url, version=_version_from(meta, "server_version"))


def _probe_docker(url: str, reply: Reply) -> Found | None:
    data = reply.json()
    if reply.status != 200 or not isinstance(data, dict):
        return None
    platform = data.get("Platform")
    is_docker = "ApiVersion" in data and ("Version" in data or "Components" in data)
    if not is_docker:
        return None
    name = str(platform.get("Name") or "") if isinstance(platform, dict) else ""
    return Found("docker", url=url, version=_version_from(data, "Version"), note=name)


def _probe_portainer(url: str, reply: Reply) -> Found | None:
    data = reply.json()
    if reply.status != 200 or not isinstance(data, dict):
        return None
    if "Version" not in data or ("InstanceID" not in data and "DatabaseVersion" not in data):
        return None
    return Found("portainer", url=url, version=_version_from(data, "Version"))


def _probe_netdata(url: str, reply: Reply) -> Found | None:
    data = reply.json()
    if reply.status != 200 or not isinstance(data, dict):
        return None
    if "version" not in data or ("uid" not in data and "mirrored_hosts" not in data):
        return None
    return Found("netdata", url=url, version=_version_from(data, "version"))


# product -> (path to ask, reader). One request per product, and every one of them unauthenticated.
PROBES: dict[str, tuple[str, Callable[[str, Reply], Found | None]]] = {
    **{app: (f"/api/{ARR_API_VERSION[app]}/system/status",
             (lambda a: lambda u, r: _probe_arr(u, r, want=a))(app)) for app in ARR_APPS},
    "qbittorrent": ("/api/v2/app/version", _probe_qbittorrent),
    "sabnzbd": ("/api?mode=version&output=json", _probe_sabnzbd),
    "plex": ("/identity", _probe_plex),
    "jellyfin": ("/System/Info/Public", _probe_jellyfin),
    "overseerr": ("/api/v1/status", _probe_overseerr),
    "prometheus": ("/-/ready", _probe_prometheus),
    "alertmanager": ("/api/v2/status", _probe_alertmanager),
    "grafana": ("/api/health", _probe_grafana),
    "proxmox": ("/api2/json/version", _probe_proxmox),
    "unifi": ("/status", _probe_unifi),
    "docker": ("/version", _probe_docker),
    "portainer": ("/api/status", _probe_portainer),
    "netdata": ("/api/v1/info", _probe_netdata),
}


@contextlib.asynccontextmanager
async def http_fetcher(*, timeout: float = READ_TIMEOUT_S) -> AsyncIterator[Fetch]:
    """The real fetcher, over one shared session for however many requests it is given.

    A whole scan runs through a single session and connection pool — opening one per request would cost more
    sockets than the scan itself. Certificates are not verified: lab boxes nearly all present a self-signed
    one, and nothing here reads anything but an unauthenticated version endpoint.
    """
    import aiohttp

    conn = aiohttp.TCPConnector(ssl=False, limit=IN_FLIGHT)
    client_timeout = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(connector=conn, timeout=client_timeout) as session:

        async def fetch(url: str) -> Reply:
            async with session.get(url, allow_redirects=True) as resp:
                body = await resp.text(errors="replace")
                return Reply(resp.status, body[:8192], dict(resp.headers))

        yield fetch


async def identify(url: str, *, fetch: Fetch | None = None, candidates: Sequence[str] | None = None,
                   timeout: float = READ_TIMEOUT_S, _flipped: bool = False) -> Found | None:
    """Ask one candidate URL what it is, and believe only what it says about itself.

    Which products to ask about comes from the port (`PORT_HINTS`), then everything else as a fallback, so a
    Grafana on an odd port is still found — it just costs a few more requests. Each product is asked exactly
    one unauthenticated question. When nothing answers at all the scheme gets flipped once (http ↔ https) and
    the round is repeated, which is what a port open to a plain-http Proxmox looks like. Returns None when
    nothing recognisable answers either way.
    """
    if fetch is None:      # no session to borrow — open one just for this URL
        async with http_fetcher(timeout=timeout) as own:
            return await identify(url, fetch=own, candidates=candidates, timeout=timeout)
    parts = urlsplit(url if "://" in url else f"http://{url}")
    host, port = parts.hostname or "", parts.port or (443 if parts.scheme == "https" else 80)
    order = list(candidates) if candidates is not None else [
        *PORT_HINTS.get(port, ()), *[p for p in PROBES if p not in PORT_HINTS.get(port, ())]]
    doer = fetch
    base = url.rstrip("/")
    answered = False       # did anything at all come back, or is the scheme simply wrong?
    for name in order:
        probe = PROBES.get(name)
        if probe is None:
            continue
        path, read = probe
        try:
            reply = await doer(base + path)
        except asyncio.TimeoutError:
            continue
        except Exception:      # noqa: BLE001 — an unreachable or rude candidate is just not a match
            log.debug("identify %s: %s did not answer", base, name, exc_info=True)
            continue
        answered = True
        try:
            hit = read(base, reply)
        except Exception:      # noqa: BLE001 — a malformed answer is not a match either
            log.debug("identify %s: %s answered something unreadable", base, name, exc_info=True)
            continue
        if hit is not None:
            return replace(hit, host=host, port=port, source="scan")
    # A port that accepted a connection but answered nothing at all is nearly always the other scheme —
    # a Proxmox proxied over plain http, a Docker socket on 2376 without TLS. Try the other one, once.
    if not answered and not _flipped and parts.scheme in ("http", "https"):
        other = "http" if parts.scheme == "https" else "https"
        return await identify(f"{other}://{parts.netloc}{parts.path}".rstrip("/"), fetch=fetch,
                              candidates=candidates, timeout=timeout, _flipped=True)
    return None


async def scan(hosts: str | Iterable[str], ports: Sequence[int] | None = None, *, fetch: Fetch | None = None,
               connect: Connect | None = None, in_flight: int = IN_FLIGHT,
               timeout: float = CONNECT_TIMEOUT_S, max_hosts: int = MAX_HOSTS) -> list[Found]:
    """probe_targets() then identify(), with the same bound on how much is in the air at once.

    One HTTP session covers every candidate the scan turned up, rather than one per request.
    """
    targets = await probe_targets(hosts, ports, in_flight=in_flight, timeout=timeout, connect=connect,
                                  max_hosts=max_hosts)
    if not targets:
        return []
    gate = asyncio.Semaphore(max(1, int(in_flight)))
    out: list[Found] = []

    async def one(target: Target, doer: Fetch) -> None:
        async with gate:
            hit = await identify(target.url, fetch=doer)
        if hit is not None:
            out.append(hit)

    if fetch is not None:
        await asyncio.gather(*(one(t, fetch) for t in targets))
    else:
        async with http_fetcher() as shared:
            await asyncio.gather(*(one(t, shared) for t in targets))
    out.sort(key=lambda f: (f.service, f.host, f.port))
    return out


# ----- reading files instead --------------------------------------------------------------------------
def _product_from_image(image: str) -> str:
    """Longest matching substring wins, so prowlarr is not mistaken for radarr."""
    low = image.lower()
    best, best_len = "", 0
    for hint, product in IMAGE_HINTS.items():
        if hint in low and len(hint) > best_len:
            best, best_len = product, len(hint)
    return best


def _compose_env(raw: Any) -> dict[str, str]:
    """compose accepts environment as a mapping or as a list of KEY=VALUE strings."""
    out: dict[str, str] = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            out[str(k)] = "" if v is None else str(v)
    elif isinstance(raw, list):
        for item in raw:
            key, _, value = str(item).partition("=")
            if key.strip():
                out[key.strip()] = value.strip()
    return out


def _published_port(raw: Any) -> int:
    """The host-side port of a compose `ports:` entry — "8989:8989", "127.0.0.1:8989:8989/tcp", or a mapping."""
    if isinstance(raw, dict):
        for key in ("published", "target"):
            try:
                return int(str(raw.get(key)))
            except (TypeError, ValueError):
                continue
        return 0
    text = str(raw).strip().split("/")[0]
    bits = [b for b in text.split(":") if b]
    if not bits:
        return 0
    candidate = bits[-2] if len(bits) >= 2 else bits[0]
    try:
        return int(candidate)
    except ValueError:
        return 0


def from_compose(text_or_path: str | Path, *, host: str = "localhost") -> list[Found]:
    """Read a docker-compose.yml and report the services periscope knows how to talk to.

    Image names say which product, published ports say where, environment and labels give up an API key when
    the file happens to carry one. Nothing here connects to anything — hand it a path or the text itself.
    `host` is what goes in the URLs, for a compose file that belongs to another box.
    """
    import yaml

    text = str(text_or_path)
    looks_like_path = "\n" not in text and len(text) < 4096
    if isinstance(text_or_path, Path) or (looks_like_path and Path(text).expanduser().is_file()):
        text = Path(str(text_or_path)).expanduser().read_text(encoding="utf-8", errors="replace")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ValueError(f"that does not parse as YAML: {e}") from e
    if not isinstance(data, dict):
        return []
    services = data.get("services")
    if not isinstance(services, dict):
        return []

    out: list[Found] = []
    for raw_name, body in services.items():
        if not isinstance(body, dict):
            continue
        product = _product_from_image(str(body.get("image") or "")) or _product_from_image(str(raw_name))
        if not product:
            continue
        ports = body.get("ports") if isinstance(body.get("ports"), list) else []
        port = next((p for p in (_published_port(x) for x in ports) if p), 0)
        if not port:
            port = next((p for p, names in PORT_HINTS.items() if product in names), 0)
        scheme = "https" if port in TLS_PORTS else "http"
        url = f"{scheme}://{host}:{port}" if port else ""
        env = _compose_env(body.get("environment"))
        env.update(_compose_env(body.get("labels")))
        settings: dict[str, str] = {}
        secrets: tuple[str, ...] = ()
        target = KEY_SETTING.get(product, "")
        if target:
            value = next((env[n] for n in COMPOSE_KEY_NAMES.get(product, ()) if env.get(n)), "")
            if value and not value.startswith("${"):        # an unresolved ${VAR} is not a key
                settings[target] = value
                secrets = (target,)
        out.append(Found(product, url=url, host=host, port=port, source="compose", settings=settings,
                         secret_keys=secrets, note=f"compose service {raw_name}"))
    out.sort(key=lambda f: (f.service, f.port))
    return out


_ARR_FROM_TEXT = {"sonarr": "sonarr", "radarr": "radarr", "lidarr": "lidarr", "prowlarr": "prowlarr"}


def _arr_from_path(path: Path, instance: str = "") -> str:
    """Which *arr this config belongs to: its own InstanceName if it has one, else the directory it sits in."""
    for text in (instance.lower(), *(p.name.lower() for p in (path, *path.parents)[:4])):
        for needle, product in _ARR_FROM_TEXT.items():
            if needle in text:
                return product
    return ""


def from_arr_config(path: str | Path) -> list[Found]:
    """Read an *arr `config.xml` — the one next to its database — for Port, UrlBase and ApiKey.

    Which app it is comes from `<InstanceName>` when present, otherwise from the folder the file sits in
    (…/sonarr/config.xml). Returns a single finding, or nothing when the file is not an *arr config.
    """
    p = Path(str(path)).expanduser()
    if p.is_dir():
        p = p / "config.xml"
    try:
        root = ET.fromstring(p.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ET.ParseError) as e:
        raise ValueError(f"could not read {p.name} as an *arr config: {e}") from e
    if root.tag.lower() != "config":
        return []

    def text(tag: str) -> str:
        for child in root:
            if child.tag.lower() == tag.lower():
                return (child.text or "").strip()
        return ""

    product = _arr_from_path(p, text("InstanceName"))
    if not product:
        return []
    use_ssl = text("EnableSsl").lower() in ("true", "yes", "1")
    port_text = text("SslPort") if use_ssl else text("Port")
    try:
        port = int(port_text)
    except ValueError:
        port = next((k for k, v in PORT_HINTS.items() if product in v), 0)
    base = text("UrlBase").strip("/")
    host = "localhost"
    url = f"{'https' if use_ssl else 'http'}://{host}:{port}" + (f"/{base}" if base else "")
    key = text("ApiKey")
    settings: dict[str, str] = {}
    secrets: tuple[str, ...] = ()
    target = KEY_SETTING.get(product, "")
    if key and target:
        settings[target] = key
        secrets = (target,)
    return [Found(product, url=url, host=host, port=port, source="config", settings=settings,
                  secret_keys=secrets, note=f"read from {p.name}")]


# ----- turning findings into settings ------------------------------------------------------------------
def _dedupe(found: Iterable[Found]) -> list[Found]:
    """One finding per product. A file beats a scan (it carries the key), and a product that named itself
    beats one the port named."""
    rank = {"config": 0, "compose": 1, "scan": 2}
    best: dict[str, Found] = {}
    for f in found:
        current = best.get(f.service)
        if current is None:
            best[f.service] = f
            continue
        mine = (rank.get(f.source, 3), 0 if f.confidence == "named" else 1, 0 if f.settings else 1)
        theirs = (rank.get(current.source, 3), 0 if current.confidence == "named" else 1,
                  0 if current.settings else 1)
        if mine < theirs:
            best[f.service] = f
    return list(best.values())


def suggestions(found: Iterable[Found], store: Any, *, overwrite: bool = False) -> list[Suggestion]:
    """What periscope would write for each finding, and what it is leaving alone.

    Uses the setting keys the specs already declare. A value the user has already set is never replaced: it
    lands in `skipped` with the reason instead, unless `overwrite=True` asks for it. Products periscope has
    no service for are dropped — they were still worth showing on the scan page, but there is nothing to
    write for them.
    """
    out: list[Suggestion] = []
    for f in sorted(_dedupe(found), key=lambda x: x.service):
        mapping = SERVICE_FOR.get(f.service)
        if mapping is None:
            continue
        service, url_key = mapping
        try:
            existing = dict(store.env_for(service))
        except Exception:      # noqa: BLE001 — a store that cannot answer means nothing is set yet
            log.debug("store.env_for(%s) failed", service, exc_info=True)
            existing = {}
        entry = {}
        try:
            entry = dict(store.services.get(service) or {})
        except Exception:      # noqa: BLE001
            log.debug("store.services unreadable", exc_info=True)

        wanted: dict[str, str] = {}
        if f.url:
            wanted[url_key] = f.url
        for k, v in f.settings.items():
            if v:
                wanted[k] = v

        write: dict[str, str] = {}
        skipped: dict[str, str] = {}
        for key, value in wanted.items():
            current = str(existing.get(key) or "").strip()
            if current and not overwrite:
                skipped[key] = "already set — left as it is"
            elif current and current == value:
                skipped[key] = "already set to this"
            else:
                write[key] = value
        secrets = tuple(k for k in f.secret_keys if k in write)
        out.append(Suggestion(
            service=service, title=TITLES.get(f.service, service), url=f.url, settings=write, skipped=skipped,
            secret_keys=secrets, already_configured=bool(existing.get(url_key)),
            enabled=bool(entry.get("enabled")), found=f))
    return out

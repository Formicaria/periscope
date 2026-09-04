"""Representative Engine API payloads the Messages page previews the board from (and the tests read).

One fictional host running a small media and infrastructure stack. The shapes are Docker's own — a container
list, a stats sample, an image list, a framed log stream — trimmed to the fields this bot reads. Everything is
fixed: no clocks, no random values, so a preview looks the same every time.
"""

from __future__ import annotations

from typing import Any

from .util import Container, cpu_percent, memory, parse_containers, watched

VERSION = {"Version": "27.1.1", "ApiVersion": "1.46", "Os": "linux", "Arch": "amd64",
           "KernelVersion": "6.8.0-45-generic", "GitCommit": "cc13f95"}
ENDPOINT = "/var/run/docker.sock"

# what a poll with DOCKER_IGNORE=buildx_* sees
INCLUDE: list[str] = []
IGNORE = ["buildx_*"]

CONTAINERS: list[dict[str, Any]] = [
    {"Id": "3f0a1c9d5e7b2a4c6d8e0f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5e",
     "Names": ["/jellyfin"], "Image": "linuxserver/jellyfin:10.9.11", "State": "running",
     "Status": "Up 12 days (healthy)", "Created": 1723900000,
     "Labels": {"com.docker.compose.project": "media"}},
    {"Id": "a1b2c3d4e5f60718293a4b5c6d7e8f9001122334455667788990aabbccddeeff",
     "Names": ["/traefik"], "Image": "traefik:v3.1", "State": "running", "Status": "Up 12 days",
     "Created": 1723900001, "Labels": {"com.docker.compose.project": "edge"}},
    {"Id": "9fceb02d3a1b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c4d5",
     "Names": ["/sonarr"], "Image": "linuxserver/sonarr:4.0.9", "State": "running",
     "Status": "Up 3 hours (unhealthy)", "Created": 1724800000,
     "Labels": {"com.docker.compose.project": "media"}},
    {"Id": "4b825dc642cb6eb9a060e54bf8d69288fbee4904aabbccddeeff00112233445",
     "Names": ["/radarr"], "Image": "linuxserver/radarr:5.11.0", "State": "exited",
     "Status": "Exited (137) 6 minutes ago", "Created": 1724800001,
     "Labels": {"com.docker.compose.project": "media"}},
    {"Id": "1f7ec0a5b3d94c2e8a6f0b1c3d5e7f9a2b4c6d8e0a1b2c3d4e5f60718293a4b5",
     "Names": ["/immich-ml"], "Image": "ghcr.io/immich-app/immich-machine-learning:v1.117.0",
     "State": "restarting", "Status": "Restarting (1) 12 seconds ago", "Created": 1725100000, "Labels": {}},
    {"Id": "c0ffee11deadbeef2233445566778899aabbccddeeff00112233445566778899",
     "Names": ["/pgbackup"], "Image": "prodrigestivill/postgres-backup-local:16", "State": "exited",
     "Status": "Exited (0) 2 hours ago", "Created": 1725000000, "Labels": {}},
    {"Id": "b0b1b2b3b4b5b6b7b8b9c0c1c2c3c4c5c6c7c8c9d0d1d2d3d4d5d6d7d8d9e0e1",
     "Names": ["/buildx_buildkit_default"], "Image": "moby/buildkit:buildx-stable-1", "State": "running",
     "Status": "Up 5 days", "Created": 1724500000, "Labels": {}},
]


def _stats(cpu_delta: int, system_delta: int, cpus: int, usage: int, cache: int, limit: int,
           rx: int, tx: int, read: int, write: int) -> dict[str, Any]:
    """A `GET /containers/{id}/stats?stream=false` body, written as the deltas it is meant to produce."""
    return {
        "cpu_stats": {"cpu_usage": {"total_usage": 12_000_000_000 + cpu_delta},
                      "system_cpu_usage": 1_000_000_000_000 + system_delta, "online_cpus": cpus},
        "precpu_stats": {"cpu_usage": {"total_usage": 12_000_000_000}, "system_cpu_usage": 1_000_000_000_000,
                         "online_cpus": cpus},
        "memory_stats": {"usage": usage, "limit": limit, "stats": {"inactive_file": cache}},
        "networks": {"eth0": {"rx_bytes": rx, "tx_bytes": tx}},
        "blkio_stats": {"io_service_bytes_recursive": [{"op": "read", "value": read}, {"op": "write", "value": write}]},
    }


# one sample per running container: jellyfin is busy, traefik is idle, sonarr is unhealthy but not loaded
STATS: dict[str, dict[str, Any]] = {
    "jellyfin": _stats(300_000_000, 10_000_000_000, 8, 1_476_395_008, 268_435_456, 8_589_934_592,
                       9_876_543_210, 1_234_567_890, 5_368_709_120, 1_073_741_824),
    "traefik": _stats(12_000_000, 10_000_000_000, 8, 96_468_992, 4_194_304, 8_589_934_592,
                      812_345_678, 734_003_200, 12_582_912, 4_194_304),
    "sonarr": _stats(150_000_000, 10_000_000_000, 8, 411_041_792, 27_262_976, 8_589_934_592,
                     345_678_901, 45_678_901, 268_435_456, 134_217_728),
}

IMAGES: list[dict[str, Any]] = [
    {"Id": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
     "RepoTags": ["linuxserver/jellyfin:10.9.11"],
     "RepoDigests": ["linuxserver/jellyfin@sha256:aaaa000000000000000000000000000000000000000000000000000000000001"],
     "Size": 1_073_741_824},
    {"Id": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
     "RepoTags": ["traefik:v3.1"],
     "RepoDigests": ["traefik@sha256:bbbb000000000000000000000000000000000000000000000000000000000002"],
     "Size": 178_257_920},
    {"Id": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
     "RepoTags": ["linuxserver/sonarr:4.0.9"],
     "RepoDigests": ["linuxserver/sonarr@sha256:cccc000000000000000000000000000000000000000000000000000000000003"],
     "Size": 419_430_400},
    {"Id": "sha256:4444444444444444444444444444444444444444444444444444444444444444",
     "RepoTags": ["<none>:<none>"], "RepoDigests": [], "Size": 12_582_912},
]

# what `GET /distribution/{ref}/json` answers: traefik and sonarr have moved on, jellyfin has not
REGISTRY_DIGESTS = {
    "linuxserver/jellyfin:10.9.11": "sha256:aaaa000000000000000000000000000000000000000000000000000000000001",
    "traefik:v3.1": "sha256:bbbb000000000000000000000000000000000000000000000000000000000099",
    "linuxserver/sonarr:4.0.9": "sha256:cccc000000000000000000000000000000000000000000000000000000000077",
}
UPDATES = ["linuxserver/sonarr:4.0.9", "traefik:v3.1"]

LOG_LINES = [
    "[2026-09-02 21:14:03] Starting Sonarr v4.0.9.2244",
    "[2026-09-02 21:14:04] Connected to database",
    "[2026-09-02 21:14:09] Health check failed: Indexer NZBgeek is unavailable",
]


def log_stream(lines: list[str] | None = None, stream: int = 1) -> bytes:
    """The lines as the daemon frames them for a container without a TTY: 8 header bytes then the payload."""
    out = b""
    for line in lines if lines is not None else LOG_LINES:
        payload = (line + "\n").encode()
        out += bytes([stream, 0, 0, 0]) + len(payload).to_bytes(4, "big") + payload
    return out


def containers() -> list[Container]:
    """The watched containers of one poll: parsed, filtered, and with the stats sample applied."""
    out = [c for c in parse_containers(CONTAINERS) if watched(c.name, INCLUDE, IGNORE)]
    for c in out:
        sample = STATS.get(c.name)
        if sample and c.running:
            c.cpu_pct = cpu_percent(sample)
            c.mem_used, c.mem_limit = memory(sample)
    return out

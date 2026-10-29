"""Is it up? One probe per check kind, each answering in a few seconds and never raising.

    plex       GET <url>/identity — Plex answers this without a token; we read its version back
    http       GET <url> — any answer below 500 counts (a login page or a 401 still means the app is alive)
    tcp        connect to host:port
    minecraft  connect, then the Server List Ping for version + players; a server that accepts the connection
               but fumbles the ping still counts as up
    guest      no network probe at all — the Proxmox guest's state decides (filled in by the watcher)
"""

from __future__ import annotations

import asyncio
import json
import re
import struct
import time
from dataclasses import dataclass

import aiohttp

from .config import Resource

TIMEOUT_S = 6


@dataclass
class Probe:
    ok: bool | None            # None = this check has no network probe (guest)
    ms: int | None = None
    detail: str = ""           # "v1.43.5", "3/20 players", "HTTP 502", "connection refused"


def _split(target: str) -> tuple[str, int]:
    host, _, port = target.rpartition(":")
    return host, int(port)


async def probe_http(session: aiohttp.ClientSession, url: str, *, plex: bool = False) -> Probe:
    t0 = time.monotonic()
    full = url.rstrip("/") + "/identity" if plex else url
    try:
        async with session.get(full, ssl=False, allow_redirects=False,
                               headers={"Accept": "application/json"}) as r:
            body = await r.text(errors="replace")
            ms = int((time.monotonic() - t0) * 1000)
            if plex:
                if r.status != 200:
                    return Probe(False, ms, f"HTTP {r.status}")
                version = ""
                try:
                    version = json.loads(body).get("MediaContainer", {}).get("version", "")
                except ValueError:
                    m = re.search(r'version="([^"]+)"', body)
                    version = m.group(1) if m else ""
                return Probe(True, ms, f"v{version.split('-')[0]}" if version else "")
            if r.status >= 500:
                return Probe(False, ms, f"HTTP {r.status}")
            return Probe(True, ms, "")
    except asyncio.TimeoutError:
        return Probe(False, None, "timed out")
    except aiohttp.ClientConnectorError as e:
        return Probe(False, None, _words(e))
    except aiohttp.ClientError as e:
        return Probe(False, None, type(e).__name__)


def _words(e: BaseException) -> str:
    text = str(getattr(e, "os_error", "") or e).lower()
    if "refused" in text:
        return "connection refused"
    if "no route" in text or "unreachable" in text:
        return "host unreachable"
    if "timed out" in text or "timeout" in text:
        return "timed out"
    if "name" in text and ("resolve" in text or "not known" in text):
        return "name doesn't resolve"
    return "can't connect"


async def probe_tcp(target: str) -> Probe:
    host, port = _split(target)
    t0 = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TIMEOUT_S)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return Probe(True, int((time.monotonic() - t0) * 1000))
    except asyncio.TimeoutError:
        return Probe(False, None, "timed out")
    except OSError as e:
        return Probe(False, None, _words(e))


def _varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += struct.pack("B", b | (0x80 if n else 0))
        if not n:
            return out


async def _read_varint(reader: asyncio.StreamReader) -> int:
    n = shift = 0
    for _ in range(5):
        b = (await reader.readexactly(1))[0]
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n
        shift += 7
    raise ValueError("varint too long")


async def probe_minecraft(target: str) -> Probe:
    host, port = _split(target)
    t0 = time.monotonic()
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), TIMEOUT_S)
    except asyncio.TimeoutError:
        return Probe(False, None, "timed out")
    except OSError as e:
        return Probe(False, None, _words(e))
    ms = int((time.monotonic() - t0) * 1000)
    try:
        hb = host.encode()
        # handshake (protocol 760 — servers answer a status ping whatever version you claim), then status request
        handshake = b"\x00" + _varint(760) + _varint(len(hb)) + hb + struct.pack(">H", port) + b"\x01"
        writer.write(_varint(len(handshake)) + handshake + b"\x01\x00")
        await writer.drain()
        await asyncio.wait_for(_read_varint(reader), TIMEOUT_S)         # packet length
        await asyncio.wait_for(_read_varint(reader), TIMEOUT_S)         # packet id
        size = await asyncio.wait_for(_read_varint(reader), TIMEOUT_S)
        data = json.loads((await asyncio.wait_for(reader.readexactly(size), TIMEOUT_S)).decode("utf-8", "replace"))
        players = data.get("players") or {}
        version = (data.get("version") or {}).get("name", "")
        detail = f"{players.get('online', 0)}/{players.get('max', '?')} players"
        if version:
            detail += f" · {version[:24]}"
        return Probe(True, ms, detail)
    except Exception:  # noqa: BLE001 — accepted the connection, fumbled the ping: still up
        return Probe(True, ms, "")
    finally:
        writer.close()


async def probe(session: aiohttp.ClientSession, res: Resource) -> Probe:
    try:
        if res.check == "plex":
            return await probe_http(session, res.target, plex=True)
        if res.check == "http":
            return await probe_http(session, res.target)
        if res.check == "tcp":
            return await probe_tcp(res.target)
        if res.check == "minecraft":
            return await probe_minecraft(res.target)
        return Probe(None)
    except Exception as e:  # noqa: BLE001 — a probe never takes the loop down
        return Probe(False, None, type(e).__name__)

"""Pure helpers for interpreting UniFi API payloads (no network, unit-tested)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from periscope import human_bytes, human_duration

_MAC_RE = re.compile(r"^([0-9a-f]{2}:){5}[0-9a-f]{2}$")
_MAC_BARE_RE = re.compile(r"^[0-9a-f]{12}$")

DEVICE_TYPES = {"uap": "Access Point", "usw": "Switch", "ugw": "Gateway", "udm": "Console", "uxg": "Gateway",
                "uck": "Cloud Key"}
_STATUS_DOTS = {"ok": "🟢", "warning": "🟡", "error": "🔴"}


# ----- macs & names ------------------------------------------------------

def normalize_mac(raw: str) -> str:
    """Accepts `AA-BB-CC-DD-EE-FF`, `aabb.ccdd.eeff`, `aabbccddeeff`, `aa:bb:...` → `aa:bb:cc:dd:ee:ff`."""
    s = raw.strip().lower().replace("-", ":").replace(".", "")
    if ":" not in s and _MAC_BARE_RE.match(s):
        s = ":".join(s[i:i + 2] for i in range(0, 12, 2))
    return s


def is_mac(s: str) -> bool:
    return bool(_MAC_RE.match(s))


def client_name(c: dict[str, Any]) -> str:
    return c.get("name") or c.get("hostname") or c.get("mac") or "?"


def device_name(d: dict[str, Any]) -> str:
    return d.get("name") or d.get("model") or d.get("mac") or "?"


def device_type(d: dict[str, Any]) -> str:
    return DEVICE_TYPES.get(str(d.get("type", "")).lower(), str(d.get("type") or "device"))


def find_client(clients: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    """Match by MAC first, then exact/prefix/substring on name or hostname (case-insensitive)."""
    q = normalize_mac(query)
    if is_mac(q):
        return next((c for c in clients if c.get("mac") == q), None)
    ql = query.strip().lower()
    names = [(c, str(c.get("name") or "").lower(), str(c.get("hostname") or "").lower()) for c in clients]
    for c, n, h in names:
        if ql in (n, h):
            return c
    for c, n, h in names:
        if n.startswith(ql) or h.startswith(ql):
            return c
    for c, n, h in names:
        if ql in n or ql in h:
            return c
    return None


def find_device(devices: list[dict[str, Any]], query: str) -> dict[str, Any] | None:
    q = normalize_mac(query)
    if is_mac(q):
        return next((d for d in devices if d.get("mac") == q), None)
    ql = query.strip().lower()
    return (next((d for d in devices if device_name(d).lower() == ql), None)
            or next((d for d in devices if ql in device_name(d).lower()), None))


# ----- clients -----------------------------------------------------------

def client_link(c: dict[str, Any], devices_by_mac: dict[str, dict[str, Any]] | None = None) -> str:
    """Where the client is attached: `📶 SSID` or `🔌 Switch port N`."""
    if c.get("is_wired"):
        sw = (devices_by_mac or {}).get(c.get("sw_mac") or "")
        label = device_name(sw) if sw else "wired"
        port = c.get("sw_port")
        return f"🔌 {label}" + (f" port {port}" if port else "")
    ap = (devices_by_mac or {}).get(c.get("ap_mac") or "")
    ssid = c.get("essid") or "wifi"
    return f"📶 {ssid}" + (f" via {device_name(ap)}" if ap else "")


def client_signal(c: dict[str, Any]) -> str:
    if c.get("is_wired"):
        return ""
    sig = c.get("signal")
    if sig is None:
        sig = c.get("rssi")
    return f"{sig} dBm" if sig is not None else ""


def client_line(c: dict[str, Any], devices_by_mac: dict[str, dict[str, Any]] | None = None) -> str:
    bits = [client_link(c, devices_by_mac)]
    if sig := client_signal(c):
        bits.append(sig)
    if c.get("uptime"):
        bits.append(f"up {human_duration(c['uptime'])}")
    bits.append(f"↓{human_bytes(c.get('rx_bytes'))} ↑{human_bytes(c.get('tx_bytes'))}")
    return f"**{client_name(c)}** `{c.get('ip') or '—'}` `{c.get('mac', '?')}`\n└ " + " · ".join(bits)


# ----- devices -----------------------------------------------------------

def device_online(d: dict[str, Any]) -> bool:
    return d.get("state") == 1


def _stat(d: dict[str, Any], key: str) -> float | None:
    try:
        v = (d.get("system-stats") or {}).get(key)
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def device_cpu(d: dict[str, Any]) -> float | None:
    return _stat(d, "cpu")


def device_mem(d: dict[str, Any]) -> float | None:
    return _stat(d, "mem")


def device_temp(d: dict[str, Any]) -> float | None:
    if d.get("general_temperature") is not None:
        return float(d["general_temperature"])
    temps = [t.get("value") for t in d.get("temperatures") or [] if isinstance(t.get("value"), (int, float))]
    return max(temps) if temps else None


def device_clients(d: dict[str, Any]) -> int:
    n = d.get("num_sta")
    if n is None:
        n = d.get("user-num_sta")
    return int(n or 0)


def device_line(d: dict[str, Any]) -> str:
    dot = "🟢" if device_online(d) else "🔴"
    bits = []
    if (cpu := device_cpu(d)) is not None:
        bits.append(f"cpu {cpu:.0f}%")
    if (temp := device_temp(d)) is not None:
        bits.append(f"{temp:.0f}°C")
    bits.append(f"{device_clients(d)} clients")
    if d.get("upgradable"):
        bits.append("⬆ fw")
    return f"{dot} **{device_name(d)}** ({d.get('model', '?')}) · " + " · ".join(bits)


# ----- health ------------------------------------------------------------

@dataclass
class WanStatus:
    present: bool
    ok: bool | None          # None when no gateway/WAN subsystem is reported
    status: str
    ip: str | None
    latency_ms: float | None
    uptime_s: int | None
    rx_bps: float | None
    tx_bps: float | None
    gw_name: str | None
    isp: str | None


def health_map(health: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(h.get("subsystem")): h for h in health}


def status_dot_for(status: str | None) -> str:
    return _STATUS_DOTS.get(str(status or "").lower(), "⚪")


def wan_summary(health: list[dict[str, Any]]) -> WanStatus:
    m = health_map(health)
    wan, www = m.get("wan"), m.get("www", {})
    if not wan:
        return WanStatus(False, None, "unknown", None, None, None, None, None, None, None)
    status = str(wan.get("status") or "unknown")
    www_status = str(www.get("status") or "unknown")
    ok = status == "ok" and www_status != "error"
    latency = www.get("latency")
    if latency is None:
        latency = wan.get("latency")
    uptime = www.get("uptime")
    if uptime is None:
        uptime = (wan.get("gw_system-stats") or {}).get("uptime")
    return WanStatus(
        present=True, ok=ok, status=status if ok else (www_status if www_status == "error" else status),
        ip=wan.get("wan_ip"),
        latency_ms=float(latency) if latency is not None else None,
        uptime_s=int(uptime) if uptime is not None else None,
        rx_bps=wan.get("rx_bytes-r"), tx_bps=wan.get("tx_bytes-r"),
        gw_name=wan.get("gw_name"), isp=www.get("isp_name") or wan.get("isp_name"),
    )


def client_counts(health: list[dict[str, Any]]) -> dict[str, int]:
    m = health_map(health)
    lan, wlan = m.get("lan", {}), m.get("wlan", {})
    wired = int(lan.get("num_user") or 0)
    wireless = int(wlan.get("num_user") or 0)
    guest = int(lan.get("num_guest") or 0) + int(wlan.get("num_guest") or 0)
    iot = int(lan.get("num_iot") or 0) + int(wlan.get("num_iot") or 0)
    return {"wired": wired, "wireless": wireless, "guest": guest, "iot": iot,
            "total": wired + wireless + guest + iot}


# ----- events & alarms ---------------------------------------------------

def event_line(ev: dict[str, Any]) -> str:
    ts = int(int(ev.get("time") or 0) / 1000)
    msg = str(ev.get("msg") or ev.get("key") or "event")
    return f"<t:{ts}:R> {msg}" if ts else msg

"""Small network helpers shared by the CLI and the web UI (which address is this box reachable on?)."""

from __future__ import annotations

import socket


def lan_ip() -> str:
    """The address this box would use to reach the LAN/Internet — what a browser on the same network can
    open. Falls back to the hostname when there is no route (an unplugged box, a sandbox)."""
    for probe in ("10.255.255.255", "1.1.1.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 53))  # UDP: nothing is sent, the kernel just picks the source address
            ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            pass
        finally:
            s.close()
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return socket.gethostname()


def web_url(store, host: str | None = None, port: int | None = None) -> str:
    """`web.base_url` when set, else http://<lan ip>:<port> — the address printed at startup and by `periscope web`."""
    base = str(store.web.get("base_url") or "").strip().rstrip("/")
    if base:
        return base
    h = host or str(store.web.get("host") or "0.0.0.0")
    if h in ("0.0.0.0", "::", ""):
        h = lan_ip()
    return f"http://{h}:{port or store.web.get('port', 8090)}"

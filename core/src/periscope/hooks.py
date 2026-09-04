"""Optional facilities a bot always has, whether or not the real thing is installed or configured.

`bot.history` and `bot.windows` exist on every ServiceBot and LabBot. When the event log or the maintenance
windows are not available (a bare install, a test, a service built by hand), they are the no-op versions here,
so a send site can call them unconditionally:

    bot.history.record(service="proxmox", kind="alert", key=fp, severity="warning", title="High CPU on pve1")
    if bot.windows.quiet("proxmox", server="main"):
        return          # a maintenance window is open: do not page anyone

The real implementations live in `periscope.history` and `periscope.maintenance`; the runtime swaps them in
when they are importable. Neither may raise: a failed write is logged, never propagated to the bot.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class NullHistory:
    """What `bot.history` is when nothing records: every call is accepted and dropped."""

    enabled = False

    def record(self, *, service: str, kind: str, key: str = "", severity: str = "info", title: str = "",
               detail: str = "", server: str = "", value: float | None = None,
               payload: dict[str, Any] | None = None) -> None:
        return None

    def sample(self, *, service: str, metric: str, value: float, key: str = "", server: str = "") -> None:
        return None

    def events(self, **kw: Any) -> list[dict[str, Any]]:
        return []

    def series(self, **kw: Any) -> list[tuple[float, float]]:
        return []

    def counts(self, **kw: Any) -> dict[str, int]:
        return {}

    def uptime(self, **kw: Any) -> float | None:
        return None

    def prune(self) -> int:
        return 0

    def close(self) -> None:
        return None


class NullWindows:
    """What `bot.windows` is when maintenance windows are not configured: nothing is ever quiet."""

    enabled = False

    def quiet(self, service: str = "", *, server: str = "", key: str = "") -> bool:
        return False

    def active(self) -> list[dict[str, Any]]:
        return []

    def reason(self, service: str = "", *, server: str = "", key: str = "") -> str:
        return ""


def history_for(path: Any, retention_days: int = 90) -> Any:
    """The real event log when `periscope.history` is installed, else the no-op one."""
    try:
        from .history import History
    except ImportError:
        return NullHistory()
    try:
        return History(path, retention_days=retention_days)
    except Exception as e:  # noqa: BLE001 - the bots must run even when the log cannot be opened
        log.error("event log unavailable (%s) — running without history", e)
        return NullHistory()


def windows_for(path: Any) -> Any:
    """The real maintenance windows when `periscope.maintenance` is installed, else the no-op ones."""
    try:
        from .maintenance import Windows
    except ImportError:
        return NullWindows()
    try:
        return Windows(path)
    except Exception as e:  # noqa: BLE001
        log.error("maintenance windows unavailable (%s) — nothing will be suppressed", e)
        return NullWindows()

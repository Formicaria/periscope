"""Whole-process restart: re-exec the runtime with its original command line after a short delay.

Config edits are written to config/periscope.yaml immediately but the runtime reads them at start, so every
"saved" page says "restart to apply". Under systemd the PID stays the same (exec, not exit), so the unit keeps
running; the listening sockets are close-on-exec and are re-bound by the new image.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

log = logging.getLogger(__name__)


def command_line() -> list[str]:
    """[executable, *args] that started this process (`python -m periscope` survives the round trip)."""
    args = list(getattr(sys, "orig_argv", None) or sys.argv)
    return [sys.executable, *args[1:]] if args else [sys.executable, "-m", "periscope"]


def exec_now() -> None:
    argv = command_line()
    log.warning("restarting: %s", " ".join(argv))
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    os.execv(argv[0], argv)


def schedule(delay: float = 1.0, loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Re-exec in `delay` seconds — enough for the HTTP response to leave."""
    loop = loop or asyncio.get_running_loop()
    loop.call_later(delay, exec_now)

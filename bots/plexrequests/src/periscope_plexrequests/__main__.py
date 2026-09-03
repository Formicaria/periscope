"""Entry point: `python -m periscope_plexrequests` — run this service alone from a `.env` (the v1-style
`periscope@plexrequests` unit and the Docker image use this). It hosts the service on the v2 runtime with a
single presence built from DISCORD_TOKEN, so behaviour is identical to running inside `python -m periscope`."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Mapping

from periscope import Store, load_dotenv_if_present, setup_logging
from periscope.runtime import Runtime

from .service import SERVICES

SERVICE = SERVICES[0]
LAB_KEYS = {"LAB_NAME": "name", "LAB_COLOR": "color", "GUILD_ID": "guild_id", "STATUS_CHANNEL_ID": "status_channel_id",
            "ALERT_CHANNEL_ID": "alert_channel_id", "ALERT_ROLE_ID": "alert_role_id", "LOG_LEVEL": "log_level"}


def store_from_env(environ: Mapping[str, str], root: Path) -> Store:
    """An in-memory v2 store with one presence (DISCORD_TOKEN) and this service enabled from the flat env."""
    store = Store(root / "config" / "periscope.yaml")
    for key, field in LAB_KEYS.items():
        if environ.get(key):
            store.lab[field] = environ[key]
    if environ.get("ADMIN_ROLE_IDS"):
        store.lab["admin_role_ids"] = [x.strip() for x in environ["ADMIN_ROLE_IDS"].split(",") if x.strip()]
    if environ.get("WEBHOOK_PORT", "").isdigit():
        store.webhook["port"] = int(environ["WEBHOOK_PORT"])
    store.presences["default"] = {"token": environ.get("DISCORD_TOKEN", ""), "label": SERVICE.title}
    keys = {s.key for s in SERVICE.settings}
    env = {k: v for k, v in environ.items() if k in keys and v}
    store.services[SERVICE.name] = {"enabled": True, "presence": "default", "env": env}
    return store


def main() -> int:
    load_dotenv_if_present(".env")
    if not os.environ.get("DISCORD_TOKEN"):
        print("config error: DISCORD_TOKEN is not set", file=sys.stderr)
        return 2
    missing = SERVICE.required_missing(dict(os.environ))
    if missing:
        print(f"config error: missing {', '.join(missing)}", file=sys.stderr)
        return 2
    root = Path(os.environ.get("DATA_DIR") or "data").resolve().parent
    store = store_from_env(os.environ, root)
    setup_logging(str(store.lab.get("log_level") or "INFO"))
    rt = Runtime(store, root)

    async def _run() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, rt.request_stop)
            except (NotImplementedError, RuntimeError):
                pass
        await rt.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

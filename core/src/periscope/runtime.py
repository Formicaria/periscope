"""v2 runtime: one process hosting every enabled service across one or more Discord presences.

    python -m periscope                # uses ./config/periscope.yaml (migrating bots/*/.env on first run)
    PERISCOPE_CONFIG=/path/x.yaml ...

The runtime also starts the shared webhook listener and the web UI, and writes data/runtime.json every
few seconds so the CLI (`periscope list`) can show health without talking to Discord.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any

from .config import Settings, env_scope
from .logging import setup_logging
from .presence import Presence, build_intents
from .registry import discover
from .service import ServiceBot, ServiceSpec
from .state import JsonState
from .store import Store
from .webhook import WebhookServer

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, store: Store, root: Path):
        self.store = store
        self.root = root
        self.data_dir = root / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state = JsonState(self.data_dir / "state.json")
        self.specs: dict[str, ServiceSpec] = discover()
        self.presences: dict[str, Presence] = {}
        self.services: dict[str, ServiceBot] = {}
        self.skipped: dict[str, str] = {}
        self.webhook: WebhookServer | None = None
        self.started = time.time()
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []

    # ----- assembly --------------------------------------------------------------------------
    def assemble(self) -> None:
        lab = self.store.lab
        guild_id = int(lab["guild_id"]) if str(lab.get("guild_id") or "").strip() else None
        admin_ids = [int(x) for x in (lab.get("admin_role_ids") or []) if str(x).strip()]
        wh = self.store.webhook
        self.webhook = WebhookServer(str(wh.get("host", "0.0.0.0")), int(wh.get("port", 8080)), str(wh.get("secret") or "") or None)

        # pass 1: which services are runnable, and which gateway intents each presence must carry — the
        # intents are part of the Discord identify payload, so they have to be known before Presence() is built
        runnable: list[tuple[str, ServiceSpec, str, str, dict[str, str]]] = []
        intents: dict[str, set[str]] = {}
        for name in self.store.enabled_services():
            spec = self.specs.get(name)
            if spec is None:
                self.skipped[name] = "unknown service (package not installed?)"
                log.warning("service %s enabled but not installed", name)
                continue
            pname = self.store.presence_for(name)
            token = self.store.token_for(name)
            if not token:
                self.skipped[name] = f"presence '{pname}' has no token"
                log.warning("service %s skipped: presence %s has no token", name, pname)
                continue
            env = self.store.env_for(name)
            missing = spec.required_missing(env)
            if missing:
                self.skipped[name] = "missing: " + ", ".join(missing)
                log.warning("service %s skipped: missing %s", name, missing)
                continue
            runnable.append((name, spec, pname, token, env))
            intents.setdefault(pname, set()).update(spec.intents)

        # pass 2: one presence per token, carrying the union of its services' intents
        for name, spec, pname, token, env in runnable:
            pres = self.presences.get(pname)
            if pres is None:
                pres = Presence(pname, token, guild_id=guild_id, admin_role_ids=admin_ids, lab_name=str(lab.get("name") or "lab"),
                                intents=build_intents(intents.get(pname, ())))
                self.presences[pname] = pres
                if intents.get(pname):
                    log.info("[%s] gateway intents beyond default: %s", pname, ", ".join(sorted(intents[pname])))
            with env_scope(env):
                settings = Settings.from_env()
            sb = ServiceBot(spec, pres, settings, env, self.state, self.webhook)
            if self.webhook and env.get("WEBHOOK_SECRET"):
                self.webhook.accept_secret(env["WEBHOOK_SECRET"])
            pres.services.append(sb)
            self.services[name] = sb
        log.info("assembled %d services on %d presences (%d skipped)", len(self.services), len(self.presences), len(self.skipped))

    # ----- run ----------------------------------------------------------------------------------
    async def run(self) -> None:
        self.assemble()
        if self.webhook:
            self.webhook.set_health_check(lambda: all(p.connected for p in self.presences.values()) if self.presences else True)
            try:
                await self.webhook.start()
            except OSError as e:
                log.error("webhook listener failed to start: %s", e)
        for pres in self.presences.values():
            self._tasks.append(asyncio.create_task(self._supervise(pres), name=f"presence:{pres.name}"))
        self._tasks.append(asyncio.create_task(self._heartbeat(), name="heartbeat"))
        web_task = await self._start_web()
        if web_task:
            self._tasks.append(web_task)
        if not self.presences:
            log.warning("no service is runnable — open the web UI or run `periscope init` to configure one")
        await self._stop.wait()
        await self.shutdown()

    async def _supervise(self, pres: Presence) -> None:
        backoff = 5
        last_msg = ""
        while not self._stop.is_set():
            try:
                await pres.start(pres.token)
                return  # clean close
            except Exception as e:  # noqa: BLE001
                pres.connected = False
                msg = f"{type(e).__name__}: {e}"
                if msg != last_msg:
                    log.error("[%s] presence down: %s — retrying in %ss", pres.name, msg, backoff, exc_info=backoff == 5)
                    last_msg = msg
                else:
                    log.warning("[%s] still down (%s) — retrying in %ss", pres.name, type(e).__name__, backoff)
                for sb in pres.services:
                    sb.last_error = msg
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300)

    async def _start_web(self) -> asyncio.Task | None:
        try:
            from periscope_web.app import serve  # optional package
        except ImportError:
            log.info("web UI not installed (periscope_web); skipping")
            return None
        web = self.store.web
        try:
            return asyncio.create_task(serve(self, str(web.get("host", "0.0.0.0")), int(web.get("port", 8090))), name="web")
        except Exception as e:  # noqa: BLE001
            log.error("web UI failed to start: %s", e)
            return None

    async def _heartbeat(self) -> None:
        while not self._stop.is_set():
            try:
                self.write_status()
            except Exception:  # noqa: BLE001
                log.debug("status write failed", exc_info=True)
            await asyncio.sleep(10)

    # ----- status ---------------------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        out: dict[str, Any] = {"pid": os.getpid(), "started": self.started, "uptime_s": int(time.time() - self.started),
                               "presences": {}, "services": {}}
        for pname, pres in self.presences.items():
            out["presences"][pname] = {"connected": pres.connected, "user": str(pres.user) if pres.user else None,
                                       "services": [s.name for s in pres.services]}
        for name, sb in self.services.items():
            out["services"][name] = {"state": "running" if sb.healthy and sb.presence.connected else ("error" if not sb.healthy else "connecting"),
                                     "presence": sb.presence.name, "error": sb.last_error}
        for name, why in self.skipped.items():
            out["services"][name] = {"state": "skipped", "presence": self.store.presence_for(name), "error": why}
        return out

    def write_status(self) -> None:
        p = self.data_dir / "runtime.json"
        p.write_text(json.dumps(self.status(), indent=1))

    # ----- stop -----------------------------------------------------------------------------------
    def request_stop(self) -> None:
        self._stop.set()

    async def shutdown(self) -> None:
        log.info("shutting down")
        for pres in self.presences.values():
            try:
                await pres.close()
            except Exception:  # noqa: BLE001
                pass
        if self.webhook:
            await self.webhook.stop()
        for t in self._tasks:
            t.cancel()
        try:
            (self.data_dir / "runtime.json").unlink()
        except FileNotFoundError:
            pass


def find_root() -> Path:
    env = os.environ.get("PERISCOPE_ROOT")
    if env:
        return Path(env)
    here = Path.cwd()
    for cand in (here, *here.parents):
        if (cand / "periscope.cli").exists() or (cand / "config" / "periscope.yaml").exists():
            return cand
    return here


def load_store(root: Path) -> Store:
    path = Path(os.environ.get("PERISCOPE_CONFIG") or root / "config" / "periscope.yaml")
    store = Store.load(path)
    if not store.exists:
        from .migrate import migrate_v1
        imported = migrate_v1(store, root)
        if imported:
            log.info("imported v1 config for: %s", ", ".join(imported))
        store.save()
    return store


def main() -> int:
    root = find_root()
    store = load_store(root)
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

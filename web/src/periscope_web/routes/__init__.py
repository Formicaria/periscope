"""One APIRouter per page; `register()` mounts them all."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import FastAPI, Request

log = logging.getLogger(__name__)


def register(app: FastAPI) -> None:
    from . import (
        alerts, api, discover, login, logs, messages, overview, presences, routing, servers, services, setup, trends,
    )

    for mod in (login, overview, services, presences, servers, messages, alerts, trends, routing, discover, logs,
                setup, api):
        app.include_router(mod.router)


def save(request: Request) -> None:
    """Persist the store and make it true in the running process: every service whose settings changed is
    rebuilt in place. Only what a rebuild cannot cover — a new bot token, a brand-new bot — is remembered as
    something the header should ask for a restart about."""
    st = request.app.state
    st.runtime.store.save()
    st.guild.invalidate()
    apply = getattr(st.runtime, "apply_config", None)
    if apply is None:                      # a runtime without hot apply (tests, an older core)
        st.pending.append("settings changed")
        return
    task = asyncio.create_task(_apply(st))
    st.applying = task


async def _apply(st: Any) -> None:
    try:
        notes = await st.runtime.apply_config()
    except Exception:  # noqa: BLE001 - a failed apply must never break the page that saved
        log.exception("applying the new configuration failed")
        st.pending.append("the new settings could not be applied — restart to be sure")
        return
    for note in notes:
        if "restart" in note:
            st.pending.append(note)
        else:
            log.info("applied: %s", note)


def messages_saved(request: Request) -> None:
    """After a message customisation: the message store already wrote itself, and the bots re-read it before the
    next post — so this only picks the write back up, and never raises the "restart to apply" flag."""
    request.app.state.runtime.messages.reload()

"""One APIRouter per page; `register()` mounts them all."""

from __future__ import annotations

from fastapi import FastAPI, Request


def register(app: FastAPI) -> None:
    from . import api, login, logs, messages, overview, presences, routing, servers, services, setup

    for mod in (login, overview, services, presences, servers, messages, routing, logs, setup, api):
        app.include_router(mod.router)


def save(request: Request) -> None:
    """Persist the store and remember that a restart is now needed."""
    st = request.app.state
    st.runtime.store.save()
    st.changed = True
    st.guild.invalidate()


def messages_saved(request: Request) -> None:
    """After a message customisation: the message store already wrote itself, and the bots re-read it before the
    next post — so this only picks the write back up, and never raises the "restart to apply" flag."""
    request.app.state.runtime.messages.reload()

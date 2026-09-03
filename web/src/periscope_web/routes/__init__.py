"""One APIRouter per page; `register()` mounts them all."""

from __future__ import annotations

from fastapi import FastAPI, Request


def register(app: FastAPI) -> None:
    from . import api, lab, login, logs, overview, presences, routing, services, setup

    for mod in (login, overview, services, presences, lab, routing, logs, setup, api):
        app.include_router(mod.router)


def save(request: Request) -> None:
    """Persist the store and remember that a restart is now needed."""
    st = request.app.state
    st.runtime.store.save()
    st.changed = True
    st.guild.invalidate()

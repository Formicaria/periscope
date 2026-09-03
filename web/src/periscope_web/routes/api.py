"""JSON: /api/status (runtime.status()), /api/config (store.redacted()), /healthz (public)."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..forms import mask_secrets

router = APIRouter()


@router.get("/api/status")
async def api_status(request: Request):
    runtime = request.app.state.runtime
    data = mask_secrets(runtime.status())
    data["restart_needed"] = bool(request.app.state.dirty())
    return JSONResponse(data)


@router.get("/api/config")
async def api_config(request: Request):
    return JSONResponse(mask_secrets(request.app.state.runtime.store.redacted()))


@router.get("/healthz")
async def healthz(request: Request):
    runtime = request.app.state.runtime
    presences = getattr(runtime, "presences", {}) or {}
    connected = all(getattr(p, "connected", False) for p in presences.values()) if presences else True
    return JSONResponse({"ok": True, "presences_connected": connected, "uptime_s": int(time.time() - float(getattr(runtime, "started", time.time())))})

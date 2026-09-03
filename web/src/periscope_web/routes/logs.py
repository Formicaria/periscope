"""/logs: the ring buffer, a substring/level filter, a live SSE tail, and a download."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query, Request
from fastapi.responses import PlainTextResponse, StreamingResponse

from ..logs import LEVELS, sse
from ..render import render

log = logging.getLogger(__name__)
router = APIRouter()


def _filters(request: Request) -> list[dict]:
    """Quick-pick chips: every service + presence name (log lines carry `periscope_<x>` / `[<x>]`)."""
    runtime = request.app.state.runtime
    names = list(runtime.specs) + [n for n in runtime.store.services if n not in runtime.specs]
    chips = [{"label": n, "q": n} for n in names]
    chips += [{"label": f"[{p}]", "q": f"[{p}]"} for p in runtime.store.presences]
    return chips


@router.get("/logs")
async def logs_page(request: Request, q: str = "", level: str = ""):
    buf = request.app.state.logs
    level = level.upper() if level.upper() in LEVELS else ""
    lines = [line for line in buf.snapshot() if line.matches(q, level)]
    return render(request, "logs.html", {"lines": lines[-2000:], "q": q, "level": level, "levels": LEVELS[1:],
                                         "since": buf.last_seq(), "chips": _filters(request)})


@router.get("/logs/stream")
async def logs_stream(request: Request, q: str = "", level: str = "", since: int = 0,
                      limit: int | None = Query(None, alias="max", description="end after N lines (curl/tests)")):
    buf = request.app.state.logs
    level = level.upper() if level.upper() in LEVELS else ""
    limit = limit if limit and limit > 0 else None

    async def gen():
        async for frame in _frames(buf, since, q, level, limit):
            yield frame

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


async def _frames(buf, since: int, q: str, level: str, limit: int | None):
    async for line in buf.stream(since=since, q=q, min_level=level, limit=limit):
        for frame in sse([line]):
            yield frame


@router.get("/logs/download")
async def logs_download(request: Request, q: str = "", level: str = ""):
    buf = request.app.state.logs
    level = level.upper() if level.upper() in LEVELS else ""
    text = "\n".join(line.text for line in buf.snapshot() if line.matches(q, level)) + "\n"
    return PlainTextResponse(text, headers={"Content-Disposition": 'attachment; filename="periscope.log"'})

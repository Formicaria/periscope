"""/discover: find the services already running here, and fill their settings in.

A scan runs as a background job so the request comes straight back; the page polls a fragment until the job
says it is finished. Nothing scans on its own — an admin has to press the button, and the copy on the page
says so.

`Use this` writes only the settings that are still empty, runs the service's own `check()` against what was
written, and then offers to switch it on. Findings that carry an API key (from a compose file or an *arr
config) keep it in the job on the server; the key is never rendered and never put in a form, so the page can
say "found an API key" and nothing more.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from periscope.discovery import (
    SERVICE_FOR,
    Found,
    Suggestion,
    default_hosts,
    from_arr_config,
    from_compose,
    scan,
    suggestions,
)

from ..render import flash, is_htmx, partial, redirect, render, toasts
from . import save

log = logging.getLogger(__name__)
router = APIRouter()

POLL_MS = 1200          # how often the page asks the job how it is doing
JOB_MAX_AGE_S = 900     # a finished job is kept this long, so a reload still shows the last result
CHECK_TIMEOUT_S = 30


@dataclass
class ScanJob:
    """One run of the scanner, and whatever has been imported from files alongside it."""

    hosts: str = ""
    state: str = "idle"                     # idle | running | done | failed
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    found: list[Found] = field(default_factory=list)         # from the scan
    imported: list[Found] = field(default_factory=list)      # from compose/config files
    task: Any = None

    @property
    def running(self) -> bool:
        return self.state == "running"

    @property
    def everything(self) -> list[Found]:
        return [*self.imported, *self.found]

    @property
    def elapsed(self) -> float:
        return (self.finished or time.time()) - self.started if self.started else 0.0


def _job(request: Request) -> ScanJob:
    """The one job this install has. Kept on app.state so a reload shows what the last scan found."""
    st = request.app.state
    job = getattr(st, "discovery_job", None)
    if job is None:
        job = ScanJob()
        st.discovery_job = job
    if job.state in ("done", "failed") and job.finished and time.time() - job.finished > JOB_MAX_AGE_S:
        job = ScanJob()
        st.discovery_job = job
    return job


def _scanner(request: Request):
    """The scan function to use — swapped out in tests so nothing opens a socket."""
    return getattr(request.app.state, "discovery_scan", scan)


def _suggestions(request: Request, job: ScanJob) -> list[Suggestion]:
    return suggestions(job.everything, request.app.state.runtime.store)


def _rows(request: Request, job: ScanJob) -> list[dict[str, Any]]:
    """One row per suggestion — redacted, so no key value can reach the template."""
    runtime = request.app.state.runtime
    rows: list[dict[str, Any]] = []
    for s in _suggestions(request, job):
        spec = runtime.specs.get(s.service)
        safe = s.redacted()
        rows.append({
            "service": s.service, "title": spec.title if spec else s.title, "url": s.url,
            "found": safe.found, "installed": spec is not None, "has_check": bool(spec and spec.check),
            "writes": sorted(safe.settings), "skipped": safe.skipped, "has_secret": s.has_secret,
            "already_configured": s.already_configured, "enabled": s.enabled,
            "writes_nothing": s.writes_nothing,
        })
    return rows


def _extras(job: ScanJob) -> list[dict[str, str]]:
    """Products that answered but that periscope has no service for — worth naming, nothing to write."""
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for f in job.everything:
        if f.service in SERVICE_FOR or f.service in seen:
            continue
        seen.add(f.service)
        out.append({"title": f.title, "where": f.where, "version": f.version})
    return out


def _ctx(request: Request, job: ScanJob | None = None, **extra: Any) -> dict[str, Any]:
    job = job or _job(request)
    rows, extras = _rows(request, job), _extras(job)
    return {
        "job": job, "rows": rows, "extras": extras, "poll_ms": POLL_MS,
        "hosts": job.hosts or ", ".join(default_hosts()),
        "scanned": bool(job.found) or job.state in ("done", "failed"),
        **extra,
    }


# ----- the page ----------------------------------------------------------------------------------------
@router.get("/discover")
async def discover_page(request: Request):
    return render(request, "discover.html", _ctx(request))


@router.get("/discover/results")
async def discover_results(request: Request):
    """The fragment the page polls while a scan is running (and swaps in once when it finishes)."""
    return partial(request, "partials/found_list.html", _ctx(request))


@router.post("/discover/scan")
async def discover_scan(request: Request):
    """Start a scan in the background and answer immediately — the page polls for the result."""
    job = _job(request)
    if job.running:
        flash(request, "a scan is already running", "info")
        return partial(request, "partials/found_list.html", _ctx(request))
    form = await request.form()
    hosts = str(form.get("hosts") or "").strip() or ", ".join(default_hosts())
    fresh = ScanJob(hosts=hosts, state="running", started=time.time(), imported=list(job.imported))
    request.app.state.discovery_job = fresh
    scanner = _scanner(request)

    async def run() -> None:
        try:
            fresh.found = list(await scanner(hosts))
            fresh.state = "done"
        except ValueError as e:                     # a range we will not sweep, or one that does not parse
            fresh.state, fresh.error = "failed", str(e)
        except asyncio.CancelledError:
            fresh.state, fresh.error = "failed", "the scan was stopped"
            raise
        except Exception as e:                      # noqa: BLE001 — a failed scan is a message, not a 500
            log.warning("discovery scan of %s failed: %s", hosts, e)
            fresh.state, fresh.error = "failed", f"{type(e).__name__}: {e}"
        finally:
            fresh.finished = time.time()

    fresh.task = asyncio.create_task(run())
    log.info("discovery: scan of %s started from the web UI", hosts)
    return partial(request, "partials/found_list.html", _ctx(request, job=fresh))


# ----- files, for whatever the scan cannot reach ---------------------------------------------------------
@router.post("/discover/import")
async def discover_import(request: Request):
    """Read a pasted docker-compose.yml, or an *arr config directory on this box."""
    job = _job(request)
    form = await request.form()
    text = str(form.get("compose") or "").strip()
    folder = str(form.get("config_dir") or "").strip()
    host = str(form.get("compose_host") or "").strip() or "localhost"
    got: list[Found] = []
    problems: list[str] = []
    if text:
        try:
            got += from_compose(text, host=host)
        except ValueError as e:
            problems.append(str(e))
    if folder:
        got += _read_config_dir(folder, problems)
    if not text and not folder:
        flash(request, "paste a compose file, or give the folder your *arr configs live in", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/discover")
    for p in problems:
        flash(request, p, "warning")
    if got:
        keep = {(f.service, f.source) for f in got}
        job.imported = [f for f in job.imported if (f.service, f.source) not in keep] + got
        keys = sum(1 for f in got if f.has_secret)
        flash(request, f"read {len(got)} service(s)" + (f", {keys} of them with an API key" if keys else ""),
              "success")
    elif not problems:
        flash(request, "nothing in there that periscope knows how to talk to", "info")
    request.app.state.discovery_job = job
    return partial(request, "partials/found_list.html", _ctx(request, job=job))


def _read_config_dir(folder: str, problems: list[str]) -> list[Found]:
    """Every `config.xml` one level down from the folder given (…/arr/sonarr/config.xml), plus the folder
    itself when the config sits directly in it."""
    root = Path(folder).expanduser()
    if not root.is_dir():
        problems.append(f"{folder} is not a folder on this box")
        return []
    out: list[Found] = []
    candidates = [root, *sorted(p for p in root.iterdir() if p.is_dir())]
    for d in candidates[:32]:
        if not (d / "config.xml").is_file():
            continue
        try:
            out += from_arr_config(d)
        except ValueError as e:
            problems.append(str(e))
    if not out:
        problems.append(f"no *arr config.xml found in {folder}")
    return out


# ----- using a finding ----------------------------------------------------------------------------------
@router.post("/discover/use/{name}")
async def discover_use(request: Request, name: str):
    """Write the settings this finding would fill in, then run the service's own credentials check."""
    job = _job(request)
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    form = await request.form()
    overwrite = str(form.get("overwrite") or "").lower() in ("1", "true", "on", "yes")
    picked = next((s for s in suggestions(job.everything, store, overwrite=overwrite) if s.service == name), None)
    if picked is None:
        flash(request, f"nothing was found for {name} — scan again", "error")
        return toasts(request, 404) if is_htmx(request) else redirect(request, "/discover")
    spec = runtime.specs.get(name)
    if spec is None:
        flash(request, f"{name} is not installed on this box", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/discover")
    if not picked.settings:
        flash(request, f"{picked.title} already has everything this would fill in", "info")
        return partial(request, "partials/found_list.html", _ctx(request, job=job))

    store.update_service_env(name, dict(picked.settings))
    save(request)
    written = ", ".join(sorted(picked.settings))
    log.info("discovery: filled in %s for %s", written, name)     # keys are names only, never values
    flash(request, f"{picked.title}: filled in {written}", "success")
    ok, message = await _run_check(spec, store.env_for(name))
    return partial(request, "partials/found_list.html",
                   _ctx(request, job=job, checked={"service": name, "ok": ok, "message": message}))


async def _run_check(spec, env: dict[str, str]) -> tuple[bool | None, str]:
    """The service's own Test, on what was just written. Same shape overview.py's check button uses."""
    if spec.check is None:
        return None, "this service has no credentials check — open its settings to finish"
    try:
        return await asyncio.wait_for(spec.check(env), timeout=CHECK_TIMEOUT_S)
    except asyncio.TimeoutError:
        return False, f"the check timed out after {CHECK_TIMEOUT_S} s"
    except Exception as e:      # noqa: BLE001 — a service's own check must not take the page down
        log.exception("check() of %s raised during discovery", spec.name)
        return False, f"{type(e).__name__}: {e}"


@router.post("/discover/enable/{name}")
async def discover_enable(request: Request, name: str):
    """Switch a service on after its settings were filled in. Applies on the next restart, like everywhere."""
    job = _job(request)
    st = request.app.state
    runtime = st.runtime
    store = runtime.store
    spec = runtime.specs.get(name)
    if spec is None:
        flash(request, f"{name} is not installed on this box", "error")
        return toasts(request, 422) if is_htmx(request) else redirect(request, "/discover")
    missing = spec.required_missing(store.env_for(name))
    if missing:
        labels = [(spec.setting(k).label if spec.setting(k) else k) for k in missing]
        flash(request, f"{spec.title} still needs {', '.join(labels)} — open its settings", "warning")
        return partial(request, "partials/found_list.html", _ctx(request, job=job))
    store.set_enabled(name, True)
    save(request)
    flash(request, f"{spec.title} is on — restart to apply (button in the header)", "success")
    return partial(request, "partials/found_list.html", _ctx(request, job=job))

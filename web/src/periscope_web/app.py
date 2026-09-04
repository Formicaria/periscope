"""FastAPI application + `serve()` (the coroutine the runtime awaits inside its own event loop)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from periscope.net import web_url

from .auth import PUBLIC_PREFIXES, CsrfFailed, NotLoggedIn, Sessions, User, csrf_ok, noauth
from .discordapi import DiscordAPI
from .guild import GuildDirectory
from .logs import LogBuffer
from .render import ENV, is_htmx, render

log = logging.getLogger(__name__)


# ----- gates (global dependencies) --------------------------------------------------------------------
async def auth_gate(request: Request) -> None:
    st = request.app.state
    if st.noauth:
        request.state.user = st.noauth_user
        return
    user = st.sessions.load(request)
    request.state.user = user
    if user is None and not request.url.path.startswith(PUBLIC_PREFIXES):
        raise NotLoggedIn()


async def csrf_gate(request: Request) -> None:
    if request.method not in ("POST", "PUT", "PATCH", "DELETE") or request.url.path.startswith("/auth/"):
        return
    token = request.headers.get("X-CSRF-Token")
    if not token:
        ctype = request.headers.get("content-type", "")
        if ctype.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
            form = await request.form()
            token = str(form.get("csrf") or "")
    if not csrf_ok(getattr(request.state, "user", None), token):
        raise CsrfFailed()


def public_url(store, host: str | None = None, port: int | None = None) -> str:
    """`web.base_url`, else http://<lan ip>:<port> — what the log and `periscope web` print."""
    return web_url(store, host, port)


SETUP_TOKEN_FILE = "web-setup-token"


def write_setup_token(runtime, token: str) -> None:
    """Keep the one-time sign-in token where `periscope web` can print it as a link (data/, mode 0600)."""
    try:
        p = runtime.data_dir / SETUP_TOKEN_FILE
        p.write_text(token + "\n")
        p.chmod(0o600)
    except Exception:  # noqa: BLE001
        log.debug("could not write the setup token file", exc_info=True)


def clear_setup_token(app: FastAPI) -> None:
    app.state.setup_token = None
    try:
        (app.state.runtime.data_dir / SETUP_TOKEN_FILE).unlink()
    except Exception:  # noqa: BLE001
        pass


def site_url(request: Request) -> str:
    """`web.base_url`, else the origin the browser actually used (so the OAuth redirect URI matches what
    was registered even when the box is reached by IP)."""
    base = str(request.app.state.runtime.store.web.get("base_url") or "").strip().rstrip("/")
    return base or str(request.base_url).rstrip("/")


# ----- app ---------------------------------------------------------------------------------------------
def create_app(runtime, *, discord_api: DiscordAPI | None = None, setup_token: str | None = None,
               log_buffer: LogBuffer | None = None) -> FastAPI:
    store = runtime.store
    if not str(store.web.get("session_secret") or "").strip():
        store.web["session_secret"] = secrets.token_hex(32)
        with contextlib.suppress(Exception):
            store.save()
    secure = str(store.web.get("base_url") or "").lower().startswith("https")

    app = FastAPI(title="periscope", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(auth_gate), Depends(csrf_gate)])
    st = app.state
    st.runtime = runtime
    st.discord = discord_api or DiscordAPI()
    st.sessions = Sessions(str(store.web["session_secret"]), secure=secure)
    st.setup_token = setup_token
    st.noauth = noauth()
    st.noauth_user = User(id="local", name="local admin", via="noauth")
    st.logs = log_buffer or LogBuffer()
    st.guild = GuildDirectory(runtime, st.discord)
    st.app_ids = {}  # presence name → application id (for invite links)
    # settings apply themselves (the runtime rebuilds the service in place); these are the few things that
    # genuinely need the process to start again, collected as plain sentences for the header banner
    st.pending: list[str] = []

    def dirty() -> bool:
        return bool(st.pending)

    st.dirty = dirty
    st.pending_reasons = lambda: list(dict.fromkeys(st.pending))

    from .routes import register

    register(app)

    @app.exception_handler(NotLoggedIn)
    async def _not_logged_in(request: Request, exc: NotLoggedIn) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if is_htmx(request):
            return Response(status_code=401, headers={"HX-Redirect": "/login"})
        nxt = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse(f"/login?next={quote(nxt, safe='')}", status_code=302)

    @app.exception_handler(CsrfFailed)
    async def _csrf_failed(request: Request, exc: CsrfFailed) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse({"error": "csrf"}, status_code=403)
        if is_htmx(request):
            html = ENV.get_template("partials/toasts.html").render(flashes=[("error", "Session expired — reload the page")], oob=True)
            return HTMLResponse(html, status_code=403, headers={"HX-Reswap": "none"})
        return render(request, "error.html", {"title": "Session expired", "message": "The form token did not match — reload and try again."}, status=403)

    @app.exception_handler(HTTPException)
    async def _http_error(request: Request, exc: HTTPException) -> Response:
        if request.url.path.startswith("/api/") or exc.status_code in (401, 405):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=getattr(exc, "headers", None))
        if is_htmx(request):
            html = ENV.get_template("partials/toasts.html").render(flashes=[("error", str(exc.detail))], oob=True)
            return HTMLResponse(html, status_code=exc.status_code, headers={"HX-Reswap": "none"})
        if getattr(request.state, "user", None) is None and not request.app.state.noauth:
            return Response(str(exc.detail), status_code=exc.status_code)
        return render(request, "error.html", {"title": f"{exc.status_code}", "message": str(exc.detail)}, status=exc.status_code)

    return app


# ----- in-loop server -------------------------------------------------------------------------------------
def _make_server(app: FastAPI, host: str, port: int) -> Any:
    import uvicorn

    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield  # the runtime owns SIGINT/SIGTERM; uvicorn must not replace its handlers

    config = uvicorn.Config(app, host=host, port=port, log_level="warning", loop="none", log_config=None, access_log=False,
                            lifespan="off")
    return Server(config)


def _bind(host: str, port: int) -> socket.socket:
    """Bind the listening socket ourselves: uvicorn answers a bind failure with sys.exit(), which would take the
    whole runtime down; this way a busy port only costs the web UI. Sockets are close-on-exec, so a Restart
    (os.execv) releases the port for the new process."""
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    return sock


async def serve(runtime, host: str = "0.0.0.0", port: int = 8090) -> None:
    """Run the web UI inside the runtime's event loop until cancelled (the runtime cancels it on shutdown)."""
    buf = LogBuffer()
    buf.bind(asyncio.get_running_loop())
    logging.getLogger().addHandler(buf)
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    token = secrets.token_urlsafe(24)
    app = create_app(runtime, setup_token=token, log_buffer=buf)
    url = public_url(runtime.store, host, port)
    if app.state.noauth:
        log.warning("PERISCOPE_WEB_NOAUTH=1 — the web UI at %s treats EVERY visitor as admin; development only", url)
    else:
        log.info("web UI at %s", url)
        write_setup_token(runtime, token)
        if not str(runtime.store.web.get("oauth_client_id") or "").strip():
            log.warning("web UI sign-in: open %s/login?token=%s (one-time link; `periscope web` prints it again)", url, token)
        else:
            log.info("web UI setup token (if Discord sign-in is locked out): %s", token)
    try:
        sock = _bind(host, port)
    except OSError as e:
        log.error("web UI cannot listen on %s:%s (%s) — change web.port in config/periscope.yaml", host, port, e)
        logging.getLogger().removeHandler(buf)
        return
    server = _make_server(app, host, port)
    inner = asyncio.ensure_future(server.serve(sockets=[sock]))
    try:
        await asyncio.shield(inner)
    except asyncio.CancelledError:
        # let uvicorn close its sockets/connections instead of tearing its internals down mid-await
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(inner, timeout=5)
        raise
    finally:
        logging.getLogger().removeHandler(buf)
        clear_setup_token(app)
        with contextlib.suppress(Exception):
            sock.close()
        with contextlib.suppress(Exception):
            await app.state.discord.aclose()

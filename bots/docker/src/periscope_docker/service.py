"""v2 service definition for Docker: same wiring as DockerBot.__init__, on a shared presence."""

from __future__ import annotations

import logging
from pathlib import Path

from periscope import ServiceBot, ServiceSpec, Setting, env_scope, settings_from_example
from periscope.http import HttpError

from . import messages  # noqa: F401  — importing messages registers the docker.* message kinds
from .bot import COGS
from .client import DockerClient
from .config import DockerConfig

log = logging.getLogger(__name__)

EXAMPLE = Path(__file__).resolve().parents[2] / ".env.example"
# these two are comma-separated globs; the example's plain value cannot say so on its own
LIST_KEYS = ("DOCKER_INCLUDE", "DOCKER_IGNORE")


def _settings() -> list[Setting]:
    out = settings_from_example(EXAMPLE)
    for s in out:
        if s.key in LIST_KEYS:
            s.type = "list"
    return out


async def build(bot: ServiceBot) -> None:
    with env_scope(bot.env):
        cfg = DockerConfig.from_env()
    bot.cfg = cfg
    bot.docker = DockerClient(cfg)
    log.info("docker: talking to %s (%s)", cfg.endpoint, cfg.mode)
    for path in COGS:
        await bot.load_extension(path)


def explain(cfg: DockerConfig, exc: Exception) -> str:
    """Why a connection attempt failed, in the words someone fixing it needs."""
    cause = getattr(exc, "os_error", None) or exc
    if cfg.socket_path:
        if isinstance(cause, PermissionError):
            return (f"cannot reach the Docker socket at {cfg.socket_path} — is periscope in the docker group? "
                    "(`sudo usermod -aG docker periscope`, then restart it)")
        if isinstance(cause, FileNotFoundError):
            return (f"there is no Docker socket at {cfg.socket_path} — check DOCKER_HOST, or mount the socket "
                    "into the container")
        return f"cannot reach the Docker socket at {cfg.socket_path}: {cause}"
    if cfg.via_portainer:
        return f"cannot reach Portainer at {cfg.portainer_url}: {cause}"
    return f"cannot reach the Docker daemon at {cfg.base_url}: {cause} — is it listening there?"


def explain_http(cfg: DockerConfig, e: HttpError) -> str:
    if cfg.via_portainer:
        if e.status in (401, 403):
            return f"Portainer rejected the API key ({e.status}) — check PORTAINER_API_KEY"
        if e.status == 404:
            return (f"Portainer has no environment {cfg.portainer_endpoint_id} — check PORTAINER_ENDPOINT_ID "
                    "(the number in the URL when you open the environment)")
        return f"Portainer answered {e.status} on /version"
    if e.status == 400 and "HTTPS" in e.body.upper():
        return f"{cfg.base_url} wants TLS — set DOCKER_TLS_VERIFY and the certificate paths"
    if e.status in (401, 403):
        return f"the daemon at {cfg.base_url} refused the request ({e.status}) — it expects a client certificate"
    return f"Docker answered {e.status} on /version"


async def check(env: dict[str, str]) -> tuple[bool, str]:
    """Connect the way the bot will and read `GET /version`, then count the containers it can see."""
    try:
        with env_scope(env):
            cfg = DockerConfig.from_env()
    except RuntimeError as e:
        return False, str(e)
    client = DockerClient(cfg)
    try:
        version = str((await client.version()).get("Version") or "?")
        containers = await client.raw_containers()
        return True, f"Docker {version} · {len(containers)} container{'s' if len(containers) != 1 else ''}"
    except HttpError as e:
        return False, explain_http(cfg, e)
    except Exception as e:  # noqa: BLE001
        return False, explain(cfg, e)
    finally:
        await client.close()


SERVICES = [
    ServiceSpec(
        name="docker",
        title="Docker",
        description="Live container board (state, image, uptime, cpu/memory), exited / unhealthy / restart-loop / "
                    "daemon-unreachable alerts, optional image update checks, /docker ps, logs, stats, restart.",
        group="infra",
        settings=_settings(),
        build=build,
        check=check,
        slash="/docker",
    )
]

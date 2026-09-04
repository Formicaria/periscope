"""Docker-specific environment configuration, including which of the two transports to use.

There are two ways to reach the Engine API and they are tried in that order: `DOCKER_HOST` (a unix socket path
or a tcp:// / https:// endpoint) first, then `PORTAINER_URL` for people who would rather not expose the socket.
An explicitly configured `DOCKER_HOST` therefore always wins; Portainer takes over when `DOCKER_HOST` is left at
its default and a Portainer URL is filled in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from periscope import env, env_bool, env_int, env_list, load_dotenv_if_present

from .util import watched

DEFAULT_HOST = "/var/run/docker.sock"
# what the docker CLI calls the files inside DOCKER_CERT_PATH when that points at a directory
CERT_FILES = {"ca_path": "ca.pem", "cert_path": "cert.pem", "key_path": "key.pem"}
MIN_POLL_S = 10


@dataclass
class DockerConfig:
    host: str = DEFAULT_HOST
    tls_verify: bool = False
    ca_path: str = ""
    cert_path: str = ""
    key_path: str = ""
    portainer_url: str = ""
    portainer_api_key: str = ""
    portainer_endpoint_id: int = 1
    include: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    poll_s: int = 60
    restart_loop_n: int = 3
    alert_on_stop: bool = False
    check_updates: bool = False
    update_check_h: int = 12

    # ----- transport ----------------------------------------------------

    @property
    def via_portainer(self) -> bool:
        """Portainer is used when it is configured and DOCKER_HOST was left alone."""
        return bool(self.portainer_url) and self.host.strip() in ("", DEFAULT_HOST)

    @property
    def socket_path(self) -> str:
        """The unix socket to dial, or "" when this config talks to a TCP endpoint."""
        if self.via_portainer:
            return ""
        host = self.host.strip() or DEFAULT_HOST
        if host.startswith("unix://"):
            return host[len("unix://"):]
        if host.startswith(("http://", "https://", "tcp://")):
            return ""
        return host

    @property
    def base_url(self) -> str:
        """What every API path hangs off: a dummy host for the socket, the endpoint proxy for Portainer."""
        if self.via_portainer:
            return f"{self.portainer_url.rstrip('/')}/api/endpoints/{self.portainer_endpoint_id}/docker"
        if self.socket_path:
            return "http://docker"          # the socket ignores the host, but aiohttp wants a URL
        host = self.host.strip()
        if host.startswith(("http://", "https://")):
            return host.rstrip("/")
        rest = host[len("tcp://"):] if host.startswith("tcp://") else host
        scheme = "https" if (self.tls_verify or self.cert_path) else "http"
        return f"{scheme}://{rest.rstrip('/')}"

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.portainer_api_key} if self.via_portainer else {}

    @property
    def mode(self) -> str:
        if self.via_portainer:
            return "portainer"
        return "socket" if self.socket_path else "tcp"

    @property
    def endpoint(self) -> str:
        """Where this config talks to, in the words an error message should use."""
        if self.via_portainer:
            return f"Portainer environment {self.portainer_endpoint_id} at {self.portainer_url.rstrip('/')}"
        if self.socket_path:
            return f"the Docker socket at {self.socket_path}"
        return self.base_url

    @property
    def label(self) -> str:
        """The same thing, short enough for a board line."""
        if self.via_portainer:
            return f"{self.portainer_url.rstrip('/')} (environment {self.portainer_endpoint_id})"
        return self.socket_path or self.base_url

    # ----- loading ------------------------------------------------------

    @classmethod
    def from_env(cls) -> DockerConfig:
        load_dotenv_if_present()
        cfg = cls(
            host=str(env("DOCKER_HOST", DEFAULT_HOST) or DEFAULT_HOST).strip(),
            tls_verify=env_bool("DOCKER_TLS_VERIFY", False),
            ca_path=str(env("DOCKER_CA_PATH", "") or "").strip(),
            cert_path=str(env("DOCKER_CERT_PATH", "") or "").strip(),
            key_path=str(env("DOCKER_KEY_PATH", "") or "").strip(),
            portainer_url=str(env("PORTAINER_URL", "") or "").strip().rstrip("/"),
            portainer_api_key=str(env("PORTAINER_API_KEY", "") or "").strip(),
            portainer_endpoint_id=env_int("PORTAINER_ENDPOINT_ID", 1),
            include=env_list("DOCKER_INCLUDE"),
            ignore=env_list("DOCKER_IGNORE"),
            poll_s=env_int("DOCKER_POLL_S", 60),
            restart_loop_n=env_int("DOCKER_RESTART_LOOP_N", 3),
            alert_on_stop=env_bool("DOCKER_ALERT_ON_STOP", False),
            check_updates=env_bool("DOCKER_CHECK_UPDATES", False),
            update_check_h=env_int("DOCKER_UPDATE_CHECK_H", 12),
        )
        cfg.expand_cert_dir()
        cfg.validate()
        return cfg

    def expand_cert_dir(self) -> None:
        """A DOCKER_CERT_PATH that names a directory means the docker CLI's ca.pem / cert.pem / key.pem in it.
        The two other settings still win when they were filled in by hand."""
        if not self.cert_path or not os.path.isdir(self.cert_path):
            return
        directory = self.cert_path
        self.cert_path = os.path.join(directory, CERT_FILES["cert_path"])
        self.ca_path = self.ca_path or os.path.join(directory, CERT_FILES["ca_path"])
        self.key_path = self.key_path or os.path.join(directory, CERT_FILES["key_path"])

    def validate(self) -> None:
        if self.via_portainer:
            if not self.portainer_url.startswith(("http://", "https://")):
                raise RuntimeError("PORTAINER_URL must start with http:// or https:// (e.g. https://portainer.lan:9443)")
            if not self.portainer_api_key:
                raise RuntimeError("PORTAINER_API_KEY is required when PORTAINER_URL is set "
                                   "(Portainer → My account → Access tokens)")
        else:
            host = self.host.strip() or DEFAULT_HOST
            if host.startswith("npipe://"):
                raise RuntimeError("DOCKER_HOST: Windows named pipes are not supported; expose the daemon over "
                                   "tcp:// or use PORTAINER_URL")
            if not host.startswith(("unix://", "http://", "https://", "tcp://", "/", "./")):
                raise RuntimeError("DOCKER_HOST must be a socket path (/var/run/docker.sock) or a URL "
                                   "(tcp://10.0.0.5:2375, https://docker.lan:2376)")
            if not self.socket_path and not self.base_url.split("//", 1)[-1]:
                raise RuntimeError("DOCKER_HOST names no host (e.g. tcp://10.0.0.5:2375)")
        if (self.cert_path and not self.key_path) or (self.key_path and not self.cert_path):
            raise RuntimeError("DOCKER_CERT_PATH and DOCKER_KEY_PATH go together — set both or neither")
        if self.poll_s < MIN_POLL_S:
            raise RuntimeError(f"DOCKER_POLL_S must be at least {MIN_POLL_S} seconds (got {self.poll_s})")
        if self.restart_loop_n < 2:
            raise RuntimeError(f"DOCKER_RESTART_LOOP_N must be at least 2 (got {self.restart_loop_n})")
        if self.update_check_h < 1:
            raise RuntimeError(f"DOCKER_UPDATE_CHECK_H must be at least 1 hour (got {self.update_check_h})")

    def watches(self, name: str) -> bool:
        """Whether a container name is one this service reports on (DOCKER_INCLUDE then DOCKER_IGNORE)."""
        return watched(name, self.include, self.ignore)

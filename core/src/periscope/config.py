"""Environment-driven configuration shared by every lab bot."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, TypeVar

T = TypeVar("T")


def load_dotenv_if_present(path: str | os.PathLike | None = None) -> None:
    """Load a .env file if it exists. Never overrides real environment variables."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(path or ".env", override=False)


def env(name: str, default: T | None = None, cast: Callable[[str], T] | None = None, required: bool = False):
    raw = os.environ.get(name)
    if raw is not None and "  #" in raw:
        # systemd EnvironmentFile passes inline comments through; tolerate "VALUE  # note"
        raw = raw.split("  #", 1)[0].rstrip()
    if raw is None or raw == "":
        if required and default is None:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return default
    return cast(raw) if cast else raw


def env_int(name: str, default: int | None = None, required: bool = False) -> int | None:
    return env(name, default, int, required)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_list(name: str, default: list[str] | None = None, sep: str = ",") -> list[str]:
    raw = os.environ.get(name)
    if not raw:
        return list(default or [])
    return [x.strip() for x in raw.split(sep) if x.strip()]


@dataclass
class Settings:
    """Settings every lab bot needs. Integration-specific settings live in each bot."""

    discord_token: str
    lab_name: str = "lab"
    lab_color: int = 0x5865F2
    guild_id: int | None = None
    alert_channel_id: int | None = None
    status_channel_id: int | None = None
    alert_role_id: int | None = None
    admin_role_ids: list[int] = field(default_factory=list)
    data_dir: Path = Path("data")
    log_level: str = "INFO"
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_secret: str | None = None
    status_interval_s: int = 60

    @classmethod
    def from_env(cls) -> "Settings":
        load_dotenv_if_present()
        color = env("LAB_COLOR", "5865F2")
        return cls(
            discord_token=env("DISCORD_TOKEN", required=True),
            lab_name=env("LAB_NAME", "lab"),
            lab_color=int(str(color).lstrip("#"), 16),
            guild_id=env_int("GUILD_ID"),
            alert_channel_id=env_int("ALERT_CHANNEL_ID"),
            status_channel_id=env_int("STATUS_CHANNEL_ID"),
            alert_role_id=env_int("ALERT_ROLE_ID"),
            admin_role_ids=[int(x) for x in env_list("ADMIN_ROLE_IDS")],
            data_dir=Path(env("DATA_DIR", "data")),
            log_level=env("LOG_LEVEL", "INFO"),
            webhook_host=env("WEBHOOK_HOST", "0.0.0.0"),
            webhook_port=env_int("WEBHOOK_PORT", 8080),
            webhook_secret=env("WEBHOOK_SECRET"),
            status_interval_s=env_int("STATUS_INTERVAL_S", 60),
        )

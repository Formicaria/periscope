"""Proxmox-specific environment configuration."""

from __future__ import annotations

from dataclasses import dataclass

from periscope import env, env_bool, env_int


@dataclass
class PveSettings:
    url: str
    token_id: str
    token_secret: str
    verify_ssl: bool = False
    cpu_warn: int = 85
    mem_warn: int = 90
    storage_warn: int = 85
    storage_crit: int = 95
    watch_backups: bool = True

    @classmethod
    def from_env(cls) -> "PveSettings":
        url = env("PVE_URL", required=True).rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise RuntimeError("PVE_URL must start with http:// or https:// (e.g. https://pve.local:8006)")
        token_id = env("PVE_TOKEN_ID", required=True)
        if "!" not in token_id or "@" not in token_id:
            raise RuntimeError("PVE_TOKEN_ID must look like user@realm!tokenname")
        cfg = cls(
            url=url,
            token_id=token_id,
            token_secret=env("PVE_TOKEN_SECRET", required=True),
            verify_ssl=env_bool("PVE_VERIFY_SSL", False),
            cpu_warn=env_int("PVE_CPU_WARN", 85),
            mem_warn=env_int("PVE_MEM_WARN", 90),
            storage_warn=env_int("PVE_STORAGE_WARN", 85),
            storage_crit=env_int("PVE_STORAGE_CRIT", 95),
            watch_backups=env_bool("PVE_WATCH_BACKUPS", True),
        )
        for name in ("cpu_warn", "mem_warn", "storage_warn", "storage_crit"):
            v = getattr(cfg, name)
            if not 0 < v <= 100:
                raise RuntimeError(f"PVE_{name.upper()} must be between 1 and 100 (got {v})")
        if cfg.storage_crit < cfg.storage_warn:
            raise RuntimeError("PVE_STORAGE_CRIT must be >= PVE_STORAGE_WARN")
        return cfg

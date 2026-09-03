"""Service discovery: every installed `periscope_<pkg>` package may expose `service.SERVICES: list[ServiceSpec]`."""

from __future__ import annotations

import importlib
import logging
import pkgutil

from .service import ServiceSpec

log = logging.getLogger(__name__)

_cache: dict[str, ServiceSpec] | None = None


def discover(force: bool = False) -> dict[str, ServiceSpec]:
    global _cache
    if _cache is not None and not force:
        return _cache
    specs: dict[str, ServiceSpec] = {}
    for mod in pkgutil.iter_modules():
        if not mod.name.startswith("periscope_"):
            continue
        try:
            m = importlib.import_module(f"{mod.name}.service")
        except ModuleNotFoundError as e:
            if e.name and e.name.startswith(mod.name):
                continue  # package has no service module (v1-only package)
            log.warning("service module %s.service failed to import: %s", mod.name, e)
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("service module %s.service failed to import: %s", mod.name, e)
            continue
        for spec in getattr(m, "SERVICES", []):
            if spec.name in specs:
                log.warning("duplicate service name %s (%s)", spec.name, mod.name)
            specs[spec.name] = spec
    _cache = dict(sorted(specs.items(), key=lambda kv: (kv[1].group, kv[0])))
    return _cache


def get(name: str) -> ServiceSpec | None:
    return discover().get(name)

"""Tiny JSON-backed persistent state (message ids, last-seen fingerprints, etc.)."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


class JsonState:
    def __init__(self, path: str | os.PathLike):
        self.path = Path(path)
        self._data: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        try:
            self._data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump(self._data, f, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self.save()

    def pop(self, key: str, default: Any = None) -> Any:
        v = self._data.pop(key, default)
        self.save()
        return v

    def namespace(self, prefix: str) -> "NamespacedState":
        return NamespacedState(self, prefix)


class NamespacedState:
    def __init__(self, parent: JsonState, prefix: str):
        self._p = parent
        self._prefix = prefix.rstrip(":") + ":"

    def get(self, key: str, default: Any = None) -> Any:
        return self._p.get(self._prefix + key, default)

    def set(self, key: str, value: Any) -> None:
        self._p.set(self._prefix + key, value)

    def pop(self, key: str, default: Any = None) -> Any:
        return self._p.pop(self._prefix + key, default)

    def namespace(self, prefix: str) -> "NamespacedState":
        return NamespacedState(self._p, self._prefix + prefix)

"""Permanent ID allocation.

marine:id values are UUIDs recorded in assets/ids.lock.json, keyed by a
stable *key* (library definition path or vessel occurrence path). The lock
is append-only: once a key has an ID it keeps it forever, and an ID is never
handed to a different key. CI enforces this against the previous lock
(see check_append_only).

Keys look like:
    def:propeller_cpp_4b/Blade_1          library definition
    occ:demo_osv/EngineRoom/Propulsion/Propeller/Blade_1   vessel occurrence
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

from .paths import ID_LOCK


class IdLock:
    def __init__(self, path: Path = ID_LOCK):
        self.path = Path(path)
        self.ids: dict[str, str] = {}
        if self.path.exists():
            self.ids = json.loads(self.path.read_text())["ids"]
        self.new_keys: list[str] = []
        self.used: set[str] = set()

    def get(self, key: str) -> str:
        self.used.add(key)
        if key not in self.ids:
            self.ids[key] = str(uuid.uuid4())
            self.new_keys.append(key)
        return self.ids[key]

    def adopt(self, key: str, id_: str) -> None:
        """Record an ID that arrived from a source file (e.g. a PLM-issued UUID)."""
        self.used.add(key)
        self.ids[key] = id_
        self.new_keys.append(key)

    def lookup(self, key: str) -> str | None:
        return self.ids.get(key)

    def key_for(self, id_: str) -> str | None:
        for k, v in self.ids.items():
            if v == id_:
                return k
        return None

    def unused(self, prefix: str) -> list[str]:
        """Keys under prefix not touched this run: parts that disappeared.
        They are flagged for review, never deleted (the plan's re-ingest rule)."""
        return sorted(k for k in self.ids if k.startswith(prefix) and k not in self.used)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        doc = {
            "_comment": "Append-only. Never edit or remove an entry; CI rejects changed IDs.",
            "ids": dict(sorted(self.ids.items())),
        }
        self.path.write_text(json.dumps(doc, indent=1) + "\n")


def check_append_only(old: dict[str, str], new: dict[str, str]) -> list[str]:
    """Return a list of violations between a baseline lock and the current one."""
    errors = []
    for k, v in old.items():
        if k not in new:
            errors.append(f"key removed from ID lock: {k}")
        elif new[k] != v:
            errors.append(f"ID changed for {k}: {v} -> {new[k]}")
    seen: dict[str, str] = {}
    for k, v in new.items():
        if v in seen:
            errors.append(f"ID {v} reused by {seen[v]} and {k}")
        seen[v] = k
    return errors

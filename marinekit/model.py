"""In-memory part tree shared by generators, the STEP adapter and the USD writer.

Generators build a Part tree in CAD units (millimetres, like every STEP file
a real CAD system exports). The USD writer normalises to metres. The STEP
writer and the STEP ingest adapter both round-trip through this same type,
which is what lets the tests compare "generator -> USD" against
"generator -> STEP -> ingest -> USD" field by field.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator

import numpy as np

# marine: attributes that are carried as metadata on STEP products. Lengths
# in these attrs are always metres (they are schema values, not geometry).
STRING_ATTRS = ["id", "code", "class", "function", "material", "massSource",
                "geometryFidelity", "generator", "definitionId"]
REAL_ATTRS = ["mass", "diameter", "length", "thickness", "explodeDist"]
INT_ATTRS = ["lodLevel"]
VEC_ATTRS = ["explodeDir"]
REL_ATTRS = ["connectedTo", "serves"]


def identity() -> np.ndarray:
    return np.eye(4)


def translate(x: float = 0, y: float = 0, z: float = 0) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = (x, y, z)
    return m


def rot_x(deg: float) -> np.ndarray:
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    m = np.eye(4)
    m[1, 1], m[1, 2], m[2, 1], m[2, 2] = c, -s, s, c
    return m


def rot_y(deg: float) -> np.ndarray:
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    m = np.eye(4)
    m[0, 0], m[0, 2], m[2, 0], m[2, 2] = c, s, -s, c
    return m


def rot_z(deg: float) -> np.ndarray:
    a = np.radians(deg)
    c, s = np.cos(a), np.sin(a)
    m = np.eye(4)
    m[0, 0], m[0, 1], m[1, 0], m[1, 1] = c, -s, s, c
    return m


@dataclass
class Instance:
    name: str
    xform: np.ndarray
    id: str = ""


@dataclass
class Part:
    """One asset node. If `instances` is non-empty, `shape` is a prototype
    placed at each instance transform (becomes a USD PointInstancer)."""

    name: str
    cls: str
    kind: str = "subcomponent"
    attrs: dict[str, Any] = field(default_factory=dict)
    shape: Any = None                 # TopoDS_Shape / build123d shape, local frame, CAD units
    xform: np.ndarray = field(default_factory=identity)  # local -> parent, CAD units
    children: list["Part"] = field(default_factory=list)
    instances: list[Instance] = field(default_factory=list)
    relations: dict[str, list[str]] = field(default_factory=dict)  # rel -> sibling-relative names
    id: str = ""
    key: str = ""
    meta: dict[str, str] = field(default_factory=dict)  # non-schema asset info: version, source

    def add(self, *parts: "Part") -> "Part":
        self.children.extend(parts)
        return self

    def walk(self, path: str = "") -> Iterator[tuple[str, "Part"]]:
        p = f"{path}/{self.name}" if path else self.name
        yield p, self
        for c in self.children:
            yield from c.walk(p)

    def find(self, rel_path: str) -> "Part":
        node = self
        for name in rel_path.split("/"):
            node = next(c for c in node.children if c.name == name)
        return node


def assign_ids(root: Part, lock, prefix: str) -> None:
    """Give every part (and instance) a permanent ID from the lock."""
    for path, part in root.walk():
        part.key = f"{prefix}{path}"
        part.id = lock.get(part.key)
        part.attrs["id"] = part.id
        for inst in part.instances:
            inst.id = lock.get(f"{part.key}/{inst.name}")

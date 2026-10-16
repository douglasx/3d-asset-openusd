"""Controlled vocabularies (materials, classes, budgets, breakdown codes)."""
from __future__ import annotations

from functools import lru_cache

import yaml

from .paths import VOCAB


@lru_cache(maxsize=None)
def _load(name: str) -> dict:
    return yaml.safe_load((VOCAB / f"{name}.yaml").read_text())


def materials() -> dict:
    return _load("materials")["materials"]


def classes() -> dict:
    return _load("classes")["classes"]


def budgets() -> dict:
    return _load("budgets")


def codes() -> dict:
    return _load("breakdown")["codes"]


def density(material: str) -> float:
    return float(materials()[material]["density"])


def class_info(cls: str) -> dict:
    return classes()[cls]


def class_ancestors(cls: str) -> list[str]:
    out, cur = [], cls
    while cur:
        out.append(cur)
        cur = classes().get(cur, {}).get("parent")
    return out


ALWAYS_REQUIRED = ["id", "code", "class", "function", "material", "mass"]


def required_attrs(cls: str) -> list[str]:
    req = list(ALWAYS_REQUIRED)
    for c in class_ancestors(cls):
        for a in classes().get(c, {}).get("require", []):
            if a not in req:
                req.append(a)
    return req

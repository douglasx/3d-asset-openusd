"""Stage 1: run generators.

For each catalog entry:
  library/<family>/<name>/<name>.step         the simulated CAD export (input to ingest)
  library/<family>/<name>/<name>.params.json  the parameter set that produced it
  build/out/generated/<name>.usda             direct generator -> USD, used only by the
                                              round-trip test against the ingested USD
"""
from __future__ import annotations

import json
from pathlib import Path

from generators.catalog import CATALOG

from .ids import IdLock
from .model import assign_ids
from .paths import LIBRARY, OUT
from .step_io import write_step
from .usd_io import write_asset


def asset_dir(family: str, name: str) -> Path:
    return LIBRARY / family / name


def run(lock: IdLock | None = None, only: list[str] | None = None, direct_usd: bool = True) -> list[Path]:
    lock = lock or IdLock()
    written = []
    for entry in CATALOG:
        if only and entry.name not in only:
            continue
        root = entry.generate(entry.params)
        root.meta = {"version": entry.version, "source": "generator"}
        assign_ids(root, lock, "def:")       # keys: def:<asset>/<path>
        d = asset_dir(entry.family, entry.name)
        step = write_step(root, d / f"{entry.name}.step")
        (d / f"{entry.name}.params.json").write_text(
            json.dumps({"generator": root.attrs.get("generator"), "version": entry.version,
                        "params": entry.params_dict()}, indent=2, sort_keys=True) + "\n")
        if direct_usd:
            write_asset(root, OUT / "generated" / f"{entry.name}.usda")
        written.append(step)
        print(f"generated {entry.name}: {step.relative_to(LIBRARY.parent)}")
    lock.save()
    return written


if __name__ == "__main__":
    run()

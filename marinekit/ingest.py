"""Stage 2: STEP ingest adapter.

Reads a STEP file exactly as it would read one from a real CAD system and
writes the library USD asset. It does not import or know about the
generators; everything it learns comes from the file.

Normalisation steps:
  units     STEP length unit -> metres (OpenCascade reads in mm; scale 1e-3)
  axes      optional source up-axis (Y-up sources are rotated to Z-up)
  IDs       match by attribute key, then by name path, via assets/ids.lock.json:
            - file ID == locked ID          -> keep
            - file ID, no lock entry         -> adopt the file's ID
            - no file ID, lock entry by path -> reuse (a renamed CAD revision keeps its ID)
            - neither                        -> new ID
            - lock entries not seen          -> flagged for review, never deleted
  classes   kind defaulted from the class level when the file has none
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from . import vocab
from .ids import IdLock
from .model import Part, rot_x
from .paths import LIBRARY, OUT
from .step_io import read_step
from .usd_io import write_asset

KIND_BY_LEVEL = {"vessel": "assembly", "zone": "assembly", "system": "assembly",
                 "equipment": "component", "component": "subcomponent", "fastener": "subcomponent"}


def _match_ids(root: Part, lock: IdLock, report: dict) -> None:
    touched = set()
    for path, part in root.walk():
        key = f"def:{path}"
        touched.add(key)
        file_id = part.attrs.get("id") or None
        locked = lock.lookup(key)
        if locked and file_id and locked != file_id:
            report["conflicts"].append({"key": key, "file": file_id, "lock": locked})
            part.id = lock.get(key)
        elif file_id and not locked:
            owner = lock.key_for(file_id)
            if owner and owner != key:
                report["conflicts"].append({"key": key, "file": file_id, "owned_by": owner})
                part.id = lock.get(key)
            else:
                lock.adopt(key, file_id)
                part.id = file_id
                report["adopted"].append(key)
        else:
            if not locked:
                report["new"].append(key)
            part.id = lock.get(key)
        part.key = key
        part.attrs["id"] = part.id
        for inst in part.instances:
            ikey = f"{key}/{inst.name}"
            touched.add(ikey)
            if lock.lookup(ikey) is None:
                report["new"].append(ikey)
            inst.id = lock.get(ikey)
    prefix = f"def:{root.name}/"
    report["missing"] = sorted(k for k in lock.ids if (k.startswith(prefix) or k == f"def:{root.name}")
                               and k not in touched)


def _defaults(root: Part, report: dict) -> None:
    known = vocab.classes()
    for path, part in root.walk():
        if not part.cls or part.cls not in known:
            report["warnings"].append(f"{path}: class '{part.cls}' not in vocabulary")
            part.cls = part.cls or "Component"
        if part.kind not in KIND_BY_LEVEL.values():
            part.kind = KIND_BY_LEVEL.get(known.get(part.cls, {}).get("level", "component"), "subcomponent")


def _normalise_axes(root: Part, up_axis: str) -> None:
    if up_axis.upper() == "Z":
        return
    if up_axis.upper() != "Y":
        raise ValueError(f"unsupported up axis {up_axis}")
    r = rot_x(90.0)  # Y-up -> Z-up
    for c in root.children:
        c.xform = r @ c.xform
    if root.shape is not None:
        from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
        from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf
        t = gp_Trsf()
        t.SetRotation(gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(1, 0, 0)), np.pi / 2)
        root.shape = BRepBuilderAPI_Transform(root.shape, t, True).Shape()


def ingest_step(step_path: Path, lock: IdLock, out_path: Path | None = None, up_axis: str = "Z") -> dict:
    step_path = Path(step_path)
    root = read_step(step_path)
    report = {"source": str(step_path.name), "asset": root.name, "new": [], "adopted": [],
              "conflicts": [], "missing": [], "warnings": []}
    _normalise_axes(root, up_axis)
    _defaults(root, report)
    _match_ids(root, lock, report)
    root.meta.setdefault("source", "step")
    out_path = out_path or step_path.with_suffix(".usda")
    write_asset(root, out_path, unit_scale=1e-3)   # OpenCascade session unit is mm
    report["usd"] = str(out_path.name)
    report["parts"] = sum(1 for _ in root.walk())
    report["instances"] = sum(len(p.instances) for _, p in root.walk())
    return report


def write_variant_wrapper(family: str, spec: dict) -> Path:
    """library/<family>/<family>.usda: one asset whose variant set picks a catalog entry."""
    from pxr import Sdf, Usd, UsdGeom
    path = LIBRARY / family / f"{family}.usda"
    layer = Sdf.Layer.FindOrOpen(str(path))
    if layer:
        layer.Clear()
    else:
        layer = Sdf.Layer.CreateNew(str(path))
    stage = Usd.Stage.Open(layer)
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    prim = stage.DefinePrim(f"/{family}", "Xform")
    stage.SetDefaultPrim(prim)
    Usd.ModelAPI(prim).SetKind("component")
    vset = prim.GetVariantSets().AddVariantSet(spec["set"])
    for opt, asset in spec["options"].items():
        vset.AddVariant(opt)
        vset.SetVariantSelection(opt)
        with vset.GetVariantEditContext():
            prim.GetReferences().AddReference(f"./{asset}/{asset}.usda")
    vset.SetVariantSelection(spec["default"])
    layer.Save()
    return path


def run(lock: IdLock | None = None) -> list[dict]:
    from generators.catalog import VARIANTS
    lock = lock or IdLock()
    reports = []
    for step in sorted(LIBRARY.glob("*/*/*.step")):
        rep = ingest_step(step, lock)
        reports.append(rep)
        flag = " CONFLICTS" if rep["conflicts"] else ""
        print(f"ingested {rep['asset']}: {rep['parts']} parts, {rep['instances']} instances, "
              f"{len(rep['new'])} new IDs, {len(rep['missing'])} missing{flag}")
    for family, spec in VARIANTS.items():
        write_variant_wrapper(family, spec)
    lock.save()
    (OUT / "reports").mkdir(parents=True, exist_ok=True)
    (OUT / "reports" / "ingest.json").write_text(json.dumps(reports, indent=2) + "\n")
    return reports


if __name__ == "__main__":
    run()

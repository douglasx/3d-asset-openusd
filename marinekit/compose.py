"""Stage 3: compose vessels from the library.

Writes, per vessel (assets/vessels/<vessel>/):
  <system>.usda + <system>.geom.usdc   system: equipment by reference, occurrence
                                       IDs, connections, explode, system proxy
  <vessel>.usda + <vessel>.geom.usdc   vessel and zones; systems are payloads

Library prims carry *definition* IDs. Every occurrence in a vessel gets its
own permanent ID (lock key occ:<vessel>/<path>) authored as an override,
with marine:definitionId pointing back at the library part, so the same
library propeller can sit in many vessels without ID collisions.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml
from pxr import Sdf, Usd, UsdGeom, Vt

from . import vocab
from .explode import load_overrides, run_on_stage
from .ids import IdLock
from .paths import ASSETS, LIBRARY
from .usd_io import SCHEMA_VERSION, gf_matrix, get_marine, is_asset, set_marine
from .model import translate


def _new_layer_stage(path: Path):
    for p in (path, path.with_name(path.stem + ".geom.usdc")):
        layer = Sdf.Layer.Find(str(p))
        if layer is not None:
            layer.Clear()
        if p.exists():
            p.unlink()
    geom = Sdf.Layer.CreateNew(str(path.with_name(path.stem + ".geom.usdc")))
    stage = Usd.Stage.CreateNew(str(path))
    stage.GetRootLayer().subLayerPaths.append(f"./{geom.identifier.rsplit('/', 1)[-1]}")
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    stage.GetRootLayer().customLayerData = {"marine:schemaVersion": SCHEMA_VERSION}
    return stage, geom


def _asset_prim(stage, path: str, cls: str, kind: str, function: str, code: str, lock: IdLock, key: str):
    prim = UsdGeom.Xform.Define(stage, path).GetPrim()
    Usd.ModelAPI(prim).SetKind(kind)
    prim.AddAppliedSchema("MarineAssetAPI")
    set_marine(prim, "id", lock.get(key))
    set_marine(prim, "class", cls)
    set_marine(prim, "function", function)
    set_marine(prim, "code", code)
    set_marine(prim, "material", "proxy")
    set_marine(prim, "massSource", "rollup")
    set_marine(prim, "lodLevel", 0)
    return prim


def _rollup_mass(prim: Usd.Prim) -> float:
    total = 0.0
    for c in prim.GetChildren():
        if is_asset(c):
            total += float(get_marine(c).get("mass", 0.0))
    return round(total, 3)


def compose_system(vessel: str, vdir: Path, zone_path: str, sysdef: dict, lock: IdLock) -> Path:
    codes = vocab.codes()
    path = vdir / sysdef["layer"]
    stage, geom = _new_layer_stage(path)
    sname = sysdef["name"]
    root = f"/{sname}"
    sys_key = f"occ:{vessel}{zone_path}/{sname}"
    sprim = _asset_prim(stage, root, sysdef["class"], "assembly", sysdef["function"],
                        codes[sysdef["code_key"]], lock, sys_key)
    stage.SetDefaultPrim(sprim)

    for eq in sysdef["equipment"]:
        epath = f"{root}/{eq['name']}"
        eprim = UsdGeom.Xform.Define(stage, epath).GetPrim()
        rel_asset = Path("../../../library") / eq["asset"]
        eprim.GetReferences().AddReference(rel_asset.as_posix())
        for vset, sel in (eq.get("variants") or {}).items():
            eprim.GetVariantSets().GetVariantSet(vset).SetVariantSelection(str(sel))
        UsdGeom.Xformable(eprim).MakeMatrixXform().Set(gf_matrix(translate(*eq["position"])))
        ecode = codes[eq["code_key"]]
        eprim.CreateRelationship("marine:serves", custom=False).SetTargets([Sdf.Path(root)])
        # occurrence IDs for every asset prim in the referenced subtree
        for p in Usd.PrimRange(eprim):
            if not is_asset(p):
                continue
            rel = str(p.GetPath())[len(epath):]          # "" for the equipment itself
            okey = f"occ:{vessel}{zone_path}/{sname}/{eq['name']}{rel}"
            m = get_marine(p)
            set_marine(p, "definitionId", m.get("id", ""))
            set_marine(p, "id", lock.get(okey))
            set_marine(p, "code", ecode + rel)
            if "instanceNames" in m:
                ids = [lock.get(f"{okey}/{n}") for n in m["instanceNames"]]
                set_marine(p, "instanceIds", Vt.StringArray(ids))
    for a, b in sysdef.get("connections", []):
        pa = stage.GetPrimAtPath(f"{root}/{a}")
        pb = Sdf.Path(f"{root}/{b}")
        if not pa or not stage.GetPrimAtPath(pb):
            raise ValueError(f"connection {a} -> {b}: prim not found")
        r = pa.CreateRelationship("marine:connectedTo", custom=False)
        r.AddTarget(pb)
    set_marine(sprim, "mass", _rollup_mass(sprim))

    overrides = load_overrides(vdir / "explode_overrides.yaml").get(sname, {})
    report = run_on_stage(stage, [sprim], stage.GetRootLayer(), geom, overrides)
    stage.GetRootLayer().Save()
    geom.Save()
    print(f"composed system {sname}: {len(sysdef['equipment'])} equipment, "
          f"explode {[(e['prim'].rsplit('/', 1)[-1], e['dist']) for e in report['explode']]}")
    return path


def compose_vessel(vdir: Path, lock: IdLock | None = None) -> Path:
    lock = lock or IdLock()
    arr = yaml.safe_load((vdir / "arrangement.yaml").read_text())
    vessel = vdir.name
    codes = vocab.codes()
    vdef = arr["vessel"]
    layers = []
    for z in arr["zones"]:
        for s in z["systems"]:
            layers.append((z, s, compose_system(vessel, vdir, f"/{z['name']}", s, lock)))

    path = vdir / f"{vessel}.usda"
    stage, geom = _new_layer_stage(path)
    root = f"/{vdef['name']}"
    vprim = _asset_prim(stage, root, vdef["class"], "assembly", vdef["function"], codes.get("vessel", ""),
                        lock, f"occ:{vessel}")
    set_marine(vprim, "lodLevel", 0)
    stage.SetDefaultPrim(vprim)
    Usd.ModelAPI(vprim).SetAssetInfo({"name": vessel, "version": arr.get("version", "0.0.0")})
    zprims = []
    for z in arr["zones"]:
        zprim = _asset_prim(stage, f"{root}/{z['name']}", z["class"], "assembly", z["function"],
                            codes[z["code_key"]], lock, f"occ:{vessel}/{z['name']}")
        zprims.append(zprim)
    for z, s, layer in layers:
        sp = stage.DefinePrim(f"{root}/{z['name']}/{s['name']}", "Xform")
        sp.GetPayloads().AddPayload(f"./{layer.name}")
    stage.Load()
    for zp in zprims:
        set_marine(zp, "mass", _rollup_mass(zp))
    set_marine(vprim, "mass", _rollup_mass(vprim))
    overrides = load_overrides(vdir / "explode_overrides.yaml").get(vdef["name"], {})
    run_on_stage(stage, [vprim] + zprims, stage.GetRootLayer(), geom, overrides)
    stage.GetRootLayer().Save()
    geom.Save()
    lock.save()
    print(f"composed vessel {vessel}: {path.relative_to(ASSETS.parent)}")
    return path


def run(lock: IdLock | None = None) -> list[Path]:
    lock = lock or IdLock()
    return [compose_vessel(d, lock) for d in sorted((ASSETS / "vessels").iterdir()) if (d / "arrangement.yaml").exists()]


if __name__ == "__main__":
    run()

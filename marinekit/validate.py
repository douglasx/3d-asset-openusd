"""Stage 6: validators. Every check from the plan, run in CI on every build.

    python -m marinekit.validate [--baseline-lock path]

Exit code is non-zero when any check reports an error. A JSON report is
written to build/out/reports/validation.json.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pygltflib
from pxr import Sdf, Usd, UsdGeom, UsdValidation

from . import geom, graph, vocab
from .explode import asset_children
from .ids import IdLock, check_append_only
from .paths import ASSETS, ID_LOCK, LIBRARY, OUT, ROOT
from .stagegeom import (bbox_cache, boxes_overlap, iter_render_meshes, mesh_arrays, relative_box,
                        transform_points)
from .usd_io import get_marine, is_asset, np_matrix

DIM_TOL = 0.02       # 2 % for tessellated dimension checks
ROLLUP_TOL = 0.001


@dataclass
class Finding:
    check: str
    severity: str        # error | warn | info
    where: str
    message: str


class Report:
    def __init__(self):
        self.findings: list[Finding] = []
        self.counts: Counter = Counter()

    def ok(self, check: str, n: int = 1):
        self.counts[(check, "pass")] += n

    def add(self, check: str, severity: str, where: str, message: str):
        self.findings.append(Finding(check, severity, where, message))
        self.counts[(check, severity)] += 1

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "error"]


# ------------------------------------------------------------------ helpers
def library_assets() -> list[Path]:
    return sorted(p for p in LIBRARY.glob("*/*/*.usda"))


def vessel_files() -> list[Path]:
    return sorted(d / f"{d.name}.usda" for d in (ASSETS / "vessels").iterdir() if (d / f"{d.name}.usda").exists())


def open_loaded(path: Path) -> Usd.Stage:
    return Usd.Stage.Open(str(path), Usd.Stage.LoadAll)


def own_and_subtree_mesh(prim: Usd.Prim):
    """(points, tris) of all render geometry under prim, in prim's frame.
    For a PointInstancer: its prototype only (one fastener)."""
    if prim.IsA(UsdGeom.PointInstancer):
        pi = UsdGeom.PointInstancer(prim)
        proto = prim.GetStage().GetPrimAtPath(pi.GetPrototypesRel().GetTargets()[0])
        pts, _, tris = mesh_arrays(UsdGeom.Mesh(proto))
        return pts, tris
    P, T, off = [], [], 0
    for mesh, m in iter_render_meshes(prim, prim):
        pts, _, tris = mesh_arrays(mesh)
        P.append(transform_points(pts, m))
        T.append(tris + off)
        off += len(pts)
    if not P:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=int)
    return np.vstack(P), np.vstack(T)


def slice_radius(pts: np.ndarray, tris: np.ndarray, x: float) -> float:
    """Largest distance from the x axis where the plane x = const cuts the mesh."""
    best = 0.0
    a, b, c = pts[tris[:, 0]], pts[tris[:, 1]], pts[tris[:, 2]]
    for p, q in ((a, b), (b, c), (c, a)):
        dp, dq = p[:, 0] - x, q[:, 0] - x
        mask = dp * dq < 0
        if not np.any(mask):
            continue
        t = (dp[mask] / (dp[mask] - dq[mask]))[:, None]
        hit = p[mask] + t * (q[mask] - p[mask])
        best = max(best, float(np.sqrt(hit[:, 1] ** 2 + hit[:, 2] ** 2).max()))
    return best


def measure(rule: str, pts: np.ndarray, tris: np.ndarray) -> float:
    if rule == "radial_max":
        return 2.0 * float(np.sqrt(pts[:, 1] ** 2 + pts[:, 2] ** 2).max())
    if rule == "mid_section":
        return 2.0 * slice_radius(pts, tris, 0.5 * (pts[:, 0].min() + pts[:, 0].max()))
    if rule in ("x_extent", "x_extent_le"):
        return float(pts[:, 0].max() - pts[:, 0].min())
    raise ValueError(rule)


# ------------------------------------------------------------------ checks
def check_usd_compliance(rep: Report, paths: list[Path]) -> None:
    """usdchecker equivalent: OpenUSD's registered validators (UsdValidation)."""
    reg = UsdValidation.ValidationRegistry()
    ctx = UsdValidation.ValidationContext(reg.GetOrLoadAllValidators())
    for p in paths:
        errs = ctx.Validate(open_loaded(p))
        for e in errs:
            sev = "error" if "Error" in str(e.GetType()) else "warn"
            rep.add("usdchecker", sev, str(p.relative_to(ROOT)), f"{e.GetName()}: {e.GetMessage()}")
        if not errs:
            rep.ok("usdchecker")


def check_units_axis(rep: Report, paths: list[Path]) -> None:
    for p in paths:
        layer = Sdf.Layer.FindOrOpen(str(p))
        mpu = layer.pseudoRoot.GetInfo("metersPerUnit") if layer.pseudoRoot.HasInfo("metersPerUnit") else None
        up = layer.pseudoRoot.GetInfo("upAxis") if layer.pseudoRoot.HasInfo("upAxis") else None
        where = str(p.relative_to(ROOT))
        if mpu != 1.0:
            rep.add("units_axis", "error", where, f"metersPerUnit is {mpu}, expected 1")
        elif up != "Z":
            rep.add("units_axis", "error", where, f"upAxis is {up}, expected Z")
        else:
            rep.ok("units_axis")
        stage = open_loaded(p)
        for prim in Usd.PrimRange(stage.GetPseudoRoot()):
            x = UsdGeom.Xformable(prim)
            if not x or not prim.GetAttribute("xformOpOrder").HasAuthoredValue():
                continue
            m = np_matrix(x.GetLocalTransformation())[:3, :3]
            if not np.allclose(m @ m.T, np.eye(3), atol=1e-6) or abs(np.linalg.det(m) - 1) > 1e-6:
                rep.add("units_axis", "error", str(prim.GetPath()), "transform has scale or shear (unapplied scale)")


def check_required_and_vocab(rep: Report, stage: Usd.Stage, occurrence: bool) -> None:
    classes, materials = vocab.classes(), vocab.materials()
    for prim in Usd.PrimRange(stage.GetDefaultPrim()):
        if not is_asset(prim):
            continue
        m = get_marine(prim)
        where = str(prim.GetPath())
        cls = m.get("class", "")
        if cls not in classes:
            rep.add("vocabulary", "error", where, f"class '{cls}' not in classes.yaml")
            continue
        if m.get("material") not in materials:
            rep.add("vocabulary", "error", where, f"material '{m.get('material')}' not in materials.yaml")
        else:
            rep.ok("vocabulary")
        req = vocab.required_attrs(cls)
        if not occurrence:
            req = [r for r in req if r != "code"]
        missing = [r for r in req if m.get(r) in (None, "")]
        if missing:
            rep.add("required_properties", "error", where, f"missing {missing}")
        else:
            rep.ok("required_properties")


def check_ids(rep: Report, vessel_stage: Usd.Stage, vessel: str, lock: IdLock, baseline: dict | None) -> None:
    seen: dict[str, str] = {}
    vroot = vessel_stage.GetDefaultPrim()
    for prim in Usd.PrimRange(vroot):
        if not is_asset(prim):
            continue
        m = get_marine(prim)
        rel = str(prim.GetPath())[len(str(vroot.GetPath())):]
        key = f"occ:{vessel}{rel}"
        ids = [(m.get("id"), key)] + [(i, f"{key}/{n}") for i, n in zip(m.get("instanceIds", []),
                                                                         m.get("instanceNames", []))]
        for id_, k in ids:
            if not id_:
                rep.add("id_unique_stable", "error", k, "no marine:id")
                continue
            if id_ in seen:
                rep.add("id_unique_stable", "error", k, f"duplicate ID {id_} (also {seen[id_]})")
            seen[id_] = k
            if lock.lookup(k) != id_:
                rep.add("id_unique_stable", "error", k, f"ID {id_} does not match lock ({lock.lookup(k)})")
            else:
                rep.ok("id_unique_stable")
    if baseline is not None:
        for err in check_append_only(baseline, lock.ids):
            rep.add("id_unique_stable", "error", "assets/ids.lock.json", err)
    else:
        rep.add("id_unique_stable", "info", "assets/ids.lock.json", "no baseline lock: append-only check skipped")


def check_dimensions_and_mass(rep: Report, stage: Usd.Stage) -> None:
    classes = vocab.classes()
    tol_mass = vocab.budgets()["mass_check"]["tolerance"]
    for prim in Usd.PrimRange(stage.GetDefaultPrim()):
        if not is_asset(prim):
            continue
        m = get_marine(prim)
        where = str(prim.GetPath())
        rules = classes.get(m.get("class", ""), {}).get("check", {})
        pts = tris = None
        if rules:
            pts, tris = own_and_subtree_mesh(prim)
        for attr, rule in rules.items():
            if m.get(attr) is None or len(pts) == 0:
                continue
            declared, got = float(m[attr]), measure(rule, pts, tris)
            ok = got >= declared * (1 - DIM_TOL) if rule == "x_extent_le" else abs(got - declared) <= DIM_TOL * declared
            if ok:
                rep.ok("bbox_vs_parameters")
            else:
                rep.add("bbox_vs_parameters", "error", where,
                        f"{attr} declared {declared:.4f} m, mesh {rule} gives {got:.4f} m")
        src = m.get("massSource")
        mass = float(m.get("mass", 0.0))
        if src == "rollup":
            total = sum(float(get_marine(c).get("mass", 0.0)) for c in asset_children(prim))
            if abs(total - mass) > ROLLUP_TOL * max(mass, 1.0):
                rep.add("mass", "error", where, f"rollup mass {mass} != sum of children {total:.3f}")
            else:
                rep.ok("mass")
        elif src == "computed":
            density = vocab.density(m["material"])
            thin = classes.get(m.get("class", ""), {}).get("mass_check") == "thin_plate"
            if thin and m.get("thickness"):
                g = prim.GetChild("geom")
                p, _, t = mesh_arrays(UsdGeom.Mesh(g))
                vol = 0.5 * geom.mesh_area(p, t) * float(m["thickness"])
            elif prim.IsA(UsdGeom.PointInstancer):
                p, t = own_and_subtree_mesh(prim)
                vol = geom.mesh_volume(p, t) * len(m.get("instanceIds", []))
            else:
                g = prim.GetChild("geom")
                if not g:
                    continue
                p, _, t = mesh_arrays(UsdGeom.Mesh(g))
                vol = geom.mesh_volume(p, t)
            est = vol * density
            if mass <= 0 or abs(est - mass) / mass > tol_mass:
                rep.add("mass", "error", where, f"declared {mass:.1f} kg vs mesh volume x density {est:.1f} kg")
            else:
                rep.ok("mass")
        else:
            rep.ok("mass")


def check_explode_proxy(rep: Report, stage: Usd.Stage) -> None:
    cache = bbox_cache()
    L0 = vocab.budgets()["lod"]["L0"]
    for prim in Usd.PrimRange(stage.GetDefaultPrim()):
        if not is_asset(prim):
            continue
        kids = asset_children(prim)
        if not kids:
            continue
        where = str(prim.GetPath())
        proxy = prim.GetChild("proxy")
        if not proxy or not proxy.IsActive() or UsdGeom.Imageable(proxy).ComputePurpose() != UsdGeom.Tokens.proxy:
            rep.add("explode_proxy", "error", where, "assembly with children has no L0 proxy")
        else:
            n = len(UsdGeom.Mesh(proxy).GetFaceVertexCountsAttr().Get())
            if not (L0["min"] <= n <= L0["max"]):
                rep.add("triangle_budget", "error", where, f"L0 proxy has {n} triangles (budget {L0['min']}..{L0['max']})")
            else:
                rep.ok("triangle_budget")
            rep.ok("explode_proxy")
        moved = []
        own = prim.GetChild("geom")
        if own:
            b = relative_box(cache, own, prim)
            if b:
                moved.append(("<own geometry>", b))
        for k in kids:
            b = relative_box(cache, k, prim)
            if b is None:
                continue
            km = get_marine(k)
            off = np.array(km.get("explodeDir", [0, 0, 0])) * float(km.get("explodeDist", 0.0))
            moved.append((k.GetName(), (b[0] + off, b[1] + off)))
        for i in range(len(moved)):
            for j in range(i + 1, len(moved)):
                if boxes_overlap(moved[i][1], moved[j][1]):
                    rep.add("explode_proxy", "error", where,
                            f"exploded siblings overlap at slider=1: {moved[i][0]} / {moved[j][0]}")
                else:
                    rep.ok("explode_proxy")


def check_shacl(rep: Report, vessel_usda: Path) -> None:
    g = graph.build_graph(vessel_usda)
    ok, _, issues = graph.validate(g)
    for i in issues:
        rep.add("ontology_shacl", "error" if i["severity"] == "Violation" else "info", i["focus"],
                f"{i['path'].rsplit('#', 1)[-1]}: {i['message']}")
    if ok:
        rep.ok("ontology_shacl")


def glb_nodes(path: Path) -> list:
    return pygltflib.GLTF2().load(str(path)).nodes


def raw_glb_bbox(path: Path):
    """World AABB of a (non-compressed) GLB from accessor min/max and node TRS."""
    from scipy.spatial.transform import Rotation
    doc = pygltflib.GLTF2().load(str(path))
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)

    def visit(i, parent):
        n = doc.nodes[i]
        m = np.eye(4)
        if n.rotation:
            m[:3, :3] = Rotation.from_quat(n.rotation).as_matrix()
        if n.translation:
            m[:3, 3] = n.translation
        w = parent @ m
        nonlocal lo, hi
        if n.mesh is not None:
            for prim in doc.meshes[n.mesh].primitives:
                a = doc.accessors[prim.attributes.POSITION]
                corners = np.array([[x, y, z] for x in (a.min[0], a.max[0]) for y in (a.min[1], a.max[1])
                                    for z in (a.min[2], a.max[2])])
                c = transform_points(corners, w)
                lo, hi = np.minimum(lo, c.min(0)), np.maximum(hi, c.max(0))
        for c in n.children or []:
            visit(c, w)
    for r in doc.scenes[0].nodes:
        visit(r, np.eye(4))
    return lo, hi


def check_glb_roundtrip(rep: Report, vessel_stage: Usd.Stage, manifest_path: Path) -> None:
    man = json.loads(manifest_path.read_text())
    out_dir = manifest_path.parent
    by_id = {get_marine(p)["id"]: p for p in Usd.PrimRange(vessel_stage.GetDefaultPrim()) if is_asset(p)}
    keys = ["class", "material", "mass", "code", "function", "diameter", "length", "thickness",
            "explodeDist", "definitionId"]
    for c in man["chunks"]:
        if c["lod"] != "L2":
            continue
        sysprim = by_id[c["system"]]
        expect = set()
        for p in Usd.PrimRange(sysprim):
            if is_asset(p):
                m = get_marine(p)
                expect.add(m["id"])
                expect.update(m.get("instanceIds", []))
        nodes = glb_nodes(out_dir / c["url"])
        got = {n.name for n in nodes if n.extras and n.extras.get("marine", {}).get("role") in ("asset", "instance")}
        where = c["url"]
        if got != expect:
            rep.add("glb_roundtrip", "error", where,
                    f"ID sets differ: {len(expect - got)} missing in GLB, {len(got - expect)} extra")
        else:
            rep.ok("glb_roundtrip")
        bad = 0
        for n in nodes:
            mx = (n.extras or {}).get("marine", {})
            if mx.get("role") != "asset":
                continue
            um = get_marine(by_id[n.name])
            for k in keys:
                if k in um and um[k] != mx.get(k):
                    bad += 1
                    rep.add("glb_roundtrip", "error", where, f"{n.name} {k}: USD {um[k]!r} != GLB {mx.get(k)!r}")
        if not bad:
            rep.ok("glb_roundtrip")
        raw = OUT / "vessels" / manifest_path.parent.name / "raw" / f"{c['systemName'].lower()}.L2.glb"
        if raw.exists():
            lo, hi = raw_glb_bbox(raw)
            b = relative_box(bbox_cache(), sysprim, sysprim)
            ub = (b[0], b[1])
            # chunk root is the system node with identity transform in its own frame
            if not (np.allclose(lo, ub[0], atol=2e-3) and np.allclose(hi, ub[1], atol=2e-3)):
                rep.add("glb_roundtrip", "error", where, f"bbox GLB {lo.round(3)}..{hi.round(3)} vs USD "
                        f"{np.round(ub[0], 3)}..{np.round(ub[1], 3)}")
            else:
                rep.ok("glb_roundtrip")


def check_budgets(rep: Report, manifest_path: Path) -> None:
    man = json.loads(manifest_path.read_text())
    lod = vocab.budgets()["lod"]
    for c in man["chunks"]:
        if c["lod"] in ("L1", "L2"):
            mx = lod[c["lod"]]["max"]
            if c["triangles"] > mx:
                rep.add("triangle_budget", "error", c["url"], f"{c['triangles']} triangles > {c['lod']} budget {mx}")
            else:
                rep.ok("triangle_budget")


def check_step_roundtrip(rep: Report) -> None:
    """Generator -> USD vs generator -> STEP -> ingest -> USD: same prims, IDs,
    attributes, instances; extents within 1 mm."""
    gen_dir = OUT / "generated"
    for lib in library_assets():
        gen = gen_dir / lib.name
        if not gen.exists():
            continue
        a, b = Usd.Stage.Open(str(gen)), Usd.Stage.Open(str(lib))
        pa = {str(p.GetPath()): p for p in Usd.PrimRange(a.GetDefaultPrim()) if is_asset(p)}
        pb = {str(p.GetPath()): p for p in Usd.PrimRange(b.GetDefaultPrim()) if is_asset(p)}
        where = lib.name
        if set(pa) != set(pb):
            rep.add("step_roundtrip", "error", where, f"prim sets differ: {sorted(set(pa) ^ set(pb))[:5]}")
            continue
        cache_a, cache_b = bbox_cache(), bbox_cache()
        bad = 0
        for path in pa:
            ma, mb = get_marine(pa[path]), get_marine(pb[path])
            for k in set(ma) | set(mb):
                if k in ("explodeDir", "explodeDist"):
                    continue
                va, vb = ma.get(k), mb.get(k)
                if isinstance(va, float) and isinstance(vb, float):
                    same = abs(va - vb) <= 1e-9 * max(1.0, abs(va))
                else:
                    same = va == vb
                if not same:
                    bad += 1
                    rep.add("step_roundtrip", "error", f"{where}:{path}", f"{k}: generator {va!r} vs ingested {vb!r}")
            ga, gb = pa[path].GetChild("geom"), pb[path].GetChild("geom")
            if ga and gb:
                pa_, _, ta_ = mesh_arrays(UsdGeom.Mesh(ga))
                pb_, _, tb_ = mesh_arrays(UsdGeom.Mesh(gb))
                aa, ab = geom.mesh_area(pa_, ta_), geom.mesh_area(pb_, tb_)
                if aa > 0 and abs(aa - ab) / aa > 0.01:
                    bad += 1
                    rep.add("step_roundtrip", "error", f"{where}:{path}",
                            f"surface area changed through STEP: {aa:.3f} -> {ab:.3f} m2 (faces lost or added)")
            ba, bb = relative_box(cache_a, pa[path], a.GetDefaultPrim()), relative_box(cache_b, pb[path], b.GetDefaultPrim())
            if (ba is None) != (bb is None) or (ba is not None and not (np.allclose(ba[0], bb[0], atol=1e-3)
                                                                          and np.allclose(ba[1], bb[1], atol=1e-3))):
                bad += 1
                rep.add("step_roundtrip", "error", f"{where}:{path}", f"extent differs {ba} vs {bb}")
        if not bad:
            rep.ok("step_roundtrip")


def baseline_lock(path: str | None) -> dict | None:
    if path:
        return json.loads(Path(path).read_text())["ids"]
    try:
        txt = subprocess.run(["git", "show", f"HEAD:{ID_LOCK.relative_to(ROOT).as_posix()}"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout
        return json.loads(txt)["ids"]
    except Exception:
        return None


def run(baseline: str | None = None, quiet: bool = False) -> Report:
    rep = Report()
    lock = IdLock()
    libs = library_assets()
    vessels = vessel_files()
    layer_files = libs + sorted(LIBRARY.glob("*/*.usda")) + vessels + sorted(ASSETS.glob("vessels/*/*.usda"))
    layer_files = sorted(set(layer_files))
    check_usd_compliance(rep, libs + vessels)
    check_units_axis(rep, layer_files)
    for lib in libs:
        st = open_loaded(lib)
        check_required_and_vocab(rep, st, occurrence=False)
        check_dimensions_and_mass(rep, st)
        check_explode_proxy(rep, st)
    check_step_roundtrip(rep)
    base = baseline_lock(baseline)
    for v in vessels:
        st = open_loaded(v)
        check_required_and_vocab(rep, st, occurrence=True)
        check_ids(rep, st, v.parent.name, lock, base)
        check_explode_proxy(rep, st)
        check_shacl(rep, v)
        man = OUT / "vessels" / v.parent.name / "manifest.json"
        if man.exists():
            check_budgets(rep, man)
            check_glb_roundtrip(rep, st, man)
        else:
            rep.add("glb_roundtrip", "error", str(man), "manifest missing; run the build first")
    out = OUT / "reports"
    out.mkdir(parents=True, exist_ok=True)
    summary = {}
    for (check, sev), n in sorted(rep.counts.items()):
        summary.setdefault(check, {})[sev] = n
    (out / "validation.json").write_text(json.dumps(
        {"summary": summary, "findings": [asdict(f) for f in rep.findings]}, indent=1) + "\n")
    if not quiet:
        print(f"{'check':24s} {'pass':>6s} {'error':>6s} {'warn':>6s} {'info':>6s}")
        for check, s in summary.items():
            print(f"{check:24s} {s.get('pass', 0):6d} {s.get('error', 0):6d} {s.get('warn', 0):6d} {s.get('info', 0):6d}")
        for f in rep.findings:
            if f.severity == "error":
                print(f"  ERROR {f.check}: {f.where}: {f.message}")
    return rep


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--baseline-lock", help="previous ids.lock.json (default: git HEAD)")
    args = ap.parse_args(argv)
    rep = run(args.baseline_lock)
    return 1 if rep.errors else 0


if __name__ == "__main__":
    sys.exit(main())

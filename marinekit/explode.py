"""Default explode vectors and L0 proxies.

Explode: the assembled pose is canonical. Each asset child gets
marine:explodeDir (unit vector, parent frame) and marine:explodeDist (m), so
    exploded position = assembled position + explodeDir * explodeDist * slider
one hierarchy level at a time. Defaults come from the class pattern
(schema/vocab/classes.yaml: axial / radial / vertical / anchor) and are spaced
greedily by bounding box so siblings never overlap at slider = 1. Engineers
override individual prims in an explode_overrides.yaml; the override wins.

Proxy: every asset prim with asset children gets an L0 proxy mesh: the union
of one convex hull per child (plus one for its own geometry), written as a
child Mesh "proxy" with purpose = proxy.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import yaml
from pxr import Sdf, Usd, UsdGeom
from scipy.spatial import ConvexHull, QhullError

from . import vocab
from .stagegeom import (bbox_cache, boxes_overlap, iter_render_meshes, mesh_arrays, relative_box,
                        subtree_points, transform_points)
from .usd_io import get_marine, is_asset, set_marine, write_mesh

AXES = [np.array(v, dtype=float) for v in
        [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]]


def asset_children(prim: Usd.Prim) -> list[Usd.Prim]:
    return [c for c in prim.GetChildren() if is_asset(c)]


def _pattern(prim: Usd.Prim) -> str:
    cls = get_marine(prim).get("class", "")
    return vocab.classes().get(cls, {}).get("explode", "radial")


def _preferred_dir(pattern: str, c: np.ndarray, pc: np.ndarray, size: float) -> np.ndarray:
    d = c - pc
    if pattern == "anchor":
        return np.zeros(3)
    if pattern == "axial":
        return np.array([1.0 if d[0] >= -1e-6 * size else -1.0, 0, 0])
    if pattern == "radial":
        v = np.array([0.0, d[1], d[2]])
        n = np.linalg.norm(v)
        if n > 0.02 * size:
            return v / n
        pattern = "vertical"
    if pattern == "vertical":
        return np.array([0, 0, 1.0 if d[2] >= -0.05 * size else -1.0])
    raise ValueError(pattern)


def explode_parent(parent: Usd.Prim, cache, overrides: dict, layer_ctx) -> list[dict]:
    kids = asset_children(parent)
    if not kids:
        return []
    boxes = {k.GetPath(): relative_box(cache, k, parent) for k in kids}
    own = parent.GetChild("geom")
    own_box = relative_box(cache, own, parent) if own and own.IsValid() else None
    all_boxes = [b for b in boxes.values() if b is not None] + ([own_box] if own_box else [])
    if not all_boxes:
        return []
    lo = np.min([b[0] for b in all_boxes], axis=0)
    hi = np.max([b[1] for b in all_boxes], axis=0)
    pc, size = 0.5 * (lo + hi), float(np.linalg.norm(hi - lo)) or 1.0
    step, margin, min_steps = 0.01 * size, 0.01 * size, 4

    placed = [own_box] if own_box else []
    plan = []
    for k in kids:
        b = boxes[k.GetPath()]
        if b is None:
            continue
        pat = _pattern(k)
        c = 0.5 * (b[0] + b[1])
        d = _preferred_dir(pat, c, pc, size)
        plan.append((k, b, pat, d, abs(float(np.dot(c - pc, d))) if pat != "anchor" else -1.0))
    # no anchor among the children: the largest child (by box volume) stays put
    if plan and not any(t[2] == "anchor" for t in plan) and own_box is None:
        vol = lambda t: float(np.prod(t[1][1] - t[1][0]))
        i = max(range(len(plan)), key=lambda j: (vol(plan[j]), str(plan[j][0].GetPath())))
        k, b, _, _, _ = plan[i]
        plan[i] = (k, b, "anchor", np.zeros(3), -1.0)
    # anchors first, then closest-to-centre first so offsets chain outward
    # fasteners go last: they find space after the parts they join
    def is_fastener(t):
        return vocab.classes().get(get_marine(t[0]).get("class", ""), {}).get("level") == "fastener"
    # then biggest first: large parts take the clean preferred moves, small
    # parts (bearings around a shaft, covers) adapt around them
    def vol(t):
        return float(np.prod(t[1][1] - t[1][0]))
    # axial siblings chain outward from the anchor (closest first), so a shaft
    # line arrangement separates in order; everything else biggest-first
    anchor_c = next((0.5 * (t[1][0] + t[1][1]) for t in plan if t[2] == "anchor"), pc)

    def order(t):
        if t[2] == "anchor":
            return (0, 0, 0.0, str(t[0].GetPath()))
        c = 0.5 * (t[1][0] + t[1][1])
        if t[2] == "axial" and not is_fastener(t):
            return (1, 0, abs(float(c[0] - anchor_c[0])), str(t[0].GetPath()))
        return (1, 1 + int(is_fastener(t)), -round(vol(t), 9), str(t[0].GetPath()))
    plan.sort(key=order)
    results = []
    for k, b, pat, d, _ in plan:
        rel = str(k.GetPath())
        if rel in overrides:
            o = overrides[rel]
            d_best, dist_best = np.array(o["dir"], dtype=float), float(o["dist"])
            src = "override"
        elif pat == "anchor":
            d_best, dist_best, src = np.zeros(3), 0.0, "anchor"
        else:
            best = None
            for cand in [d] + [a for a in AXES if not np.allclose(a, d)]:
                penalty = 1.0 if cand is d else 1.6
                for i in range(min_steps, 600):
                    dist = i * step
                    mb = (b[0] + cand * dist, b[1] + cand * dist)
                    if not any(boxes_overlap(mb, p, margin) for p in placed):
                        break
                cost = dist * penalty
                if best is None or cost < best[0]:
                    best = (cost, cand, dist)
            _, d_best, dist_best = best
            src = "default"
        n = np.linalg.norm(d_best)
        d_best = d_best / n if n > 0 else d_best
        placed.append((b[0] + d_best * dist_best, b[1] + d_best * dist_best))
        results.append({"prim": rel, "dir": d_best.round(6).tolist(), "dist": round(dist_best, 4),
                        "source": src, "cls": get_marine(k).get("class"), "pattern": pat,
                        "box": b})
    _uniform_symmetric(results, margin)
    for r in results:
        k = parent.GetStage().GetPrimAtPath(r["prim"])
        with layer_ctx():
            set_marine(k, "explodeDir", r["dir"])
            set_marine(k, "explodeDist", r["dist"])
        r.pop("box"), r.pop("cls"), r.pop("pattern")
    return results


def _uniform_symmetric(results: list[dict], margin: float) -> None:
    """Give rotationally symmetric siblings (same class, radial) one common
    distance, the largest of the set, if that keeps every box clear."""
    groups: dict[str, list[dict]] = {}
    for r in results:
        if r["source"] == "default" and r["pattern"] == "radial":
            groups.setdefault(r["cls"], []).append(r)
    for members in groups.values():
        if len(members) < 2:
            continue
        dmax = max(m["dist"] for m in members)
        trial = {id(r): (r["dist"] if r not in members else dmax) for r in results}
        moved = [(r["box"][0] + np.array(r["dir"]) * trial[id(r)], r["box"][1] + np.array(r["dir"]) * trial[id(r)])
                 for r in results]
        clear = all(not boxes_overlap(moved[i], moved[j], margin)
                    for i in range(len(moved)) for j in range(i + 1, len(moved))
                    if results[i]["source"] != "anchor" or results[j]["source"] != "anchor")
        if clear:
            for m in members:
                m["dist"] = dmax


def _hull(points: np.ndarray):
    if len(points) < 4:
        return None
    try:
        h = ConvexHull(points)
    except QhullError:
        return None
    tris = h.simplices.copy()
    # orient outward
    centre = points[h.vertices].mean(0)
    a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
    flip = np.einsum("ij,ij->i", np.cross(b - a, c - a), a - centre) < 0
    tris[flip] = tris[flip][:, [0, 2, 1]]
    return tris


def proxy_mesh(parent: Usd.Prim, max_tris: int):
    """Union of convex hulls (one per child subtree + own geometry), flat-shaded."""
    groups = [c for c in asset_children(parent)]
    own = parent.GetChild("geom")
    if own and own.IsValid():
        groups.append(own)
    out_p, out_n, out_t = [], [], []
    budget_pts = 400
    while True:
        out_p, out_n, out_t = [], [], []
        off = 0
        for g in groups:
            pts = subtree_points(g, parent, max_per_mesh=budget_pts)
            if len(pts) == 0:
                continue
            tris = _hull(pts)
            if tris is None:
                continue
            tri_pts = pts[tris].reshape(-1, 3)
            a, b, c = tri_pts[0::3], tri_pts[1::3], tri_pts[2::3]
            fn = np.cross(b - a, c - a)
            fn /= np.maximum(np.linalg.norm(fn, axis=1, keepdims=True), 1e-12)
            out_p.append(tri_pts)
            out_n.append(np.repeat(fn, 3, axis=0))
            out_t.append(np.arange(len(tri_pts)).reshape(-1, 3) + off)
            off += len(tri_pts)
        total = sum(len(t) for t in out_t)
        if total <= max_tris or budget_pts <= 20:
            break
        budget_pts //= 2
    if not out_t:
        return None
    return np.vstack(out_p), np.vstack(out_n).astype(np.float32), np.vstack(out_t)


def dominant_material(parent: Usd.Prim) -> str:
    best, mass = "proxy", -1.0
    for p in Usd.PrimRange(parent):
        if is_asset(p) and p != parent:
            m = get_marine(p)
            if m.get("material", "proxy") != "proxy" and float(m.get("mass", 0)) > mass:
                best, mass = m["material"], float(m.get("mass", 0))
    return best


def write_proxies(stage: Usd.Stage, parents: list[Usd.Prim], geom_layer: Sdf.Layer, attr_layer: Sdf.Layer) -> list[dict]:
    max_tris = vocab.budgets()["lod"]["L0"]["max"]
    out = []
    # deepest first so hulls never include stale proxies (proxies are excluded anyway)
    for parent in sorted(parents, key=lambda p: -len(p.GetPath().pathString)):
        res = proxy_mesh(parent, max_tris)
        if res is None:
            continue
        pts, nrm, tris = res
        path = parent.GetPath().AppendChild("proxy")
        with Usd.EditContext(stage, geom_layer):
            write_mesh(stage, path, pts, nrm, tris, dominant_material(parent), purpose=UsdGeom.Tokens.proxy)
        out.append({"prim": str(parent.GetPath()), "triangles": int(len(tris))})
    return out


def load_overrides(path: Path) -> dict:
    if path and Path(path).exists():
        return yaml.safe_load(Path(path).read_text()) or {}
    return {}


def run_on_stage(stage: Usd.Stage, parents: list[Usd.Prim], attr_layer: Sdf.Layer, geom_layer: Sdf.Layer,
                 overrides: dict | None = None) -> dict:
    cache = bbox_cache()
    overrides = overrides or {}
    explode = []

    def ctx():
        return Usd.EditContext(stage, attr_layer)

    # outermost first: exploding a parent never depends on its children's explode values
    for parent in sorted(parents, key=lambda p: len(p.GetPath().pathString)):
        explode += explode_parent(parent, cache, overrides, ctx)
    proxies = write_proxies(stage, [p for p in parents if asset_children(p)], geom_layer, attr_layer)
    return {"explode": explode, "proxies": proxies}


def run_library_asset(usda: Path, overrides: dict | None = None) -> dict:
    stage = Usd.Stage.Open(str(usda))
    root_layer = stage.GetRootLayer()
    geom_layer = Sdf.Layer.FindOrOpen(str(usda.with_name(usda.stem + ".geom.usdc")))
    parents = [p for p in Usd.PrimRange(stage.GetDefaultPrim()) if is_asset(p) and asset_children(p)]
    rep = run_on_stage(stage, parents, root_layer, geom_layer, overrides)
    root_layer.Save()
    geom_layer.Save()
    return rep

"""Geometry queries on composed USD stages: bounds and point/mesh gathering
for explode, proxies, validators and the glTF converter."""
from __future__ import annotations

import numpy as np
from pxr import Gf, Usd, UsdGeom

from .usd_io import np_matrix

RENDER_PURPOSES = [UsdGeom.Tokens.default_, UsdGeom.Tokens.render]
TIME = Usd.TimeCode.Default()


def bbox_cache() -> UsdGeom.BBoxCache:
    return UsdGeom.BBoxCache(TIME, RENDER_PURPOSES, useExtentsHint=False)


def relative_box(cache: UsdGeom.BBoxCache, prim: Usd.Prim, ancestor: Usd.Prim):
    """Axis-aligned (lo, hi) of prim's render geometry in ancestor's frame,
    including prim's own transform. None if empty."""
    rng = cache.ComputeRelativeBound(prim, ancestor).ComputeAlignedRange()
    if rng.IsEmpty():
        return None
    return np.array(rng.GetMin()), np.array(rng.GetMax())


def local_box(cache: UsdGeom.BBoxCache, prim: Usd.Prim):
    """AABB of prim's subtree in prim's own frame (excluding its own transform)."""
    rng = cache.ComputeUntransformedBound(prim).ComputeAlignedRange()
    if rng.IsEmpty():
        return None
    return np.array(rng.GetMin()), np.array(rng.GetMax())


def relative_xform(xf_cache: UsdGeom.XformCache, prim: Usd.Prim, ancestor: Usd.Prim) -> np.ndarray:
    m, _ = xf_cache.ComputeRelativeTransform(prim, ancestor)
    return np_matrix(m)


def mesh_arrays(mesh: UsdGeom.Mesh):
    pts = np.array(mesh.GetPointsAttr().Get(), dtype=np.float64).reshape(-1, 3)
    idx = np.array(mesh.GetFaceVertexIndicesAttr().Get(), dtype=np.int64)
    counts = np.array(mesh.GetFaceVertexCountsAttr().Get(), dtype=np.int64)
    if len(counts) and not np.all(counts == 3):
        raise ValueError(f"{mesh.GetPath()}: only triangle meshes are supported")
    nrm = mesh.GetNormalsAttr().Get()
    nrm = np.array(nrm, dtype=np.float32).reshape(-1, 3) if nrm is not None and len(nrm) == len(pts) else None
    return pts, nrm, idx.reshape(-1, 3)


def instancer_matrices(pi: UsdGeom.PointInstancer) -> list[np.ndarray]:
    mats = pi.ComputeInstanceTransformsAtTime(TIME, TIME)
    return [np_matrix(m) for m in mats]


def _is_proxy(prim: Usd.Prim) -> bool:
    img = UsdGeom.Imageable(prim)
    return bool(img) and img.ComputePurpose() == UsdGeom.Tokens.proxy


def iter_render_meshes(prim: Usd.Prim, relative_to: Usd.Prim, xf_cache: UsdGeom.XformCache | None = None):
    """Yield (mesh, matrix_to_relative_frame) for every render mesh under prim,
    expanding PointInstancers."""
    xf_cache = xf_cache or UsdGeom.XformCache(TIME)
    it = iter(Usd.PrimRange(prim))
    for p in it:
        if p.GetName() == "Prototypes" or _is_proxy(p):
            it.PruneChildren() if hasattr(it, "PruneChildren") else None
            continue
        if p.IsA(UsdGeom.PointInstancer):
            pi = UsdGeom.PointInstancer(p)
            base = relative_xform(xf_cache, p, relative_to)
            protos = pi.GetPrototypesRel().GetTargets()
            idxs = pi.GetProtoIndicesAttr().Get()
            for m, k in zip(instancer_matrices(pi), idxs):
                proto = p.GetStage().GetPrimAtPath(protos[k])
                for q in Usd.PrimRange(proto):
                    if q.IsA(UsdGeom.Mesh):
                        pm = relative_xform(xf_cache, q, proto) if q != proto else np.eye(4)
                        yield UsdGeom.Mesh(q), base @ m @ pm
            continue
        if p.IsA(UsdGeom.Mesh):
            yield UsdGeom.Mesh(p), relative_xform(xf_cache, p, relative_to)


def transform_points(pts: np.ndarray, m: np.ndarray) -> np.ndarray:
    return pts @ m[:3, :3].T + m[:3, 3]


def subtree_points(prim: Usd.Prim, relative_to: Usd.Prim, max_per_mesh: int = 4000) -> np.ndarray:
    chunks = []
    xf = UsdGeom.XformCache(TIME)
    for mesh, m in iter_render_meshes(prim, relative_to, xf):
        pts, _, _ = mesh_arrays(mesh)
        if len(pts) > max_per_mesh:
            pts = pts[:: int(np.ceil(len(pts) / max_per_mesh))]
        chunks.append(transform_points(pts, m))
    return np.vstack(chunks) if chunks else np.zeros((0, 3))


def boxes_overlap(a, b, margin: float = 0.0) -> bool:
    return bool(np.all(a[0] < b[1] + margin) and np.all(b[0] < a[1] + margin))


def gf(v) -> Gf.Vec3d:
    return Gf.Vec3d(*[float(x) for x in v])

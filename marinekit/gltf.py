"""Stage 4: USD -> GLB chunks + manifest (custom converter, per the plan).

Per system, three chunks:
  L0  system and equipment nodes carrying only their L0 proxy meshes
  L1  equipment as single decimated meshes (one primitive per material), no fasteners
  L2  full component tree, proxies for every assembly, fasteners as one shared
      mesh placed per instance (one node per instance so each keeps its ID)

glTF conventions (the viewer depends on these):
  node.name            = marine:id of the asset (or instance)
  node.extras.marine   = all marine: attributes, plus name, path, kind, role
  "<id>:geom" child    = the asset's own geometry      (extras.marine.role = "geom")
  "<id>:proxy" child   = the asset's L0 proxy          (extras.marine.role = "proxy")
  coordinates          = ship frame, Z up, metres; the viewer converts to Y-up once at its root
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pygltflib as g
from pxr import Usd, UsdGeom
from scipy.spatial.transform import Rotation

from . import vocab
from .stagegeom import (TIME, instancer_matrices, iter_render_meshes, mesh_arrays, transform_points)
from .usd_io import SCHEMA_VERSION, get_marine, is_asset, np_matrix

FASTENER_CLASSES = {c for c, i in vocab.classes().items() if i.get("level") == "fastener"}


def srgb_to_linear(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


@dataclass
class GltfBuilder:
    nodes: list = field(default_factory=list)
    meshes: list = field(default_factory=list)
    materials: list = field(default_factory=list)
    accessors: list = field(default_factory=list)
    views: list = field(default_factory=list)
    blob: bytearray = field(default_factory=bytearray)
    mesh_index: dict = field(default_factory=dict)
    mat_index: dict = field(default_factory=dict)
    triangles: int = 0

    def _view(self, data: bytes, target: int | None) -> int:
        while len(self.blob) % 4:
            self.blob.append(0)
        off = len(self.blob)
        self.blob.extend(data)
        self.views.append(g.BufferView(buffer=0, byteOffset=off, byteLength=len(data), target=target))
        return len(self.views) - 1

    def _accessor(self, arr: np.ndarray, kind: str, target: int, minmax: bool = False) -> int:
        comp = {np.float32: g.FLOAT, np.uint16: g.UNSIGNED_SHORT, np.uint32: g.UNSIGNED_INT}[arr.dtype.type]
        v = self._view(arr.tobytes(), target)
        acc = g.Accessor(bufferView=v, componentType=comp, count=int(arr.shape[0]) if arr.ndim > 1 else int(arr.size),
                         type=kind)
        if minmax:
            acc.min = arr.min(0).astype(float).tolist()
            acc.max = arr.max(0).astype(float).tolist()
        self.accessors.append(acc)
        return len(self.accessors) - 1

    def material(self, name: str) -> int:
        if name not in self.mat_index:
            info = vocab.materials().get(name, vocab.materials()["proxy"])
            h = info["color"].lstrip("#")
            rgb = [srgb_to_linear(int(h[i:i + 2], 16) / 255.0) for i in (0, 2, 4)]
            self.materials.append(g.Material(
                name=name, doubleSided=False,
                pbrMetallicRoughness=g.PbrMetallicRoughness(
                    baseColorFactor=rgb + [1.0], metallicFactor=float(info.get("metalness", 0.0)),
                    roughnessFactor=float(info.get("roughness", 0.5))),
                extras={"marine": {"material": name}}))
            self.mat_index[name] = len(self.materials) - 1
        return self.mat_index[name]

    def mesh(self, prims: list[tuple[np.ndarray, np.ndarray | None, np.ndarray, str]], name: str) -> int:
        """prims: list of (points, normals, triangles, material). Deduplicated by content."""
        h = hashlib.sha1()
        for p, n, t, m in prims:
            h.update(np.ascontiguousarray(p, np.float32).tobytes())
            h.update(np.ascontiguousarray(t, np.uint32).tobytes())
            h.update(m.encode())
        key = h.hexdigest()
        if key in self.mesh_index:
            return self.mesh_index[key]
        gp = []
        for p, n, t, m in prims:
            pos = np.ascontiguousarray(p, dtype=np.float32)
            attrs = g.Attributes(POSITION=self._accessor(pos, g.VEC3, g.ARRAY_BUFFER, minmax=True))
            if n is not None:
                attrs.NORMAL = self._accessor(np.ascontiguousarray(n, dtype=np.float32), g.VEC3, g.ARRAY_BUFFER)
            idt = np.uint16 if len(pos) < 65535 else np.uint32
            ind = self._accessor(np.ascontiguousarray(t, dtype=idt).ravel(), g.SCALAR, g.ELEMENT_ARRAY_BUFFER)
            gp.append(g.Primitive(attributes=attrs, indices=ind, material=self.material(m)))
        self.meshes.append(g.Mesh(name=name, primitives=gp))
        self.mesh_index[key] = len(self.meshes) - 1
        return self.mesh_index[key]

    def node(self, name: str, matrix: np.ndarray | None = None, mesh: int | None = None,
             extras: dict | None = None, children: list[int] | None = None) -> int:
        n = g.Node(name=name)
        if matrix is not None and not np.allclose(matrix, np.eye(4)):
            t = matrix[:3, 3]
            q = Rotation.from_matrix(matrix[:3, :3]).as_quat()  # x, y, z, w
            if not np.allclose(t, 0):
                n.translation = [float(x) for x in t]
            if not np.allclose(q, [0, 0, 0, 1]):
                n.rotation = [float(x) for x in q]
        if mesh is not None:
            n.mesh = mesh
        if extras:
            n.extras = extras
        if children:
            n.children = children
        self.nodes.append(n)
        return len(self.nodes) - 1

    def count(self, mesh_idx: int, times: int = 1) -> None:
        for p in self.meshes[mesh_idx].primitives:
            self.triangles += self.accessors[p.indices].count // 3 * times

    def save(self, path: Path, root: int, asset_extras: dict) -> None:
        doc = g.GLTF2(
            asset=g.Asset(version="2.0", generator="marinekit", extras=asset_extras),
            scene=0, scenes=[g.Scene(nodes=[root])], nodes=self.nodes, meshes=self.meshes,
            materials=self.materials, accessors=self.accessors, bufferViews=self.views,
            buffers=[g.Buffer(byteLength=len(self.blob))])
        doc.set_binary_blob(bytes(self.blob))
        path.parent.mkdir(parents=True, exist_ok=True)
        doc.save_binary(str(path))


# ------------------------------------------------------------ USD helpers
def local_matrix(prim: Usd.Prim) -> np.ndarray:
    x = UsdGeom.Xformable(prim)
    return np_matrix(x.GetLocalTransformation(TIME)) if x else np.eye(4)


def asset_children(prim: Usd.Prim) -> list[Usd.Prim]:
    return [c for c in prim.GetChildren() if is_asset(c)]


def marine_extras(prim: Usd.Prim, root: Usd.Prim, id_by_path: dict[str, str], role: str = "asset") -> dict:
    m = get_marine(prim)
    for rel in ("connectedTo", "serves"):
        if rel in m:
            m[rel] = [id_by_path.get(t, t) for t in m[rel]]
    m.pop("instanceIds", None)
    m.pop("instanceNames", None)
    m["name"] = prim.GetName()
    m["path"] = str(prim.GetPath())
    m["kind"] = Usd.ModelAPI(prim).GetKind()
    m["role"] = role
    return {"marine": m}


def _geom_prims(mesh_prim: Usd.Prim, material: str):
    pts, nrm, tris = mesh_arrays(UsdGeom.Mesh(mesh_prim))
    return [(pts, nrm, tris, material)]


def _material_of(prim: Usd.Prim) -> str:
    return get_marine(prim).get("material", "proxy")


def _proxy_material(proxy: Usd.Prim) -> str:
    """Proxies use the dominant material colour written as displayColor; map back to a vocab token."""
    col = UsdGeom.Mesh(proxy).GetDisplayColorAttr().Get()
    if col:
        c = col[0]
        hexc = "#%02x%02x%02x" % tuple(int(round(v * 255)) for v in c)
        for k, v in vocab.materials().items():
            if v["color"].lower() == hexc:
                return k
    return "proxy"


# ------------------------------------------------------------ chunk writers
def build_l2(b: GltfBuilder, prim: Usd.Prim, root: Usd.Prim, ids: dict) -> int:
    m = get_marine(prim)
    kids = []
    geom = prim.GetChild("geom")
    if geom and geom.IsA(UsdGeom.Mesh):
        mi = b.mesh(_geom_prims(geom, _material_of(prim)), f"{m.get('class')}:{prim.GetName()}")
        b.count(mi)
        kids.append(b.node(f"{m['id']}:geom", local_matrix(geom), mi, {"marine": {"role": "geom", "of": m["id"]}}))
    proxy = prim.GetChild("proxy")
    if proxy and proxy.IsA(UsdGeom.Mesh):
        mi = b.mesh(_geom_prims(proxy, _proxy_material(proxy)), f"proxy:{prim.GetName()}")
        kids.append(b.node(f"{m['id']}:proxy", None, mi, {"marine": {"role": "proxy", "of": m["id"]}}))
    if prim.IsA(UsdGeom.PointInstancer):
        pi = UsdGeom.PointInstancer(prim)
        protos = pi.GetPrototypesRel().GetTargets()
        proto_meshes = []
        for pth in protos:
            pm = prim.GetStage().GetPrimAtPath(pth)
            proto_meshes.append(b.mesh(_geom_prims(pm, _material_of(prim)), f"{m.get('class')}:proto"))
        names, iids = m.get("instanceNames", []), m.get("instanceIds", [])
        for k, (mat, pidx) in enumerate(zip(instancer_matrices(pi), pi.GetProtoIndicesAttr().Get())):
            b.count(proto_meshes[pidx])
            kids.append(b.node(iids[k], mat, proto_meshes[pidx],
                               {"marine": {"role": "instance", "id": iids[k], "name": names[k],
                                           "instanceOf": m["id"], "class": m.get("class"),
                                           "material": m.get("material")}}))
    for c in asset_children(prim):
        kids.append(build_l2(b, c, root, ids))
    return b.node(m["id"], local_matrix(prim) if prim != root else None, None,
                  marine_extras(prim, root, ids), kids)


def build_l0(b: GltfBuilder, prim: Usd.Prim, root: Usd.Prim, ids: dict, depth: int = 0) -> int:
    m = get_marine(prim)
    kids = []
    proxy = prim.GetChild("proxy")
    if proxy and proxy.IsA(UsdGeom.Mesh):
        mi = b.mesh(_geom_prims(proxy, _proxy_material(proxy)), f"proxy:{prim.GetName()}")
        if depth == 1:
            b.count(mi)
        kids.append(b.node(f"{m['id']}:proxy", None, mi, {"marine": {"role": "proxy", "of": m["id"]}}))
    if depth == 0:
        for c in asset_children(prim):
            kids.append(build_l0(b, c, root, ids, depth + 1))
    return b.node(m["id"], local_matrix(prim) if prim != root else None, None, marine_extras(prim, root, ids), kids)


def merged_equipment_mesh(eq: Usd.Prim, ratio: float):
    """All non-fastener render geometry under eq, in eq's frame, grouped by material, decimated."""
    import fast_simplification
    by_mat: dict[str, list] = {}
    xf = UsdGeom.XformCache(TIME)

    def visit(p: Usd.Prim):
        if is_asset(p) and get_marine(p).get("class") in FASTENER_CLASSES:
            return
        geom = p.GetChild("geom")
        if is_asset(p) and geom and geom.IsA(UsdGeom.Mesh):
            for mesh, mtx in iter_render_meshes(geom, eq, xf):
                pts, _, tris = mesh_arrays(mesh)
                by_mat.setdefault(_material_of(p), []).append((transform_points(pts, mtx), tris))
        for c in p.GetChildren():
            if is_asset(c):
                visit(c)
    visit(eq)
    out = []
    for mat in sorted(by_mat):
        pts_all, tri_all, off = [], [], 0
        for pts, tris in by_mat[mat]:
            pts_all.append(pts)
            tri_all.append(tris + off)
            off += len(pts)
        P = np.vstack(pts_all).astype(np.float32)
        T = np.vstack(tri_all).astype(np.int64)
        # weld coincident vertices so the decimator sees connected surfaces
        key = np.round(P / 1e-5).astype(np.int64)
        _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
        P, T = P[first], inv.reshape(-1)[T]
        T = T[(T[:, 0] != T[:, 1]) & (T[:, 1] != T[:, 2]) & (T[:, 0] != T[:, 2])]
        if len(T) > 200:
            P, T = fast_simplification.simplify(P, T.astype(np.int32), target_reduction=1.0 - ratio)
        out.append((P.astype(np.float32), None, T.astype(np.int64), mat))
    return out


def build_l1(b: GltfBuilder, sysprim: Usd.Prim, ids: dict, ratio: float) -> int:
    kids = []
    for eq in asset_children(sysprim):
        m = get_marine(eq)
        ek = []
        prims = merged_equipment_mesh(eq, ratio)
        if prims:
            mi = b.mesh(prims, f"L1:{eq.GetName()}")
            b.count(mi)
            ek.append(b.node(f"{m['id']}:geom", None, mi, {"marine": {"role": "geom", "of": m["id"], "lod": 1}}))
        proxy = eq.GetChild("proxy")
        if proxy and proxy.IsA(UsdGeom.Mesh):
            pmi = b.mesh(_geom_prims(proxy, _proxy_material(proxy)), f"proxy:{eq.GetName()}")
            ek.append(b.node(f"{m['id']}:proxy", None, pmi, {"marine": {"role": "proxy", "of": m["id"]}}))
        kids.append(b.node(m["id"], local_matrix(eq), None, marine_extras(eq, sysprim, ids), ek))
    m = get_marine(sysprim)
    return b.node(m["id"], None, None, marine_extras(sysprim, sysprim, ids), kids)


def subtree_box(prim: Usd.Prim, relative_to: Usd.Prim):
    from .stagegeom import bbox_cache, relative_box
    return relative_box(bbox_cache(), prim, relative_to)


# ------------------------------------------------------------ driver
def tree_entry(prim: Usd.Prim, ids: dict) -> dict:
    m = get_marine(prim)
    mat = local_matrix(prim)
    e = {"id": m["id"], "name": prim.GetName(), "class": m.get("class"), "code": m.get("code", ""),
         "kind": Usd.ModelAPI(prim).GetKind(), "function": m.get("function", ""), "mass": m.get("mass"),
         "translation": mat[:3, 3].round(6).tolist(),
         "rotation": Rotation.from_matrix(mat[:3, :3]).as_quat().round(9).tolist(),
         "explodeDir": m.get("explodeDir", [0, 0, 0]), "explodeDist": m.get("explodeDist", 0.0),
         "children": []}
    return e


def convert_vessel(vessel_usda: Path, out_dir: Path) -> dict:
    stage = Usd.Stage.Open(str(vessel_usda), Usd.Stage.LoadAll)
    vprim = stage.GetDefaultPrim()
    ids = {str(p.GetPath()): get_marine(p)["id"] for p in Usd.PrimRange(vprim) if is_asset(p)}
    budgets = vocab.budgets()
    ratio = budgets["tessellation"]["L1"]["decimate_ratio"]
    raw = out_dir / "raw"
    chunks = []

    def walk(prim: Usd.Prim, level: int) -> dict:
        e = tree_entry(prim, ids)
        cls = get_marine(prim).get("class")
        if vocab.classes().get(cls, {}).get("level") == "system":
            e["system"] = True
            box = subtree_box(prim, vprim)
            for lod, fn in (("L0", lambda b: build_l0(b, prim, prim, ids)),
                            ("L1", lambda b: build_l1(b, prim, ids, ratio)),
                            ("L2", lambda b: build_l2(b, prim, prim, ids))):
                b = GltfBuilder()
                root = fn(b)
                p = raw / f"{prim.GetName().lower()}.{lod}.glb"
                b.save(p, root, {"marine": {"schemaVersion": SCHEMA_VERSION, "upAxis": "Z",
                                            "metersPerUnit": 1.0, "system": e["id"], "lod": lod}})
                chunks.append({"system": e["id"], "systemName": prim.GetName(), "lod": lod, "raw": str(p),
                               "triangles": b.triangles, "nodes": len(b.nodes), "meshes": len(b.meshes),
                               "bbox": [box[0].round(4).tolist(), box[1].round(4).tolist()] if box else None})
        else:
            for c in asset_children(prim):
                e["children"].append(walk(c, level + 1))
        return e

    tree = walk(vprim, 0)
    m = get_marine(vprim)
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "vessel": {"id": m["id"], "name": vprim.GetName(), "title": m.get("function", ""),
                   "assetVersion": Usd.ModelAPI(vprim).GetAssetInfo().get("version", "0.0.0")},
        "frame": {"upAxis": "Z", "metersPerUnit": 1.0, "x": "forward", "y": "port", "z": "up",
                  "origin": "aft perpendicular, centreline, baseline"},
        "budgets": budgets["lod"],
        "tree": tree,
        "chunks": chunks,
    }
    return manifest


def finalize(manifest: dict, out_dir: Path, optimized: dict[str, Path] | None = None) -> Path:
    """Content-hash chunk file names and write manifest.json."""
    for c in manifest["chunks"]:
        src = Path(optimized[c["raw"]]) if optimized and c["raw"] in optimized else Path(c["raw"])
        data = src.read_bytes()
        h = hashlib.sha256(data).hexdigest()
        name = f"{c['systemName'].lower()}.{c['lod']}.{h[:12]}.glb"
        (out_dir / name).write_bytes(data)
        c.update({"url": name, "bytes": len(data), "sha256": h, "optimized": bool(optimized)})
        c.pop("raw")
    path = out_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=1) + "\n")
    return path

"""Write a Part tree to a USD asset, and small helpers for reading marine:
attributes back.

Asset layout (one asset = two files):
    <name>.usda       structure: prims, kinds, transforms, marine: attributes,
                      relationships, instancer placements. Text, diffable.
    <name>.geom.usdc  geometry and looks (meshes, materials). Binary, sublayered
                      under the .usda, so attribute edits never touch it.

Stage conventions: metersPerUnit = 1, upAxis = Z, ship frame.
Asset prims are Xforms (or PointInstancers) carrying MarineAssetAPI; their
geometry is a child Mesh named "geom"; an L0 proxy is a child Mesh named
"proxy" with purpose = proxy.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
from pxr import Gf, Sdf, Tf, Usd, UsdGeom, UsdShade, Vt

from . import geom, vocab
from .model import INT_ATTRS, REAL_ATTRS, REL_ATTRS, STRING_ATTRS, VEC_ATTRS, Part

SCHEMA_VERSION = "0.1.0"
TOKEN_ATTRS = {"class", "material", "massSource", "geometryFidelity"}


def attr_type(name: str):
    if name in TOKEN_ATTRS:
        return Sdf.ValueTypeNames.Token
    if name in STRING_ATTRS:
        return Sdf.ValueTypeNames.String
    if name in REAL_ATTRS:
        return Sdf.ValueTypeNames.Double
    if name in INT_ATTRS:
        return Sdf.ValueTypeNames.Int
    if name in VEC_ATTRS:
        return Sdf.ValueTypeNames.Vector3d
    if name in ("instanceIds", "instanceNames"):
        return Sdf.ValueTypeNames.StringArray
    raise KeyError(name)


def set_marine(prim: Usd.Prim, name: str, value) -> None:
    attr = prim.CreateAttribute(f"marine:{name}", attr_type(name), custom=False)
    if name in VEC_ATTRS:
        value = Gf.Vec3d(*[float(v) for v in value])
    attr.Set(value)


def get_marine(prim: Usd.Prim) -> dict:
    out = {}
    for a in prim.GetAttributes():
        n = a.GetName()
        if n.startswith("marine:") and a.HasAuthoredValue():
            v = a.Get()
            if isinstance(v, Gf.Vec3d):
                v = [float(x) for x in v]
            elif isinstance(v, Vt.StringArray):
                v = list(v)
            out[n[7:]] = v
    for r in prim.GetRelationships():
        n = r.GetName()
        if n.startswith("marine:"):
            out[n[7:]] = [str(t) for t in r.GetTargets()]
    return out


def is_asset(prim: Usd.Prim) -> bool:
    return prim.HasAPI("MarineAssetAPI") if _schema_loaded() else \
        "MarineAssetAPI" in prim.GetPrimTypeInfo().GetAppliedAPISchemas()


def _schema_loaded() -> bool:
    return Usd.SchemaRegistry().FindAppliedAPIPrimDefinition("MarineAssetAPI") is not None


def gf_matrix(m: np.ndarray, unit_scale: float = 1.0) -> Gf.Matrix4d:
    m = np.array(m, dtype=float)
    m[:3, 3] *= unit_scale
    return Gf.Matrix4d(*m.T.ravel().tolist())  # USD uses row vectors


def np_matrix(g: Gf.Matrix4d) -> np.ndarray:
    return np.array(g, dtype=float).T


def hex_rgb(h: str) -> Gf.Vec3f:
    h = h.lstrip("#")
    return Gf.Vec3f(*(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4)))


def tess_params(cls: str) -> tuple[float, float]:
    t = vocab.budgets()["tessellation"]
    o = t.get("class_overrides", {}).get(cls, t["L2"])
    return o["relative_deflection"], o["angular_deflection"]


def write_mesh(stage, path: Sdf.Path, points, normals, tris, material: str | None = None,
               purpose: str | None = None) -> UsdGeom.Mesh:
    m = UsdGeom.Mesh.Define(stage, path)
    pts = np.ascontiguousarray(points, dtype=np.float32)
    m.CreatePointsAttr(Vt.Vec3fArray.FromNumpy(pts))
    m.CreateFaceVertexCountsAttr(Vt.IntArray.FromNumpy(np.full(len(tris), 3, dtype=np.int32)))
    m.CreateFaceVertexIndicesAttr(Vt.IntArray.FromNumpy(np.ascontiguousarray(tris, dtype=np.int32).ravel()))
    if normals is not None and len(normals):
        m.CreateNormalsAttr(Vt.Vec3fArray.FromNumpy(np.ascontiguousarray(normals, dtype=np.float32)))
        m.SetNormalsInterpolation(UsdGeom.Tokens.vertex)
    m.CreateSubdivisionSchemeAttr(UsdGeom.Tokens.none)
    if len(pts):
        m.CreateExtentAttr(Vt.Vec3fArray([Gf.Vec3f(*pts.min(0).tolist()), Gf.Vec3f(*pts.max(0).tolist())]))
    if material:
        m.CreateDisplayColorAttr(Vt.Vec3fArray([hex_rgb(vocab.materials()[material]["color"])]))
    if purpose:
        m.CreatePurposeAttr(purpose)
    return m


def ensure_material(stage, looks: Sdf.Path, material: str) -> UsdShade.Material:
    path = looks.AppendChild(Tf.MakeValidIdentifier(material))
    mat = UsdShade.Material.Get(stage, path)
    if mat:
        return mat
    info = vocab.materials()[material]
    mat = UsdShade.Material.Define(stage, path)
    sh = UsdShade.Shader.Define(stage, path.AppendChild("PreviewSurface"))
    sh.CreateIdAttr("UsdPreviewSurface")
    sh.CreateInput("diffuseColor", Sdf.ValueTypeNames.Color3f).Set(hex_rgb(info["color"]))
    sh.CreateInput("roughness", Sdf.ValueTypeNames.Float).Set(float(info.get("roughness", 0.5)))
    sh.CreateInput("metallic", Sdf.ValueTypeNames.Float).Set(float(info.get("metalness", 0.0)))
    mat.CreateSurfaceOutput().ConnectToSource(sh.ConnectableAPI(), "surface")
    return mat


def _remove(path: Path) -> None:
    layer = Sdf.Layer.Find(str(path))
    if layer is not None:
        layer.Clear()
    if path.exists():
        os.remove(path)


def write_asset(root: Part, path: Path, unit_scale: float = 1e-3) -> Path:
    """Write `root` (CAD units * unit_scale = metres) to path (.usda) + .geom.usdc."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    geom_path = path.with_name(path.stem + ".geom.usdc")
    _remove(path)
    _remove(geom_path)
    geom_layer = Sdf.Layer.CreateNew(str(geom_path))
    stage = Usd.Stage.CreateNew(str(path))
    stage.GetRootLayer().subLayerPaths.append(f"./{geom_path.name}")
    UsdGeom.SetStageMetersPerUnit(stage, 1.0)
    UsdGeom.SetStageUpAxis(stage, UsdGeom.Tokens.z)
    stage.GetRootLayer().customLayerData = {"marine:schemaVersion": SCHEMA_VERSION}
    mesh_cache: dict[tuple, tuple] = {}

    def tessellated(part: Part):
        key = (id(part.shape), part.cls)
        if key not in mesh_cache:
            rel, ang = tess_params(part.cls)
            pts, nrm, tris = geom.tessellate(part.shape, rel, ang)
            mesh_cache[key] = (pts * unit_scale, nrm, tris)
        return mesh_cache[key]

    root_path = Sdf.Path("/" + Tf.MakeValidIdentifier(root.name))
    looks = root_path.AppendChild("Looks")

    def write(part: Part, prim_path: Sdf.Path, is_root: bool) -> None:
        if part.instances:
            pi = UsdGeom.PointInstancer.Define(stage, prim_path)
            prim = pi.GetPrim()
            proto_path = prim_path.AppendChild("Prototypes").AppendChild("geom")
            with Usd.EditContext(stage, geom_layer):
                stage.DefinePrim(prim_path.AppendChild("Prototypes"), "Scope")
                pts, nrm, tris = tessellated(part)
                mesh = write_mesh(stage, proto_path, pts, nrm, tris, part.attrs.get("material"))
                UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(
                    ensure_material(stage, looks, part.attrs.get("material", "proxy")))
            pi.CreatePrototypesRel().SetTargets([proto_path])
            pos, ori = [], []
            for inst in part.instances:
                g = gf_matrix(inst.xform, unit_scale)
                pos.append(Gf.Vec3f(g.ExtractTranslation()))
                q = g.ExtractRotationQuat()
                ori.append(Gf.Quath(q.GetReal(), Gf.Vec3h(q.GetImaginary())))
            pi.CreateProtoIndicesAttr(Vt.IntArray([0] * len(part.instances)))
            pi.CreatePositionsAttr(Vt.Vec3fArray(pos))
            pi.CreateOrientationsAttr(Vt.QuathArray(ori))
            set_marine(prim, "instanceIds", Vt.StringArray([i.id for i in part.instances]))
            set_marine(prim, "instanceNames", Vt.StringArray([i.name for i in part.instances]))
        else:
            prim = UsdGeom.Xform.Define(stage, prim_path).GetPrim()
        if not is_root and not np.allclose(part.xform, np.eye(4)):
            UsdGeom.Xformable(prim).MakeMatrixXform().Set(gf_matrix(part.xform, unit_scale))
        Usd.ModelAPI(prim).SetKind(part.kind)
        prim.AddAppliedSchema("MarineAssetAPI")
        attrs = dict(part.attrs)
        attrs["class"] = part.cls
        attrs.setdefault("lodLevel", {"assembly": 0, "component": 1}.get(part.kind, 2))
        for k in STRING_ATTRS + REAL_ATTRS + INT_ATTRS + VEC_ATTRS:
            if attrs.get(k) is not None and attrs.get(k) != "":
                set_marine(prim, k, attrs[k])
        for rel, names in part.relations.items():
            r = prim.CreateRelationship(f"marine:{rel}", custom=False)
            r.SetTargets([prim_path.GetParentPath().AppendChild(n) for n in names])
        if part.shape is not None and not part.instances:
            with Usd.EditContext(stage, geom_layer):
                pts, nrm, tris = tessellated(part)
                mesh = write_mesh(stage, prim_path.AppendChild("geom"), pts, nrm, tris, part.attrs.get("material"))
                UsdShade.MaterialBindingAPI.Apply(mesh.GetPrim()).Bind(
                    ensure_material(stage, looks, part.attrs.get("material", "proxy")))
        for child in part.children:
            write(child, prim_path.AppendChild(Tf.MakeValidIdentifier(child.name)), False)

    write(root, root_path, True)
    rp = stage.GetPrimAtPath(root_path)
    stage.SetDefaultPrim(rp)
    info = {"name": root.name, "version": root.meta.get("version", "0.0.0")}
    Usd.ModelAPI(rp).SetAssetInfo(info)
    geom_layer.defaultPrim = root_path.name
    stage.GetRootLayer().Save()
    geom_layer.Save()
    return path


def open_stage(path: Path) -> Usd.Stage:
    return Usd.Stage.Open(str(path))


def iter_assets(stage: Usd.Stage, root: Sdf.Path | None = None):
    start = stage.GetPrimAtPath(root) if root else stage.GetDefaultPrim()
    for prim in Usd.PrimRange(start):
        if prim.GetName() in ("geom", "proxy", "Prototypes", "Looks"):
            continue
        if is_asset(prim):
            yield prim

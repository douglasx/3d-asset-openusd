"""OpenCascade tessellation and solid properties."""
from __future__ import annotations

import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepGProp import BRepGProp
from OCP.BRepLib import BRepLib_ToolTriangulatedShape
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.Bnd import Bnd_Box
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS


def as_topods(shape):
    """Accept build123d shapes or raw TopoDS_Shape."""
    return getattr(shape, "wrapped", shape)


def bbox(shape) -> tuple[np.ndarray, np.ndarray]:
    b = Bnd_Box()
    b.SetGap(0.0)
    BRepBndLib.AddOptimal_s(as_topods(shape), b, False, False)
    lo, hi = b.CornerMin(), b.CornerMax()
    return np.array([lo.X(), lo.Y(), lo.Z()]), np.array([hi.X(), hi.Y(), hi.Z()])


def volume(shape) -> float:
    props = GProp_GProps()
    BRepGProp.VolumeProperties_s(as_topods(shape), props)
    return abs(props.Mass())


def tessellate(shape, relative_deflection: float = 0.004, angular_deflection: float = 0.35):
    """Return (points[N,3], normals[N,3], triangles[M,3]) in the shape's units.

    Vertices are per face (not welded) so normals stay sharp at edges.
    Deterministic: single-threaded meshing with fixed parameters.
    """
    s = as_topods(shape)
    lo, hi = bbox(s)
    size = float(np.linalg.norm(hi - lo)) or 1.0
    lin = max(size * relative_deflection, 1e-3)
    BRepTools.Clean_s(s)  # drop cached triangulation: results must not depend on call history
    BRepMesh_IncrementalMesh(s, lin, False, angular_deflection, False)

    pts, nrm, tris = [], [], []
    offset = 0
    exp = TopExp_Explorer(s, TopAbs_FACE)
    while exp.More():
        face = TopoDS.Face(exp.Current())
        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, loc)
        exp.Next()
        if tri is None:
            continue
        if not tri.HasNormals():
            BRepLib_ToolTriangulatedShape.ComputeNormals_s(face, tri)
        trsf = loc.Transformation()
        reversed_ = face.Orientation() == TopAbs_REVERSED
        n = tri.NbNodes()
        for i in range(1, n + 1):
            p = tri.Node(i).Transformed(trsf)
            pts.append((p.X(), p.Y(), p.Z()))
            d = tri.Normal(i)
            d.Transform(trsf)
            v = (d.X(), d.Y(), d.Z())
            nrm.append((-v[0], -v[1], -v[2]) if reversed_ else v)
        for i in range(1, tri.NbTriangles() + 1):
            a, b, c = tri.Triangle(i).Get()
            if reversed_:
                b, c = c, b
            tris.append((offset + a - 1, offset + b - 1, offset + c - 1))
        offset += n
    return (np.asarray(pts, dtype=np.float64).reshape(-1, 3),
            np.asarray(nrm, dtype=np.float32).reshape(-1, 3),
            np.asarray(tris, dtype=np.int32).reshape(-1, 3))


def mesh_volume(points: np.ndarray, tris: np.ndarray) -> float:
    """Signed-volume sum; equals the enclosed volume for closed, outward meshes."""
    if len(tris) == 0:
        return 0.0
    a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
    return float(abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6.0))


def mesh_area(points: np.ndarray, tris: np.ndarray) -> float:
    if len(tris) == 0:
        return 0.0
    a, b, c = points[tris[:, 0]], points[tris[:, 1]], points[tris[:, 2]]
    return float(0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1).sum())

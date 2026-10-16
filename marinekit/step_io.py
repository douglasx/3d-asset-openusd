"""STEP AP242 writer (the "CAD simulator") and reader (the ingest adapter's
front half), via OpenCascade XCAF.

What a STEP file carries here, as a real CAD export would:
- product structure (assemblies, components, placements), names, colours;
- user-defined attributes (AP242 property definitions) on each product:
  marine:id, marine:class, marine:material, marine:mass, dimensions ...

Conventions in the STEP structure (readable by any CAD tool):
- An asset with its own geometry *and* children is an assembly containing a
  component "<name>__geom" whose product has marine:role = geometry.
- Repeated parts (fasteners, cylinder heads) are one product with
  marine:instanced = 1, placed once per instance; instance names are the
  component names. OpenCascade drops attributes on component instances,
  so per-instance IDs are matched by instance name through the ID lock.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
from OCP.gp import gp_Trsf
from OCP.Interface import Interface_Static
from OCP.Quantity import Quantity_Color, Quantity_TOC_sRGB
from OCP.STEPCAFControl import STEPCAFControl_Reader, STEPCAFControl_Writer
from OCP.STEPControl import STEPControl_AsIs
from OCP.TCollection import TCollection_ExtendedString as ES
from OCP.TDataStd import TDataStd_Name, TDataStd_NamedData
from OCP.TDF import TDF_ChildIterator, TDF_Label
from OCP.TDocStd import TDocStd_Document
from OCP.TopLoc import TopLoc_Location
from OCP.XCAFApp import XCAFApp_Application
from OCP.XCAFDoc import XCAFDoc_ColorSurf, XCAFDoc_DocumentTool, XCAFDoc_ShapeTool

from . import vocab
from .geom import as_topods
from .model import INT_ATTRS, REAL_ATTRS, REL_ATTRS, STRING_ATTRS, Instance, Part

FIXED_TIMESTAMP = "2026-01-01T00:00:00"
META_KEYS = ["version", "source", "params"]


# ---------------------------------------------------------------- helpers
import contextlib
import os
import sys


@contextlib.contextmanager
def quiet():
    """Silence OpenCascade's C-level transfer statistics on stdout."""
    sys.stdout.flush()
    fd = os.dup(1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    try:
        yield
    finally:
        os.dup2(fd, 1)
        os.close(fd)
        os.close(devnull)


def _init_step() -> None:
    """STEP statics are ignored until the controller is initialised; without
    this the first file written in a process silently falls back to AP214."""
    from OCP.STEPCAFControl import STEPCAFControl_Controller
    STEPCAFControl_Controller.Init_s()


def _new_doc():
    app = XCAFApp_Application.GetApplication_s()
    doc = TDocStd_Document(ES("XmlXCAF"))
    app.InitDocument(doc)
    return doc


def _trsf(m: np.ndarray) -> TopLoc_Location:
    t = gp_Trsf()
    t.SetValues(*[float(m[r, c]) for r in range(3) for c in range(4)])
    return TopLoc_Location(t)


def _matrix(loc: TopLoc_Location) -> np.ndarray:
    t = loc.Transformation()
    m = np.eye(4)
    for r in range(3):
        for c in range(4):
            m[r, c] = t.Value(r + 1, c + 1)
    return m


def _bake(shape):
    """Bake a shape's own placement into its geometry. XCAF otherwise turns a
    located shape into an extra reference level and attributes get lost."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    s = as_topods(shape)
    if s.Location().IsIdentity():
        return s
    trsf = s.Location().Transformation()
    return BRepBuilderAPI_Transform(s.Located(TopLoc_Location()), trsf, True).Shape()


def _set_name(label, name: str) -> None:
    TDataStd_Name.Set_s(label, ES(name))


def _get_name(label) -> str:
    n = TDataStd_Name()
    return n.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(), n) else ""


def _set_meta(label, part: Part, extra: dict | None = None) -> None:
    nd = TDataStd_NamedData.Set_s(label)
    attrs = dict(part.attrs)
    attrs["class"] = part.cls
    for k in STRING_ATTRS:
        if attrs.get(k):
            nd.SetString(ES(f"marine:{k}"), ES(str(attrs[k])))
    for k in REAL_ATTRS:
        if k in attrs and attrs[k] is not None:
            nd.SetReal(ES(f"marine:{k}"), float(attrs[k]))
    for k in INT_ATTRS:
        if k in attrs:
            nd.SetInteger(ES(f"marine:{k}"), int(attrs[k]))
    for k in REL_ATTRS:
        if part.relations.get(k):
            nd.SetString(ES(f"marine:{k}"), ES(";".join(part.relations[k])))
    nd.SetString(ES("marine:kind"), ES(part.kind))
    for k, v in part.meta.items():
        nd.SetString(ES(f"meta:{k}"), ES(str(v)))
    for k, v in (extra or {}).items():
        nd.SetString(ES(k), ES(str(v)))


def _get_meta(label) -> dict:
    nd = TDataStd_NamedData()
    out: dict = {}
    if not label.FindAttribute(TDataStd_NamedData.GetID_s(), nd):
        return out
    keys = ([f"marine:{k}" for k in STRING_ATTRS + REL_ATTRS]
            + ["marine:kind", "marine:role", "marine:instanced"])
    for k in keys:
        if nd.HasString(ES(k)):
            out[k] = nd.GetString(ES(k)).ToExtString()
    for k in META_KEYS:
        if nd.HasString(ES(f"meta:{k}")):
            out[f"meta:{k}"] = nd.GetString(ES(f"meta:{k}")).ToExtString()
    for k in REAL_ATTRS:
        if nd.HasReal(ES(f"marine:{k}")):
            out[f"marine:{k}"] = nd.GetReal(ES(f"marine:{k}"))
    for k in INT_ATTRS:
        if nd.HasInteger(ES(f"marine:{k}")):
            out[f"marine:{k}"] = nd.GetInteger(ES(f"marine:{k}"))
    return out


def _color(material: str) -> Quantity_Color:
    h = vocab.materials().get(material, vocab.materials()["proxy"])["color"].lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    return Quantity_Color(r, g, b, Quantity_TOC_sRGB)


# ---------------------------------------------------------------- writer
def write_step(root: Part, path: Path) -> Path:
    doc = _new_doc()
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    ct = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    geom_protos: dict[int, TDF_Label] = {}

    def geometry_proto(part: Part) -> TDF_Label:
        key = id(part.shape)
        if key not in geom_protos:
            lab = st.AddShape(_bake(part.shape), False)
            nm = part.name if not part.children else f"{part.name}__geom"
            _set_name(lab, nm)
            ct.SetColor(lab, _color(part.attrs.get("material", "proxy")), XCAFDoc_ColorSurf)
            geom_protos[key] = lab
        return geom_protos[key]

    def emit(part: Part) -> TDF_Label:
        """Return the prototype label for this part."""
        if part.instances:
            lab = st.AddShape(_bake(part.shape), False)
            _set_name(lab, part.name)
            _set_meta(lab, part, {"marine:instanced": "1"})
            ct.SetColor(lab, _color(part.attrs.get("material", "proxy")), XCAFDoc_ColorSurf)
            return lab
        if not part.children:
            lab = st.AddShape(_bake(part.shape), False)
            _set_name(lab, part.name)
            _set_meta(lab, part)
            ct.SetColor(lab, _color(part.attrs.get("material", "proxy")), XCAFDoc_ColorSurf)
            return lab
        asm = st.NewShape()
        _set_name(asm, part.name)
        _set_meta(asm, part)
        if part.shape is not None:
            g = geometry_proto(part)
            meta = TDataStd_NamedData.Set_s(g)
            meta.SetString(ES("marine:role"), ES("geometry"))
            comp = st.AddComponent(asm, g, TopLoc_Location())
            _set_name(comp, f"{part.name}__geom")
        for child in part.children:
            proto = emit(child)
            if child.instances:
                for inst in child.instances:
                    comp = st.AddComponent(asm, proto, _trsf(child.xform @ inst.xform))
                    _set_name(comp, inst.name)
            else:
                comp = st.AddComponent(asm, proto, _trsf(child.xform))
                _set_name(comp, child.name)
        return asm

    emit(root)
    st.UpdateAssemblies()
    _init_step()
    Interface_Static.SetCVal_s("write.step.schema", "AP242DIS")
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    Interface_Static.SetCVal_s("write.step.product.name", root.name)
    w = STEPCAFControl_Writer()
    w.SetNameMode(True)
    # OpenCascade emits colour styles in memory-address order, which breaks
    # byte-identical rebuilds; materials travel as marine:material anyway.
    # Set MARINE_STEP_COLORS=1 for colour-coded files to inspect in FreeCAD.
    w.SetColorMode(os.environ.get("MARINE_STEP_COLORS") == "1")
    w.SetMetadataMode(True)
    w.SetPropsMode(False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with quiet():
        w.Transfer(doc, STEPControl_AsIs)
        w.Write(str(path))
    # deterministic output: pin the header timestamp
    txt = path.read_text()
    txt = re.sub(r"(FILE_NAME\('[^']*',')[^']*(')", rf"\g<1>{FIXED_TIMESTAMP}\g<2>", txt, count=1)
    # occurrence ids come from a process-wide counter; renumber per file
    counter = iter(range(1, 1 << 30))
    txt = re.sub(r"NEXT_ASSEMBLY_USAGE_OCCURRENCE\('\d+'",
                 lambda m: f"NEXT_ASSEMBLY_USAGE_OCCURRENCE('{next(counter)}'", txt)
    path.write_text(txt)
    return path


# ---------------------------------------------------------------- reader
def read_step(path: Path) -> Part:
    """Read a STEP file into a Part tree (CAD units: millimetres).

    Knows nothing about how the file was produced: attributes come only from
    what is in the file; missing ones are left for the ingest adapter.
    """
    _init_step()
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
    r = STEPCAFControl_Reader()
    r.SetNameMode(True)
    r.SetColorMode(True)
    r.SetMetaMode(True) if hasattr(r, "SetMetaMode") else r.SetMetadataMode(True)
    with quiet():
        status = r.ReadFile(str(path))
    if int(status) != 1:  # IFSelect_RetDone
        raise IOError(f"cannot read STEP file {path} (status {status})")
    doc = _new_doc()
    with quiet():
        r.Transfer(doc)
    st = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    free = []
    it = TDF_ChildIterator(st.Label())
    while it.More():
        lab = it.Value()
        if XCAFDoc_ShapeTool.IsFree_s(lab):
            free.append(lab)
        it.Next()
    if len(free) != 1:
        raise ValueError(f"{path}: expected one root product, found {len(free)}")

    def to_part(proto: TDF_Label, name: str, xform: np.ndarray) -> Part:
        meta = _get_meta(proto)
        part = Part(name=name, cls=meta.get("marine:class", ""), kind=meta.get("marine:kind", "subcomponent"),
                    xform=xform)
        part.attrs = {k.split(":", 1)[1]: v for k, v in meta.items()
                      if k.startswith("marine:") and k[7:] not in REL_ATTRS + ["kind", "role", "instanced"]}
        part.attrs.pop("class", None)
        part.meta = {k[5:]: v for k, v in meta.items() if k.startswith("meta:")}
        for k in REL_ATTRS:
            if meta.get(f"marine:{k}"):
                part.relations[k] = meta[f"marine:{k}"].split(";")
        if not XCAFDoc_ShapeTool.IsAssembly_s(proto):
            part.shape = XCAFDoc_ShapeTool.GetShape_s(proto)
            return part
        groups: dict[str, Part] = {}
        cit = TDF_ChildIterator(proto)
        while cit.More():
            comp = cit.Value()
            cit.Next()
            if not XCAFDoc_ShapeTool.IsComponent_s(comp):
                continue
            ref = TDF_Label()
            XCAFDoc_ShapeTool.GetReferredShape_s(comp, ref)
            loc = XCAFDoc_ShapeTool.GetLocation_s(comp)
            cmeta = _get_meta(ref)
            cname = _get_name(comp) or _get_name(ref)
            if cmeta.get("marine:role") == "geometry":
                part.shape = XCAFDoc_ShapeTool.GetShape_s(ref).Moved(loc)
            elif cmeta.get("marine:instanced") == "1":
                key = ref.EntryDumpToString() if hasattr(ref, "EntryDumpToString") else _get_name(ref)
                if key not in groups:
                    grp = to_part(ref, _get_name(ref), np.eye(4))
                    groups[key] = grp
                    part.children.append(grp)
                groups[key].instances.append(Instance(cname, _matrix(loc)))
            else:
                part.children.append(to_part(ref, cname, _matrix(loc)))
        return part

    root = free[0]
    return to_part(root, _get_name(root), np.eye(4))

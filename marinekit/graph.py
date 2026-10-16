"""Stage 5: USD -> RDF graph, and SHACL validation.

Every asset prim becomes mv:<uuid> with its class, data properties and
relations (partOf, connectedTo, serves, madeOf, definedBy). Instances of
repeated parts (bolts, cylinder heads) become their own nodes with
mo:instanceOf. The same UUIDs are glTF node names and API keys, so a
SPARQL result can be highlighted in the viewer directly.

Upserts are idempotent: the graph for a vessel is rebuilt from USD on every
build and replaces the previous named graph in the store.
"""
from __future__ import annotations

from pathlib import Path

from pxr import Usd
from rdflib import RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef

from . import vocab
from .paths import ONTOLOGY
from .usd_io import get_marine, is_asset

MO = Namespace("https://example.org/marine/ontology#")
MV = Namespace("urn:uuid:")
MAT = Namespace("https://example.org/marine/material#")


def node(id_: str) -> URIRef:
    return MV[id_]


def build_graph(vessel_usda: Path) -> Graph:
    stage = Usd.Stage.Open(str(vessel_usda), Usd.Stage.LoadAll)
    g = Graph()
    g.bind("mo", MO)
    g.bind("mat", MAT)
    for k, m in vocab.materials().items():
        g.add((MAT[k], RDF.type, MO.Material))
        g.add((MAT[k], RDFS.label, Literal(m["name"])))
        g.add((MAT[k], MO.density, Literal(float(m["density"]), datatype=XSD.double)))
    id_by_path = {}
    prims = [p for p in Usd.PrimRange(stage.GetDefaultPrim()) if is_asset(p)]
    for p in prims:
        id_by_path[str(p.GetPath())] = get_marine(p)["id"]
    for p in prims:
        m = get_marine(p)
        s = node(m["id"])
        g.add((s, RDF.type, MO[m.get("class", "Asset")]))
        g.add((s, MO.id, Literal(m["id"])))
        g.add((s, MO.name, Literal(p.GetName())))
        g.add((s, MO.usdPath, Literal(str(p.GetPath()))))
        for k in ("code", "function"):
            if m.get(k):
                g.add((s, MO[k], Literal(m[k])))
        for k in ("mass", "diameter", "length", "thickness"):
            if m.get(k) is not None:
                g.add((s, MO[k], Literal(float(m[k]), datatype=XSD.double)))
        mat = m.get("material")
        if mat and mat != "proxy":
            g.add((s, MO.madeOf, MAT[mat]))
        if m.get("definitionId"):
            g.add((s, MO.definedBy, node(m["definitionId"])))
        parent = p.GetParent()
        while parent and parent.IsValid() and str(parent.GetPath()) not in id_by_path and not parent.IsPseudoRoot():
            parent = parent.GetParent()
        if parent and str(parent.GetPath()) in id_by_path:
            g.add((s, MO.partOf, node(id_by_path[str(parent.GetPath())])))
        for rel in ("connectedTo", "serves"):
            for t in m.get(rel, []):
                if t in id_by_path:
                    g.add((s, MO[rel], node(id_by_path[t])))
                    if rel == "connectedTo":
                        g.add((node(id_by_path[t]), MO.connectedTo, s))
        for iid, nm in zip(m.get("instanceIds", []), m.get("instanceNames", [])):
            i = node(iid)
            g.add((i, RDF.type, MO.PartInstance))
            g.add((i, MO.instanceOf, s))
            g.add((i, MO.partOf, s))
            g.add((i, MO.id, Literal(iid)))
            g.add((i, MO.name, Literal(nm)))
    return g


def validate(g: Graph) -> tuple[bool, str, list[dict]]:
    from pyshacl import validate as shacl
    ont = Graph().parse(ONTOLOGY / "marine.ttl")
    shapes = Graph().parse(ONTOLOGY / "shapes.ttl")
    conforms, results_graph, text = shacl(g, shacl_graph=shapes, ont_graph=ont, inference="rdfs",
                                          abort_on_first=False, allow_warnings=True)
    SH = Namespace("http://www.w3.org/ns/shacl#")
    issues = []
    for r in results_graph.subjects(RDF.type, SH.ValidationResult):
        sev = results_graph.value(r, SH.resultSeverity)
        issues.append({
            "focus": str(results_graph.value(r, SH.focusNode)),
            "path": str(results_graph.value(r, SH.resultPath)),
            "message": str(results_graph.value(r, SH.resultMessage)),
            "severity": str(sev).rsplit("#", 1)[-1] if sev else "Violation",
        })
    hard = [i for i in issues if i["severity"] == "Violation"]
    return len(hard) == 0, text, issues


def write(g: Graph, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    g.serialize(destination=str(path), format="turtle")
    return path


QUERY_PREFIX = """
PREFIX mo: <https://example.org/marine/ontology#>
PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
"""

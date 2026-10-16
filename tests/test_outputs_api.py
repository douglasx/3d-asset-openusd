"""Gate for the spike: IDs match across USD, GLB, graph and API; explode data is consistent."""
import json
import sqlite3

import pygltflib
from fastapi.testclient import TestClient
from pxr import Usd
from rdflib import Graph

from marinekit.paths import ASSETS
from marinekit.usd_io import get_marine, is_asset


def _usd_ids():
    st = Usd.Stage.Open(str(ASSETS / "vessels" / "demo_osv" / "demo_osv.usda"), Usd.Stage.LoadAll)
    ids = set()
    for p in Usd.PrimRange(st.GetDefaultPrim()):
        if is_asset(p):
            m = get_marine(p)
            ids.add(m["id"])
            ids.update(m.get("instanceIds", []))
    return ids


def test_ids_match_across_usd_glb_graph_registry(built):
    usd = _usd_ids()
    man = json.loads((built / "vessels" / "demo_osv" / "manifest.json").read_text())
    l2 = next(c for c in man["chunks"] if c["lod"] == "L2")
    doc = pygltflib.GLTF2().load(str(built / "vessels" / "demo_osv" / l2["url"]))
    glb = {n.name for n in doc.nodes if (n.extras or {}).get("marine", {}).get("role") in ("asset", "instance")}
    # GLB carries the system subtree; vessel and zone live in the manifest tree
    tree_ids = set()
    stack = [man["tree"]]
    while stack:
        e = stack.pop()
        tree_ids.add(e["id"])
        stack.extend(e["children"])
    assert glb | tree_ids == usd
    g = Graph().parse(built / "vessels" / "demo_osv" / "graph.ttl")
    graph_ids = {str(o) for o in g.objects(None, g.namespace_manager.expand_curie("mo:id"))}
    assert graph_ids == usd
    con = sqlite3.connect(built / "registry.sqlite")
    reg = {r[0] for r in con.execute("SELECT id FROM assets WHERE scope='occurrence'")} | \
          {r[0] for r in con.execute("SELECT id FROM instances")}
    assert reg == usd


def test_lod_chunks_within_budget_and_content_hashed(built):
    man = json.loads((built / "vessels" / "demo_osv" / "manifest.json").read_text())
    assert {c["lod"] for c in man["chunks"]} == {"L0", "L1", "L2"}
    for c in man["chunks"]:
        assert c["sha256"][:12] in c["url"]
        assert c["triangles"] <= man["budgets"][c["lod"]]["max"] or c["lod"] == "L0"


def test_api_joins_by_id(built):
    from api.app import app
    c = TestClient(app)
    vessel = c.get("/vessels").json()[0]
    man = c.get(vessel["manifest"]).json()
    sys_id = man["chunks"][0]["system"]
    kids = c.get(f"/assets/{sys_id}/children").json()
    assert {k["name"] for k in kids} == {"MainEngine", "Gearbox", "ShaftLine", "Propeller"}
    blade = c.get("/assets", params={"class": "PropellerBlade"}).json()[0]
    a = c.get(f"/assets/{blade['id']}").json()
    assert a["attrs"]["material"] == "bronze_nab_cu3" and a["definitionId"]
    st = c.get(f"/assets/{blade['id']}/status").json()
    assert st["id"] == blade["id"] and st["channels"]
    con = sqlite3.connect(built / "registry.sqlite")
    bolt = con.execute("SELECT id FROM instances LIMIT 1").fetchone()[0]
    assert c.get(f"/assets/{bolt}").json()["instanceOf"]
    rows = c.post("/sparql", json={"query": "SELECT ?id WHERE { ?x a mo:Bearing ; mo:id ?id }"}).json()["rows"]
    assert len(rows) == 3
    assert c.get("/assets/does-not-exist").status_code == 404

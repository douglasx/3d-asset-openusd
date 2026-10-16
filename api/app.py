"""Backend API: static properties, relationships and live status, joined by marine:id.

    uvicorn api.app:app --reload          (from the repo root, after a build)

Reads the registry (build/out/registry.sqlite), the vessel manifests and the
RDF graph produced by the build. Live data is simulated here
(see `simulate_status`); in production that function is replaced by a call
to the condition-monitoring / CMMS source, keyed by the same ID.
OpenAPI docs at /docs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(os.environ.get("MARINE_ROOT", Path(__file__).resolve().parent.parent))
OUT = ROOT / "build" / "out"
DB = Path(os.environ.get("MARINE_REGISTRY", OUT / "registry.sqlite"))
VIEWER_DIST = ROOT / "viewer" / "dist"

app = FastAPI(title="Marine asset API", version="0.1.0",
              description="Static properties, relationships and live status of marine assets, keyed by marine:id.")
app.add_middleware(CORSMiddleware, allow_origins=os.environ.get("MARINE_CORS", "*").split(","),
                   allow_methods=["GET", "POST"], allow_headers=["*"])


# ------------------------------------------------------------------ models
class Asset(BaseModel):
    id: str
    name: str
    scope: str = Field(description="definition (library) or occurrence (vessel)")
    vessel: str | None
    path: str = Field(description="dotted hierarchy path")
    usdPath: str
    usdFile: str
    parentId: str | None
    systemId: str | None
    cls: str = Field(alias="class")
    kind: str | None
    code: str | None
    definitionId: str | None
    status: str
    version: str | None
    attrs: dict[str, Any]
    instanceOf: str | None = None
    instanceName: str | None = None

    model_config = {"populate_by_name": True}


class Child(BaseModel):
    id: str
    name: str
    cls: str = Field(alias="class")
    explodeDir: list[float]
    explodeDist: float
    hasChildren: bool
    model_config = {"populate_by_name": True}


class Channel(BaseModel):
    name: str
    iso19848: str = Field(description="ISO 19848-style data channel id (illustrative, not verified)")
    value: float
    unit: str


class Status(BaseModel):
    id: str
    timestamp: float
    condition: str = Field(description="good | fair | poor")
    channels: list[Channel]
    workOrders: list[dict[str, Any]]
    lastInspection: str
    simulated: bool = True


class Job(BaseModel):
    id: str
    state: str
    filename: str
    report: dict[str, Any] | None = None
    error: str | None = None


# ------------------------------------------------------------------ data access
def db() -> sqlite3.Connection:
    if not DB.exists():
        raise HTTPException(503, "registry not built; run `python -m marinekit.build`")
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def row_to_asset(r: sqlite3.Row, **extra) -> Asset:
    return Asset(id=r["id"], name=r["name"], scope=r["scope"], vessel=r["vessel"], path=r["path"],
                 usdPath=r["usd_path"], usdFile=r["usd_file"], parentId=r["parent_id"], systemId=r["system_id"],
                 **{"class": r["class"]}, kind=r["kind"], code=r["code"], definitionId=r["definition_id"],
                 status=r["status"], version=r["version"], attrs=json.loads(r["attrs"]), **extra)


def get_asset_row(con, id_: str):
    return con.execute("SELECT * FROM assets WHERE id = ?", (id_,)).fetchone()


def vessel_dirs() -> dict[str, Path]:
    out = {}
    for d in sorted((OUT / "vessels").glob("*")):
        m = d / "manifest.json"
        if m.exists():
            out[json.loads(m.read_text())["vessel"]["id"]] = d
    return out


# ------------------------------------------------------------------ live data (simulated)
CHANNELS = {
    "MainEngine": [("speed", "rpm", 720, 6), ("load", "%", 78, 8), ("exhaustTemp", "degC", 385, 12)],
    "Gearbox": [("oilTemp", "degC", 58, 3), ("outputSpeed", "rpm", 172, 2)],
    "ShaftLine": [("torque", "kNm", 205, 15), ("speed", "rpm", 172, 2)],
    "Shaft": [("torque", "kNm", 205, 15)],
    "Bearing": [("temperature", "degC", 46, 4)],
    "SternTube": [("sealOilLevel", "%", 92, 3)],
    "Propeller": [("pitch", "%", 85, 5), ("thrust", "kN", 410, 25)],
    "PropellerBlade": [("vibration", "mm/s", 2.1, 0.6)],
    "Turbocharger": [("speed", "rpm", 28500, 900)],
    "CylinderHead": [("exhaustTemp", "degC", 390, 15)],
    "BoltSet": [("preloadIndex", "%", 97, 2)],
}


def _seed(id_: str) -> int:
    return int(hashlib.sha256(id_.encode()).hexdigest()[:8], 16)


def simulate_status(id_: str, cls: str, code: str) -> Status:
    s = _seed(id_)
    t = time.time()
    chans = []
    for name, unit, base, amp in CHANNELS.get(cls, []):
        phase = (s % 1000) / 1000 * 2 * math.pi
        v = base + amp * math.sin(t / 30 + phase) + amp * 0.2 * math.sin(t / 3.7 + 2 * phase)
        chans.append(Channel(name=name, unit=unit, value=round(v, 2),
                             iso19848=f"/dnv-v2/vis-3-4a/{code or 'unknown'}/meta/qty-{name.lower()}"))
    condition = ["good", "good", "good", "fair", "good", "poor", "good", "fair"][s % 8]
    wos = []
    if condition != "good":
        wos.append({"id": f"WO-{s % 90000 + 10000}", "title": f"Inspect {cls}", "priority":
                    "high" if condition == "poor" else "normal", "due": str(date.today() + timedelta(days=s % 30))})
    return Status(id=id_, timestamp=t, condition=condition, channels=chans, workOrders=wos,
                  lastInspection=str(date.today() - timedelta(days=30 + s % 400)))


# ------------------------------------------------------------------ routes
@app.get("/health")
def health() -> dict:
    return {"ok": True, "registry": DB.exists()}


@app.get("/vessels")
def vessels() -> list[dict]:
    out = []
    for vid, d in vessel_dirs().items():
        m = json.loads((d / "manifest.json").read_text())
        out.append({"id": vid, "name": m["vessel"]["name"], "manifest": f"/vessels/{vid}/manifest"})
    return out


@app.get("/vessels/{vessel_id}/manifest")
def manifest(vessel_id: str) -> dict:
    d = vessel_dirs().get(vessel_id)
    if not d:
        raise HTTPException(404, "unknown vessel")
    m = json.loads((d / "manifest.json").read_text())
    for c in m["chunks"]:
        c["href"] = f"/files/{d.name}/{c['url']}"
    return m


@app.get("/assets", response_model=list[Asset])
def find_assets(cls: str | None = Query(None, alias="class"), system: str | None = None,
                material: str | None = None, vessel: str | None = None, scope: str = "occurrence",
                limit: int = 500) -> list[Asset]:
    q, args = "SELECT * FROM assets WHERE scope = ?", [scope]
    if cls:
        q += " AND class = ?"
        args.append(cls)
    if system:
        q += " AND system_id = ?"
        args.append(system)
    if vessel:
        q += " AND vessel = ?"
        args.append(vessel)
    if material:
        q += " AND json_extract(attrs, '$.material') = ?"
        args.append(material)
    q += " ORDER BY path LIMIT ?"
    args.append(limit)
    with db() as con:
        return [row_to_asset(r) for r in con.execute(q, args)]


@app.get("/assets/{asset_id}", response_model=Asset)
def get_asset(asset_id: str) -> Asset:
    with db() as con:
        r = get_asset_row(con, asset_id)
        if r:
            return row_to_asset(r)
        i = con.execute("SELECT * FROM instances WHERE id = ?", (asset_id,)).fetchone()
        if i:
            s = get_asset_row(con, i["set_id"])
            a = row_to_asset(s, instanceOf=s["id"], instanceName=i["name"])
            a.id, a.name = i["id"], i["name"]
            a.path, a.usdPath = f"{s['path']}.{i['name']}", f"{s['usd_path']}#{i['name']}"
            return a
    raise HTTPException(404, "unknown asset id")


@app.get("/assets/{asset_id}/children", response_model=list[Child])
def children(asset_id: str) -> list[Child]:
    with db() as con:
        if not get_asset_row(con, asset_id):
            raise HTTPException(404, "unknown asset id")
        rows = con.execute("SELECT * FROM assets WHERE parent_id = ? ORDER BY path", (asset_id,)).fetchall()
        out = []
        for r in rows:
            a = json.loads(r["attrs"])
            n = con.execute("SELECT COUNT(*) FROM assets WHERE parent_id = ?", (r["id"],)).fetchone()[0]
            out.append(Child(id=r["id"], name=r["name"], **{"class": r["class"]},
                             explodeDir=a.get("explodeDir", [0, 0, 0]), explodeDist=a.get("explodeDist", 0.0),
                             hasChildren=n > 0))
        return out


@app.get("/assets/{asset_id}/status", response_model=Status)
def status(asset_id: str) -> Status:
    a = get_asset(asset_id)
    return simulate_status(a.id, a.cls, a.code or "")


class SparqlIn(BaseModel):
    query: str = Field(examples=["SELECT ?id WHERE { ?x a mo:PropellerBlade ; mo:id ?id }"])
    vessel: str | None = None


@app.post("/sparql")
def sparql(body: SparqlIn) -> dict:
    """Read-only SPARQL over a vessel graph. The `mo:` prefix is predeclared."""
    from rdflib import Graph
    q = body.query.strip()
    if not q.upper().lstrip().startswith(("SELECT", "PREFIX", "ASK")):
        raise HTTPException(400, "only SELECT / ASK queries")
    dirs = vessel_dirs()
    d = dirs.get(body.vessel) if body.vessel else next(iter(dirs.values()), None)
    if not d:
        raise HTTPException(404, "no vessel graph")
    g = _graph(d / "graph.ttl")
    prefix = "PREFIX mo: <https://example.org/marine/ontology#>\n"
    res = g.query(prefix + q)
    if res.type == "ASK":
        return {"boolean": bool(res.askAnswer)}
    return {"vars": [str(v) for v in res.vars],
            "rows": [{str(k): str(v) for k, v in zip(res.vars, row)} for row in res]}


_graph_cache: dict[Path, tuple[float, Any]] = {}


def _graph(path: Path):
    from rdflib import Graph
    mt = path.stat().st_mtime
    if path not in _graph_cache or _graph_cache[path][0] != mt:
        _graph_cache[path] = (mt, Graph().parse(path))
    return _graph_cache[path][1]


# ------------------------------------------------------------------ ingest jobs
JOBS: dict[str, Job] = {}
_lock = threading.Lock()


def _run_ingest(job_id: str, step_path: Path) -> None:
    os.environ.setdefault("PXR_PLUGINPATH_NAME", str(ROOT / "schema" / "marine"))
    from marinekit.ids import IdLock
    from marinekit.ingest import ingest_step
    try:
        JOBS[job_id].state = "running"
        staging = OUT / "staging" / job_id
        staging.mkdir(parents=True, exist_ok=True)
        # ingest into staging against a copy of the lock: a preview that never
        # changes the library; promoting it is a reviewed commit.
        lock = IdLock(ROOT / "assets" / "ids.lock.json")
        lock.path = staging / "ids.lock.json"
        report = ingest_step(step_path, lock, staging / (step_path.stem + ".usda"))
        lock.save()
        JOBS[job_id].report = report
        JOBS[job_id].state = "done"
    except Exception as e:  # pragma: no cover - surfaced through the job
        JOBS[job_id].state = "failed"
        JOBS[job_id].error = str(e)


@app.post("/ingest", response_model=Job, status_code=202)
async def ingest(background: BackgroundTasks, file: UploadFile = File(...)) -> Job:
    if not file.filename.lower().endswith((".step", ".stp")):
        raise HTTPException(400, "STEP files only (OCX adapter is phase 3)")
    job_id = uuid.uuid4().hex[:12]
    tmp = Path(tempfile.mkdtemp()) / Path(file.filename).name
    with tmp.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    JOBS[job_id] = Job(id=job_id, state="queued", filename=file.filename)
    background.add_task(_run_ingest, job_id, tmp)
    return JOBS[job_id]


@app.get("/jobs/{job_id}", response_model=Job)
def job(job_id: str) -> Job:
    if job_id not in JOBS:
        raise HTTPException(404, "unknown job")
    return JOBS[job_id]


# ------------------------------------------------------------------ static files
if (OUT / "vessels").exists():
    app.mount("/files", StaticFiles(directory=OUT / "vessels"), name="files")
if VIEWER_DIST.exists():
    app.mount("/viewer", StaticFiles(directory=VIEWER_DIST, html=True), name="viewer")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/viewer/" if VIEWER_DIST.exists() else "/docs")

"""Asset registry. SQLite for the spike (the plan's stated alternative); the
schema is plain SQL that ports to PostgreSQL, where `path` becomes an ltree
column and `attrs` JSONB.

Tables
  assets     one row per asset occurrence or definition, keyed by marine:id
  instances  one row per instance of a repeated part
  artifacts  every build output with content hash
  builds     toolchain versions and parameter sets per build
"""
from __future__ import annotations

import hashlib
import json
import platform
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pxr import Usd

from .paths import LIBRARY, OUT, REGISTRY_DB, ROOT
from .usd_io import get_marine, is_asset

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
  id            TEXT PRIMARY KEY,
  scope         TEXT NOT NULL,          -- 'definition' (library) or 'occurrence' (vessel)
  vessel        TEXT,
  name          TEXT NOT NULL,
  path          TEXT NOT NULL,          -- dotted hierarchy path (ltree in Postgres)
  usd_path      TEXT NOT NULL,
  usd_file      TEXT NOT NULL,
  parent_id     TEXT,
  system_id     TEXT,
  class         TEXT NOT NULL,
  kind          TEXT,
  code          TEXT,
  definition_id TEXT,
  status        TEXT NOT NULL DEFAULT 'draft',   -- draft | validated | released | deprecated
  version       TEXT,
  attrs         TEXT NOT NULL           -- JSON (JSONB in Postgres)
);
CREATE INDEX IF NOT EXISTS assets_path ON assets(path);
CREATE INDEX IF NOT EXISTS assets_class ON assets(class);
CREATE INDEX IF NOT EXISTS assets_parent ON assets(parent_id);
CREATE TABLE IF NOT EXISTS instances (
  id        TEXT PRIMARY KEY,
  set_id    TEXT NOT NULL REFERENCES assets(id),
  name      TEXT NOT NULL,
  idx       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  path      TEXT PRIMARY KEY,
  sha256    TEXT NOT NULL,
  bytes     INTEGER NOT NULL,
  kind      TEXT NOT NULL,
  system_id TEXT,
  lod       TEXT,
  triangles INTEGER
);
CREATE TABLE IF NOT EXISTS builds (
  build_id   TEXT PRIMARY KEY,
  created    TEXT NOT NULL,
  toolchain  TEXT NOT NULL,
  params     TEXT NOT NULL,
  validation TEXT
);
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def toolchain() -> dict:
    import build123d, pxr, pygltflib, rdflib, pyshacl
    try:
        import OCP
        ocp = getattr(OCP, "__version__", "unknown")
    except Exception:  # pragma: no cover
        ocp = "unknown"
    return {"python": platform.python_version(), "openusd": ".".join(map(str, pxr.Usd.GetVersion())),
            "build123d": build123d.__version__, "ocp": ocp, "pygltflib": pygltflib.__version__ if hasattr(pygltflib, "__version__") else "?",
            "rdflib": rdflib.__version__, "pyshacl": pyshacl.__version__}


def connect(path: Path = REGISTRY_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _rows(stage: Usd.Stage, scope: str, vessel: str | None, usd_file: str, status: str):
    root = stage.GetDefaultPrim()
    ids = {}
    system_of = {}
    for p in Usd.PrimRange(root):
        if not is_asset(p):
            continue
        m = get_marine(p)
        ids[str(p.GetPath())] = m["id"]
        par = p.GetParent()
        while par and not par.IsPseudoRoot() and str(par.GetPath()) not in ids:
            par = par.GetParent()
        parent_id = ids.get(str(par.GetPath())) if par else None
        if m.get("class", "").endswith("System") or m.get("class") == "System":
            system_of[m["id"]] = m["id"]
        sys_id = system_of.get(parent_id) if parent_id else None
        if sys_id:
            system_of[m["id"]] = sys_id
        attrs = {k: v for k, v in m.items() if k not in ("instanceIds", "instanceNames")}
        for rel in ("connectedTo", "serves"):
            if rel in attrs:
                attrs[rel] = [ids.get(t, t) for t in attrs[rel]]
        dotted = ".".join(p.GetPath().pathString.strip("/").split("/"))
        yield ("asset", (m["id"], scope, vessel, p.GetName(), dotted, str(p.GetPath()), usd_file, parent_id,
                         system_of.get(m["id"]) if m["id"] not in system_of or system_of[m["id"]] != m["id"] else m["id"],
                         m.get("class"), Usd.ModelAPI(p).GetKind(), m.get("code", ""), m.get("definitionId"),
                         status, Usd.ModelAPI(root).GetAssetInfo().get("version", "0.0.0"), json.dumps(attrs)))
        for i, (iid, nm) in enumerate(zip(m.get("instanceIds", []), m.get("instanceNames", []))):
            yield ("instance", (iid, m["id"], nm, i))


def rebuild(vessel_files: list[Path], library_files: list[Path], manifest_files: list[Path],
            status: str = "validated", validation: dict | None = None) -> Path:
    if REGISTRY_DB.exists():
        REGISTRY_DB.unlink()
    con = connect()
    with con:
        for f in library_files:
            st = Usd.Stage.Open(str(f))
            for kind, row in _rows(st, "definition", None, str(f.relative_to(ROOT)), status):
                if kind == "asset":
                    con.execute("INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
        for f in vessel_files:
            st = Usd.Stage.Open(str(f), Usd.Stage.LoadAll)
            for kind, row in _rows(st, "occurrence", f.parent.name, str(f.relative_to(ROOT)), status):
                if kind == "asset":
                    con.execute("INSERT OR REPLACE INTO assets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
                else:
                    con.execute("INSERT OR REPLACE INTO instances VALUES (?,?,?,?)", row)
        for man in manifest_files:
            m = json.loads(man.read_text())
            for c in m["chunks"]:
                con.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?,?)",
                            (str((man.parent / c["url"]).relative_to(ROOT)), c["sha256"], c["bytes"], "glb",
                             c["system"], c["lod"], c["triangles"]))
        for f in sorted(LIBRARY.rglob("*")):
            if f.is_file() and f.suffix in (".usda", ".usdc", ".step"):
                con.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?,?,?,?,?)",
                            (str(f.relative_to(ROOT)), sha256(f), f.stat().st_size, f.suffix[1:], None, None, None))
        params = {}
        for pj in sorted(LIBRARY.glob("*/*/*.params.json")):
            params[pj.stem.replace(".params", "")] = json.loads(pj.read_text())
        build_id = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:16]
        con.execute("INSERT OR REPLACE INTO builds VALUES (?,?,?,?,?)",
                    (build_id, datetime.now(timezone.utc).isoformat(timespec="seconds"),
                     json.dumps(toolchain()), json.dumps(params), json.dumps(validation or {})))
    con.close()
    return REGISTRY_DB

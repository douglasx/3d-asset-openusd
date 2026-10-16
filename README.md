# Marine vessel 3D asset pipeline

Phase 1 (propulsion spike) of the plan in `doc/Marine Vessel 3D Asset Pipeline Plan and Tech Stack.pdf`.
One command builds a propulsion system end to end: parametric generators write STEP (the simulated CAD
export), an ingest adapter reads that STEP into OpenUSD exactly as it would read real CAD, the vessel is
composed from a shared equipment library, and a converter publishes optimised GLB chunks that a three.js
viewer explodes, collapses and queries by ID through a REST API.

```
generators (build123d) ──STEP──▶ ingest ──▶ library USD ──▶ vessel USD ──▶ GLB L0/L1/L2 + manifest ──▶ viewer
                                                   │             │
                                                   └─ ids.lock ──┴─▶ RDF graph + SHACL, SQLite registry ──▶ API
```

## Quick start

Requires Python 3.11+ and Node 20+.

```bash
make setup      # .venv with pinned Python deps; npm install in build/ and viewer/
make build      # generate → ingest → layout → compose → convert → optimise → graph → validate → registry (~10 s)
make test       # 21 tests: round trips, validators (incl. negative cases), ID joins, API
make api        # builds the viewer, serves API + viewer at http://localhost:8000
```

Or `make docker` (Docker Compose; the image builds and validates the assets, then serves).

The viewer also runs with no API: `cd viewer && npm run build`, then serve `viewer/dist/` as static
files. `npm run single-file` additionally writes `viewer/dist/viewer-single.html`, one self-contained
file (code, manifest and GLB chunks inlined) that opens straight from disk or as a shared page. Live
status needs the API; everything else works offline from the GLB data.

## What is in the repository

| Folder | Contents | Edited by |
|---|---|---|
| `generators/` | Parametric equipment generators and the catalogue of parameter sets (`catalog.py`) | people |
| `assets/vessels/<vessel>/arrangement.yaml` | Vessel arrangement: equipment positions, variant choices, connections | people |
| `schema/marine/` | Codeless `marine:` USD API schema (`schema.usda` is the source; the other two files are generated) | people |
| `schema/vocab/` | Controlled vocabularies: classes, materials, breakdown codes, LOD budgets | people |
| `schema/ontology/` | OWL ontology and SHACL shapes | people |
| `library/` | Generated equipment library: STEP (simulated CAD), USD (`.usda` attributes + `.geom.usdc` geometry), parameters | build |
| `assets/vessels/<vessel>/*.usda` | Composed vessel and system layers (library by reference, occurrence IDs, explode, proxies) | build |
| `assets/ids.lock.json` | Append-only map of stable keys to permanent UUIDs | build (CI-guarded) |
| `marinekit/` | Pipeline code: STEP I/O, ingest, USD writer, explode/proxy, compose, glTF converter, graph, validators, registry | people |
| `build/` | GLB optimiser (glTF-Transform + meshopt) and `build/out/` outputs (git-ignored) | – |
| `api/` | FastAPI backend | people |
| `viewer/` | three.js + TypeScript + Vite viewer, embeddable class `MarineViewer` | people |
| `tests/` | pytest suite | people |
| `tools/preview.py` | Matplotlib preview of a generator's Part tree (debugging, no GPU) | – |

## Conventions (from the hand-off, implemented)

- **Hierarchy**: Vessel → Zone → System → Equipment → Component (→ parts such as fastener sets). USD kind:
  `assembly` for vessel/zone/system, `component` for equipment, `subcomponent` below.
- **Units and axes**: `metersPerUnit = 1`, `upAxis = Z`, ship frame (origin at aft perpendicular,
  centreline, baseline; x forward, y port, z up). STEP is millimetres; ingest converts. The viewer
  converts to Y-up once, at its root.
- **IDs**: `marine:id` is a UUID from `assets/ids.lock.json`, never reused. Library prims carry
  *definition* IDs; each vessel occurrence gets its own ID with `marine:definitionId` pointing back, so
  one library propeller can sit in many vessels. Repeated parts (bolts, cylinder heads) are one
  PointInstancer with `marine:instanceIds`, so every bolt still has its own ID.
- **Asset prims** are Xforms (or PointInstancers) carrying `MarineAssetAPI`. Geometry is a child Mesh
  `geom`; the L0 proxy is a child Mesh `proxy` with `purpose = proxy`. Position is the transform, never
  an attribute.
- **Explode**: assembled pose is canonical; each child stores `explodeDir` (parent frame) and
  `explodeDist`. Defaults come from the class pattern in `classes.yaml` and are spaced so siblings never
  overlap at slider = 1. Override any prim in `explode_overrides.yaml` beside the asset.
- **glTF**: node name = `marine:id`; `extras.marine` holds every attribute plus `role`
  (`asset | instance | geom | proxy`). One GLB per system per LOD, content-hashed file names.

## Adding equipment

1. Write a generator in `generators/<area>/<name>.py` returning a `Part` tree (helpers in
   `generators/common.py`: axis-x primitives, bolts, mass from volume × density).
2. Add its classes to `schema/vocab/classes.yaml` (explode pattern, required attributes, dimension
   checks) and to `schema/ontology/marine.ttl`.
3. Add a parameter set to `generators/catalog.py`; place it in a vessel's `arrangement.yaml`.
4. `make build`. Validators tell you what is wrong; IDs are allocated on first build and kept.

## Swapping in real CAD

Put the CAD system's STEP AP242 export at `library/<family>/<name>/<name>.step` and run
`python -m marinekit.build --skip-generate`. The ingest adapter never uses generator code. It takes
`marine:*` user-defined attributes when the file has them, matches parts to existing IDs by name path
when it does not, gives new parts new IDs, and lists vanished parts for review instead of deleting them
(see `tests/test_step_ingest.py`). `POST /ingest` runs the same adapter as a preview job.

## API

`uvicorn api.app:app` (OpenAPI docs at `/docs`):

| Call | Returns |
|---|---|
| `GET /vessels` | vessels with manifests |
| `GET /vessels/{id}/manifest` | chunk list and tree for the viewer |
| `GET /assets/{id}` | static properties and relationships (works for single bolt IDs too) |
| `GET /assets/{id}/children` | next explode level |
| `GET /assets/{id}/status` | live/inspection data (simulated; replace `simulate_status`) |
| `GET /assets?class=Bearing&material=…` | filtered search |
| `POST /sparql` | read-only SPARQL over the vessel graph |
| `POST /ingest`, `GET /jobs/{id}` | STEP ingest preview job |

## Validators (`python -m marinekit.validate`)

OpenUSD's own validators (what `usdchecker` runs in 26.x), required properties, ID uniqueness and
stability (append-only lock vs. previous commit), units and axes (no scale in transforms), declared
dimensions vs. mesh (radial max, mid-section cut, x extent), mass (mesh volume × density, roll-ups),
vocabulary, SHACL conformance, explode overlap and proxy presence, triangle budgets per LOD, GLB round
trip (ID sets, attributes, bounding box) and STEP round trip (generator → USD vs. generator → STEP →
ingest → USD). Report: `build/out/reports/validation.json`.

See `docs/spike-report.md` for measured results, deviations from the plan and open items.

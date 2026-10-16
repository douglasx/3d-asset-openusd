# Propulsion spike: results

Built 16 Oct 2026 against the plan and hand-off of 15 Oct 2026.

## Gate

**Passed: explode works, and IDs match across USD, GLB, graph, registry and API.**
`tests/test_outputs_api.py::test_ids_match_across_usd_glb_graph_registry` asserts the same 96 IDs
(36 asset occurrences + 60 instanced parts) in all five places; the viewer explodes three levels and
collapses any assembly to its proxy.

## What was built

| Hand-off task | Status |
|---|---|
| Codeless `marine:` schema and monorepo skeleton | Done. Registers in OpenUSD 26.8 via `PXR_PLUGINPATH_NAME`; 18 attributes + 2 relationships |
| Propulsion generators (shaft line, gearbox, engine, CPP propeller, instanced bolts) | Done. 5 catalogue entries incl. 4- and 5-blade variants |
| STEP ingest adapter | Done. AP242 with user-defined attributes; ID matching by attribute, then name path |
| USD → GLB converter with round-trip tests | Done. Custom converter (pxr + pygltflib), validator and tests |
| Default explode vectors, L0 proxies, explode and proxy validators | Done |
| Minimal viewer: manifest, tree, picking, explode slider per level | Done, plus collapse-to-proxy, isolate, section plane, measure, colour by equipment/condition |
| `GET /assets/{id}` joined by ID | Done, plus the rest of the plan's API |
| Read OCX 3.x docs; verify licences | Licences partly verified (below). OCX not read; not needed until phase 3 |
| One-week bake-off (build123d vs Blender vs FreeCAD) | Not done: only the build123d path was built |
| Profile on the slowest device, set real budgets | Not done: needs your target hardware |
| Test in a third-party USD viewer; Blender custom-property export | Not done: no usdview/Blender in the build environment |

## Measured numbers (propulsion system, 92.1 t, 36 assets, 60 instances)

| Tier | Triangles | GLB raw | GLB optimised | Budget |
|---|---|---|---|---|
| L0 (proxies) | 4,502 | 440 KB | 116 KB | ≤ 5,000 per assembly |
| L1 (merged, no fasteners) | 18,714 | 584 KB | 159 KB | ≤ 30,000 |
| L2 (full) | 41,348 | 1,087 KB | 321 KB | ≤ 150,000 |

Full build (generate → registry, all validators): about 8 s on the cloud build container. Outputs are
byte-identical across rebuilds (26 files compared over three builds, one with an asset regenerated
alone in between). Mesh volumes are within 2.5% of the exact B-rep volumes; the blade tip lands exactly
on the declared 3.600 m diameter.

## Deviations from the plan, and why

- **STEP colours off by default.** OpenCascade writes colour styles in memory-address order, so STEP
  files with colours change on every build. Materials travel as `marine:material` anyway.
  `MARINE_STEP_COLORS=1` turns colours on for inspection.
- **Occurrence vs definition IDs.** The plan has one ID per part. A library part referenced into several
  vessels needs one ID per occurrence, so library prims hold definition IDs and vessel layers author
  occurrence IDs (`marine:definitionId` links them). Without this, reuse breaks ID uniqueness.
- **usdchecker.** OpenUSD 26 replaced the Python ComplianceChecker with the `UsdValidation` framework;
  the validator runs all 28 registered validators.
- **Viewer UI layer.** Plain TypeScript instead of Svelte/React. The plan allowed a thin layer; none was
  needed yet. `MarineViewer` is the embeddable class.
- **Registry and storage.** SQLite and the filesystem, as the plan allows for the spike. The SQL is
  Postgres-portable (`path` becomes `ltree`, `attrs` becomes JSONB).

## Findings worth knowing

1. OpenCascade drops attributes on component *instances* when reading STEP; only definition attributes
   and instance names survive. Per-instance IDs therefore come from the lock by instance name, which is
   also how real CAD revisions must be handled.
2. The first STEP file written in a process silently falls back to AP214 unless the STEP controller is
   initialised first; assembly-occurrence IDs come from a process-wide counter. Both are worked around
   in `marinekit/step_io.py`.
3. BRepMesh caches triangulations on shapes, so tessellation depended on call history until the cache
   was cleared before each mesh.
4. Truly cylindrical (non-planar) blade sections give an invalid loft in OpenCascade; planar sections
   pulled in to the station radius keep the solid valid and dimension-true.
5. RDFS domain declarations on `partOf` made every bolt instance infer as a full asset; constraints
   belong in SHACL, not in `rdfs:domain`.
6. Bounding-box explode spacing needs ordering rules (anchors first, axial chains outward, large parts
   before small, fasteners last). Bearings and bolts still move sideways where their axial path is
   blocked; override them per prim if that matters.

## Licences checked (16 Oct 2026)

| Component | Licence | Source |
|---|---|---|
| build123d 0.13 | Apache-2.0 | package metadata |
| OpenUSD (usd-core 26.8) | TOST-1.0 (modified Apache 2.0, renamed in 24.08, terms unchanged) | package metadata, AOUSD forum |
| OCP bindings 8.0.1 | Apache-2.0 (OpenCascade itself is LGPL-2.1 with exception) | package metadata |
| OCX Python package 3.2.0 | Apache 2.0 | PyPI |
| three.js, glTF-Transform, meshoptimizer, pygltflib, FastAPI | MIT | package metadata |
| rdflib / pySHACL | BSD-3-Clause / Apache | package metadata |
| **Garage** | **AGPL-3.0** — same concern as MinIO; prefer SeaweedFS (Apache-2.0, not re-verified here) | GitHub |
| DNV Vista SDK | MIT; packages `vista-sdk` (PyPI), `dnv-vista-sdk` (npm), `DNV.Vista.SDK` (NuGet); VIS 3.4a–3.10a | [GitHub](https://github.com/dnv-opensource/vista-sdk) |

## Open items for phase 2

- Replace placeholder breakdown codes in `schema/vocab/breakdown.yaml` with real VIS/Gmod codes from
  the Vista SDK (MIT, Python package `vista-sdk`, pin one VIS version such as 3.10a). IDs stay; only
  `marine:code` changes.
- Profile on the slowest target device and replace the budgets in `schema/vocab/budgets.yaml`.
- Postgres registry, S3-compatible artifact store, SPARQL store, and the visual-regression check
  (headless render vs baseline) in CI.
- Run the bake-off if Blender/FreeCAD authoring is still on the table; build123d covered everything in
  this spike.
- Owner questions from the hand-off are still open: required ID scheme, viewer audience, CAD round trip,
  slowest devices.

"""Run the whole pipeline: the one command CI and humans use.

    python -m marinekit.build            # everything
    python -m marinekit.build --skip-generate   # start from existing STEP (real-CAD mode)

Stages
  0 schema      regenerate the codeless schema plugin from schema.usda
  1 generate    generators -> STEP (+ direct USD for the round-trip test)
  2 ingest      STEP -> library USD (units, axes, ID matching)
  3 layout      default explode vectors + L0 proxies inside each library asset
  4 compose     arrangement.yaml -> vessel + system USD layers, occurrence IDs
  5 convert     USD -> GLB chunks (L0/L1/L2) + manifest
  6 optimize    glTF-Transform + meshopt (Node); falls back to raw GLB if Node is missing
  7 graph       USD -> RDF (Turtle) per vessel
  8 validate    every validator; non-zero exit on any error
  9 registry    SQLite registry rebuilt, status = validated (or draft on failure)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .paths import BUILD, LIBRARY, OUT, ROOT, SCHEMA_PLUGIN


def _env() -> None:
    os.environ.setdefault("PXR_PLUGINPATH_NAME", str(SCHEMA_PLUGIN))


def optimize(raw_files: list[str], out_dir: Path) -> dict[str, str] | None:
    node = shutil.which("node")
    if not node or not (BUILD / "node_modules").exists():
        print("  optimize: Node or build/node_modules missing; shipping raw GLB (run `npm ci` in build/)")
        return None
    res = subprocess.run([node, str(BUILD / "optimize.mjs"), str(out_dir), *raw_files], cwd=BUILD,
                         capture_output=True, text=True)
    if res.returncode != 0:
        print(res.stderr)
        raise RuntimeError("GLB optimisation failed")
    return json.loads(res.stdout.strip().splitlines()[-1])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-generate", action="store_true", help="ingest existing STEP files only")
    ap.add_argument("--no-validate", action="store_true")
    ap.add_argument("--baseline-lock", help="previous ids.lock.json for the append-only check")
    args = ap.parse_args(argv)
    _env()
    # imports after the plugin path is set so the codeless schema registers
    from . import compose, explode, generate, gltf, graph, ingest, registry, schema_gen, validate
    from .ids import IdLock

    t0 = time.time()
    lock = IdLock()

    def stage(n, name):
        print(f"[{n}] {name}  (+{time.time() - t0:.1f}s)")

    stage(0, "schema")
    schema_gen.generate()
    if not args.skip_generate:
        stage(1, "generate")
        generate.run(lock)
    stage(2, "ingest")
    reports = ingest.run(lock)
    if any(r["conflicts"] for r in reports):
        print("ID conflicts during ingest; see build/out/reports/ingest.json")
        return 2
    stage(3, "layout (explode + proxies)")
    for f in sorted(LIBRARY.glob("*/*/*.usda")):
        overrides = explode.load_overrides(f.with_name("explode_overrides.yaml"))
        explode.run_library_asset(f, overrides)
    stage(4, "compose")
    vessels = compose.run(lock)
    manifests = []
    for v in vessels:
        name = v.parent.name
        out = OUT / "vessels" / name
        if out.exists():
            shutil.rmtree(out)
        stage(5, f"convert {name}")
        man = gltf.convert_vessel(v, out)
        stage(6, f"optimize {name}")
        opt = optimize([c["raw"] for c in man["chunks"]], out / "opt")
        manifests.append(gltf.finalize(man, out, opt))
        if (out / "opt").exists():
            shutil.rmtree(out / "opt")
        stage(7, f"graph {name}")
        graph.write(graph.build_graph(v), out / "graph.ttl")
    lock.save()
    status, summary = "draft", {}
    if not args.no_validate:
        stage(8, "validate")
        rep = validate.run(args.baseline_lock)
        summary = json.loads((OUT / "reports" / "validation.json").read_text())["summary"]
        status = "draft" if rep.errors else "validated"
    stage(9, f"registry (status={status})")
    registry.rebuild(vessels, sorted(LIBRARY.glob("*/*/*.usda")), manifests, status, summary)
    print(f"done in {time.time() - t0:.1f}s")
    return 0 if status == "validated" or args.no_validate else 1


if __name__ == "__main__":
    sys.exit(main())

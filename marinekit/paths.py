"""Repository paths, in one place."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "schema"
SCHEMA_PLUGIN = SCHEMA / "marine"
VOCAB = SCHEMA / "vocab"
ONTOLOGY = SCHEMA / "ontology"
GENERATORS = ROOT / "generators"
LIBRARY = ROOT / "library"
ASSETS = ROOT / "assets"
BUILD = ROOT / "build"
OUT = BUILD / "out"
ID_LOCK = ASSETS / "ids.lock.json"
REGISTRY_DB = OUT / "registry.sqlite"

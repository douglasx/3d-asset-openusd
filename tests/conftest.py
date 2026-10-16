import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
os.environ.setdefault("PXR_PLUGINPATH_NAME", str(ROOT / "schema" / "marine"))
sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session")
def built():
    """Make sure a full build exists (runs it once if build/out is missing)."""
    man = ROOT / "build" / "out" / "vessels" / "demo_osv" / "manifest.json"
    if not man.exists():
        subprocess.run([sys.executable, "-m", "marinekit.build"], cwd=ROOT, check=True)
    return ROOT / "build" / "out"

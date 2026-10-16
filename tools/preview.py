"""Debug preview: render a Part tree to PNG with matplotlib (no GPU needed)."""
from __future__ import annotations
import sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
from marinekit import geom, vocab


def collect(part, parent=np.eye(4), out=None):
    out = [] if out is None else out
    m = parent @ part.xform
    if part.shape is not None:
        pts, _, tris = geom.tessellate(part.shape, 0.01, 0.5)
        col = vocab.materials()[part.attrs.get("material", "proxy")]["color"]
        places = [m @ i.xform for i in part.instances] or [m]
        for pm in places:
            p = (np.c_[pts, np.ones(len(pts))] @ pm.T)[:, :3]
            out.append((p[tris], col))
    for c in part.children:
        collect(c, m, out)
    return out


def render(part, path, elev=20, azim=-60):
    items = collect(part)
    fig = plt.figure(figsize=(10, 7))
    ax = fig.add_subplot(projection="3d")
    allp = []
    for tri, col in items:
        ax.add_collection3d(Poly3DCollection(tri, facecolors=col, edgecolors=col, linewidths=0, alpha=1.0, shade=True))
        allp.append(tri.reshape(-1, 3))
    allp = np.vstack(allp)
    lo, hi = allp.min(0), allp.max(0)
    c, r = (lo + hi) / 2, (hi - lo).max() / 2
    ax.set_xlim(c[0] - r, c[0] + r); ax.set_ylim(c[1] - r, c[1] + r); ax.set_zlim(c[2] - r, c[2] + r)
    ax.view_init(elev=elev, azim=azim)
    ax.set_box_aspect((1, 1, 1))
    fig.savefig(path, dpi=90, bbox_inches="tight")
    print("wrote", path)

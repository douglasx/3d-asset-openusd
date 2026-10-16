"""Parametric hull form for the demo vessel (stand-in for real faired lines).

Ship frame, millimetres here (CAD units): x forward from the aft
perpendicular (AP = rudder stock axis), y to port, z up from baseline.

The form is deliberately simple but consistent with the machinery already
placed in the vessel:
- flat bottom amidships; the keel line rises aft (cut-up) so the 3.6 m
  propeller at x = 3.0 m has tip clearance, and a centreline skeg carries
  the stern tube and shaft;
- transom stern at x = -3.0 m; raked stem with the bow narrowing to a stem bar.

Every hull block, the skeg, and the bow-thruster tunnel length are derived
from these functions, so changing the form regenerates them consistently.
Replace this module with real offsets (or OCX/STEP surfaces) in phase 3.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class HullForm:
    name: str = "demo_osv_form"
    x_transom: float = -3000.0
    x_stem_deck: float = 78500.0      # stem top
    x_stem_keel: float = 75500.0      # forefoot
    beam: float = 18000.0
    depth: float = 8000.0
    beam_transom: float = 16000.0
    bilge_radius: float = 1500.0
    cutup_height: float = 4600.0      # keel height over the propeller
    x_cutup_aft: float = 4500.0       # keel flat at cutup_height aft of this
    x_cutup_fwd: float = 18000.0      # flat bottom from here forward
    x_parallel_aft: float = 8000.0
    x_parallel_fwd: float = 62000.0
    stem_halfwidth: float = 120.0     # stem bar, keeps sections non-degenerate

    # ------------------------------------------------------------- lines
    def keel_z(self, x: float) -> float:
        if x >= self.x_stem_keel:          # raked stem: forefoot rises to the deck at the stem top
            t = min(1.0, (x - self.x_stem_keel) / (self.x_stem_deck - self.x_stem_keel))
            return (self.depth - 400.0) * t ** 1.4
        if x <= self.x_cutup_aft:
            return self.cutup_height
        if x >= self.x_cutup_fwd:
            return 0.0
        s = (self.x_cutup_fwd - x) / (self.x_cutup_fwd - self.x_cutup_aft)
        return self.cutup_height * s * s

    def halfbreadth_deck(self, x: float) -> float:
        hb = 0.5 * self.beam
        if x < self.x_parallel_aft:
            t = (self.x_parallel_aft - x) / (self.x_parallel_aft - self.x_transom)
            return hb - t * (hb - 0.5 * self.beam_transom)
        if x > self.x_parallel_fwd:
            t = min(1.0, (x - self.x_parallel_fwd) / (self.x_stem_deck - self.x_parallel_fwd))
            return max(self.stem_halfwidth, hb * math.sqrt(max(0.0, 1.0 - t * t)))
        return hb

    def halfbreadth_bottom(self, x: float) -> float:
        """Half-width of the flat of bottom (where the bilge turn starts)."""
        flat = self.halfbreadth_deck(x) - self.bilge_radius
        if x > self.x_parallel_fwd:
            t = min(1.0, (x - self.x_parallel_fwd) / (self.x_stem_keel - self.x_parallel_fwd))
            flat = (0.5 * self.beam - self.bilge_radius) * max(0.0, 1.0 - t) ** 1.6
        return max(self.stem_halfwidth * 0.5, flat)

    def bilge_r(self, x: float) -> float:
        room = min(self.halfbreadth_deck(x) - self.halfbreadth_bottom(x),
                   0.45 * (self.depth - self.keel_z(x)))
        return max(20.0, min(self.bilge_radius, room))

    # ------------------------------------------------------------- sections
    def section_half(self, x: float, n_arc: int = 5) -> list[tuple[float, float]]:
        """Starboard-free half section (y >= 0), from deck edge down to the
        keel at y = 0. Fixed point count at every station (lofting needs it)."""
        D, kz = self.depth, self.keel_z(x)
        hbd, hbb, r = self.halfbreadth_deck(x), self.halfbreadth_bottom(x), self.bilge_r(x)
        side_top = (hbd, D)
        side_low = (hbb + r, kz + r)
        pts = [side_top,
               (side_top[0] + (side_low[0] - side_top[0]) / 3, D - (D - side_low[1]) / 3),
               (side_top[0] + 2 * (side_low[0] - side_top[0]) / 3, D - 2 * (D - side_low[1]) / 3)]
        for i in range(n_arc + 1):
            a = -math.pi / 2 * i / n_arc
            pts.append((hbb + r * math.cos(a), kz + r + r * math.sin(a)))
        pts.append((0.5 * hbb, kz))
        pts.append((0.0, kz))
        return pts

    def section_full(self, x: float) -> list[tuple[float, float]]:
        """Closed-ready outline from port deck edge round the keel to starboard deck edge."""
        half = self.section_half(x)
        port = half                                   # y >= 0 is port
        stbd = [(-y, z) for (y, z) in reversed(half[:-1])]
        return port + stbd

    def halfbreadth_at(self, x: float, z: float) -> float:
        """Hull half-breadth at height z (linear on the section polyline)."""
        half = self.section_half(x, n_arc=12)
        for (y0, z0), (y1, z1) in zip(half, half[1:]):
            if min(z0, z1) <= z <= max(z0, z1) and z0 != z1:
                return y0 + (z - z0) / (z1 - z0) * (y1 - y0)
        return 0.0


FORM = HullForm()

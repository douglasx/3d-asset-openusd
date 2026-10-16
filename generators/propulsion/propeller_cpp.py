"""Controllable-pitch propeller generator.

Local frame: origin at the propeller plane on the shaft axis, +x forward
(towards the hull), blades radial. The hub's forward flange face is at
x = hub_length/2 + flange_t; the shaft line attaches there.

Blade geometry is a lofted set of lens-shaped sections whose chord,
thickness, pitch and skew follow simple textbook distributions. It is
plausible, dimension-true to its parameters (diameter, hub size, blade
count), and NOT a hydrodynamic design.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from build123d import Compound, Face, Spline, Vector, Wire, loft

from generators.common import (assembly, bolt_holes, bolt_shape, circle_instances,
                               cone_x, cyl_x, part)
from marinekit.model import Instance, rot_x, rot_y, translate


@dataclass(frozen=True)
class PropellerParams:
    name: str = "propeller_cpp_4b"
    diameter: float = 3600.0       # mm
    blades: int = 4
    hub_diameter: float = 1000.0
    hub_length: float = 1100.0
    flange_diameter: float = 820.0
    flange_t: float = 110.0
    pitch_ratio: float = 0.95      # P/D at 0.7R
    expanded_area_ratio: float = 0.55
    skew_deg: float = 18.0
    palm_diameter: float = 620.0
    palm_height: float = 90.0
    blade_bolts: int = 8
    blade_bolt_d: float = 36.0
    blade_bolt_len: float = 160.0
    hub_bolts: int = 10
    hub_bolt_d: float = 42.0
    hub_bolt_len: float = 260.0
    cone_length: float = 650.0
    cone_tip_diameter: float = 260.0


# Chord shape factor vs r/R (multiplies the mean chord) and section stations.
_STATIONS = [0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.00]
_CHORD = [0.80, 0.92, 1.02, 1.10, 1.13, 1.06, 0.88, 0.68, 0.30]


def _section(p: PropellerParams, x_rel: float, chord_f: float) -> Wire:
    R = 0.5 * p.diameter
    r = x_rel * R
    r_hub = 0.5 * p.hub_diameter
    mean_chord = p.expanded_area_ratio * math.pi * R * R / (p.blades * (R - r_hub))
    c = mean_chord * chord_f
    t = p.diameter * max(0.040 - 0.036 * x_rel, 0.004)       # max thickness
    pitch = p.pitch_ratio * p.diameter
    phi = math.atan(pitch / (2 * math.pi * r))                # pitch angle
    skew = math.radians(p.skew_deg) * ((x_rel - _STATIONS[0]) / (1 - _STATIONS[0])) ** 1.5
    # chord direction and section normal in the (x axial, y tangential) plane
    dx, dy = math.sin(phi), math.cos(phi)
    nx, ny = math.cos(phi), -math.sin(phi)
    pts = []
    n = 10
    # back (suction) side LE->TE, then face (pressure) side TE->LE
    for i in range(n + 1):
        s = i / n
        u = (s - 0.5) * c
        v = t * (1 - (2 * s - 1) ** 2) * 0.85 + 0.03 * t
        pts.append((u, v))
    for i in range(n - 1, 0, -1):
        s = i / n
        u = (s - 0.5) * c
        v = -0.15 * t * (1 - (2 * s - 1) ** 2) - 0.03 * t * 0  # slight camber on the face
        pts.append((u, v))
    # Skew moves the section's mid-chord round the shaft axis by the skew
    # angle. Section planes stay parallel (constant z), which keeps the
    # OpenCascade loft valid; truly cylindrical (non-planar) sections produced
    # invalid solids. Planar sections stick out past radius r at their
    # tangential extremes, so the section is pulled inward until no point
    # exceeds r: the blade tip then sits exactly on the declared diameter.
    tang = [(u * dx + v * nx, u * dy + v * ny) for u, v in pts]
    k = 1.0
    for _ in range(8):
        y0, z0 = k * r * math.sin(skew), k * r * math.cos(skew)
        rmax = max(math.hypot(yt + y0, z0) for _, yt in tang)
        k *= r / rmax
    y0, z0 = k * r * math.sin(skew), k * r * math.cos(skew)
    pts3 = [Vector(x, yt + y0, z0) for x, yt in tang]
    return Wire([Spline(*pts3, periodic=True)])


def blade_shape(p: PropellerParams):
    r_hub = 0.5 * p.hub_diameter
    sections = [_section(p, x, f) for x, f in zip(_STATIONS, _CHORD)]
    body = loft([Face(w) for w in sections])
    palm_z0 = r_hub - 40.0
    palm = translate_shape(cyl_z(0.5 * p.palm_diameter, palm_z0, r_hub + p.palm_height))
    palm = bolt_holes_z(palm, p.blade_bolts, 0.78 * p.palm_diameter, p.blade_bolt_d + 3,
                        palm_z0, r_hub + p.palm_height)
    try:
        return body + palm
    except Exception:  # pragma: no cover - boolean fallback keeps the part usable
        return Compound([body, palm])


def cyl_z(r, z0, z1):
    from build123d import Cylinder, Pos, Align
    return Pos(0, 0, z0) * Cylinder(r, z1 - z0, align=(Align.CENTER, Align.CENTER, Align.MIN))


def translate_shape(s):
    return s


def bolt_holes_z(body, n, pcd, d_hole, z0, z1):
    for i in range(n):
        a = 2 * math.pi * (i + 0.5) / n
        h = cyl_z(0.5 * d_hole, z0 - 1, z1 + 1)
        from build123d import Pos
        body = body - Pos(0.5 * pcd * math.cos(a), 0.5 * pcd * math.sin(a), 0) * h
    return body


def hub_shape(p: PropellerParams):
    r = 0.5 * p.hub_diameter
    half = 0.5 * p.hub_length
    body = cyl_x(r, -half, half)
    body = body + cone_x(r, 0.5 * p.flange_diameter + 40, half - 1, half + 60)
    flange = cyl_x(0.5 * p.flange_diameter, half + 50, half + 50 + p.flange_t)
    flange = bolt_holes(flange, p.hub_bolts, 0.8 * p.flange_diameter, p.hub_bolt_d + 3,
                        half + 50, half + 50 + p.flange_t, phase=180.0 / p.hub_bolts)
    return body + flange


def cone_shape(p: PropellerParams):
    half = 0.5 * p.hub_length
    r0 = 0.5 * p.hub_diameter * 0.92
    return cone_x(0.5 * p.cone_tip_diameter, r0, -half - p.cone_length, -half)


def generate(p: PropellerParams = PropellerParams()):
    half = 0.5 * p.hub_length
    r_hub = 0.5 * p.hub_diameter
    flange_x1 = half + 50 + p.flange_t
    hub = part("Hub", "PropellerHub", hub_shape(p), "bronze_nab_cu3",
               "carries blades and houses the pitch actuating mechanism",
               mass=None, diameter=p.hub_diameter, length=flange_x1 + half)
    cone = part("Cone", "HubCone", cone_shape(p), "bronze_nab_cu3",
                "fairs the hub aft end to reduce drag",
                diameter=0.92 * p.hub_diameter, length=p.cone_length)
    blade_solid = blade_shape(p)
    children = [hub, cone]
    for i in range(p.blades):
        angle = 360.0 * i / p.blades
        blade = part(f"Blade_{i + 1}", "PropellerBlade", blade_solid, "bronze_nab_cu3",
                     "converts shaft torque into thrust; pitch set by the hub mechanism",
                     xform=rot_x(angle), length=0.5 * p.diameter - r_hub)
        # blade palm bolts: axis radial (+z in the blade frame), head on the palm top
        bolts = part("PalmBolts", "BoltSet", bolt_shape(p.blade_bolt_d, p.blade_bolt_len),
                     "steel_bolt_10_9", "clamps the blade palm to the hub crank ring",
                     count=p.blade_bolts, diameter=p.blade_bolt_d, length=p.blade_bolt_len)
        pcd = 0.78 * p.palm_diameter
        for j in range(p.blade_bolts):
            a = 2 * math.pi * (j + 0.5) / p.blade_bolts
            m = translate(0.5 * pcd * math.cos(a), 0.5 * pcd * math.sin(a), r_hub + p.palm_height) @ rot_y(-90)
            bolts.instances.append(Instance(f"Bolt_{j + 1:02d}", m))
        blade.children.append(bolts)
        blade.attrs["mass"] = round(blade.attrs["mass"], 3)
        children.append(blade)
    hub_bolts = part("HubFlangeBolts", "BoltSet", bolt_shape(p.hub_bolt_d, p.hub_bolt_len),
                     "steel_bolt_10_9", "clamps the hub flange to the propeller shaft flange",
                     count=p.hub_bolts, diameter=p.hub_bolt_d, length=p.hub_bolt_len)
    hub_bolts.instances = circle_instances("Bolt", p.hub_bolts, 0.8 * p.flange_diameter,
                                           x=half + 50, phase=180.0 / p.hub_bolts, flip=True)
    children.append(hub_bolts)
    hub.relations["connectedTo"] = ["Cone"] + [f"Blade_{i + 1}" for i in range(p.blades)]
    prop = assembly(p.name, "Propeller", "converts engine power into ship thrust (controllable pitch)",
                    children, kind="component", diameter=p.diameter)
    prop.attrs["generator"] = f"propulsion.propeller_cpp:{p.name}"
    return prop

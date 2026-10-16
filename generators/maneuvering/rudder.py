"""Semi-balanced spade rudder with stock, neck bearing and rudder trunk.

Local frame: origin on the rudder stock axis at the top of the blade; z up,
+x forward. The vessel places it at the aft perpendicular (x = 0), with the
blade top just below the hull bottom. Parts that rotate about the stock are
built along their own local x and turned so x points up (the project rule:
rotationally symmetric parts have their axis on local x).

Blade sections are NACA 00xx, plated and hollow (mass from plate volume).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from build123d import Compound, Face, Kind, Pos, Spline, Vector, Wire, extrude, loft, offset

from generators.common import assembly, cyl_x, part
from marinekit.model import rot_y, translate

UP = rot_y(-90)   # local x -> ship z


@dataclass(frozen=True)
class RudderParams:
    name: str = "rudder_spade_7m2"
    span: float = 3600.0
    chord_root: float = 2300.0
    chord_tip: float = 1900.0
    thickness_ratio: float = 0.18
    balance: float = 0.30          # stock position as a fraction of chord from the leading edge
    plate_t: float = 16.0
    stock_d: float = 320.0
    stock_below: float = 1300.0    # stock length inside the blade
    stock_above: float = 2200.0    # stock length above the blade top (to the steering gear)
    trunk_od: float = 600.0
    trunk_id: float = 500.0
    trunk_z: tuple = (200.0, 1400.0)
    neck_len: float = 600.0


def _naca(chord: float, tc: float, z: float, balance: float, inset: float = 0.0, n: int = 14) -> Wire:
    """Closed NACA 00xx section in the plane z, leading edge towards +x."""
    c = chord - 2 * inset
    t = max(tc * chord - 2 * inset, 4.0)
    pts = []
    for i in range(n + 1):
        beta = math.pi * i / n
        xc = 0.5 * (1 - math.cos(beta))           # cosine spacing, 0 = LE
        yt = 5 * (t / c) * c * (0.2969 * math.sqrt(xc) - 0.1260 * xc - 0.3516 * xc ** 2
                                + 0.2843 * xc ** 3 - 0.1036 * xc ** 4)
        pts.append((xc, max(yt, 2.0 if 0 < i < n else 1.0)))
    upper = [(balance * chord - inset - xc * c, y) for xc, y in pts]
    lower = [(balance * chord - inset - xc * c, -y) for xc, y in reversed(pts[1:-1])]
    loop = upper + lower
    return Wire([Spline(*[Vector(x, y, z) for x, y in loop], periodic=True)])


def generate(p: RudderParams = RudderParams()):
    z_tip = -p.span
    root, tip = (Face(_naca(p.chord_root, p.thickness_ratio, 0.0, p.balance)),
                 Face(_naca(p.chord_tip, p.thickness_ratio, z_tip, p.balance)))
    outer = loft([root, tip])
    # uniform plate: offset each planar section inward in 2D, then loft the
    # inner skin between sections moved in by the end-plate thickness
    from generators.hull.hull_block import _inset
    # Plating as built: side/bottom skin open at the top, plus a separate top
    # plate. A closed hollow solid (internal void) does not survive STEP
    # exchange: readers drop the void and the blade comes back solid.
    inner = loft([Pos(0, 0, 1.0) * _inset(root, p.plate_t),
                  Pos(0, 0, p.plate_t) * _inset(tip, p.plate_t)])
    skin = outer - inner
    top_plate = extrude(Pos(0, 0, -p.plate_t) * _inset(root, p.plate_t), amount=p.plate_t)
    blade_shape = Compound([skin, top_plate])
    blade = part("Blade", "RudderBlade", blade_shape, "steel_rudder",
                 "deflects propeller slipstream to produce steering force",
                 length=p.span, thickness=p.plate_t)

    stock = part("Stock", "RudderStock",
                 cyl_x(0.5 * p.stock_d, -p.stock_below, p.stock_above), "steel_forged_34crnimo6",
                 "transmits steering torque from the steering gear to the blade",
                 xform=UP, diameter=p.stock_d, length=p.stock_below + p.stock_above)
    z0, z1 = p.trunk_z
    trunk = part("Trunk", "RudderTrunk",
                 cyl_x(0.5 * p.trunk_od, z0, z1, r_in=0.5 * p.trunk_id), "steel_ah36",
                 "watertight tube carrying the stock through the hull bottom",
                 xform=UP, diameter=p.trunk_od, length=z1 - z0)
    neck = part("NeckBearing", "Bearing",
                cyl_x(0.5 * p.trunk_id - 1, z0, z0 + p.neck_len, r_in=0.5 * p.stock_d + 2), "whitemetal_babbitt",
                "radial support of the rudder stock where it leaves the hull",
                xform=UP, diameter=p.stock_d + 4, length=p.neck_len)

    blade.relations["connectedTo"] = ["Stock"]
    neck.relations["connectedTo"] = ["Stock", "Trunk"]
    r = assembly(p.name, "Rudder", "produces the side force that turns the ship",
                 [blade, stock, trunk, neck], length=p.span)
    r.attrs["generator"] = f"maneuvering.rudder:{p.name}"
    return r

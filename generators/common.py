"""Shared generator helpers.

Conventions (see README "Conventions"):
- CAD units are millimetres; marine: dimension attributes are metres.
- Rotationally symmetric parts have their axis on local +x.
- Every generator returns a marinekit.model.Part tree for one piece of
  equipment, in that equipment's local frame.
"""
from __future__ import annotations

import math

from build123d import (Align, Axis, Compound, Cylinder, Pos, RegularPolygon, Rot,
                       Solid, extrude)

from marinekit import geom, vocab
from marinekit.model import Instance, Part

C, MIN, MAX = Align.CENTER, Align.MIN, Align.MAX
MM = 1e-3  # mm -> m


def cyl_x(r: float, x0: float, x1: float, r_in: float = 0.0, y: float = 0, z: float = 0):
    """Solid (or tube) cylinder along +x from x0 to x1."""
    body = Pos(x0, y, z) * Rot(0, 90, 0) * Cylinder(r, x1 - x0, align=(C, C, MIN))
    if r_in > 0:
        body = body - Pos(x0 - 1, y, z) * Rot(0, 90, 0) * Cylinder(r_in, x1 - x0 + 2, align=(C, C, MIN))
    return body


def cone_x(r0: float, r1: float, x0: float, x1: float):
    """Truncated cone along +x, radius r0 at x0 and r1 at x1."""
    return Pos(x0, 0, 0) * Rot(0, 90, 0) * Solid.make_cone(r0, r1, x1 - x0)


def bolt_holes(body, n: int, pcd: float, d_hole: float, x0: float, x1: float, phase: float = 0.0):
    for i in range(n):
        a = math.radians(phase + 360.0 * i / n)
        y, z = 0.5 * pcd * math.cos(a), 0.5 * pcd * math.sin(a)
        body = body - cyl_x(0.5 * d_hole, x0 - 1, x1 + 1, y=y, z=z)
    return body


def bolt_shape(d: float, length: float):
    """Hex-head bolt with nut, axis +x. Head bearing face at x=0, shank to -length."""
    s = 1.5 * d                       # across flats
    k = 0.65 * d                      # head height
    head = Rot(0, 90, 0) * extrude(RegularPolygon(s / math.sqrt(3), 6), amount=k)
    shank = cyl_x(0.5 * d, -length, 0.0)
    nut = Pos(-length + 0.9 * d, 0, 0) * Rot(0, -90, 0) * extrude(RegularPolygon(s / math.sqrt(3), 6), amount=0.8 * d)
    return Compound([head, shank, nut])


def circle_instances(prefix: str, n: int, pcd: float, x: float, phase: float = 0.0,
                     flip: bool = False) -> list[Instance]:
    """Placements for n bolts on a pitch circle in the y-z plane, axis along x."""
    from marinekit.model import rot_y, translate
    out = []
    for i in range(n):
        a = math.radians(phase + 360.0 * i / n)
        m = translate(x, 0.5 * pcd * math.cos(a), 0.5 * pcd * math.sin(a))
        if flip:
            m = m @ rot_y(180)
        out.append(Instance(f"{prefix}_{i + 1:02d}", m))
    return out


def part(name: str, cls: str, shape, material: str, function: str, *,
         kind: str = "subcomponent", mass: float | None = None, fidelity: str = "true",
         xform=None, count: int = 1, **dims_mm) -> Part:
    """Create a Part with attributes filled in from parameters.

    mass=None means computed from the exact solid volume x material density
    (times count for instanced parts); pass mass= for declared spec-sheet values.
    Dimension keyword arguments are in mm and stored in metres.
    """
    attrs = {"class": cls, "material": material, "function": function, "geometryFidelity": fidelity}
    if shape is not None and mass is None:
        vol_m3 = geom.volume(shape) * 1e-9
        attrs["mass"] = round(vol_m3 * vocab.density(material) * count, 3)
        attrs["massSource"] = "computed"
    elif mass is not None:
        attrs["mass"] = float(mass)
        attrs["massSource"] = "declared"
    for k, v in dims_mm.items():
        attrs[k] = round(v * MM, 6)
    p = Part(name=name, cls=cls, kind=kind, attrs=attrs, shape=shape)
    if xform is not None:
        p.xform = xform
    return p


def assembly(name: str, cls: str, function: str, children: list[Part], *, kind: str = "component",
             material: str = "proxy", xform=None, **dims_mm) -> Part:
    """An equipment or group whose mass is the roll-up of its children."""
    p = part(name, cls, None, material, function, kind=kind, mass=0.0, xform=xform, **dims_mm)
    p.children = children
    p.attrs["massSource"] = "rollup"
    p.attrs["mass"] = round(sum(c.attrs.get("mass", 0.0) for c in children), 3)
    return p

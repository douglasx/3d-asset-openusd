"""Single-reduction marine gearbox with vertical offset (input above output).

Local frame: origin on the OUTPUT shaft axis at the aft face of the output
flange; +x forward. The input shaft axis is at z = centre_distance.
The casing is an envelope solid (geometryFidelity = envelope, declared mass);
gears inside are dimension-true discs so exploding the casing reveals them.
"""
from __future__ import annotations

from dataclasses import dataclass

from build123d import Axis, Box, Pos, fillet

from generators.common import (MIN, C, assembly, bolt_holes, cyl_x, part)
from marinekit.model import translate


@dataclass(frozen=True)
class GearboxParams:
    name: str = "gearbox_sr_4mw"
    wheel_d: float = 1500.0
    pinion_d: float = 550.0
    face_width: float = 420.0
    casing_len: float = 2000.0
    casing_w: float = 2100.0
    casing_below: float = 1050.0       # casing bottom below output axis
    casing_above: float = 450.0        # casing top above input axis
    flange_d: float = 820.0
    flange_t: float = 110.0
    stub_len: float = 250.0
    shaft_d: float = 320.0
    casing_mass: float = 11500.0       # declared, kg (spec sheet stand-in)
    flange_bolt_holes: int = 10

    @property
    def centre_distance(self) -> float:
        return 0.5 * (self.wheel_d + self.pinion_d)


def generate(p: GearboxParams = GearboxParams()):
    a = p.centre_distance
    x_case0 = p.flange_t + p.stub_len
    x_case1 = x_case0 + p.casing_len
    h = p.casing_below + a + p.casing_above
    casing_solid = Pos(x_case0, 0, -p.casing_below) * Box(p.casing_len, p.casing_w, h, align=(MIN, C, MIN))
    casing_solid = fillet(casing_solid.edges().filter_by_position(Axis.Z, -p.casing_below + h - 1, -p.casing_below + h + 1),
                          radius=150)
    # hollow out so the gears fit inside (envelope wall 60 mm)
    inner = Pos(x_case0 + 60, 0, -p.casing_below + 60) * Box(p.casing_len - 120, p.casing_w - 120, h - 120,
                                                              align=(MIN, C, MIN))
    casing_solid = casing_solid - inner
    casing = part("Casing", "GearboxCasing", casing_solid, "paint_gearbox_blue",
                  "encloses gears and bearings, carries thrust bearing loads to the seating",
                  mass=p.casing_mass, fidelity="envelope")

    xg = x_case0 + 0.5 * p.casing_len - 0.5 * p.face_width
    wheel = part("GearWheel", "GearWheel",
                 cyl_x(0.5 * p.wheel_d, 0, p.face_width, r_in=0.5 * p.shaft_d),
                 "steel_forged_34crnimo6", "output gear: reduces engine speed to propeller speed",
                 xform=translate(xg, 0, 0), diameter=p.wheel_d, thickness=p.face_width)
    pinion = part("Pinion", "Pinion",
                  cyl_x(0.5 * p.pinion_d, -10, p.face_width + 10, r_in=0.3 * p.pinion_d),
                  "steel_forged_34crnimo6", "input gear driven by the main engine",
                  xform=translate(xg, 0, a), diameter=p.pinion_d, thickness=p.face_width + 20)

    out_fl = cyl_x(0.5 * p.flange_d, 0, p.flange_t) + cyl_x(0.5 * p.shaft_d, p.flange_t, x_case0 + 1)
    out_fl = bolt_holes(out_fl, p.flange_bolt_holes, 0.8 * p.flange_d, 45, 0, p.flange_t,
                        phase=180.0 / p.flange_bolt_holes)
    output = part("OutputFlange", "Flange", out_fl, "steel_forged_34crnimo6",
                  "connects the gearbox output shaft to the intermediate shaft",
                  diameter=p.flange_d, thickness=p.flange_t)
    in_fl = cyl_x(0.36 * p.flange_d, x_case1 - 1, x_case1 + p.stub_len) + \
        cyl_x(0.5 * p.flange_d * 0.9, x_case1 + p.stub_len, x_case1 + p.stub_len + p.flange_t)
    inp = part("InputFlange", "Flange", in_fl, "steel_forged_34crnimo6",
               "connects the gearbox input shaft to the engine flexible coupling",
               xform=translate(0, 0, a), diameter=0.9 * p.flange_d, thickness=p.flange_t)

    output.relations["connectedTo"] = ["GearWheel"]
    inp.relations["connectedTo"] = ["Pinion"]
    pinion.relations["connectedTo"] = ["GearWheel"]

    gb = assembly(p.name, "Gearbox", "reduces engine speed and carries propeller thrust",
                  [casing, wheel, pinion, output, inp],
                  length=x_case1 + p.stub_len + p.flange_t)
    gb.attrs["generator"] = f"propulsion.gearbox:{p.name}"
    return gb

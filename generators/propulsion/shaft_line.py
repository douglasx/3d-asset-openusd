"""Shaft line generator: propeller shaft, stern tube with bearings,
intermediate shaft, plummer bearing and coupling bolts.

Local frame: origin on the shaft axis at the aft face of the propeller
shaft's aft flange (where the propeller hub flange bolts on); +x forward.
"""
from __future__ import annotations

from dataclasses import dataclass

from build123d import Box, Pos

from generators.common import (MAX, MIN, C, assembly, bolt_holes, bolt_shape, circle_instances,
                               cyl_x, part)
from marinekit.model import translate


@dataclass(frozen=True)
class ShaftLineParams:
    name: str = "shaft_line_d320"
    prop_shaft_d: float = 320.0
    prop_shaft_bore: float = 110.0      # CPP oil distribution bore
    prop_shaft_len: float = 7000.0
    inter_shaft_d: float = 300.0
    inter_shaft_bore: float = 110.0
    inter_shaft_len: float = 5500.0
    flange_d: float = 820.0
    flange_t: float = 110.0
    coupling_bolts: int = 10
    coupling_bolt_d: float = 42.0
    coupling_bolt_len: float = 260.0
    stern_tube_od: float = 560.0
    stern_tube_id: float = 440.0
    stern_tube_x0: float = 450.0
    stern_tube_len: float = 4200.0
    st_bearing_len: float = 900.0
    plummer_len: float = 600.0


def flanged_shaft(d, bore, length, flange_d, flange_t, n_holes, hole_d, aft_flange=True, fwd_flange=True):
    body = cyl_x(0.5 * d, 0, length, r_in=0.5 * bore)
    if aft_flange:
        f = cyl_x(0.5 * flange_d, 0, flange_t, r_in=0.5 * bore)
        body = body + bolt_holes(f, n_holes, 0.8 * flange_d, hole_d, 0, flange_t, phase=180.0 / n_holes)
    if fwd_flange:
        f = cyl_x(0.5 * flange_d, length - flange_t, length, r_in=0.5 * bore)
        body = body + bolt_holes(f, n_holes, 0.8 * flange_d, hole_d, length - flange_t, length,
                                 phase=180.0 / n_holes)
    return body


def plummer_shape(d_bore, length):
    w, h_base = 2.4 * d_bore, 0.55 * d_bore
    housing = cyl_x(1.0 * d_bore, -0.5 * length, 0.5 * length)
    pedestal = Pos(0, 0, -0.5 * d_bore) * Box(length, w * 0.75, d_bore * 0.9, align=(C, C, MAX))
    foot = Pos(0, 0, -1.3 * d_bore) * Box(length * 1.3, w, h_base * 0.5, align=(C, C, MAX))
    body = housing + pedestal + foot
    return body - cyl_x(0.5 * d_bore + 1.0, -0.5 * length - 1, 0.5 * length + 1)


def generate(p: ShaftLineParams = ShaftLineParams()):
    hole = p.coupling_bolt_d + 3
    ps = part("PropellerShaft", "Shaft",
              flanged_shaft(p.prop_shaft_d, p.prop_shaft_bore, p.prop_shaft_len, p.flange_d, p.flange_t,
                            p.coupling_bolts, hole),
              "steel_forged_34crnimo6", "transmits torque and thrust between propeller and intermediate shaft",
              diameter=p.prop_shaft_d, length=p.prop_shaft_len)

    st_x0, st_x1 = p.stern_tube_x0, p.stern_tube_x0 + p.stern_tube_len
    stern = part("SternTube", "SternTube",
                 cyl_x(0.5 * p.stern_tube_od, st_x0, st_x1, r_in=0.5 * p.stern_tube_id),
                 "steel_s355", "supports the stern bearings and seals the shaft penetration through the hull",
                 diameter=p.stern_tube_od, length=p.stern_tube_len)
    bearings = []
    for nm, x0 in (("SternTubeBearingAft", st_x0 + 50), ("SternTubeBearingFwd", st_x1 - 50 - p.st_bearing_len)):
        b = part(nm, "Bearing",
                 cyl_x(0.5 * p.stern_tube_id - 1, x0, x0 + p.st_bearing_len, r_in=0.5 * p.prop_shaft_d + 1),
                 "whitemetal_babbitt", "radial support of the propeller shaft (oil-lubricated white-metal bush)",
                 diameter=p.prop_shaft_d + 2, length=p.st_bearing_len)
        bearings.append(b)

    x_int = p.prop_shaft_len
    inter = part("IntermediateShaft", "Shaft",
                 flanged_shaft(p.inter_shaft_d, p.inter_shaft_bore, p.inter_shaft_len, p.flange_d, p.flange_t,
                               p.coupling_bolts, hole),
                 "steel_forged_34crnimo6", "transmits torque and thrust from gearbox to propeller shaft",
                 xform=translate(x_int, 0, 0), diameter=p.inter_shaft_d, length=p.inter_shaft_len)

    plummer = part("PlummerBearing", "Bearing", plummer_shape(p.inter_shaft_d, p.plummer_len),
                   "iron_nodular_gjs400", "radial support of the intermediate shaft on its seating",
                   xform=translate(x_int + 0.5 * p.inter_shaft_len, 0, 0),
                   diameter=p.inter_shaft_d + 2, length=p.plummer_len)

    def bolts(name, x):
        b = part(name, "BoltSet", bolt_shape(p.coupling_bolt_d, p.coupling_bolt_len), "steel_bolt_10_9",
                 "clamps the coupling flanges together", count=p.coupling_bolts,
                 diameter=p.coupling_bolt_d, length=p.coupling_bolt_len)
        b.instances = circle_instances("Bolt", p.coupling_bolts, 0.8 * p.flange_d, x=x,
                                       phase=180.0 / p.coupling_bolts)
        return b

    c1 = bolts("CouplingBolts_PS_IS", x_int + p.flange_t)
    ps.relations["connectedTo"] = ["IntermediateShaft"]
    stern.relations["connectedTo"] = ["SternTubeBearingAft", "SternTubeBearingFwd"]
    for b in bearings:
        b.relations["connectedTo"] = ["PropellerShaft"]
    plummer.relations["connectedTo"] = ["IntermediateShaft"]

    sl = assembly(p.name, "ShaftLine", "carries torque and thrust between gearbox and propeller",
                  [ps, stern, *bearings, inter, plummer, c1],
                  length=p.prop_shaft_len + p.inter_shaft_len, diameter=p.prop_shaft_d)
    sl.attrs["generator"] = f"propulsion.shaft_line:{p.name}"
    return sl

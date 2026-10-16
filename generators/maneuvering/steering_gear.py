"""Rotary-vane steering gear with two hydraulic power units (envelope model).

Local frame: origin on the rudder stock axis at the top of the steering-gear
flat; z up, +x forward. The actuator housing is an envelope with a declared
mass; the two pump units are one repeated part (instances keep their own IDs).
"""
from __future__ import annotations

from dataclasses import dataclass

from build123d import Align, Box, Pos, fillet, Axis

from generators.common import assembly, cyl_x, part
from marinekit.model import Instance, rot_y, translate

C, MIN = Align.CENTER, Align.MIN
UP = rot_y(-90)


@dataclass(frozen=True)
class SteeringGearParams:
    name: str = "steering_gear_rv_250knm"
    housing_d: float = 1300.0
    housing_h: float = 700.0
    base_d: float = 1650.0
    base_t: float = 90.0
    bore_d: float = 330.0
    housing_mass: float = 4800.0
    hpu_l: float = 1000.0
    hpu_w: float = 700.0
    hpu_h: float = 900.0
    hpu_y: float = 1600.0
    hpu_x: float = -300.0
    hpu_mass: float = 850.0


def generate(p: SteeringGearParams = SteeringGearParams()):
    housing = cyl_x(0.5 * p.base_d, 0, p.base_t, r_in=0.5 * p.bore_d) + \
        cyl_x(0.5 * p.housing_d, p.base_t, p.base_t + p.housing_h, r_in=0.5 * p.bore_d)
    actuator = part("RotaryVaneActuator", "SteeringActuator", housing, "paint_machinery_grey",
                    "hydraulic rotary vane that turns the rudder stock",
                    mass=p.housing_mass, fidelity="envelope", xform=UP,
                    diameter=p.base_d, length=p.base_t + p.housing_h)
    hpu_shape = fillet(Box(p.hpu_l, p.hpu_w, p.hpu_h, align=(C, C, MIN)).edges().filter_by(Axis.Z), radius=60)
    hpus = part("PowerUnits", "HydraulicPowerUnit", hpu_shape, "paint_machinery_grey",
                "electric-motor-driven pump supplying oil to the actuator (one running, one standby)",
                mass=2 * p.hpu_mass, fidelity="envelope")
    hpus.instances = [Instance("HPU_1", translate(p.hpu_x, p.hpu_y, 0)),
                      Instance("HPU_2", translate(p.hpu_x, -p.hpu_y, 0))]
    hpus.relations["connectedTo"] = ["RotaryVaneActuator"]
    sg = assembly(p.name, "SteeringGear", "turns the rudder on bridge command; redundant power units",
                  [actuator, hpus], length=p.base_t + p.housing_h)
    sg.attrs["generator"] = f"maneuvering.steering_gear:{p.name}"
    return sg

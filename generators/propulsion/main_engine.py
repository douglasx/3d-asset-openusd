"""Medium-speed in-line main engine (envelope model).

Local frame: origin on the crankshaft axis at the aft face of the flexible
coupling (which bolts to the gearbox input flange); +x forward.
Block, sump and turbocharger are envelopes with declared masses; flywheel
and coupling are dimension-true. Cylinder heads are one prototype placed
per cylinder (a PointInstancer), each with its own permanent ID.
"""
from __future__ import annotations

from dataclasses import dataclass

from build123d import Box, Pos, fillet, Axis

from generators.common import (MAX, MIN, C, assembly, cyl_x, part)
from marinekit.model import Instance, rot_z, translate


@dataclass(frozen=True)
class MainEngineParams:
    name: str = "main_engine_8l32"
    cylinders: int = 8
    cyl_pitch: float = 560.0
    block_len: float = 5000.0
    block_w: float = 1800.0
    block_below: float = 650.0          # block bottom below crank axis
    block_above: float = 1450.0
    sump_h: float = 600.0
    head_l: float = 470.0
    head_w: float = 620.0
    head_h: float = 380.0
    flywheel_d: float = 1600.0
    flywheel_t: float = 230.0
    coupling_d: float = 1250.0
    coupling_len: float = 450.0
    turbo_d: float = 900.0
    turbo_len: float = 1300.0
    block_mass: float = 30500.0
    sump_mass: float = 2600.0
    head_mass: float = 620.0            # each
    turbo_mass: float = 2300.0


def generate(p: MainEngineParams = MainEngineParams()):
    x_fly0 = p.coupling_len
    x_fly1 = x_fly0 + p.flywheel_t
    x_blk0 = x_fly1 + 60
    x_blk1 = x_blk0 + p.block_len
    h = p.block_below + p.block_above

    blk = Pos(x_blk0, 0, -p.block_below) * Box(p.block_len, p.block_w, h, align=(MIN, C, MIN))
    blk = fillet(blk.edges().filter_by(Axis.X), radius=120)
    block = part("EngineBlock", "EngineBlock", blk, "paint_engine_green",
                 "frame and cylinder block carrying crankshaft and cylinders",
                 mass=p.block_mass, fidelity="envelope")

    sump_solid = Pos(x_blk0 + 100, 0, -p.block_below) * Box(p.block_len - 200, p.block_w * 0.8, p.sump_h,
                                                            align=(MIN, C, MAX))
    sump = part("OilSump", "OilSump", sump_solid, "paint_engine_green", "lubricating oil reservoir",
                mass=p.sump_mass, fidelity="envelope")

    head_solid = fillet(Box(p.head_l, p.head_w, p.head_h, align=(C, C, MIN)).edges().filter_by(Axis.Z), radius=40)
    heads = part("CylinderHeads", "CylinderHead", head_solid, "paint_engine_green",
                 "closes each cylinder and carries the valves and injector",
                 mass=p.head_mass * p.cylinders, fidelity="envelope")
    first = x_blk0 + 0.5 * (p.block_len - (p.cylinders - 1) * p.cyl_pitch)
    heads.instances = [Instance(f"Head_A{i + 1}", translate(first + i * p.cyl_pitch, 0, p.block_above))
                       for i in range(p.cylinders)]

    turbo_solid = cyl_x(0.5 * p.turbo_d, 0, p.turbo_len * 0.55) + \
        cyl_x(0.36 * p.turbo_d, p.turbo_len * 0.55, p.turbo_len)
    turbo = part("Turbocharger", "Turbocharger", turbo_solid, "steel_cast_gs",
                 "exhaust-driven compressor supplying charge air",
                 mass=p.turbo_mass, fidelity="envelope",
                 xform=translate(x_blk1 - 200, 0, p.block_above + p.head_h + 0.5 * p.turbo_d + 80) @ rot_z(90))

    fly = part("Flywheel", "Flywheel", cyl_x(0.5 * p.flywheel_d, x_fly0, x_fly1, r_in=150),
               "steel_cast_gs", "smooths crankshaft torque; carries the turning gear rim",
               diameter=p.flywheel_d, thickness=p.flywheel_t)
    coup_solid = cyl_x(0.5 * p.coupling_d, 0, p.coupling_len, r_in=200) + cyl_x(0.5 * p.coupling_d - 40, 0, 80)
    coup = part("FlexibleCoupling", "FlexibleCoupling", coup_solid, "steel_cast_gs",
                "damps torsional vibration between engine and gearbox",
                diameter=p.coupling_d, length=p.coupling_len)

    fly.relations["connectedTo"] = ["FlexibleCoupling", "EngineBlock"]
    sump.relations["connectedTo"] = ["EngineBlock"]
    heads.relations["connectedTo"] = ["EngineBlock"]
    turbo.relations["connectedTo"] = ["EngineBlock"]

    me = assembly(p.name, "MainEngine", "converts fuel energy into shaft power",
                  [block, sump, heads, turbo, fly, coup], length=x_blk1)
    me.attrs["generator"] = f"propulsion.main_engine:{p.name}"
    return me

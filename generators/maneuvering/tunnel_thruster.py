"""Transverse tunnel (bow) thruster: tunnel, gear pod with strut, fixed-pitch
propeller and vertical electric motor on a stool.

Local frame: origin at the tunnel centre; local x along the tunnel axis,
z up. The vessel rotates it so local x points to port. The tunnel length is a
parameter the catalogue derives from the hull form, so the tunnel always ends
at the shell.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from build123d import Compound, Pos, Rot

from generators.common import assembly, cone_x, cyl_x, part
from generators.propulsion.propeller_cpp import PropellerParams, blade_shape
from marinekit.model import rot_y

UP = rot_y(-90)


@dataclass(frozen=True)
class TunnelThrusterParams:
    name: str = "tunnel_thruster_d1650"
    tunnel_id: float = 1650.0
    tunnel_wall: float = 20.0
    tunnel_length: float = 6000.0
    prop_d: float = 1560.0
    blades: int = 4
    pod_d: float = 560.0
    pod_len: float = 1000.0
    motor_d: float = 900.0
    motor_h: float = 1500.0
    stool_d: float = 700.0
    stool_h: float = 450.0
    motor_mass: float = 3200.0
    pod_mass: float = 1400.0
    # Placement in the ship frame (mm). Used only to trim the tunnel ends to
    # the hull shell; keep equal to the position in arrangement.yaml.
    ship_x: float | None = None
    ship_z: float | None = None


def _hull_envelope_local(x: float, z: float, half_window: float = 1300.0):
    """Hull outer envelope around station x, expressed in the thruster's local
    frame (local x = ship +y, origin at the tunnel centre)."""
    from build123d import Face, loft
    from generators.hull.hull_block import _section_face
    from generators.hull.hullform import FORM
    xs = [x - half_window, x - 0.5 * half_window, x, x + 0.5 * half_window, x + half_window]
    env = loft([_section_face(FORM, xi, 0.0) for xi in xs])
    return Rot(0, 0, -90) * Pos(-x, 0, -z) * env


def generate(p: TunnelThrusterParams = TunnelThrusterParams()):
    r_in = 0.5 * p.tunnel_id
    r_out = r_in + p.tunnel_wall
    half = 0.5 * p.tunnel_length
    tube = cyl_x(r_out, -half, half, r_in=r_in)
    length = p.tunnel_length
    if p.ship_x is not None:
        from generators.hull.hullform import FORM
        tube = tube & _hull_envelope_local(p.ship_x, p.ship_z)    # ends follow the shell
        length = 2 * FORM.halfbreadth_at(p.ship_x, p.ship_z)      # length on the tunnel axis
    tunnel = part("Tunnel", "ThrusterTunnel", tube, "steel_ah36",
                  "transverse duct through the hull that guides the thruster jet",
                  diameter=2 * r_out, length=length)

    pod = cyl_x(0.5 * p.pod_d, -0.5 * p.pod_len, 0.3 * p.pod_len) + \
        cone_x(0.5 * p.pod_d, 80.0, 0.3 * p.pod_len, 0.5 * p.pod_len + 150.0)
    from build123d import Align, Cylinder
    strut = Pos(-0.15 * p.pod_len, 0, 0) * Cylinder(150.0, r_in + 5.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    gear = part("GearPod", "ThrusterGearbox", pod + strut, "paint_machinery_grey",
                "right-angle gear transferring motor power to the propeller shaft",
                mass=p.pod_mass, fidelity="envelope")

    bp = PropellerParams(name="tt_blade", diameter=p.prop_d, blades=p.blades, hub_diameter=0.36 * p.prop_d,
                         hub_length=0.4 * p.prop_d, pitch_ratio=1.0, expanded_area_ratio=0.5, skew_deg=8.0,
                         palm_diameter=0.17 * p.prop_d, palm_height=25.0, blade_bolts=4, blade_bolt_d=20.0)
    blade = blade_shape(bp)
    hub = cyl_x(0.5 * bp.hub_diameter, 0.3 * p.pod_len - 0.4 * bp.hub_length, 0.3 * p.pod_len)
    xb = 0.3 * p.pod_len - 0.2 * bp.hub_length
    blades = [Pos(xb, 0, 0) * Rot(360.0 * i / p.blades, 0, 0) * blade for i in range(p.blades)]
    prop = part("Propeller", "ThrusterPropeller", Compound([hub, *blades]), "bronze_nab_cu3",
                "fixed-pitch propeller producing transverse thrust in either direction",
                diameter=p.prop_d)

    stool = part("MotorStool", "Foundation",
                 cyl_x(0.5 * p.stool_d, r_out, r_out + p.stool_h, r_in=0.5 * p.stool_d - 25.0),
                 "steel_ah36", "carries the motor on the tunnel and houses the drive coupling",
                 xform=UP, diameter=p.stool_d, length=p.stool_h)
    motor = part("Motor", "ElectricMotor",
                 cyl_x(0.5 * p.motor_d, r_out + p.stool_h, r_out + p.stool_h + p.motor_h),
                 "paint_machinery_grey", "electric drive motor of the thruster",
                 mass=p.motor_mass, fidelity="envelope", xform=UP,
                 diameter=p.motor_d, length=p.motor_h)

    prop.relations["connectedTo"] = ["GearPod"]
    gear.relations["connectedTo"] = ["Tunnel", "MotorStool"]
    motor.relations["connectedTo"] = ["MotorStool"]
    stool.relations["connectedTo"] = ["Tunnel"]
    t = assembly(p.name, "TunnelThruster", "produces transverse thrust at the bow for low-speed manoeuvring",
                 [tunnel, gear, prop, stool, motor], diameter=p.tunnel_id, length=length)
    t.attrs["generator"] = f"maneuvering.tunnel_thruster:{p.name}"
    return t

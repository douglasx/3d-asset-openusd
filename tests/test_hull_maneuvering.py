"""Hull form, hull blocks and maneuvering equipment fit together and survive exchange."""
from pathlib import Path

import numpy as np
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps

from generators.catalog import BOW_THRUSTER_AT
from generators.hull import hull_block as hb
from generators.hull.hullform import FORM
from generators.maneuvering import rudder, tunnel_thruster
from generators.propulsion.propeller_cpp import PropellerParams
from marinekit import geom
from marinekit.ids import IdLock
from marinekit.model import assign_ids
from marinekit.step_io import read_step, write_step


def _area(shape) -> float:
    g = GProp_GProps()
    BRepGProp.SurfaceProperties_s(geom.as_topods(shape), g)
    return g.Mass()


def test_propeller_has_tip_clearance_under_the_stern():
    prop_x, shaft_z = 3000.0, 2000.0
    tip_top = shaft_z + 0.5 * PropellerParams().diameter
    clearance = FORM.keel_z(prop_x) - tip_top
    assert clearance >= 0.15 * PropellerParams().diameter   # common rule of thumb: >= 15-20 % D


def test_rudder_clears_propeller_cone_and_hull():
    p = rudder.RudderParams()
    leading_edge_x = p.balance * p.chord_root
    cone_tip_x = 3000.0 - 0.5 * PropellerParams().hub_length - PropellerParams().cone_length
    assert cone_tip_x - leading_edge_x > 500.0
    blade_top_ship_z = 4400.0
    assert FORM.keel_z(0.0) - blade_top_ship_z >= 100.0


def test_bow_thruster_tunnel_is_trimmed_to_the_hull():
    x, _, z = BOW_THRUSTER_AT
    t = tunnel_thruster.generate(tunnel_thruster.TunnelThrusterParams(tunnel_length=9000.0, ship_x=x, ship_z=z))
    tube = t.find("Tunnel")
    lo, hi = geom.bbox(tube.shape)
    r = 845.0
    widest = max(FORM.halfbreadth_at(x + dx, z + dz) for dx in (-r, 0.0, r) for dz in (-r, 0.0, r))
    narrowest = min(FORM.halfbreadth_at(x + dx, z + dz) for dx in (-r, 0.0, r) for dz in (-r, 0.0, r))
    assert narrowest - 50.0 <= hi[0] <= widest + 50.0   # ends lie on the shell, never beyond it
    assert abs(tube.attrs["length"] * 1000 - 2 * FORM.halfbreadth_at(x, z)) < 1.0


def test_shell_plating_has_uniform_thickness():
    """Volume of a uniform plate equals half its surface area x thickness."""
    for params in (hb.ENGINE_ROOM, hb.BOW):
        shell = hb.generate(params).find("Shell").shape
        assert shell.is_valid
        assert abs(geom.volume(shell) - 0.5 * _area(shell) * params.shell_t) / geom.volume(shell) < 0.06


def test_offset_geometry_survives_step(tmp_path: Path):
    """Offset curves are converted to NURBS: the hollow rudder blade keeps its inner skin."""
    r = rudder.generate()
    assign_ids(r, IdLock(tmp_path / "l.json"), "def:")
    back = read_step(write_step(r, tmp_path / "r.step"))
    a, b = r.find("Blade").shape, back.find("Blade").shape
    assert abs(geom.volume(a) - geom.volume(b)) / geom.volume(a) < 1e-4
    assert abs(_area(a) - _area(b)) / _area(a) < 1e-3

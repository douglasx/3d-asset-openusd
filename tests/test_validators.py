"""The build passes every validator, and each validator catches the fault it exists for."""
import json

import numpy as np
from pxr import Sdf, Usd

from marinekit import validate
from marinekit.paths import LIBRARY
from marinekit.usd_io import get_marine, set_marine

PROP = LIBRARY / "propeller_cpp" / "propeller_cpp_4b" / "propeller_cpp_4b.usda"
SHAFT = LIBRARY / "shaft_line" / "shaft_line_d320" / "shaft_line_d320.usda"


def test_full_build_has_no_validation_errors(built):
    rep = json.loads((built / "reports" / "validation.json").read_text())
    errors = [f for f in rep["findings"] if f["severity"] == "error"]
    assert errors == []
    for check in ["usdchecker", "required_properties", "id_unique_stable", "units_axis", "bbox_vs_parameters",
                  "mass", "vocabulary", "ontology_shacl", "explode_proxy", "triangle_budget", "glb_roundtrip",
                  "step_roundtrip"]:
        assert rep["summary"][check].get("pass", 0) > 0, check


def _session_copy(path):
    """Open the asset with an in-memory session layer so edits never touch disk."""
    stage = Usd.Stage.Open(str(path), Usd.Stage.LoadAll)
    stage.SetEditTarget(stage.GetSessionLayer())
    return stage


def _errors(rep, check):
    return [f for f in rep.findings if f.check == check and f.severity == "error"]


def test_wrong_declared_diameter_is_caught():
    st = _session_copy(SHAFT)
    set_marine(st.GetPrimAtPath("/shaft_line_d320/PropellerShaft"), "diameter", 0.36)  # mesh is 0.32
    rep = validate.Report()
    validate.check_dimensions_and_mass(rep, st)
    assert any("PropellerShaft" in f.where for f in _errors(rep, "bbox_vs_parameters"))


def test_wrong_mass_is_caught():
    st = _session_copy(SHAFT)
    p = st.GetPrimAtPath("/shaft_line_d320/IntermediateShaft")
    set_marine(p, "mass", get_marine(p)["mass"] * 1.2)
    rep = validate.Report()
    validate.check_dimensions_and_mass(rep, st)
    msgs = [f.where for f in _errors(rep, "mass")]
    assert any("IntermediateShaft" in m for m in msgs)
    assert any(m.endswith("/shaft_line_d320") for m in msgs), "roll-up must also break"


def test_explode_overlap_is_caught():
    st = _session_copy(PROP)
    set_marine(st.GetPrimAtPath("/propeller_cpp_4b/Blade_1"), "explodeDist", 0.0)
    set_marine(st.GetPrimAtPath("/propeller_cpp_4b/Blade_1"), "explodeDir", [0, 0, 0])
    set_marine(st.GetPrimAtPath("/propeller_cpp_4b/Cone"), "explodeDist", 0.0)
    rep = validate.Report()
    validate.check_explode_proxy(rep, st)
    assert _errors(rep, "explode_proxy")


def test_missing_proxy_is_caught():
    st = _session_copy(PROP)
    st.GetPrimAtPath("/propeller_cpp_4b/proxy").SetActive(False)
    rep = validate.Report()
    validate.check_explode_proxy(rep, st)
    assert any("no L0 proxy" in f.message for f in _errors(rep, "explode_proxy"))


def test_unknown_material_is_caught():
    st = _session_copy(PROP)
    set_marine(st.GetPrimAtPath("/propeller_cpp_4b/Hub"), "material", "unobtainium")
    rep = validate.Report()
    validate.check_required_and_vocab(rep, st, occurrence=False)
    assert _errors(rep, "vocabulary")


def test_scale_in_transform_is_caught(tmp_path):
    layer = Sdf.Layer.CreateNew(str(tmp_path / "scaled.usda"))
    st = Usd.Stage.Open(layer)
    from pxr import Gf, UsdGeom
    UsdGeom.SetStageMetersPerUnit(st, 0.001)
    x = UsdGeom.Xform.Define(st, "/a")
    x.MakeMatrixXform().Set(Gf.Matrix4d().SetScale(Gf.Vec3d(1000, 1000, 1000)))
    layer.Save()
    m = validate.np_matrix(x.GetLocalTransformation())[:3, :3]
    assert not np.allclose(m @ m.T, np.eye(3))
    assert UsdGeom.GetStageMetersPerUnit(st) != 1.0


def test_mid_section_measure_ignores_end_flanges():
    st = Usd.Stage.Open(str(SHAFT))
    pts, tris = validate.own_and_subtree_mesh(st.GetPrimAtPath("/shaft_line_d320/PropellerShaft"))
    assert abs(validate.measure("mid_section", pts, tris) - 0.32) < 0.32 * validate.DIM_TOL
    assert validate.measure("radial_max", pts, tris) > 0.8   # flange

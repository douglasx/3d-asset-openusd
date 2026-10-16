"""The simulated-CAD path: generator -> STEP -> ingest, including the cases a
real CAD source produces (no IDs in the file, renamed parts)."""
import copy
import shutil

import numpy as np

from generators.propulsion import gearbox, shaft_line
from marinekit import geom
from marinekit.ids import IdLock, check_append_only
from marinekit.ingest import ingest_step
from marinekit.model import assign_ids
from marinekit.step_io import read_step, write_step
from marinekit.paths import ID_LOCK


def _walk(p):
    return dict(p.walk())


def test_step_round_trip_preserves_structure_attrs_and_geometry(tmp_path):
    part = shaft_line.generate()
    lock = IdLock(tmp_path / "lock.json")
    assign_ids(part, lock, "def:")
    step = write_step(part, tmp_path / "sl.step")
    back = read_step(step)
    a, b = _walk(part), _walk(back)
    assert a.keys() == b.keys()
    for k in a:
        pa, pb = a[k], b[k]
        assert pb.attrs["id"] == pa.id
        assert pb.cls == pa.cls
        assert np.allclose(pa.xform, pb.xform, atol=1e-6)
        assert [i.name for i in pa.instances] == [i.name for i in pb.instances]
        assert pa.relations == pb.relations
        if pa.shape is not None:
            assert abs(geom.volume(pa.shape) - geom.volume(pb.shape)) / geom.volume(pa.shape) < 1e-4


def test_step_writer_is_deterministic(tmp_path):
    part = gearbox.generate()
    assign_ids(part, IdLock(tmp_path / "l.json"), "def:")
    a = write_step(part, tmp_path / "a.step").read_bytes()
    write_step(shaft_line.generate(), tmp_path / "other.step")  # disturb process-wide counters
    b = write_step(part, tmp_path / "b.step").read_bytes()
    assert a == b


def _strip_ids(part):
    for _, p in part.walk():
        p.attrs.pop("id", None)
        p.id = ""
    return part


def test_ingest_without_ids_matches_by_name_path(tmp_path):
    """A real CAD export carries no marine:id: IDs come back from the lock by path."""
    lock_copy = tmp_path / "ids.lock.json"
    shutil.copy(ID_LOCK, lock_copy)
    lock = IdLock(lock_copy)
    part = _strip_ids(gearbox.generate())
    step = write_step(part, tmp_path / "gearbox_sr_4mw.step")
    rep = ingest_step(step, lock, tmp_path / "gearbox_sr_4mw.usda")
    assert rep["new"] == [] and rep["missing"] == [] and rep["conflicts"] == []
    assert lock.lookup("def:gearbox_sr_4mw/Pinion") == IdLock(ID_LOCK).lookup("def:gearbox_sr_4mw/Pinion")


def test_renamed_part_gets_new_id_and_old_is_flagged_not_deleted(tmp_path):
    lock_copy = tmp_path / "ids.lock.json"
    shutil.copy(ID_LOCK, lock_copy)
    lock = IdLock(lock_copy)
    part = _strip_ids(gearbox.generate())
    part.find("Pinion").name = "InputPinion"
    step = write_step(part, tmp_path / "gearbox_sr_4mw.step")
    rep = ingest_step(step, lock, tmp_path / "gearbox_sr_4mw.usda")
    assert "def:gearbox_sr_4mw/InputPinion" in rep["new"]
    assert "def:gearbox_sr_4mw/Pinion" in rep["missing"]
    assert lock.lookup("def:gearbox_sr_4mw/Pinion") is not None        # kept for review


def test_conflicting_file_id_is_reported(tmp_path):
    lock_copy = tmp_path / "ids.lock.json"
    shutil.copy(ID_LOCK, lock_copy)
    lock = IdLock(lock_copy)
    part = gearbox.generate()
    assign_ids(part, IdLock(lock_copy), "def:")
    part.find("Pinion").attrs["id"] = "00000000-0000-4000-8000-000000000000"
    step = write_step(part, tmp_path / "gearbox_sr_4mw.step")
    rep = ingest_step(step, lock, tmp_path / "gearbox_sr_4mw.usda")
    assert any(c["key"] == "def:gearbox_sr_4mw/Pinion" for c in rep["conflicts"])


def test_append_only_lock_check():
    base = {"a": "1", "b": "2"}
    assert check_append_only(base, {"a": "1", "b": "2", "c": "3"}) == []
    errs = check_append_only(base, {"a": "9", "c": "9"})
    assert any("changed" in e for e in errs)
    assert any("removed" in e for e in errs)
    assert any("reused" in e for e in errs)

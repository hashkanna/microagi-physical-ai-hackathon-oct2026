"""Acceptance checks for the physics task: baseline succeeds, alternate layout succeeds with different
targets, unreachable layout fails for the right reason, and the compiler enforces the physical rules."""

import copy
import json
from pathlib import Path

import mujoco
import pytest

from cellcheck.compiler import SceneError, compile_scene
from cellcheck.schema import Scene
from cellcheck.sim import run_task

SCENES = Path(__file__).resolve().parent.parent / "scenes"


def load(name: str) -> Scene:
    return Scene.model_validate(json.loads((SCENES / f"{name}.json").read_text()))


def checks(res):
    return {c.id: c.status for c in res.checks.values()}


@pytest.mark.parametrize("name", ["baseline", "alt_layout"])
def test_layouts_succeed(name):
    res = run_task(load(name))
    assert res.success, res.failure_reason
    assert set(checks(res).values()) == {"pass"}
    assert res.metrics["block_lifted_m"] > 0.1  # the block really was lifted by the gripper


def test_targets_follow_the_scene():
    a = run_task(load("baseline")).waypoints
    b = run_task(load("alt_layout")).waypoints
    assert a[-1]["target"][:2] != b[-1]["target"][:2]
    assert a[-1]["target"][:2] == pytest.approx([0.74, 0.30], abs=1e-3)


def test_unreachable_fails_on_reach_and_halts():
    res = run_task(load("unreachable"))
    c = checks(res)
    assert not res.success
    assert c["reachable"] == "fail" and c["placement"] == "fail"
    assert c["collisions"] == "pass"  # halting before the move avoids driving into the table
    assert "halted_before_stage" in res.metrics


def test_only_block_is_free_and_bins_are_open_boxes():
    m = mujoco.MjModel.from_xml_string(compile_scene(load("baseline")).xml)
    free = [m.joint(j).name for j in range(m.njnt) if m.jnt_type[j] == mujoco.mjtJoint.mjJNT_FREE]
    assert free == ["block_1_free"]
    bin_geoms = [m.geom(g).name for g in range(m.ngeom) if m.geom(g).name.startswith("bin_b_")]
    assert sorted(bin_geoms) == sorted(["bin_b_floor", "bin_b_wall_front", "bin_b_wall_back", "bin_b_wall_left", "bin_b_wall_right"])
    blk = m.geom("block_1_geom")
    assert list(blk.size) == pytest.approx([0.0225] * 3)  # full 4.5 cm -> half extents


def test_overlapping_bins_rejected():
    s = load("baseline")
    s2 = copy.deepcopy(s)
    s2.obj("bin_a").x = s2.obj("bin_b").x
    with pytest.raises(SceneError, match="overlap"):
        compile_scene(s2)


def test_block_off_table_rejected():
    s = load("baseline")
    s.obj("block_1").x = -0.2
    with pytest.raises(SceneError, match="off the tabletop"):
        compile_scene(s)

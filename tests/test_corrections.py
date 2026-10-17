import json
import math
from types import SimpleNamespace

import pytest

from cellcheck.corrections import CorrectionError, InvalidCorrection, apply_patch, interpret, parse_llm, parse_rules
from cellcheck.schema import Patch, PatchOp, Scene


@pytest.fixture
def scene():
    return Scene.model_validate({
        "table": {"width": 1.0, "depth": 0.7},
        "robot": {"x": 0.5, "y": 0.6},
        "objects": [
            {"id": "block_1", "kind": "block", "label": "red cube", "x": 0.25, "y": 0.3, "size": [0.05, 0.05, 0.05]},
            {"id": "bin_a", "kind": "bin", "label": "grey tray", "x": 0.45, "y": 0.3, "size": [0.25, 0.18, 0.07]},
            {"id": "bin_b", "kind": "bin", "label": "blue tray", "x": 0.72, "y": 0.3, "size": [0.28, 0.2, 0.08]},
        ],
        "task": {"object_id": "block_1", "target_id": "bin_b"},
    })


CASES = [
    ("Move the target bin 20 centimetres toward the table's left edge.", "bin_b", {"x": 0.52}),
    ("move bin_a 5 cm back", "bin_a", {"y": 0.35}),
    ("shift the block 3cm to the right", "block_1", {"x": 0.28}),
    ("Move the blue tray 2 inches to the left", "bin_b", {"x": 0.72 - 0.0508}),
    ("move the grey tray 40 mm toward the front edge", "bin_a", {"y": 0.26}),
    ("nudge the cube 0.05 m towards the rear", "block_1", {"y": 0.35}),
    ("rotate the source bin by 30 degrees", "bin_a", {"yaw_deg": 30.0}),
    ("rotate bin_b 15° clockwise", "bin_b", {"yaw_deg": -15.0}),
    ("turn the block to 45 degrees", "block_1", {"yaw_deg": 45.0}),
    ("the target bin is 30 cm wide", "bin_b", {"size": (0.3, 0.2, 0.08)}),
    ("make the block 5 cm tall", "block_1", {"size": (0.05, 0.05, 0.05)}),
    ("the block is a 4 cm cube", "block_1", {"size": (0.04, 0.04, 0.04)}),
    ("set the depth of bin_a to 150 mm", "bin_a", {"size": (0.25, 0.15, 0.07)}),
    ("set block mass to 200 g", "block_1", {"mass": 0.2}),
    ("the cube weighs 0.3 kg", "block_1", {"mass": 0.3}),
    ("set block friction to 0.6", "block_1", {"friction": 0.6}),
    ("the grey tray walls are 5 mm thick", "bin_a", {"wall_thickness": 0.005}),
    ("move bin_b to x = 0.6", "bin_b", {"x": 0.6}),
    ("rename bin_a to grey crate", "bin_a", {"label": "grey crate"}),
]


@pytest.mark.parametrize("text, oid, expected", CASES)
def test_rule_phrasings(scene, text, oid, expected):
    patch, explanation, parser = interpret(text, scene)
    assert parser == "rules" and explanation
    after = apply_patch(scene, patch)
    o = after.obj(oid)
    for field, val in expected.items():
        assert getattr(o, field) == pytest.approx(val, abs=1e-6), field
        assert o.provenance[field] == "user_confirmed"
    for other in scene.objects:  # unrelated objects preserved exactly
        if other.id != oid:
            assert after.obj(other.id) == other
    assert after.table == scene.table and after.robot == scene.robot and after.task == scene.task


def test_example_explanation(scene):
    _, explanation, _ = interpret("Move the target bin 20 centimetres toward the table's left edge.", scene)
    assert explanation == "Moved bin_b (target bin) 0.20 m toward the left edge: x 0.72 m → 0.52 m"


def test_toward_robot_moves_along_vector_to_base(scene):
    after = apply_patch(scene, parse_rules("move the cube 10 cm toward the robot", scene))
    b0, b1 = scene.obj("block_1"), after.obj("block_1")
    assert math.hypot(b1.x - b0.x, b1.y - b0.y) == pytest.approx(0.10, abs=1e-5)
    d0 = math.hypot(scene.robot.x - b0.x, scene.robot.y - b0.y)
    assert math.hypot(scene.robot.x - b1.x, scene.robot.y - b1.y) == pytest.approx(d0 - 0.10, abs=1e-5)


def test_table_robot_and_task_targets(scene):
    after = apply_patch(scene, parse_rules("the table is 1.2 m wide", scene))
    assert after.table.width == 1.2 and after.table.provenance["width"] == "user_confirmed"
    assert after.objects == scene.objects
    after = apply_patch(scene, parse_rules("place the block in bin_a", scene))
    assert after.task.target_id == "bin_a" and after.objects == scene.objects
    assert apply_patch(scene, parse_rules("make the grey tray the target", scene)).task.target_id == "bin_a"
    after = apply_patch(scene, parse_rules("move the robot 5 cm to the right", scene))
    assert after.robot.x == pytest.approx(0.55) and after.robot.provenance["x"] == "user_confirmed"


@pytest.mark.parametrize("text, match", [
    ("make the block 30 cm tall", "too large"),
    ("the target bin is 90 cm wide", "size"),
    ("move bin_b 1 m right", "off the table"),
    ("the table is 0.5 m wide", "off the table"),
    ("set the blue tray mass to 200 g", "bins are fixed"),
])
def test_invalid_results_rejected(scene, text, match):
    with pytest.raises(InvalidCorrection, match=match):
        interpret(text, scene)


@pytest.mark.parametrize("text", ["frobnicate the widget", "move the bin 5 cm left", "move the block left", ""])
def test_not_understood(scene, text, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(CorrectionError):
        interpret(text, scene)


def test_apply_patch_guards(scene):
    for op in [PatchOp(target="bin_z", field="x", value=0.1), PatchOp(target="table", field="label", value="x"),
               PatchOp(target="block_1", field="x", value="left"), PatchOp(target="block_1", field="size", value=[0.1]),
               PatchOp(target="task", field="target_id", value="block_1")]:
        with pytest.raises(InvalidCorrection):
            apply_patch(scene, Patch(ops=[op]))
    assert scene.obj("block_1").x == 0.25  # input never mutated


def test_parse_llm_with_fake_client(scene, monkeypatch):
    calls = {}
    reply = {"ok": True, "reason": "", "summary": "Moved bin_b left by 0.2 m",
             "ops": [{"target": "bin_b", "field": "x", "op": "add", "number": -0.2, "vector": None, "text": None}]}

    class FakeOpenAI:
        def __init__(self, **kw):
            self.responses = SimpleNamespace(create=self.create)

        def create(self, **kw):
            calls.update(kw)
            return SimpleNamespace(output_text=json.dumps(reply))

    import openai
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    patch, explanation, parser = interpret("scoot the destination thingy a fifth of a metre leftwards", scene)
    assert parser == "llm" and patch.ops[0].value == -0.2
    assert "x 0.72 m → 0.52 m" in explanation
    fmt = calls["text"]["format"]
    assert fmt["strict"] is True and fmt["type"] == "json_schema"
    assert fmt["schema"]["properties"]["ops"]["items"]["properties"]["target"]["enum"] == ["block_1", "bin_a", "bin_b", "table", "robot", "task"]
    reply.update(ok=False, reason="unsupported", ops=[])
    with pytest.raises(CorrectionError, match="unsupported"):
        parse_llm("paint it green", scene)

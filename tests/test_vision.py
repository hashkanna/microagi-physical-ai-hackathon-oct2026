import copy
import json
import os
from types import SimpleNamespace

import pytest
from PIL import Image, ImageDraw

from cellcheck import vision
from cellcheck.vision import (EXTRACTION_SCHEMA, VisionError, draft_scene_from_extraction, extract_scene,
                              fixture_extraction, save_raw)

CORNERS = [[170, 660], [1110, 660], [935, 255], [345, 255]]
TRUTH = {"block_1": (0.24, 0.30), "bin_a": (0.52, 0.27), "bin_b": (0.79, 0.31)}


def test_fixture_is_marked_and_drafts_a_valid_scene():
    fx = fixture_extraction()
    assert fx["fixture"] is True
    scene, warnings = draft_scene_from_extraction(fx)
    assert [o.id for o in scene.objects] == ["block_1", "bin_a", "bin_b"]
    assert scene.obj("bin_a").label == "grey tray" and scene.obj("bin_b").label == "blue tray"
    assert scene.task.target_id == "bin_b"  # model marked the blue tray as target
    assert scene.notes[0].startswith("FIXTURE DATA")
    assert scene.table.provenance["width"] == "inferred"
    assert scene.obj("block_1").provenance["x"] == "inferred"
    assert scene.robot.provenance["x"] == "default" and scene.robot.yaw_deg == -90
    assert scene.robot.y == pytest.approx(scene.table.depth - 0.1)
    assert any("model's estimated table corners" in w for w in warnings)
    assert any("laptop" in w for w in warnings)


def test_user_corners_and_measurements_recover_positions():
    scene, warnings = draft_scene_from_extraction(fixture_extraction(), table_width=1.0, table_depth=0.7, corners_px=CORNERS)
    assert scene.table.provenance["width"] == "measured" and scene.table.width == 1.0
    for oid, (x, y) in TRUTH.items():
        assert (scene.obj(oid).x, scene.obj(oid).y) == pytest.approx((x, y), abs=0.01)
    assert not any("corners" in w for w in warnings)


def test_missing_corners_fall_back_to_metric_guess():
    fx = copy.deepcopy(fixture_extraction())
    fx["result"]["table"]["corners_px"]["back_left"] = None
    scene, warnings = draft_scene_from_extraction(fx)
    assert (scene.obj("block_1").x, scene.obj("block_1").y) == (0.26, 0.28)
    assert any("Not all table corners" in w for w in warnings)


def test_missing_objects_get_labelled_placeholders():
    fx = copy.deepcopy(fixture_extraction())
    fx["result"]["objects"] = [o for o in fx["result"]["objects"] if o["label"] == "grey tray"]
    scene, warnings = draft_scene_from_extraction(fx)
    placeholders = [o for o in scene.objects if o.label.startswith("placeholder")]
    assert {o.kind for o in placeholders} == {"block", "bin"} and len(placeholders) == 2
    assert all(o.provenance["x"] == "default" for o in placeholders)
    assert sum("PLACEHOLDER" in w for w in warnings) == 2
    assert scene.task.target_id == "bin_b" and any("no target identified" in n for n in scene.notes)


def test_extra_objects_and_bad_sizes_warn():
    fx = copy.deepcopy(fixture_extraction())
    objs = fx["result"]["objects"]
    objs.append(dict(objs[1], label="green bin", confidence=0.3, is_target_bin=False))
    objs[0]["size_m"] = [0.5, 0.05, 0.05]
    scene, warnings = draft_scene_from_extraction(fx)
    assert any("Ignored extra bin 'green bin'" in w for w in warnings)
    assert any("clipped" in w for w in warnings) and max(scene.obj("block_1").size) <= 0.2
    assert all(f"Warning: {w}" in scene.notes for w in warnings)


def _walk(schema):
    yield schema
    for v in schema.get("properties", {}).values():
        yield from _walk(v)
    for v in schema.get("anyOf", []):
        yield from _walk(v)
    if isinstance(schema.get("items"), dict):
        yield from _walk(schema["items"])


def test_schema_is_strict_compatible():
    for s in _walk(EXTRACTION_SCHEMA):
        if s.get("type") == "object":
            assert s["additionalProperties"] is False and set(s["required"]) == set(s["properties"])


def _frames(tmp_path, n=2):
    paths = []
    for i in range(n):
        im = Image.new("RGB", (640, 360), (200, 200, 200))
        d = ImageDraw.Draw(im)
        d.polygon([(85, 330), (555, 330), (467, 128), (172, 128)], fill=(150, 110, 70))
        d.rectangle((210, 200, 235, 225), fill=(220, 30, 30))
        d.rectangle((300, 190, 380, 240), outline=(120, 120, 120), width=5)
        d.rectangle((420, 190, 500, 240), outline=(30, 60, 200), width=5)
        paths.append(tmp_path / f"frame_{i:02d}.jpg")
        im.save(paths[-1])
    return paths


def test_extract_scene_requires_key(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(VisionError, match="OPENAI_API_KEY"):
        extract_scene(_frames(tmp_path))


def test_extract_scene_request_shape_with_fake_client(monkeypatch, tmp_path):
    calls = {}
    result = copy.deepcopy(fixture_extraction()["result"])
    result["overview_frame"] = 1

    class FakeOpenAI:
        def __init__(self, **kw):
            self.responses = SimpleNamespace(create=self.create)

        def create(self, **kw):
            calls.update(kw)
            return SimpleNamespace(output_text=json.dumps(result), id="resp_1", status="completed", output=[])

    import openai
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_VISION_MODEL", "test-model")
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    out = extract_scene([{"index": i, "path": str(p)} for i, p in enumerate(_frames(tmp_path))])
    assert out["fixture"] is False and out["model"] == "test-model" == calls["model"]
    assert out["frames"][1]["width"] == 640 and out["result"]["overview_frame"] == 1
    assert calls["text"]["format"]["strict"] is True and calls["text"]["format"]["schema"] is EXTRACTION_SCHEMA
    images = [c for c in calls["input"][0]["content"] if c["type"] == "input_image"]
    assert len(images) == 2 and images[0]["image_url"].startswith("data:image/jpeg;base64,")
    assert "sk-test" not in json.dumps(out)
    saved = save_raw(out, tmp_path / "raw" / "extraction.json")
    assert json.loads(saved.read_text())["raw_text"] == out["raw_text"]
    result["overview_frame"] = 7
    with pytest.raises(VisionError, match="overview_frame 7"):
        extract_scene(_frames(tmp_path))


@pytest.mark.skipif(not os.environ.get("OPENAI_API_KEY"), reason="OPENAI_API_KEY not set (live OpenAI call)")
def test_live_extraction(tmp_path):
    out = extract_scene(_frames(tmp_path))
    assert out["result"]["objects"] and out["elapsed_s"] > 0
    scene, _ = draft_scene_from_extraction(out)
    assert len(scene.objects) == 3


def test_default_model_is_configurable(monkeypatch):
    monkeypatch.delenv("OPENAI_VISION_MODEL", raising=False)
    assert vision.vision_model() == vision.DEFAULT_MODEL
    monkeypatch.setenv("OPENAI_VISION_MODEL", "x")
    assert vision.vision_model() == "x"

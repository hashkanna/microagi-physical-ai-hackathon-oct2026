"""Frames -> structured workcell estimate (OpenAI vision, strict JSON schema) -> draft Scene.

The model output is an INITIAL ESTIMATE. A schema-valid response says nothing about spatial
accuracy, so every value it produces is tagged provenance "inferred", every gap or fallback is
reported as a warning and a scene note, and the fixture is never substituted silently.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Sequence

from PIL import Image

from . import calibrate
from .frames import frame_bytes
from .schema import Scene

DEFAULT_MODEL = "gpt-6.1-sol"
FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "extraction_fixture.json"
ROBOT_BACK_OFFSET = 0.10


class VisionError(RuntimeError):
    """The vision call could not produce a usable estimate."""


def vision_model() -> str:
    return os.environ.get("OPENAI_VISION_MODEL") or DEFAULT_MODEL


def openai_configured() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


# ---- response schema (strict structured outputs) -------------------------------------------

def _num(desc: str, **kw) -> dict:
    return {"type": "number", "description": desc, **kw}


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


_PX = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
       "description": "[u, v] pixel coordinate in the overview frame (u right, v down, full frame resolution)"}

EXTRACTION_SCHEMA = _obj({
    "overview_frame": {"type": "integer", "description": "Index of the frame that best shows all four tabletop corners"},
    "table": _obj({
        "corners_px": _obj({k: _nullable(_PX) for k in ("front_left", "front_right", "back_right", "back_left")}),
        "width_m": _num("Estimated full tabletop extent left->right, metres"),
        "depth_m": _num("Estimated full tabletop extent front->back, metres"),
        "confidence": _num("0-1", minimum=0, maximum=1),
    }),
    "objects": {"type": "array", "items": _obj({
        "label": {"type": "string", "description": "Short visual name, e.g. 'red cube', 'blue tray'"},
        "kind": {"type": "string", "enum": ["block", "bin", "other"]},
        "is_target_bin": {"type": "boolean", "description": "True only if this bin is evidently where the block should go"},
        "size_m": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                   "description": "Approximate FULL extents [sx, sy, sz] in metres; sx along the object's own x before yaw"},
        "footprint_center_px": _nullable(_PX),
        "position_m": {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2,
                       "description": "Fallback [x, y] of the footprint centre in the table frame, metres"},
        "yaw_deg": _num("Rotation about vertical, degrees counter-clockwise seen from above, 0 = sides parallel to the table edges"),
        "color_rgb": _nullable({"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
                                "description": "Dominant colour, each channel 0-1"}),
        "confidence": _num("0-1", minimum=0, maximum=1),
        "notes": {"type": "string", "description": "One short sentence, e.g. occlusion or uncertainty"},
    })},
    "notes": {"type": "string", "description": "Short caveats about the whole estimate"},
})

INSTRUCTIONS = """You estimate the layout of a small robot workcell from phone-video frames.
The scene should contain one rectangular table, two open bins/trays and one small block/cube.

Table frame (use it for every metric value): origin at the FRONT-LEFT corner of the tabletop,
+x along the front edge toward the RIGHT edge, +y toward the BACK edge, metres. In the overview
frame, "front" is the table edge nearest the camera; left/right are as seen in that frame.

Rules:
- Pick overview_frame = the frame index that shows all four tabletop corners most clearly.
- corners_px and footprint_center_px are pixel coordinates IN THAT FRAME at the full resolution
  stated in its label (u to the right, v down). Use null for a corner you cannot see.
- footprint_center_px is where the centre of the object's base touches the tabletop, not the
  centre of its visible top.
- Sizes are FULL extents in metres. Use typical sizes if unsure and say so in notes.
- Report each physical object once, including anything that is not a block or bin (kind "other").
- is_target_bin is true only if something visible or the user's hint says the block goes there.
- Never invent objects you cannot see; give low confidence when unsure."""


def _image_entries(frames: Sequence[Any], detail: str) -> tuple[list[dict], list[dict]]:
    content, meta = [], []
    for i, f in enumerate(frames):
        path = Path(f["path"] if isinstance(f, dict) else f)
        idx = f.get("index", i) if isinstance(f, dict) else i
        with Image.open(path) as im:
            w, h = im.size
        meta.append({"index": idx, "path": str(path), "width": w, "height": h})
        b64 = base64.b64encode(frame_bytes(path)).decode()
        content.append({"type": "input_text", "text": f"Frame {idx} ({w}x{h} px):"})
        content.append({"type": "input_image", "image_url": f"data:image/jpeg;base64,{b64}", "detail": detail})
    return content, meta


def extract_scene(frame_paths: Sequence[Any], *, model: str | None = None, task_hint: str | None = None,
                  detail: str | None = None, timeout: float = 120.0) -> dict:
    """Ask an OpenAI vision model for a structured estimate of the workcell.

    `frame_paths` are image paths or the dicts returned by `frames.sample_frames`.
    Returns {"fixture": False, "model", "elapsed_s", "frames": [...], "result": {...}, "raw_text"}.
    """
    if not openai_configured():
        raise VisionError("OPENAI_API_KEY is not set; vision extraction is unavailable (use the fixture explicitly)")
    if not frame_paths:
        raise VisionError("no frames to analyse")
    from openai import OpenAI, OpenAIError

    model = model or vision_model()
    detail = detail or os.environ.get("OPENAI_VISION_DETAIL", "auto")
    content, meta = _image_entries(frame_paths, detail)
    hint = f"\nUser's task hint: {task_hint.strip()}" if task_hint else ""
    content.insert(0, {"type": "input_text", "text": f"{len(meta)} frames follow.{hint}"})
    t0 = time.monotonic()
    try:
        resp = OpenAI(timeout=timeout).responses.create(
            model=model,
            instructions=INSTRUCTIONS,
            input=[{"role": "user", "content": content}],
            text={"format": {"type": "json_schema", "name": "workcell_estimate", "schema": EXTRACTION_SCHEMA, "strict": True}},
        )
    except OpenAIError as e:
        raise VisionError(f"OpenAI request failed ({type(e).__name__}): {getattr(e, 'message', None) or e}") from None
    elapsed = round(time.monotonic() - t0, 2)
    raw = resp.output_text
    if not raw:
        refusal = next((c.refusal for item in resp.output if getattr(item, "type", "") == "message"
                        for c in item.content if getattr(c, "type", "") == "refusal"), None)
        raise VisionError(f"model returned no estimate (status={resp.status}{', refusal: ' + refusal if refusal else ''})")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as e:
        raise VisionError(f"model output is not valid JSON: {e}") from None
    if result["overview_frame"] not in [m["index"] for m in meta]:
        raise VisionError(f"model chose overview_frame {result['overview_frame']}, which is not one of the frames sent")
    return {"fixture": False, "model": model, "response_id": resp.id, "elapsed_s": elapsed,
            "frames": meta, "result": result, "raw_text": raw}


def fixture_extraction() -> dict:
    """Canned model response for offline demos/tests. Always carries "fixture": true."""
    data = json.loads(FIXTURE_PATH.read_text())
    assert data.get("fixture") is True, "fixture file must be marked as fixture"
    return data


def save_raw(extraction: dict, path: str | Path) -> Path:
    """Persist an extraction (including the raw model text) as evidence."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(extraction, indent=2))
    return p


# ---- extraction -> draft Scene --------------------------------------------------------------

_DEFAULT_TABLE = (1.0, 0.7)
_BLOCK_SIZE = (0.02, 0.2)  # (min, max) per side
_BIN_SIZE = (0.06, 0.8)
_BIN_HEIGHT = (0.02, 0.4)


def _clip_size(size, kind: str, label: str, warnings: list[str]) -> list[float]:
    lo, hi = _BLOCK_SIZE if kind == "block" else _BIN_SIZE
    bounds = [(lo, hi)] * 3 if kind == "block" else [(lo, hi), (lo, hi), _BIN_HEIGHT]
    out = [min(max(float(s), a), b) for s, (a, b) in zip(size, bounds)]
    if out != [float(s) for s in size]:
        warnings.append(f"{label}: estimated size {[round(float(s), 3) for s in size]} m clipped to {[round(s, 3) for s in out]} m")
    return out


def _dim(user: float | None, guess: float | None, name: str, default: float, warnings: list[str]) -> tuple[float, str]:
    if user is not None:
        return float(user), "measured"
    if guess is not None and 0.2 < guess <= 3.0:
        return float(guess), "inferred"
    warnings.append(f"Table {name} estimate {guess!r} unusable; using default {default} m. Measure it.")
    return default, "default"


def _placeholder(kind: str, oid: str, x: float, y: float) -> dict:
    size = [0.05, 0.05, 0.05] if kind == "block" else [0.25, 0.18, 0.08]
    return {"id": oid, "kind": kind, "label": f"placeholder {kind} (not detected)", "x": x, "y": y, "yaw_deg": 0.0,
            "size": size, "provenance": {k: "default" for k in ("x", "y", "yaw_deg", "size", "label")}}


def draft_scene_from_extraction(extraction: dict, *, table_width: float | None = None, table_depth: float | None = None,
                                corners_px: Sequence[Sequence[float]] | None = None) -> tuple[Scene, list[str]]:
    """Build a validated draft Scene from an extraction (vision or fixture) plus optional calibration.

    `corners_px` are user-clicked corners on the extraction's overview frame (front-left,
    front-right, back-right, back-left). Without them, the model's own corner estimate is used
    if complete; otherwise objects fall back to the model's metric position guesses.
    """
    res = extraction.get("result", extraction)
    is_fixture = bool(extraction.get("fixture"))
    warnings: list[str] = []
    notes: list[str] = []
    if is_fixture:
        notes.append("FIXTURE DATA: this scene comes from a canned example response, not from your video.")
    tab = res.get("table") or {}
    W, wp = _dim(table_width, tab.get("width_m"), "width", _DEFAULT_TABLE[0], warnings)
    D, dp = _dim(table_depth, tab.get("depth_m"), "depth", _DEFAULT_TABLE[1], warnings)
    notes.append(f"Table {W:.2f} x {D:.2f} m ({wp} width, {dp} depth); height is a default 0.75 m.")

    # pixel -> table mapping
    H, src = None, None
    model_corners = [(tab.get("corners_px") or {}).get(k) for k in ("front_left", "front_right", "back_right", "back_left")]
    if corners_px is not None:
        H, src = calibrate.homography(corners_px, W, D), "your clicked table corners"
    elif all(c is not None for c in model_corners):
        try:
            H, src = calibrate.homography(model_corners, W, D), "the model's own corner estimate (unverified)"
            warnings.append("Positions use the model's estimated table corners; click the corners to calibrate.")
        except calibrate.CalibrationError as e:
            warnings.append(f"Model corner estimate rejected ({e}); using the model's metric position guesses.")
    else:
        warnings.append("Not all table corners were located; using the model's metric position guesses. Click the corners to calibrate.")
    if H is not None:
        notes.append(f"Object positions projected from footprint pixels via a homography from {src}.")

    def place(o: dict) -> tuple[float, float]:
        px = o.get("footprint_center_px")
        if H is not None and px is not None:
            try:
                x, y = calibrate.pixel_to_table(H, *px)
                if -0.05 <= x <= W + 0.05 and -0.05 <= y <= D + 0.05:
                    return round(x, 4), round(y, 4)
                warnings.append(f"{o['label']}: footprint maps off the table ({x:.2f}, {y:.2f}); using the model's metric guess.")
            except calibrate.CalibrationError as e:
                warnings.append(f"{o['label']}: {e}; using the model's metric guess.")
        elif H is not None:
            warnings.append(f"{o['label']}: no footprint pixel; using the model's metric guess.")
        x, y = o.get("position_m") or (W / 2, D / 2)
        return round(float(x), 4), round(float(y), 4)

    objs = sorted(res.get("objects") or [], key=lambda o: -float(o.get("confidence", 0)))
    blocks = [o for o in objs if o.get("kind") == "block"]
    bins = [o for o in objs if o.get("kind") == "bin"]
    for o in blocks[1:] + bins[2:]:
        warnings.append(f"Ignored extra {o['kind']} '{o.get('label')}' (confidence {o.get('confidence', 0):.2f}); the MVP simulates 1 block + 2 bins.")
    for o in objs:
        if o.get("kind") == "other":
            warnings.append(f"'{o.get('label')}' is not simulated (not a block or bin); check it is not an obstacle.")

    def to_obj(o: dict, kind: str) -> dict:
        x, y = place(o)
        out = {"kind": kind, "label": str(o.get("label", ""))[:60], "x": x, "y": y, "yaw_deg": float(o.get("yaw_deg", 0.0)),
               "size": _clip_size(o["size_m"], kind, o.get("label", kind), warnings),
               "provenance": {k: "inferred" for k in ("x", "y", "yaw_deg", "size", "label")}}
        if o.get("color_rgb"):
            out["color"] = [min(max(float(c), 0.0), 1.0) for c in o["color_rgb"]]
        if o.get("confidence", 1) < 0.5:
            warnings.append(f"Low confidence ({o['confidence']:.2f}) for '{o.get('label')}'; please check it.")
        return out

    block = to_obj(blocks[0], "block") if blocks else None
    chosen_bins = [(to_obj(o, "bin"), bool(o.get("is_target_bin"))) for o in bins[:2]]
    if block is None:
        block = _placeholder("block", "block_1", 0.25 * W, 0.35 * D)
        warnings.append("No block detected: inserted a PLACEHOLDER block at a default position. Edit or re-capture.")
    while len(chosen_bins) < 2:
        x = (0.55 if not chosen_bins else 0.8) * W
        if chosen_bins and abs(chosen_bins[0][0]["x"] - x) < 0.3:
            x = 0.25 * W if chosen_bins[0][0]["x"] > W / 2 else 0.8 * W
        chosen_bins.append((_placeholder("bin", "", x, 0.35 * D), False))
        warnings.append("Fewer than two bins detected: inserted a PLACEHOLDER bin at a default position. Edit or re-capture.")
    block["id"] = "block_1"
    chosen_bins.sort(key=lambda b: b[0]["x"])
    for (b, _), oid in zip(chosen_bins, ("bin_a", "bin_b")):
        b["id"] = oid
    targets = [b["id"] for b, is_t in chosen_bins if is_t]
    target = targets[0] if targets else "bin_b"
    if len(targets) > 1:
        warnings.append("The model marked both bins as target; using bin_a. Confirm the target.")
    notes.append(f"Task: place block_1 in {target}" + (" (identified by the model)." if targets else
                 " (no target identified; defaulted to the right-hand bin)."))
    notes.append(f"Robot base placed by default at the back-edge centre, {ROBOT_BACK_OFFSET:.2f} m in from the back edge, facing the front.")
    notes.append("Block mass/friction and bin wall thickness are defaults, not measurements.")
    if res.get("notes"):
        notes.append(f"Model notes: {res['notes']}")
    notes += [f"Warning: {w}" for w in warnings]

    scene = Scene.model_validate({
        "name": "fixture workcell" if is_fixture else "captured workcell",
        "notes": notes,
        "table": {"width": W, "depth": D, "provenance": {"width": wp, "depth": dp, "height": "default"}},
        "robot": {"x": W / 2, "y": D - ROBOT_BACK_OFFSET, "yaw_deg": -90.0},
        "objects": [block] + [b for b, _ in chosen_bins],
        "task": {"object_id": "block_1", "target_id": target},
    })
    return scene, warnings

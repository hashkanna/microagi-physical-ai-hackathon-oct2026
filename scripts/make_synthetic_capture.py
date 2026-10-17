"""Render a SYNTHETIC "phone capture" of a workstation and a matching fixture extraction.

Outputs (all clearly labelled synthetic; nothing here is a real recording or a real model response):
  fixtures/synthetic_capture/capture.mp4        handheld-style orbit video of the bench (robot hidden)
  fixtures/synthetic_capture/frames/*.jpg       8 frames, frame 0 is the overview with all 4 corners
  fixtures/synthetic_capture/extraction.json    canned "model response" = ground truth + realistic noise
  fixtures/synthetic_capture/ground_truth.json  the scene used to render, for measuring calibration error

Because ground truth is known, we can measure how well corner calibration recovers object positions.
"""

from __future__ import annotations

import json
import math
import random
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cellcheck  # noqa: E402,F401  (sets MUJOCO_GL before mujoco import)
import mujoco  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from cellcheck.compiler import compile_scene  # noqa: E402
from cellcheck.schema import Scene  # noqa: E402

OUT = ROOT / "fixtures" / "synthetic_capture"
W, H = 1280, 720
FOVY = 55.0
FPS, SECONDS = 12, 16
random.seed(7)
np.random.seed(7)

TRUTH = {
    "schema_version": "cellcheck.scene/v1",
    "name": "synthetic_bench",
    "notes": ["Ground truth for the synthetic capture."],
    "table": {"width": 1.4, "depth": 0.7, "height": 0.75, "thickness": 0.04},
    "robot": {"x": 0.7, "y": 0.6, "yaw_deg": -90},
    "objects": [
        {"id": "block_1", "kind": "block", "label": "red cube", "x": 0.55, "y": 0.24, "yaw_deg": 18,
         "size": [0.045, 0.045, 0.045], "color": [0.85, 0.15, 0.12]},
        {"id": "bin_a", "kind": "bin", "label": "blue tray", "x": 0.28, "y": 0.32, "yaw_deg": 0,
         "size": [0.2, 0.16, 0.06], "color": [0.18, 0.4, 0.85]},
        {"id": "bin_b", "kind": "bin", "label": "grey tote", "x": 1.05, "y": 0.3, "yaw_deg": -8,
         "size": [0.22, 0.18, 0.08], "color": [0.55, 0.57, 0.6]},
    ],
    "task": {"object_id": "block_1", "target_id": "bin_b"},
}


def look_at_quat(pos, target):
    fwd = np.asarray(target, float) - np.asarray(pos, float)
    fwd /= np.linalg.norm(fwd)
    x = np.cross(fwd, [0, 0, 1.0])
    x /= np.linalg.norm(x)
    y = np.cross(x, fwd)
    R = np.column_stack([x, y, -fwd])  # camera looks along its -z, y up
    q = np.zeros(4)
    mujoco.mju_mat2Quat(q, R.flatten())
    return q, R


def project(p, cam_pos, R):
    pc = R.T @ (np.asarray(p, float) - cam_pos)
    f = (H / 2) / math.tan(math.radians(FOVY) / 2)
    return [float(W / 2 + f * pc[0] / -pc[2]), float(H / 2 - f * pc[1] / -pc[2])]


def camera_path(t: float, scene: Scene):
    """Overview from the front for 2.5 s, then a slow handheld sweep left -> right -> centre."""
    tb = scene.table
    centre = np.array([tb.width / 2, tb.depth / 2, tb.height])
    if t < 2.5:
        az = -90.0
    else:
        s = (t - 2.5) / (SECONDS - 2.5)
        az = -90 + 38 * math.sin(2 * math.pi * s)
    dist = 1.55 + 0.08 * math.sin(t * 0.7)
    el = math.radians(42 + 4 * math.sin(t * 0.5))
    a = math.radians(az)
    pos = centre + dist * np.array([math.cos(a) * math.cos(el), math.sin(a) * math.cos(el), math.sin(el)])
    shake = np.array([math.sin(t * 7.1), math.sin(t * 5.3 + 1), math.sin(t * 6.7 + 2)]) * 0.004
    target = centre + np.array([0, 0.02, 0.05]) + shake
    return pos + shake * 2, target


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "frames").mkdir(exist_ok=True)
    full = json.loads(json.dumps(TRUTH))
    scene = Scene.model_validate(full)
    xml = compile_scene(scene).xml.replace("</worldbody>", f'<camera name="phone" pos="0 0 2" fovy="{FOVY}"/></worldbody>', 1)
    model = mujoco.MjModel.from_xml_string(xml)
    model.vis.global_.offwidth, model.vis.global_.offheight = W, H
    data = mujoco.MjData(model)
    for g in range(model.ngeom):  # hide the robot (not present yet in a real capture) and the frame strips
        name = model.geom(g).name
        if model.geom_group[g] in (2, 3) or name.startswith("edge_"):
            model.geom_rgba[g, 3] = 0.0
    mujoco.mj_forward(model, data)
    cam_id = model.camera("phone").id
    r = mujoco.Renderer(model, H, W)
    opt = mujoco.MjvOption()
    opt.geomgroup[2] = opt.geomgroup[3] = 0
    opt.sitegroup[4] = 0

    proc = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                             "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "24",
                             str(OUT / "capture.mp4")], stdin=subprocess.PIPE)
    n = FPS * SECONDS
    keep = {0} | {int(n * k / 8) for k in range(1, 8)}
    frames_meta, overview = [], None
    font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18) if Path(
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf").exists() else ImageFont.load_default()
    for i in range(n):
        t = i / FPS
        pos, target = camera_path(t, scene)
        q, R = look_at_quat(pos, target)
        model.cam_pos[cam_id] = pos
        model.cam_quat[cam_id] = q
        mujoco.mj_forward(model, data)
        r.update_scene(data, camera="phone", scene_option=opt)
        r.scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION] = 0
        img = Image.fromarray(r.render())
        d = ImageDraw.Draw(img, "RGBA")
        d.rectangle((0, 0, W, 30), fill=(0, 0, 0, 150))
        d.text((10, 5), "SYNTHETIC CAPTURE (MuJoCo render, not a real recording)", font=font, fill=(255, 215, 90))
        proc.stdin.write(np.asarray(img).tobytes())
        if i in keep:
            k = len(frames_meta)
            img.save(OUT / "frames" / f"frame_{k:02d}.jpg", quality=90)
            frames_meta.append({"index": k, "path": f"fixtures/synthetic_capture/frames/frame_{k:02d}.jpg", "t": round(t, 2),
                                "width": W, "height": H})
            if i == 0:
                overview = (pos.copy(), R.copy())
    proc.stdin.close()
    proc.wait()
    r.close()

    # ---- canned model response: ground-truth projections + noise a vision model might plausibly make
    cpos, R = overview
    tb = scene.table
    Hz = tb.height
    corners = {"front_left": (0, 0), "front_right": (tb.width, 0), "back_right": (tb.width, tb.depth), "back_left": (0, tb.depth)}
    true_corners_px = {k: project([x, y, Hz], cpos, R) for k, (x, y) in corners.items()}
    noisy = lambda v, s: [round(c + random.gauss(0, s), 1) for c in v]  # noqa: E731
    objects = []
    for o in scene.objects:
        px = project([o.x, o.y, Hz], cpos, R)
        objects.append({
            "label": o.label, "kind": o.kind, "is_target_bin": o.id == scene.task.target_id,
            "size_m": [round(s * random.uniform(0.85, 1.2), 3) for s in o.size],
            "footprint_center_px": noisy(px, 6.0),
            "position_m": [round(o.x * 0.92 + random.gauss(0, 0.03), 3), round(o.y * 1.1 + random.gauss(0, 0.03), 3)],
            "yaw_deg": round(o.yaw_deg + random.gauss(0, 6), 1),
            "color_rgb": list(o.color) if o.color else None,
            "confidence": round(random.uniform(0.75, 0.92), 2),
            "notes": "synthetic fixture value",
        })
    extraction = {
        "fixture": True,
        "model": "fixture",
        "elapsed_s": 0.0,
        "description": ("SYNTHETIC fixture: canned response generated by scripts/make_synthetic_capture.py from ground truth "
                        "plus noise. It is NOT a real vision-model output."),
        "frames": frames_meta,
        "result": {
            "overview_frame": 0,
            "table": {"corners_px": {k: noisy(v, 5.0) for k, v in true_corners_px.items()},
                      "width_m": round(tb.width * 1.1, 2), "depth_m": round(tb.depth * 0.9, 2), "confidence": 0.6},
            "objects": objects,
            "notes": "Robot not present in the capture; its mount point must be confirmed by the user.",
        },
        "raw_text": None,
    }
    (OUT / "extraction.json").write_text(json.dumps(extraction, indent=2))
    (OUT / "ground_truth.json").write_text(json.dumps({"scene": scene.model_dump(mode="json"),
                                                       "true_corners_px_frame0": true_corners_px}, indent=2))
    print("wrote", OUT, "frames", len(frames_meta), "corners", {k: [round(c) for c in v] for k, v in true_corners_px.items()})


if __name__ == "__main__":
    main()
    sys.stdout.flush()
    import os

    os._exit(0)

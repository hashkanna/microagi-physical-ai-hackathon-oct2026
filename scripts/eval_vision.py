"""Run the REAL capture pipeline on a video through the server (upload -> FFmpeg -> live vision model), then
compare against ground truth when available (the synthetic capture has it). Prints timing and position errors.

    python scripts/eval_vision.py [video] [--width 1.4 --depth 0.7]
"""

import argparse
import json
import math
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = "http://localhost:8000"


def post_json(path, body):
    req = urllib.request.Request(BASE + path, method="POST", data=json.dumps(body).encode(),
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def upload(video: Path):
    b = uuid.uuid4().hex
    body = (f"--{b}\r\nContent-Disposition: form-data; name=\"video\"; filename=\"{video.name}\"\r\n"
            f"Content-Type: video/mp4\r\n\r\n").encode() + video.read_bytes() + f"\r\n--{b}--\r\n".encode()
    req = urllib.request.Request(BASE + "/api/projects/upload", method="POST", data=body,
                                 headers={"content-type": f"multipart/form-data; boundary={b}"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.loads(r.read())


def errors(scene, truth):
    by_label = {o["label"]: o for o in truth["objects"]}
    out = {}
    for o in scene["objects"]:
        t = by_label.get(o["label"]) or next((x for x in truth["objects"] if x["id"] == o["id"]), None)
        if t:
            out[o["id"]] = round(100 * math.hypot(o["x"] - t["x"], o["y"] - t["y"]), 1)
    return out


ap = argparse.ArgumentParser()
ap.add_argument("video", nargs="?", default=str(ROOT / "fixtures/synthetic_capture/capture.mp4"))
ap.add_argument("--width", type=float, default=1.4)
ap.add_argument("--depth", type=float, default=0.7)
a = ap.parse_args()
gt_path = ROOT / "fixtures/synthetic_capture/ground_truth.json"
truth = json.loads(gt_path.read_text()) if "synthetic_capture" in a.video else None

t0 = time.time()
p = upload(Path(a.video))
print(f"upload + FFmpeg sampling: {time.time() - t0:.1f} s, {len(p['frames'])} frames")
t1 = time.time()
p = post_json(f"/api/projects/{p['id']}/extract", {"mode": "vision"})
ext = p["extraction"]
print(f"vision extraction ({ext['model']}): {time.time() - t1:.1f} s; total generation {ext['elapsed_s']} s")
print("warnings:", ext["warnings"])
sc = p["scene"]
print("table estimate:", sc["table"]["width"], "x", sc["table"]["depth"], "| objects:",
      [(o["id"], o["label"], round(o["x"], 3), round(o["y"], 3), [round(s, 3) for s in o["size"]]) for o in sc["objects"]],
      "| task:", sc["task"])
raw = json.loads((ROOT / "data/projects" / p["id"] / "extraction.json").read_text())
res = raw.get("result", {})
print("model overview frame:", res.get("overview_frame"), "| model corners:", res.get("table", {}).get("corners_px"))
if truth:
    print("position error, model only (cm):", errors(sc, truth["scene"]))
    p2 = post_json(f"/api/projects/{p['id']}/calibrate", {"table_width": a.width, "table_depth": a.depth,
                                                          "corners_px": None, "frame_index": res.get("overview_frame")})
    print("position error, model corners + measured size (cm):", errors(p2["scene"], truth["scene"]))
print("project:", p["id"])

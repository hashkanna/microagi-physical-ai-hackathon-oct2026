"""Uncertainty check: re-run the physics with poses perturbed according to their provenance.

The scene is approximate, so one passing run says little on its own. Values the user measured or confirmed
are jittered slightly, while inferred/default values (model guesses, placeholders) are jittered by roughly the
calibration error we measured on the synthetic capture. This is a sensitivity check, not a guarantee.
"""

from __future__ import annotations

import copy
import multiprocessing as mp
import random

from .compiler import SceneError, precheck
from .schema import Scene, point_in_footprint

LOOSE = {"inferred", "default"}
POS_LOOSE, POS_TIGHT = 0.03, 0.005  # metres (uniform half-width)
YAW_LOOSE, YAW_TIGHT = 8.0, 2.0  # degrees
RULE = "inferred/default positions jittered ±3 cm and yaw ±8°; measured/confirmed ±0.5 cm and ±2°"


def _amp(prov: dict, key: str, loose: float, tight: float) -> float:
    return loose if prov.get(key, "default") in LOOSE else tight


def perturb(scene: Scene, rng: random.Random) -> tuple[Scene, dict]:
    s = copy.deepcopy(scene)
    offsets = {}
    block = s.obj(s.task.object_id)
    host = next((o for o in s.objects if o.kind == "bin" and point_in_footprint(o, block.x, block.y)), None)
    for o in s.objects:
        dx = rng.uniform(-1, 1) * _amp(o.provenance, "x", POS_LOOSE, POS_TIGHT)
        dy = rng.uniform(-1, 1) * _amp(o.provenance, "y", POS_LOOSE, POS_TIGHT)
        dyaw = rng.uniform(-1, 1) * _amp(o.provenance, "yaw_deg", YAW_LOOSE, YAW_TIGHT)
        offsets[o.id] = [dx, dy, dyaw]
    for o in s.objects:
        dx, dy, dyaw = offsets[o.id]
        if host is not None and o.id == block.id:  # a block resting in a bin moves with it
            dx, dy = dx * POS_TIGHT / POS_LOOSE + offsets[host.id][0], dy * POS_TIGHT / POS_LOOSE + offsets[host.id][1]
            offsets[o.id][:2] = [dx, dy]
        o.x, o.y, o.yaw_deg = o.x + dx, o.y + dy, o.yaw_deg + dyaw
    r = s.robot
    rdx = rng.uniform(-1, 1) * _amp(r.provenance, "x", POS_LOOSE, POS_TIGHT)
    rdy = rng.uniform(-1, 1) * _amp(r.provenance, "y", POS_LOOSE, POS_TIGHT)
    r.x, r.y = r.x + rdx, r.y + rdy
    offsets["robot"] = [rdx, rdy, 0.0]
    return s, {k: [round(v[0], 4), round(v[1], 4), round(v[2], 2)] for k, v in offsets.items()}


def _one(args) -> dict:
    i, scene_json, seed = args
    from .sim import run_task

    scene = Scene.model_validate_json(scene_json)
    variant, offsets = perturb(scene, random.Random(seed * 1000 + i))
    out = {"i": i, "success": None, "failed_check": None, "detail": "", "offsets": offsets}
    try:
        precheck(variant)
    except SceneError as e:
        out.update(detail=f"invalid variant: {e}")
        return out
    res = run_task(variant, fps=1)
    out["success"] = bool(res.success)
    if not res.success:
        bad = next((c for c in res.checks.values() if c.status == "fail"), None)
        out.update(failed_check=bad.id if bad else None, detail=(bad.detail if bad else res.failure_reason or "")[:200])
    return out


def check_robustness(scene: Scene, n: int = 8, seed: int = 0, workers: int = 4) -> dict:
    jobs = [(i, scene.model_dump_json(), seed) for i in range(n)]
    with mp.get_context("fork").Pool(min(workers, n)) as pool:
        variants = pool.map(_one, jobs)
    valid = [v for v in variants if v["success"] is not None]
    passed = sum(1 for v in valid if v["success"])
    return {"n": n, "passed": passed, "failed": len(valid) - passed, "invalid": n - len(valid), "rule": RULE,
            "verdict": "robust" if valid and passed == len(valid) else "sensitive", "variants": variants}

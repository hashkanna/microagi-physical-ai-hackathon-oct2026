"""Reachability map and suggested (never auto-applied) corrections.

The map is computed with the same robot-only IK model used for planning: a grid point counts as
reachable when a top-down grasp pose is solvable both near the tabletop and at transfer height.
"""

from __future__ import annotations

import copy
import math
from functools import lru_cache

import numpy as np

from .compiler import SceneError, precheck
from .schema import Patch, PatchOp, Scene
from .sim import ORI_TOL, POS_TOL, Planner, grasp_rotation, plan_task

STEP = 0.05
LOW_Z, HIGH_Z = 0.06, 0.25  # TCP heights above the tabletop that must both be reachable


def _key(scene: Scene):
    r, t = scene.robot, scene.table
    return (round(r.x, 3), round(r.y, 3), round(r.yaw_deg, 1), round(t.width, 3), round(t.depth, 3), round(t.height, 3))


def reach_grid(scene: Scene) -> dict:
    """Reachability grid for this robot pose + table, cached in memory and on disk (it ignores objects)."""
    import hashlib
    import json

    from .store import DATA

    key = _key(scene)
    path = DATA / "cache" / ("reach_" + hashlib.sha1(repr(key).encode()).hexdigest()[:16] + ".json")
    if path.exists():
        return json.loads(path.read_text())
    grid = _reach_grid_cached(key, scene.model_dump_json())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(grid))
    return grid


@lru_cache(maxsize=32)
def _reach_grid_cached(_k, scene_json: str) -> dict:
    scene = Scene.model_validate_json(scene_json)
    t = scene.table
    planner = Planner(scene)
    R = grasp_rotation(0.0, scene.robot.yaw_deg)
    xs = np.arange(STEP / 2, t.width, STEP)
    ys = np.arange(STEP / 2, t.depth, STEP)
    grid = np.zeros((len(ys), len(xs)), dtype=bool)
    q_row = planner.home.copy()
    for j, y in enumerate(ys):
        q = q_row
        order = range(len(xs)) if j % 2 == 0 else range(len(xs) - 1, -1, -1)  # serpentine warm starts
        first = True
        for i in order:
            x = xs[i]
            d = math.hypot(x - scene.robot.x, y - scene.robot.y)
            if d < 0.12 or d > 1.0:
                continue
            ok = True
            for z in (HIGH_Z, LOW_Z):
                q, perr, oerr = planner.solve(q, np.array([x, y, t.height + z]), R, iters=60)
                ok &= perr < POS_TOL and oerr < ORI_TOL
            if not ok:
                q = planner.home.copy()
            grid[j, i] = ok
            if first:
                q_row, first = q.copy(), False
    return {"x0": float(xs[0]), "y0": float(ys[0]), "step": STEP, "nx": len(xs), "ny": len(ys),
            "low_z": LOW_Z, "high_z": HIGH_Z, "reachable": grid.astype(int).tolist()}


def suggest_fix(scene: Scene, waypoints: list[dict]) -> dict | None:
    """If a run failed on reach, propose the smallest move of the offending object that makes the
    full plan reachable. Returned as a Patch for the user to accept; never applied automatically."""
    bad = [w for w in waypoints if not w["reachable"]]
    if not bad:
        return None
    place_stages = {"transfer", "descend to bin", "retreat"}
    oid = scene.task.target_id if any(w["stage"] in place_stages for w in bad) else scene.task.object_id
    obj = scene.obj(oid)
    g = reach_grid(scene)
    cands = []
    for j, row in enumerate(g["reachable"]):
        for i, ok in enumerate(row):
            if ok:
                x, y = g["x0"] + i * g["step"], g["y0"] + j * g["step"]
                cands.append((math.hypot(x - obj.x, y - obj.y), x, y))
    cands.sort()
    planner = Planner(scene)
    tried = 0
    for _, x, y in cands:
        trial = copy.deepcopy(scene)
        o = trial.obj(oid)
        o.x, o.y = round(float(x), 3), round(float(y), 3)
        if oid == scene.task.object_id:
            pass
        try:
            precheck(trial)
        except SceneError:
            continue
        # block inside a moved bin would need moving too; keep it simple: skip if block in bin
        tried += 1
        _, rep = plan_task(trial, planner)
        if all(w["reachable"] for w in rep):
            dx, dy = o.x - obj.x, o.y - obj.y
            parts = []
            if abs(dx) >= 0.005:
                parts.append(f"{abs(dx) * 100:.0f} cm toward the {'left' if dx < 0 else 'right'} edge")
            if abs(dy) >= 0.005:
                parts.append(f"{abs(dy) * 100:.0f} cm toward the {'front' if dy < 0 else 'back'} edge")
            text = f"Move {oid} ({obj.label or obj.kind}) " + " and ".join(parts)
            patch = Patch(ops=[PatchOp(target=oid, field="x", value=o.x), PatchOp(target=oid, field="y", value=o.y)],
                          summary=text)
            return {"text": text, "patch": patch.model_dump(), "object_id": oid,
                    "moved_m": round(math.hypot(dx, dy), 3), "verified": "IK plan reachable for all 6 motion targets"}
        if tried > 25:
            break
    return {"text": f"No reachable placement for {oid} found on this table; consider moving the robot base.",
            "patch": None, "object_id": oid}

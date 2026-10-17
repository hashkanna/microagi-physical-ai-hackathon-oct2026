"""Out-of-process jobs (keeps EGL contexts and long physics runs out of the web server).

    python -m cellcheck.worker run <run_id>
    python -m cellcheck.worker preview <scene.json> <out.png> [hero|topdown]
"""

from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

from . import store
from .render import render_still, render_video
from .schema import Scene

VIDEO_FPS = 20


def do_run(rid: str) -> None:
    d = store.run_dir(rid)
    run = store.load_run(rid)
    scene = Scene.model_validate(store.read_json(d / "scene.json"))
    t0 = time.time()
    store.update_run(rid, status="running", stage="compiling")
    try:
        from .reach import reach_grid, suggest_fix
        from .sim import run_task

        store.update_run(rid, stage="mapping reach")
        try:
            reach = reach_grid(scene)
        except Exception:
            reach = None  # the overlay is cosmetic; never block a run on it
        res = run_task(scene, fps=VIDEO_FPS, progress=lambda st: store.update_run(rid, stage=st), reach=reach)
        out = res.as_dict()
        if res.xml:
            (d / "scene.xml").write_text(res.xml)
        if any(c["id"] == "reachable" and c["status"] == "fail" for c in out["checks"]):
            store.update_run(rid, stage="searching for a fix")
            out["suggestion"] = suggest_fix(scene, out["waypoints"])
        out["robustness"] = None
        if res.success:
            store.update_run(rid, stage="uncertainty check", checks=out["checks"], success=out["success"],
                             metrics=out["metrics"])
            from .robust import check_robustness

            try:
                out["robustness"] = check_robustness(scene, n=8)
            except Exception as e:  # a sensitivity analysis failure must not hide the nominal result
                out["robustness"] = {"error": str(e)}
        store.write_json(d / "results.json", out)
        update = dict(checks=out["checks"], success=out["success"], failure_reason=out["failure_reason"],
                      suggestion=out["suggestion"], metrics=out["metrics"], robustness=out["robustness"])
        store.update_run(rid, stage="rendering", **update)
        if res.frames_qpos:
            verdict = ("PLACEMENT SUCCEEDED" if res.success else "TASK FAILED: " + _short(res), res.success)
            banner = f"SIMULATION · scene v{run['version']} · run {rid}"
            render_video(res.xml, scene, res.frames_qpos, res.frame_labels, d / "run.mp4", fps=VIDEO_FPS, banner=banner,
                         verdict=verdict)
            render_still(res.xml, scene, d / "poster.png", qpos=res.frames_qpos[-1], label=verdict[0])
            update.update(video_url=f"/files/runs/{rid}/run.mp4", poster_url=f"/files/runs/{rid}/poster.png")
        store.update_run(rid, status="done", stage="done", elapsed_s=round(time.time() - t0, 1), **update)
    except Exception as e:
        (d / "error.log").write_text(traceback.format_exc())
        store.update_run(rid, status="error", stage="error", failure_reason=f"Internal error: {e}",
                         elapsed_s=round(time.time() - t0, 1))


def _short(res) -> str:
    for cid in ("compiled", "settled", "reachable", "placement", "collisions"):
        if res.checks[cid].status == "fail":
            return {"compiled": "scene invalid", "settled": "scene unstable", "reachable": "target out of reach",
                    "placement": "block not in target bin", "collisions": "unexpected collision"}[cid]
    return "see checks"


def do_preview(scene_path: str, out_png: str, view: str = "hero") -> None:
    from .compiler import compile_scene

    from .reach import reach_grid

    scene = Scene.model_validate(json.loads(Path(scene_path).read_text()))
    xml = compile_scene(scene, reach=reach_grid(scene)).xml
    render_still(xml, scene, Path(out_png), view=view, settle_s=0.3)


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "run":
        do_run(sys.argv[2])
    elif cmd == "preview":
        do_preview(*sys.argv[2:])
    else:
        raise SystemExit(f"unknown command {cmd}")
    sys.stdout.flush()
    import os

    os._exit(0)  # skip EGL teardown noise at interpreter exit

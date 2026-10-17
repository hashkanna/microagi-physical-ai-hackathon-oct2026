"""CellCheck web server: FastAPI API + static frontend. See docs/API.md.

Run with `python -m cellcheck` (or `uvicorn cellcheck.server:app`).
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import __version__, store
from .compiler import SceneError, compile_bundle_xml, compile_scene
from .schema import Patch, Scene

ROOT = store.ROOT
WEB = ROOT / "web"
SAMPLES = ROOT / "scenes"
SAMPLE_INFO = {
    "baseline": "Desk-sized cell, robot at the back edge: block on the table → grey tote.",
    "alt_layout": "Robot on the LEFT edge; block starts inside the blue tray → rotated tote.",
    "unreachable": "Long bench with the target tote beyond the Panda's working envelope.",
}

app = FastAPI(title="CellCheck", version=__version__)
_runs = ThreadPoolExecutor(max_workers=1)  # one physics/render job at a time on CPU
_preview_lock = threading.Lock()


@app.exception_handler(ValidationError)
async def _validation(_: Request, exc: ValidationError):
    errs = [{"loc": ".".join(str(x) for x in e["loc"]), "msg": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": "; ".join(f"{e['loc']}: {e['msg']}" for e in errs[:4]), "errors": errs})


@app.exception_handler(SceneError)
async def _scene_error(_: Request, exc: SceneError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(KeyError)
async def _not_found(_: Request, exc: KeyError):
    return JSONResponse(status_code=404, content={"detail": f"Not found: {exc.args[0] if exc.args else ''}"})


def _vision():
    from . import vision

    return vision


def project_view(meta: dict) -> dict:
    out = dict(meta)
    v = meta["current_version"]
    out["scene"] = store.load_scene(meta["id"], v).model_dump(mode="json") if v else None
    runs = []
    for rid in meta.get("run_ids", []):
        try:
            r = store.load_run(rid)
        except KeyError:
            continue
        runs.append(_run_view(r, v))
    out["runs"] = runs[::-1]
    out.pop("run_ids", None)
    return out


def _run_view(r: dict, current_version: int | None = None) -> dict:
    r = dict(r)
    if current_version is None:
        current_version = store.load_project(r["project_id"])["current_version"]
    r["is_stale"] = r["version"] != current_version
    r["evidence_url"] = f"/api/runs/{r['id']}/bundle.zip"
    return r


# ---- status / samples / projects ---------------------------------------------------------------


@app.get("/api/status")
def status():
    vision = _vision()
    return {"openai_configured": vision.openai_configured(), "vision_model": vision.vision_model(),
            "renderer": os.environ.get("MUJOCO_GL", "egl"), "fixture_mode": True, "app_version": __version__}


@app.get("/api/samples")
def samples():
    out = []
    for sid, desc in SAMPLE_INFO.items():
        if (SAMPLES / f"{sid}.json").exists():
            sc = json.loads((SAMPLES / f"{sid}.json").read_text())
            out.append({"id": sid, "name": sc.get("name", sid), "description": desc})
    return {"samples": out}


class NewProject(BaseModel):
    source: str = "sample"
    sample_id: str = "baseline"


@app.post("/api/projects")
def create_project(body: NewProject):
    if body.source == "sample":
        if body.sample_id not in SAMPLE_INFO:
            raise HTTPException(404, f"unknown sample {body.sample_id}")
        scene = Scene.model_validate_json((SAMPLES / f"{body.sample_id}.json").read_text())
        meta = store.create_project(body.sample_id, "sample")
        meta = store.add_version(meta["id"], scene, "sample", f"Loaded sample scene '{body.sample_id}'")
        return project_view(meta)
    if body.source == "fixture":
        meta = store.create_project("fixture capture", "fixture")
        return _extract(meta["id"], "fixture")
    raise HTTPException(400, "source must be 'sample' or 'fixture' (use /api/projects/upload for videos)")


@app.post("/api/projects/upload")
def upload(video: UploadFile = File(...)):
    from .frames import VideoError, sample_frames

    meta = store.create_project(Path(video.filename or "capture").stem[:40] or "capture", "upload")
    pdir = store.project_dir(meta["id"])
    src = pdir / "upload" / ("input" + (Path(video.filename or "").suffix.lower() or ".mp4"))
    src.parent.mkdir(parents=True, exist_ok=True)
    with src.open("wb") as f:
        shutil.copyfileobj(video.file, f)
    t0 = time.time()
    try:
        frames = sample_frames(src, pdir / "frames", n=10)
    except VideoError as e:
        raise HTTPException(422, f"Could not sample frames: {e}")
    meta = store.load_project(meta["id"])
    meta["frames"] = [{"index": f["index"], "url": f"/files/projects/{meta['id']}/frames/{Path(f['path']).name}",
                       "t": round(f["t"], 2), "width": f.get("width"), "height": f.get("height")} for f in frames]
    meta["metrics"]["sampling_s"] = round(time.time() - t0, 2)
    store.save_project(meta)
    return project_view(meta)


@app.get("/api/projects")
def projects():
    return {"projects": store.list_projects()}


@app.get("/api/projects/{pid}")
def get_project(pid: str):
    return project_view(store.load_project(pid))


# ---- extraction / calibration --------------------------------------------------------------------


class ExtractBody(BaseModel):
    mode: str = "vision"


@app.post("/api/projects/{pid}/extract")
def extract(pid: str, body: ExtractBody):
    return _extract(pid, body.mode)


def _extract(pid: str, mode: str):
    vision = _vision()
    meta = store.load_project(pid)
    pdir = store.project_dir(pid)
    t0 = time.time()
    if mode == "vision":
        if not vision.openai_configured():
            raise HTTPException(400, "OPENAI_API_KEY is not set on the server. Add it to .env, or use the clearly "
                                     "labelled fixture extraction instead.")
        if not meta["frames"]:
            raise HTTPException(400, "Upload a video first: there are no sampled frames to analyse.")
        paths = [pdir / "frames" / Path(f["url"]).name for f in meta["frames"]]
        try:
            ext = vision.extract_scene(paths)
        except Exception as e:  # surface API errors verbatim; never fall back to fixture data
            raise HTTPException(502, f"Vision extraction failed: {e}")
    elif mode == "fixture":
        ext = vision.fixture_extraction()
        if not meta["frames"]:
            fx = ROOT / "fixtures" / "frames"
            if fx.exists():
                (pdir / "frames").mkdir(parents=True, exist_ok=True)
                meta["frames"] = []
                for i, p in enumerate(sorted(fx.glob("*.jpg"))):
                    shutil.copy(p, pdir / "frames" / p.name)
                    meta["frames"].append({"index": i, "url": f"/files/projects/{pid}/frames/{p.name}", "t": None})
    else:
        raise HTTPException(400, "mode must be 'vision' or 'fixture'")
    vision.save_raw(ext, pdir / "extraction.json")
    cal = meta.get("calibration") or {}
    scene, warnings = vision.draft_scene_from_extraction(ext, table_width=cal.get("table_width"),
                                                        table_depth=cal.get("table_depth"), corners_px=cal.get("corners_px"))
    elapsed = round(time.time() - t0 + meta["metrics"].get("sampling_s", 0), 2)
    meta["extraction"] = {"mode": mode, "model": ext.get("model") if mode == "vision" else "fixture",
                          "elapsed_s": elapsed, "raw_url": f"/files/projects/{pid}/extraction.json", "warnings": warnings,
                          "fixture": mode == "fixture"}
    meta["overview_frame"] = int(ext.get("overview_frame") or ext.get("result", {}).get("overview_frame") or 0) if isinstance(ext, dict) else 0
    meta["metrics"]["generation_s"] = elapsed
    store.save_project(meta)
    label = "fixture extraction (canned model response)" if mode == "fixture" else f"vision extraction ({meta['extraction']['model']})"
    meta = store.add_version(pid, scene, "extraction", f"Draft scene from {label}")
    return project_view(meta)


class CalibrateBody(BaseModel):
    table_width: float
    table_depth: float
    corners_px: list[list[float]] | None = None
    frame_index: int | None = None


@app.post("/api/projects/{pid}/calibrate")
def calibrate(pid: str, body: CalibrateBody):
    vision = _vision()
    meta = store.load_project(pid)
    if body.corners_px is not None:
        from .calibrate import CalibrationError, check_quad

        try:
            check_quad(body.corners_px)
        except CalibrationError as e:
            raise HTTPException(422, str(e))
    meta["calibration"] = {"table_width": body.table_width, "table_depth": body.table_depth,
                           "corners_px": body.corners_px, "frame_index": body.frame_index}
    store.save_project(meta)
    pdir = store.project_dir(pid)
    from .calibrate import CalibrationError

    cur = store.load_scene(pid, meta["current_version"]) if meta["current_version"] else None
    try:
        if (pdir / "extraction.json").exists():
            ext = json.loads((pdir / "extraction.json").read_text())
            fresh, warnings = vision.draft_scene_from_extraction(ext, table_width=body.table_width, table_depth=body.table_depth,
                                                                corners_px=body.corners_px)
            scene = _merge_calibrated(cur, fresh) if cur else fresh
            meta = store.load_project(pid)
            meta["extraction"]["warnings"] = warnings
            store.save_project(meta)
            how = "4 marked corners + measured table size" if body.corners_px else "measured table size (no corners marked)"
        else:
            from .calibrate import apply_calibration

            scene = apply_calibration(cur, table_width=body.table_width, table_depth=body.table_depth)
            how = "measured table size"
    except CalibrationError as e:
        raise HTTPException(422, f"Calibration rejected: {e}")
    compile_scene(scene)  # validate before accepting
    meta = store.add_version(pid, scene, "calibration", f"Calibrated with {how}")
    return project_view(meta)


def _merge_calibrated(cur: Scene, fresh: Scene) -> Scene:
    """Take re-projected values from the freshly calibrated draft only where the current scene still holds
    model guesses ("inferred"/"default"); anything the user measured or confirmed is kept."""
    data = cur.model_dump(mode="json")
    data["table"] = fresh.model_dump(mode="json")["table"]
    keep = ("measured", "user_confirmed")
    fr = fresh.model_dump(mode="json")
    if not any(data["robot"]["provenance"].get(k) in keep for k in ("x", "y")):
        data["robot"] = fr["robot"]
    fobj = {o["id"]: o for o in fr["objects"]}
    for o in data["objects"]:
        f = fobj.get(o["id"])
        if not f:
            continue
        for k in ("x", "y", "yaw_deg", "size"):
            if o["provenance"].get(k) not in keep:
                o[k] = f[k]
                o["provenance"][k] = f["provenance"].get(k, "inferred")
    data["notes"] = fr["notes"]
    return Scene.model_validate(data)


# ---- editing ------------------------------------------------------------------------------------


class SceneBody(BaseModel):
    scene: dict
    summary: str = "Edited in object editor"


@app.put("/api/projects/{pid}/scene")
def put_scene(pid: str, body: SceneBody):
    scene = Scene.model_validate(body.scene)
    compile_scene(scene)
    meta = store.add_version(pid, scene, "edit", body.summary[:200])
    return project_view(meta)


class CorrectBody(BaseModel):
    text: str


@app.post("/api/projects/{pid}/correct")
def correct(pid: str, body: CorrectBody):
    from .corrections import CorrectionError, apply_patch, interpret

    meta = store.load_project(pid)
    scene = store.load_scene(pid, meta["current_version"])
    try:
        patch, explanation, parser = interpret(body.text, scene, prefer="rules")
        new = apply_patch(scene, patch)
        compile_scene(new)
    except (CorrectionError, SceneError) as e:
        raise HTTPException(422, str(e))
    meta = store.add_version(pid, new, "correction", f"“{body.text.strip()[:120]}” → {explanation}")
    return {"project": project_view(meta), "patch": patch.model_dump(), "explanation": explanation, "parser": parser}


class SuggestionBody(BaseModel):
    run_id: str


@app.post("/api/projects/{pid}/apply_suggestion")
def apply_suggestion(pid: str, body: SuggestionBody):
    from .corrections import apply_patch

    run = store.load_run(body.run_id)
    if run["project_id"] != pid or not run.get("suggestion") or not run["suggestion"].get("patch"):
        raise HTTPException(400, "This run has no applicable suggestion.")
    meta = store.load_project(pid)
    base = store.load_scene(pid, run["version"])
    new = apply_patch(base, Patch.model_validate(run["suggestion"]["patch"]))
    compile_scene(new)
    meta = store.add_version(pid, new, "suggestion", f"Accepted suggestion from run {run['id']}: {run['suggestion']['text']}")
    return project_view(meta)


# ---- previews -----------------------------------------------------------------------------------


@app.get("/api/projects/{pid}/versions/{v}/scene.json")
def version_scene(pid: str, v: int):
    return store.load_scene(pid, v).model_dump(mode="json")


@app.get("/api/projects/{pid}/versions/{v}/reach")
def version_reach(pid: str, v: int):
    from .reach import reach_grid

    return reach_grid(store.load_scene(pid, v))


@app.get("/api/projects/{pid}/versions/{v}/{view}.png")
def version_png(pid: str, v: int, view: str):
    if view not in ("preview", "topdown"):
        raise HTTPException(404, "unknown view")
    scene = store.load_scene(pid, v)
    out = store.project_dir(pid) / "renders" / f"v{v}_{view}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        sp = out.with_suffix(".scene.json")
        sp.write_text(scene.model_dump_json())
        with _preview_lock:
            p = subprocess.run([sys.executable, "-m", "cellcheck.worker", "preview", str(sp), str(out),
                                "hero" if view == "preview" else "topdown"], capture_output=True, text=True, timeout=120)
        if p.returncode != 0 or not out.exists():
            raise HTTPException(500, f"Render failed: {p.stderr[-400:]}")
    return FileResponse(out, headers={"Cache-Control": "no-cache"})


# ---- runs ---------------------------------------------------------------------------------------


class RunBody(BaseModel):
    version: int | None = None


def _exec_run(rid: str):
    subprocess.run([sys.executable, "-m", "cellcheck.worker", "run", rid], capture_output=True, timeout=600)


@app.post("/api/projects/{pid}/runs")
def start_run(pid: str, body: RunBody):
    meta = store.load_project(pid)
    v = body.version or meta["current_version"]
    if not v:
        raise HTTPException(400, "No scene version to run yet.")
    run = store.create_run(pid, v)
    _runs.submit(_exec_run, run["id"])
    return _run_view(run)


@app.get("/api/runs/{rid}")
def get_run(rid: str):
    return _run_view(store.load_run(rid))


# ---- bundles ------------------------------------------------------------------------------------


def _zip(files: dict[str, Path | bytes | str]) -> Response:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, src in files.items():
            if isinstance(src, Path):
                if src.is_dir():
                    for p in sorted(src.rglob("*")):
                        if p.is_file():
                            z.write(p, f"{name}/{p.relative_to(src)}")
                elif src.exists():
                    z.write(src, name)
            else:
                z.writestr(name, src)
    return Response(buf.getvalue(), media_type="application/zip")


def _scene_files(scene: Scene, with_assets: bool = True) -> dict:
    files = {"scene.json": scene.model_dump_json(indent=2), "scene.xml": compile_bundle_xml(scene),
             "README.txt": ("CellCheck scene bundle. scene.json is the editable workcell (table frame: origin front-left "
                            "corner, +x toward the right edge, +y toward the back edge, metres). scene.xml is the compiled "
                            "MJCF; load it with mujoco.MjModel.from_xml_path('scene.xml'). Approximate model: positions "
                            "tagged 'inferred' are estimates, not measurements.\n")}
    if with_assets:
        files["assets"] = ROOT / "assets" / "franka_emika_panda" / "assets"
        files["LICENSE-franka_emika_panda.txt"] = ROOT / "assets" / "franka_emika_panda" / "LICENSE"
    return files


@app.get("/api/projects/{pid}/versions/{v}/bundle.zip")
def scene_bundle(pid: str, v: int):
    scene = store.load_scene(pid, v)
    resp = _zip(_scene_files(scene))
    resp.headers["Content-Disposition"] = f'attachment; filename="cellcheck_{pid}_v{v}.zip"'
    return resp


@app.get("/api/runs/{rid}/bundle.zip")
def run_bundle(rid: str):
    run = store.load_run(rid)
    d = store.run_dir(rid)
    pdir = store.project_dir(run["project_id"])
    scene = Scene.model_validate(store.read_json(d / "scene.json"))
    files = _scene_files(scene, with_assets=False)
    files.update({"run.json": d / "run.json", "results.json": d / "results.json", "run.mp4": d / "run.mp4",
                  "poster.png": d / "poster.png", "inputs/frames": pdir / "frames",
                  "inputs/model_response.json": pdir / "extraction.json", "project.json": pdir / "project.json"})
    resp = _zip(files)
    resp.headers["Content-Disposition"] = f'attachment; filename="cellcheck_evidence_{rid}.zip"'
    return resp


# ---- static -------------------------------------------------------------------------------------

store.DATA.mkdir(parents=True, exist_ok=True)
app.mount("/files", StaticFiles(directory=store.DATA), name="files")
if WEB.exists():
    app.mount("/", StaticFiles(directory=WEB, html=True), name="web")

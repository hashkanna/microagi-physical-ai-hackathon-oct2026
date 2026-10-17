"""Disk-backed projects, immutable scene versions and runs (everything is evidence, nothing is mutated).

data/projects/{pid}/project.json          project metadata (frames, extraction, calibration, version log)
data/projects/{pid}/versions/v{n}.json     immutable scene versions
data/projects/{pid}/frames/*.jpg           sampled input frames
data/projects/{pid}/extraction.json        raw vision model response (or fixture, clearly flagged)
data/runs/{rid}/run.json                   run status + checks, bound to (pid, version)
data/runs/{rid}/{scene.json,scene.xml,results.json,run.mp4,poster.png}
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from .schema import Scene

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("CELLCHECK_DATA", ROOT / "data"))
PROJECTS = DATA / "projects"
RUNS = DATA / "runs"
_lock = threading.RLock()

MANUAL_ORIGINS = {"edit", "correction", "suggestion", "calibration"}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(3)}"


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    os.replace(tmp, path)


def read_json(path: Path):
    return json.loads(path.read_text())


# ---- projects ---------------------------------------------------------------------------------


def project_dir(pid: str) -> Path:
    if not pid.startswith("p_") or "/" in pid or ".." in pid:
        raise KeyError(pid)
    return PROJECTS / pid


def create_project(name: str, source: str) -> dict:
    pid = new_id("p")
    meta = {"id": pid, "name": name, "created_at": now(), "source": source, "frames": [], "overview_frame": 0,
            "extraction": None, "calibration": None, "versions": [], "current_version": 0, "run_ids": [],
            "metrics": {"generation_s": None, "manual_corrections": 0}}
    save_project(meta)
    return meta


def load_project(pid: str) -> dict:
    p = project_dir(pid) / "project.json"
    if not p.exists():
        raise KeyError(pid)
    return read_json(p)


def save_project(meta: dict) -> None:
    write_json(project_dir(meta["id"]) / "project.json", meta)


def list_projects() -> list[dict]:
    if not PROJECTS.exists():
        return []
    out = []
    for d in sorted(PROJECTS.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            m = read_json(d / "project.json")
            out.append({"id": m["id"], "name": m["name"], "created_at": m["created_at"], "source": m["source"],
                        "current_version": m["current_version"]})
        except Exception:
            continue
    return out


def add_version(pid: str, scene: Scene, origin: str, summary: str) -> dict:
    """Append an immutable scene version and make it current."""
    with _lock:
        meta = load_project(pid)
        v = len(meta["versions"]) + 1
        write_json(project_dir(pid) / "versions" / f"v{v}.json", scene.model_dump(mode="json"))
        meta["versions"].append({"version": v, "created_at": now(), "origin": origin, "summary": summary,
                                 "manual": origin in MANUAL_ORIGINS})
        meta["current_version"] = v
        meta["metrics"]["manual_corrections"] = sum(1 for x in meta["versions"] if x["manual"])
        save_project(meta)
        return meta


def load_scene(pid: str, version: int) -> Scene:
    p = project_dir(pid) / "versions" / f"v{int(version)}.json"
    if not p.exists():
        raise KeyError(f"{pid} v{version}")
    return Scene.model_validate(read_json(p))


# ---- runs -------------------------------------------------------------------------------------


def run_dir(rid: str) -> Path:
    if not rid.startswith("r_") or "/" in rid or ".." in rid:
        raise KeyError(rid)
    return RUNS / rid


def create_run(pid: str, version: int) -> dict:
    rid = new_id("r")
    run = {"id": rid, "project_id": pid, "version": version, "status": "queued", "stage": "queued",
           "created_at": now(), "elapsed_s": None, "checks": [], "success": None, "failure_reason": None,
           "suggestion": None, "metrics": {}, "video_url": None, "poster_url": None}
    d = run_dir(rid)
    d.mkdir(parents=True, exist_ok=True)
    write_json(d / "scene.json", load_scene(pid, version).model_dump(mode="json"))
    write_json(d / "run.json", run)
    with _lock:
        meta = load_project(pid)
        meta["run_ids"].append(rid)
        save_project(meta)
    return run


def load_run(rid: str) -> dict:
    p = run_dir(rid) / "run.json"
    if not p.exists():
        raise KeyError(rid)
    return read_json(p)


def update_run(rid: str, **fields) -> dict:
    with _lock:
        run = load_run(rid)
        run.update(fields)
        write_json(run_dir(rid) / "run.json", run)
        return run

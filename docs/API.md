# CellCheck HTTP API (v1)

Server: FastAPI in `cellcheck/server.py`, serves the static frontend from `web/` at `/` and the
API under `/api`. Media/evidence files are served under `/files/...` (paths returned by the API).
All JSON. Scene JSON follows `cellcheck/schema.py` (`cellcheck.scene/v1`).

A **project** is one workcell session. It holds immutable **scene versions** (v1, v2, …) and
**runs**. Every run is bound to exactly one version. Editing never mutates a version: it creates a
new one, and runs of older versions are shown as stale.

## Status

`GET /api/status`
```json
{"openai_configured": false, "vision_model": "gpt-5.1", "renderer": "egl",
 "fixture_mode": true, "app_version": "0.1.0"}
```

## Projects

`POST /api/projects` body `{"source": "sample", "sample_id": "baseline"}` → `Project`
`POST /api/projects/upload` multipart field `video` → `Project` (frames sampled with FFmpeg, no scene yet)
`GET /api/projects` → `{"projects": [ProjectSummary]}`
`GET /api/projects/{pid}` → `Project`
`GET /api/samples` → `{"samples": [{"id": "baseline", "name": "...", "description": "..."}]}`

```jsonc
// Project
{
  "id": "p_ab12cd",
  "name": "baseline",
  "created_at": "2026-10-17T12:00:00Z",
  "source": "sample" | "upload" | "fixture",
  "frames": [{"index": 0, "url": "/files/p_ab12cd/frames/frame_00.jpg", "t": 1.5}],   // may be empty
  "overview_frame": 0,                     // frame index used for corner marking
  "extraction": null | {"mode": "vision" | "fixture", "model": "gpt-5.1", "elapsed_s": 8.2,
                        "raw_url": "/files/.../extraction.json", "warnings": ["..."]},
  "calibration": null | {"corners_px": [[u,v],[u,v],[u,v],[u,v]], "frame_index": 3,
                         "table_width": 1.0, "table_depth": 0.7},
  "versions": [ {"version": 1, "created_at": "...", "origin": "extraction" | "sample" | "edit" | "correction" | "calibration" | "suggestion",
                 "summary": "Initial scene from sample", "manual": false} ],
  "current_version": 2,
  "scene": { /* Scene JSON of current_version */ },
  "runs": [RunSummary],
  "metrics": {"generation_s": 8.2, "manual_corrections": 1}
}
```

## Scene editing (every call creates a new version and returns the updated Project)

`PUT /api/projects/{pid}/scene` body `{"scene": Scene, "summary": "Edited in object editor"}`
`POST /api/projects/{pid}/correct` body `{"text": "Move the target bin 20 centimetres toward the table's left edge."}`
→ `{"project": Project, "patch": Patch, "explanation": "...", "parser": "llm" | "rules"}`
   or HTTP 422 `{"detail": "Could not interpret ..."}`
`POST /api/projects/{pid}/calibrate` body
`{"table_width": 1.0, "table_depth": 0.7, "corners_px": [[u,v]x4] | null, "frame_index": 3}`
corners order: front-left, front-right, back-right, back-left (as seen standing at the front edge).
`POST /api/projects/{pid}/extract` body `{"mode": "vision" | "fixture"}` → Project
(`vision` requires OPENAI_API_KEY; error 400 with a clear message otherwise. Never silently falls back.)

Validation errors → HTTP 422 `{"detail": "human readable", "errors": [...]}`.

## Preview

`GET /api/projects/{pid}/versions/{v}/preview.png` – rendered perspective view of that version (settled).
`GET /api/projects/{pid}/versions/{v}/topdown.png` – top-down map with table frame axes, edge labels,
object ids, and the robot's reachable region shaded.

## Runs

`POST /api/projects/{pid}/runs` body `{"version": 2}` → `RunSummary` (status "queued"); runs execute in a
background worker. Poll `GET /api/runs/{rid}`.

```jsonc
// Run
{
  "id": "r_xy12",
  "project_id": "p_ab12cd",
  "version": 2,
  "status": "queued" | "running" | "done" | "error",
  "stage": "compiling" | "settling" | "planning" | "executing" | "rendering" | "done",
  "created_at": "...", "elapsed_s": 14.2,
  "checks": [
    {"id": "compiled",   "label": "Scene compiled",                 "status": "pass" | "fail" | "skip", "detail": "..."},
    {"id": "settled",    "label": "Scene settled",                  "status": "...", "detail": "max drift 0.4 mm"},
    {"id": "reachable",  "label": "Required targets reachable",     "status": "...", "detail": "8/8 waypoints, worst error 1.2 mm"},
    {"id": "collisions", "label": "No unexpected collisions",       "status": "...", "detail": "..."},
    {"id": "placement",  "label": "Block placed in target bin",     "status": "...", "detail": "..."}
  ],
  "success": true,
  "failure_reason": null | "Target bin centre is 0.94 m from the robot base; Panda reach ≈ 0.85 m",
  "suggestion": null | {"text": "Move bin_b 15 cm toward the back edge", "patch": Patch},
  "video_url": "/files/.../run.mp4",
  "poster_url": "/files/.../poster.png",
  "metrics": {"sim_time_s": 9.5, "block_final": [x,y,z], "max_contact_force": 12.3},
  "evidence_url": "/api/runs/{rid}/bundle.zip",
  "is_stale": false            // true if project.current_version != run.version
}
```

`GET /api/runs/{rid}/bundle.zip` – evidence bundle: frames, model response, scene.json, scene.xml,
results.json, run.mp4.
`GET /api/projects/{pid}/versions/{v}/bundle.zip` – scene bundle (scene.json + compiled MJCF + assets list).

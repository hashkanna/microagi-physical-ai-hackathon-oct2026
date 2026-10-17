# CellCheck: submission notes (Physical AI Hack, London, 17 Oct 2026)

**One-liner:** Phone capture → editable workcell → robot task check. CellCheck turns a short video of a
workstation into an approximate, editable MuJoCo workcell, runs a physical Franka Panda pick-and-place, and
shows honest per-check results. You fix the scene in plain language and rerun.

**Problem.** Before deploying a robot arm at a bench, integrators and small manufacturers need to know whether the
arm can actually do the task in *that* space: reach, clearances, where to mount it. Video-to-simulation research
(e.g. microSLAM) focuses on reconstruction fidelity. In practice, though, a scene estimated from video is always
approximate, and the useful product is the loop around it: see what was assumed, correct it, and re-test.

**What we built (all working, in the repo):**
- Upload → FFmpeg frame sampling → OpenAI structured-output scene estimate → validated scene JSON. A labelled synthetic
  capture runs the same flow without a key.
- Calibration with measured table size plus 4 clicked corners (homography). Every value is tagged inferred / measured /
  user_confirmed / default.
- Deterministic scene → MJCF compiler, with settle and penetration checks.
- Mink IK plan from the current scene coordinates. Execution uses actuator commands only; the block moves by contact
  friction (no welds or teleports).
- Five separately reported checks, an **IK-verified suggested fix** that is never auto-applied, and plain-language
  corrections that create immutable scene versions, with stale results flagged.
- **Uncertainty check:** after a pass, 8 physics variants are re-run with poses jittered according to provenance.
  It caught a real clearance weakness in our own controller during the build.
- A reach map painted on the table, a version timeline, downloadable scene and evidence bundles, and a 56 s demo
  recorded from the live app.

**Judging criteria, mapped:**
- *Execution:* real physics loop, 66 tests, verified from a fresh clone with `./run.sh`.
- *Problem identification:* robot placement and reach feasibility before deployment.
- *UX:* one screen, with capture, simulation, checks and correction side by side.
- *Creativity/impact:* provenance-driven uncertainty checking and suggested fixes.
- *Demo clarity:* captioned 56 s cut, with every frame from the live app.

**Honest limits:** This is an approximate workcell model, not an accurate reconstruction. It isn't training-ready, and
it makes no sim-to-real claims. The checks cover this simulated task only, not safety certification. The gripper servo is
stiffened 5× versus the Menagerie default (disclosed). Real-phone + live-vision validation: see README "What was not tested".

**Links (fill in at submission time):**
- Repository: https://github.com/hashkanna/microagi-physical-ai-hackathon-oct2026 (currently private; make public to submit)
- Demo video: `demo/cellcheck_demo.mp4` (upload to a public host, e.g. YouTube unlisted, Loom, or Drive "anyone with link")

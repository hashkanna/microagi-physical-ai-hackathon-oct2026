# CellCheck: 60-second demo script

Use this for the recorded demo and for the live demo. Everything shown runs in the real app (`./run.sh`).
Without an API key, use the **Bundled capture (FIXTURE)** card. It is a MuJoCo-rendered synthetic phone video
with a canned extraction, and it is labelled FIXTURE everywhere. With a key, upload your own phone video instead.

| Time | Screen | Say (≈ 140 words total) |
|---|---|---|
| 0–6 s | Start screen | "Before you bolt a robot to a bench, you want to know: can it actually do the job *here*? CellCheck answers that from a phone video." |
| 6–14 s | Capture → frames + draft workcell, provenance chips | "We sample sharp frames, and a vision model returns a structured estimate. Every value is tagged: inferred, measured, confirmed, or default." |
| 14–22 s | Calibration: type 140 × 70 cm, click 4 corners | "I enter the measured table size and click the four corners. Object positions snap into the table frame." |
| 22–30 s | Run task → playback, 5 green checks | "MuJoCo runs a real pick-and-place. The arm is driven only by actuator commands, and the block moves by contact friction. Five checks pass." |
| 30–40 s | Type *"Move the robot 50 cm toward the left edge"* → run → red reach check, HALTED, green reach zone | "Now a planning question: mount the robot further left? New version, fresh run. The tote is outside the reachable zone, so the arm halts." |
| 40–52 s | Suggestion card → *Apply as new version* → run → green | "CellCheck suggests the smallest move that makes the plan reachable. It never applies it silently. I accept it, and the rerun passes." |
| 52–60 s | Version timeline v1→v4, downloads | "Every run is bound to a scene version, with downloadable evidence. It's an approximate workcell with transparent assumptions and visible checks." |

## Typed corrections that work without an API key (deterministic parser)

* `Move the target bin 20 centimetres toward the table's left edge`
* `Move the robot 50 cm toward the left edge` / `Mount the robot 50cm further left`
* `The grey tote is actually 5 cm further back`
* `move the cube 10 cm toward the robot`
* `rotate the tote by 15 degrees`
* `put the cube in the blue tray`
* `set block mass to 200 g` · `the table is 1.2 m wide`

With `OPENAI_API_KEY` set, anything the rules don't match goes to an LLM, which returns a schema-constrained patch
that is validated before it's applied.

## Honest framing (say this if asked)

* The output is an **approximate workcell model**. It is not an accurate reconstruction, it is not training-ready, and it
  makes no sim-to-real claim.
* The checks are checks **of this simulated task**, not a safety certification.
* The gripper actuator is stiffened 5× versus the Menagerie default (≈11 N vs the real hand's 70 N rating), disclosed in the README.
* Real-capture validation with a live vision call was not completed during the build if no key or recording was available.

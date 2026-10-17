"""Drive the demo story through the HTTP API (no browser) and print timings + checks.

capture (fixture) -> calibrate -> run (expect pass) -> "move the robot 50 cm toward the left edge"
-> run (expect unreachable + suggestion) -> accept suggestion -> run (expect pass)
"""

import json
import sys
import time
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def run(pid):
    r = call("POST", f"/api/projects/{pid}/runs", {})
    while r["status"] not in ("done", "error"):
        time.sleep(1)
        r = call("GET", f"/api/runs/{r['id']}")
    print(f"  run {r['id']} on v{r['version']}: {'SUCCESS' if r['success'] else 'FAIL'} in {r['elapsed_s']} s — {r['failure_reason'] or ''}")
    for c in r["checks"]:
        print(f"     [{c['status']:4s}] {c['label']}: {c['detail'][:100]}")
    if r.get("suggestion"):
        print("     suggestion:", r["suggestion"]["text"])
    return r


t0 = time.time()
p = call("POST", "/api/projects", {"source": "fixture"})
pid = p["id"]
print("project", pid, "extraction", p["extraction"]["mode"], "warnings", p["extraction"]["warnings"])
gt = json.load(open("fixtures/synthetic_capture/ground_truth.json"))["true_corners_px_frame0"]
corners = [gt[k] for k in ("front_left", "front_right", "back_right", "back_left")]
p = call("POST", f"/api/projects/{pid}/calibrate", {"table_width": 1.4, "table_depth": 0.7, "corners_px": corners, "frame_index": 0})
print("calibrated ->", p["versions"][-1]["summary"])
run(pid)
c = call("POST", f"/api/projects/{pid}/correct", {"text": "Move the robot 50 cm toward the left edge"})
print("correction ->", c["explanation"], f"({c['parser']})")
r = run(pid)
if r.get("suggestion") and r["suggestion"].get("patch"):
    p = call("POST", f"/api/projects/{pid}/apply_suggestion", {"run_id": r["id"]})
    print("accepted suggestion ->", p["versions"][-1]["summary"])
    run(pid)
p = call("GET", f"/api/projects/{pid}")
print("versions:", [(v["version"], v["origin"]) for v in p["versions"]], "metrics", p["metrics"], f"total {time.time() - t0:.0f} s")

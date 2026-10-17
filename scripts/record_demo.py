"""Record the demo by driving the REAL app in headless Chromium (Playwright), logging step timestamps.

    python scripts/record_demo.py [--base http://localhost:8000] [--out demo/raw]

Writes demo/raw/session.webm and demo/raw/marks.json. Then run scripts/edit_demo.py to cut the 60 s video.
Every number on screen comes from the live server; nothing is mocked.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import shutil
import time
import urllib.request
from pathlib import Path

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
# Sync markers: a 16x16 swatch in the bottom-left corner changes colour at every step, so the editor can find
# each step in the video exactly (Playwright's screencast timing drifts when the CPU is busy simulating).
PALETTE = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0), (255, 0, 255), (0, 255, 255), (255, 128, 0),
           (128, 0, 255), (0, 128, 255), (255, 0, 128), (128, 255, 0), (0, 255, 128)]
CURSOR_JS = """
window.addEventListener('DOMContentLoaded', () => {
  const c = document.createElement('div');
  c.id = '__demo_cursor';
  Object.assign(c.style, {position: 'fixed', zIndex: 2147483647, width: '22px', height: '22px', borderRadius: '50%',
    border: '2px solid #fff', background: 'rgba(76,194,255,0.35)', boxShadow: '0 0 0 2px rgba(0,0,0,0.35)',
    pointerEvents: 'none', transform: 'translate(-50%,-50%)', transition: 'width .12s, height .12s', left: '-50px', top: '-50px'});
  document.body.appendChild(c);
  const m = document.createElement('div');
  m.id = '__sync';
  Object.assign(m.style, {position: 'fixed', zIndex: 2147483647, left: '0px', bottom: '0px', width: '16px', height: '16px',
    background: 'rgb(0,0,0)', pointerEvents: 'none'});
  document.body.appendChild(m);
  document.addEventListener('mousemove', e => { c.style.left = e.clientX + 'px'; c.style.top = e.clientY + 'px'; }, true);
  document.addEventListener('mousedown', () => { c.style.width = '14px'; c.style.height = '14px'; }, true);
  document.addEventListener('mouseup', () => { c.style.width = '22px'; c.style.height = '22px'; }, true);
});
"""


def api(base, path, method="GET", body=None):
    req = urllib.request.Request(base + path, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


async def main(base: str, out: Path):
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.webm"):
        f.unlink()
    exe = (glob.glob("/opt/pw-browsers/chromium-*/chrome-linux/chrome") or [None])[0]
    marks: list[dict] = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=exe)
        ctx = await browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=2,
                                        record_video_dir=str(out), record_video_size={"width": 1440, "height": 900})
        t0 = time.monotonic()
        await ctx.add_init_script(CURSOR_JS)
        page = await ctx.new_page()

        async def mark(name, shot=False, **kw):
            idx = len(marks)
            r, g, b = PALETTE[idx % len(PALETTE)]
            await page.evaluate(f"document.getElementById('__sync').style.background='rgb({r},{g},{b})'")
            await page.wait_for_timeout(120)
            marks.append({"name": name, "t": round(time.monotonic() - t0, 2), "color": [r, g, b], **kw})
            if shot:
                await page.screenshot(path=str(out / f"{name}.png"))
                marks[-1]["shot"] = f"{name}.png"
            print(f"{marks[-1]['t']:7.2f}s  {name}", flush=True)

        async def glide_click(locator, pause=350):
            box = await locator.bounding_box()
            await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=18)
            await page.wait_for_timeout(pause)
            await locator.click()

        async def type_into(locator, text):
            await glide_click(locator, 200)
            await locator.fill("")
            await locator.type(text, delay=38)

        async def run_and_wait(label):
            btn = page.locator("button", has_text="Run task on").first
            await glide_click(btn)
            await mark(f"{label}_run_clicked")
            # poll the server for the newest run of this project
            proj = api(base, f"/api/projects/{state['pid']}")
            rid = proj["runs"][0]["id"]
            while True:
                r = api(base, f"/api/runs/{rid}")
                if r["status"] in ("done", "error"):
                    break
                await page.wait_for_timeout(500)
            # wait until the UI itself shows the verdict for this run
            await page.wait_for_function("t => document.body.innerText.includes(t)", arg=rid[:8], timeout=15000)
            await page.wait_for_function(
                "() => /Placement succeeded|Task failed/i.test(document.querySelector('#checks')?.innerText || '')", timeout=15000)
            await page.wait_for_timeout(900)
            await page.evaluate("""() => { const ck = document.querySelector('#checks'); const col = document.querySelector('.col-sim');
                if (ck && col) col.scrollTo({top: Math.max(0, ck.offsetTop - 200), behavior: 'smooth'}); }""")
            await page.wait_for_timeout(700)
            await mark(f"{label}_run_done", run_id=rid, success=r["success"], video=r.get("video_url"))
            return r

        state = {}
        await page.goto(base + "/")
        await page.wait_for_timeout(2500)
        await mark("start")

        # 1. Capture (bundled synthetic capture: labelled FIXTURE in the UI)
        await glide_click(page.get_by_text("Bundled capture").first)
        await page.wait_for_timeout(3500)
        state["pid"] = api(base, "/api/projects")["projects"][0]["id"]
        await mark("capture_loaded", shot=True, pid=state["pid"])
        await page.wait_for_timeout(2500)

        # 2. Calibration: measured size + 4 corners on the overview frame
        await type_into(page.locator("#calW"), "140")
        await type_into(page.locator("#calD"), "70")
        await mark("calib_typed")
        await glide_click(page.locator("#markCorners"))
        await page.wait_for_timeout(1200)
        img = page.locator("#cmImg")
        await img.wait_for()
        await page.wait_for_timeout(600)
        nat = await img.evaluate("i => [i.naturalWidth, i.naturalHeight]")
        box = await img.bounding_box()
        gt = json.loads((ROOT / "fixtures/synthetic_capture/ground_truth.json").read_text())["true_corners_px_frame0"]
        for k in ("front_left", "front_right", "back_right", "back_left"):
            u, v = gt[k]
            x, y = box["x"] + u * box["width"] / nat[0], box["y"] + v * box["height"] / nat[1]
            await page.mouse.move(x, y, steps=16)
            await page.wait_for_timeout(250)
            await page.mouse.click(x, y)
        await mark("corners_marked", shot=True)
        await page.wait_for_timeout(900)
        await glide_click(page.locator("#cmSave"))
        await page.wait_for_timeout(3000)
        await mark("calibrated")

        # 3. Run on the calibrated scene
        await run_and_wait("run1")
        await page.wait_for_timeout(5000)
        await mark("run1_shown", shot=True)

        # 4. Plan a change: mount the robot further left
        await type_into(page.locator("#correctInput"), "Move the robot 50 cm toward the left edge")
        await glide_click(page.locator("#correctBtn"))
        await page.wait_for_timeout(2500)
        await mark("correction_applied", shot=True)
        await run_and_wait("run2")
        await page.wait_for_timeout(5500)
        await mark("run2_shown", shot=True)

        # 5. Accept the suggestion (never auto-applied)
        sug = page.locator("button", has_text="Apply as new version").first
        await glide_click(sug, 700)
        await page.wait_for_timeout(2500)
        await mark("suggestion_applied")
        await run_and_wait("run3")
        await page.wait_for_timeout(5000)
        await mark("run3_shown", shot=True)

        # 6. Timeline + downloads
        tl = page.locator("#timeline")
        b = await tl.bounding_box()
        await page.mouse.move(b["x"] + b["width"] * 0.3, b["y"] + b["height"] / 2, steps=20)
        await page.wait_for_timeout(1500)
        await page.mouse.move(b["x"] + b["width"] * 0.8, b["y"] + b["height"] / 2, steps=20)
        await page.wait_for_timeout(2000)
        await mark("end", shot=True)

        video_path = await page.video.path()
        await ctx.close()
        await browser.close()
    shutil.move(video_path, out / "session.webm")
    (out / "marks.json").write_text(json.dumps({"base": base, "pid": state["pid"], "marks": marks}, indent=2))
    print("wrote", out / "session.webm")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:8000")
    ap.add_argument("--out", default=str(ROOT / "demo" / "raw"))
    a = ap.parse_args()
    asyncio.run(main(a.base, Path(a.out)))

"""Cut demo/raw/session.webm (+ the runs' own MP4 replays) into a captioned ~60 s demo: demo/cellcheck_demo.mp4.

Waiting time is sped up and labelled as such; physics replays are the runs' real MP4s, shown sped up and labelled.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "demo" / "raw"
TMP = ROOT / "demo" / "build"
OUT = ROOT / "demo" / "cellcheck_demo.mp4"
W, H, FPS = 1440, 900, 30
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_R = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


def font(path, size):
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def caption_png(path: Path, step: str, text: str, speed: float | None = None):
    im = Image.new("RGBA", (W, 84), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, 84), fill=(8, 12, 18, 215))
    d.rectangle((0, 0, W, 3), fill=(76, 194, 255, 255))
    x = 28
    if step:
        f = font(FONT_B, 26)
        tw = d.textlength(step, font=f)
        d.rounded_rectangle((x, 22, x + tw + 28, 62), 10, fill=(76, 194, 255, 255))
        d.text((x + 14, 26), step, font=f, fill=(6, 12, 20))
        x += tw + 48
    d.text((x, 26), text, font=font(FONT_R, 27), fill=(235, 240, 248))
    if speed and speed > 1.15:
        s = f"{speed:.0f}× speed" if speed >= 2 else f"{speed:.1f}× speed"
        f = font(FONT_B, 20)
        tw = d.textlength(s, font=f)
        d.rounded_rectangle((W - tw - 52, 26, W - 24, 58), 8, outline=(240, 180, 41, 255), width=2)
        d.text((W - tw - 38, 30), s, font=f, fill=(240, 180, 41))
    im.save(path)


def card_png(path: Path, title: str, lines: list[str], small: str = ""):
    im = Image.new("RGB", (W, H), (10, 13, 18))
    d = ImageDraw.Draw(im)
    for i in range(H):
        c = int(10 + 8 * i / H)
        d.line((0, i, W, i), fill=(c, c + 3, c + 9))
    ft = font(FONT_B, 92)
    tw = d.textlength(title, font=ft)
    d.text(((W - tw) / 2, 270), title, font=ft, fill=(240, 244, 250))
    d.rectangle(((W - 120) / 2, 392, (W + 120) / 2, 397), fill=(76, 194, 255))
    y = 440
    for ln in lines:
        f = font(FONT_R, 36)
        tw = d.textlength(ln, font=f)
        d.text(((W - tw) / 2, y), ln, font=f, fill=(200, 210, 225))
        y += 56
    if small:
        f = font(FONT_R, 22)
        tw = d.textlength(small, font=f)
        d.text(((W - tw) / 2, H - 90), small, font=f, fill=(130, 140, 155))
    im.save(path)


def ff(*args):
    p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], capture_output=True, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr[-800:])


ENC = ["-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "20", "-r", str(FPS)]


def seg_ui(out, a, b, cap, speed=1.0):
    ff("-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", str(RAW / "session.webm"), "-i", str(cap), "-filter_complex",
       f"[0:v]setpts=(PTS-STARTPTS)/{speed},fps={FPS},scale={W}:{H},setsar=1[v];[v][1:v]overlay=0:H-h[o]", "-map", "[o]", *ENC, str(out))


def seg_clip(out, src, a, b, cap, speed=1.0):
    ff("-ss", f"{a:.2f}", "-t", f"{b - a:.2f}", "-i", str(src), "-i", str(cap), "-filter_complex",
       f"[0:v]setpts=(PTS-STARTPTS)/{speed},fps={FPS},scale={W}:{H}:force_original_aspect_ratio=decrease,"
       f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=0x0a0d12,setsar=1[v];[v][1:v]overlay=0:H-h[o]", "-map", "[o]", *ENC, str(out))


def seg_card(out, png, dur):
    ff("-loop", "1", "-t", f"{dur}", "-i", str(png), "-vf", f"fps={FPS},format=yuv420p", *ENC, str(out))


def marker_times(marks: list[dict], fps: int = 25) -> dict[str, float]:
    """Find when each step's sync colour first appears in the bottom-left swatch of the raw video."""
    p = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(RAW / "session.webm"), "-vf",
                        f"fps={fps},crop=8:8:4:{H - 12},scale=1:1:flags=area", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                       capture_output=True)
    px = p.stdout
    frames = [tuple(px[i:i + 3]) for i in range(0, len(px) - 2, 3)]
    out, k = {}, 0
    for fi, c in enumerate(frames):
        if k >= len(marks):
            break
        target = marks[k]["color"]
        if sum((a - b) ** 2 for a, b in zip(c, target)) < 60 ** 2:
            out[marks[k]["name"]] = fi / fps
            k += 1
    missing = [m["name"] for m in marks if m["name"] not in out]
    if missing:
        raise RuntimeError(f"sync markers not found for {missing}")
    return out


def seg_shot(out, png, box, dur, cap):
    """Still from a 2x screenshot, cropped to `box` (CSS px: x, y, w, h with w/h = 1.6) with a slow push-in."""
    x, y, w, h = (2 * v for v in box)
    frames = int(dur * FPS)
    ff("-framerate", str(FPS), "-loop", "1", "-t", f"{dur}", "-i", str(png), "-i", str(cap), "-filter_complex",
       f"[0:v]crop={w}:{h}:{x}:{y},scale={W * 2}:{H * 2},zoompan=z='1+0.03*on/{frames}':d=1:s={W}x{H}:fps={FPS}"
       f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',setsar=1[v];[v][1:v]overlay=0:H-h[o]", "-map", "[o]", *ENC, str(out))


def duration(path) -> float:
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                       capture_output=True, text=True)
    return float(p.stdout.strip())


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    info = json.loads((RAW / "marks.json").read_text())
    m = {x["name"]: x for x in info["marks"]}
    times = marker_times(info["marks"])
    t = lambda k: times[k]  # noqa: E731
    shot = lambda k: RAW / m[k]["shot"]  # noqa: E731
    data = Path(info.get("data_dir", ROOT / "data"))
    vid = lambda k: data / "runs" / m[k]["run_id"] / "run.mp4"  # noqa: E731

    plan = []  # (kind, args, step, caption, speed or target duration)

    def ui(a, b, step, text, target=None, speed=None):
        span = b - a
        sp = speed or (span / target if target else 1.0)
        plan.append(("ui", (a, b), step, text, max(sp, 1.0)))

    def still(key, box, dur, step, text):
        plan.append(("shot", (shot(key), box), step, text, dur))

    def clip(key, a, b, step, text, speed):
        src = vid(key)
        b = min(b, duration(src) - 0.05)
        plan.append(("clip", (src, a, b), step, text, speed))

    card_png(TMP / "title.png", "CellCheck", ["phone capture → editable workcell → robot task check"],
             "Physical AI Hack · London · 17 Oct 2026 · live app recording, nothing mocked")
    card_png(TMP / "end.png", "CellCheck", ["An approximate workcell you can edit,", "with transparent assumptions and visible checks."],
             "Checks of this simulated task only, not a real-world safety certification. Synthetic capture shown (labelled FIXTURE).")
    plan.append(("card", TMP / "title.png", "", "", 2.5))
    RESULT_BOX = (360, 168, 1072, 670)  # centre (checks, suggestion) + right column (correction diff, map)
    ui(t("start") - 0.3, t("capture_loaded") + 1.0, "1 Capture", "Phone video → sharp frames → structured scene estimate (synthetic capture)", target=4.5)
    ui(t("capture_loaded") + 1.0, t("calibrated") + 0.6, "2 Calibrate", "Measured table size + 4 clicked corners → positions in the table frame", target=7.0)
    ui(t("calibrated") + 0.6, t("run1_run_done"), "3 Simulate", "Scripted pick-and-place in MuJoCo physics (waiting sped up)", target=2.0)
    clip("run1_run_done", 2.0, 15.0, "3 Simulate", "Mink IK → actuator commands only → block held by contact friction", 3.2)
    still("run1_shown", RESULT_BOX, 3.2, "3 Simulate", "All 5 checks pass, and the result is bound to scene v2")
    ui(t("run1_shown"), t("correction_applied"), "4 Change", "\"Move the robot 50 cm toward the left edge\" → new scene version v3", target=4.0)
    ui(t("correction_applied"), t("run2_run_done"), "4 Change", "Re-running the task on v3 (waiting sped up)", target=1.5)
    clip("run2_run_done", 2.0, 12.0, "4 Change", "The target is outside the reachable zone, so the robot halts. No fake success", 2.6)
    still("run2_shown", RESULT_BOX, 5.5, "4 Change", "Reach check fails. Suggested fix is IK-verified and never applied silently")
    ui(t("run2_shown"), t("suggestion_applied"), "5 Fix", "Accept the suggestion → scene v4", target=2.5)
    ui(t("suggestion_applied"), t("run3_run_done"), "5 Fix", "Fresh run on v4 (waiting sped up)", target=1.5)
    clip("run3_run_done", 2.0, 15.0, "5 Fix", "Physics replay: placed inside the tote, below the rim, at rest", 3.2)
    still("run3_shown", RESULT_BOX, 3.2, "5 Fix", "Placement succeeded on v4. Earlier runs are kept and marked stale")
    still("end", (0, 0, 1440, 900), 3.0, "5 Fix", "Every run is bound to a version, with downloadable evidence (frames, JSON, MJCF, video)")
    plan.append(("card", TMP / "end.png", "", "", 3.5))

    parts = []
    for i, (kind, args, step, text, sp) in enumerate(plan):
        out = TMP / f"seg{i:02d}.mp4"
        if kind == "card":
            seg_card(out, args, sp)
        else:
            cap = TMP / f"cap{i:02d}.png"
            caption_png(cap, step, text, None if kind == "shot" else sp)
            if kind == "ui":
                seg_ui(out, *args, cap, sp)
            elif kind == "shot":
                seg_shot(out, args[0], args[1], sp, cap)
            else:
                seg_clip(out, *args, cap, sp)
        parts.append(out)
        print(f"seg{i:02d} {kind:5s} {duration(out):5.2f}s  {text[:70]}")
    lst = TMP / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    ff("-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart", str(OUT))
    print("wrote", OUT, f"{duration(OUT):.1f} s")


if __name__ == "__main__":
    sys.exit(main())

"""Headless rendering: EGL when available, OSMesa fallback. Produces PNG previews and MP4 replays.

Videos are replays of the qpos history recorded during the physics rollout (mj_forward per frame),
so rendering never feeds back into the simulation.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

if "MUJOCO_GL" not in os.environ:
    os.environ["MUJOCO_GL"] = "egl"

import mujoco  # noqa: E402
import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from .schema import Scene  # noqa: E402

W, H = 960, 600


def backend() -> str:
    return os.environ.get("MUJOCO_GL", "egl")


def _font(size: int):
    for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"):
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_renderer(model: mujoco.MjModel, w: int = W, h: int = H) -> mujoco.Renderer:
    return mujoco.Renderer(model, h, w)


def hero_camera(scene: Scene, azimuth: float = 62.0, elevation: float = -30.0) -> mujoco.MjvCamera:
    cam = mujoco.MjvCamera()
    cam.type = mujoco.mjtCamera.mjCAMERA_FREE
    t = scene.table
    cam.lookat[:] = [t.width / 2, t.depth / 2 - 0.02, t.height + 0.12]
    cam.distance = 1.05 + 0.75 * max(t.width, t.depth)
    cam.azimuth = azimuth  # from in front of the table, slightly left: left edge stays screen-left
    cam.elevation = elevation
    return cam


def topdown_camera(scene: Scene) -> mujoco.MjvCamera:
    cam = mujoco.MjvCamera()
    cam.type = mujoco.mjtCamera.mjCAMERA_FREE
    t = scene.table
    cam.lookat[:] = [t.width / 2, t.depth / 2, t.height]
    cam.distance = 1.6 * max(t.width, t.depth * 1.6)
    cam.azimuth = 90
    cam.elevation = -89.9
    return cam


def _render(r: mujoco.Renderer, data: mujoco.MjData, cam, shadows: bool) -> np.ndarray:
    r.update_scene(data, camera=cam)
    r.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = int(shadows)
    r.scene.flags[mujoco.mjtRndFlag.mjRND_REFLECTION] = 0
    return r.render()


def overlay(img: np.ndarray, top_left: str, top_right: str = "", bottom: str = "", color=(255, 255, 255)) -> Image.Image:
    im = Image.fromarray(img)
    d = ImageDraw.Draw(im, "RGBA")
    f = _font(17)
    fs = _font(14)
    if top_left:
        tw = d.textlength(top_left, font=f)
        d.rounded_rectangle((12, 12, 28 + tw, 44), 6, fill=(10, 14, 20, 190))
        d.text((20, 18), top_left, font=f, fill=color)
    if top_right:
        tw = d.textlength(top_right, font=fs)
        d.rounded_rectangle((im.width - tw - 28, 12, im.width - 12, 40), 6, fill=(10, 14, 20, 190))
        d.text((im.width - tw - 20, 18), top_right, font=fs, fill=(190, 200, 215))
    if bottom:
        tw = d.textlength(bottom, font=fs)
        d.rounded_rectangle((12, im.height - 42, 28 + tw, im.height - 12), 6, fill=(10, 14, 20, 190))
        d.text((20, im.height - 36), bottom, font=fs, fill=(190, 200, 215))
    return im


def render_still(xml: str, scene: Scene, out_png: Path, *, qpos: np.ndarray | None = None, view: str = "hero",
                 label: str = "", settle_s: float = 0.0) -> None:
    model = mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    _home(model, data)
    if qpos is not None:
        data.qpos[:] = qpos
    mujoco.mj_forward(model, data)
    for _ in range(int(settle_s / model.opt.timestep)):
        mujoco.mj_step(model, data)
    r = make_renderer(model)
    cam = hero_camera(scene) if view == "hero" else topdown_camera(scene)
    img = _render(r, data, cam, shadows=True)
    r.close()
    overlay(img, label).save(out_png)


def _home(model, data):
    from .sim import HOME_Q

    for i in range(7):
        data.qpos[model.joint(f"joint{i + 1}").qposadr[0]] = HOME_Q[i]
    for j in ("finger_joint1", "finger_joint2"):
        data.qpos[model.joint(j).qposadr[0]] = 0.04


def _encode_chunk(args) -> None:
    """Render frames [lo, hi) of a replay into one MP4 chunk (runs in a forked worker process)."""
    xml, scene_json, frames, lo, hi, fps, banner, verdict, n_motion, out = args
    scene = Scene.model_validate_json(scene_json)
    model = mujoco.MjModel.from_xml_string(xml)
    data = mujoco.MjData(model)
    r = make_renderer(model)
    cam = hero_camera(scene)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
           "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23", str(out)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for i in range(lo, hi):
        q, label = frames[i]
        data.qpos[:] = q
        mujoco.mj_forward(model, data)
        img = _render(r, data, cam, shadows=False)
        if i < n_motion:
            im = overlay(img, f"stage: {label}", banner, f"t = {i / fps:4.1f} s   ·   MuJoCo physics replay")
        else:
            text, ok = verdict
            im = overlay(img, text, banner, "", color=(120, 235, 150) if ok else (255, 120, 110))
        proc.stdin.write(np.asarray(im.convert("RGB")).tobytes())
    proc.stdin.close()
    proc.wait()
    r.close()
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed to encode a video chunk")


def render_video(xml: str, scene: Scene, qposes: list[np.ndarray], labels: list[str], out_mp4: Path, *,
                 fps: int = 25, banner: str = "", verdict: tuple[str, bool] | None = None, workers: int | None = None) -> Path:
    """Replay recorded qpos frames to an H.264 MP4. Frames are split across forked processes (each with its
    own GL context) and the chunks are concatenated losslessly with ffmpeg."""
    import multiprocessing as mp
    import tempfile

    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    frames = list(zip(qposes, labels))
    n_motion = len(frames)
    if verdict:
        frames += [(qposes[-1], "")] * int(fps * 1.6)
    workers = workers or max(1, min(4, (os.cpu_count() or 1)))
    with tempfile.TemporaryDirectory(dir=out_mp4.parent) as tmp:
        bounds = np.linspace(0, len(frames), workers + 1).astype(int)
        jobs = [(xml, scene.model_dump_json(), frames, int(lo), int(hi), fps, banner, verdict, n_motion, Path(tmp) / f"c{k}.mp4")
                for k, (lo, hi) in enumerate(zip(bounds[:-1], bounds[1:])) if hi > lo]
        if len(jobs) == 1 or backend() not in ("egl", "osmesa"):
            for j in jobs:
                _encode_chunk(j)
        else:
            with mp.get_context("fork").Pool(len(jobs)) as pool:
                pool.map(_encode_chunk, jobs)
        lst = Path(tmp) / "list.txt"
        lst.write_text("".join(f"file '{j[-1]}'\n" for j in jobs))
        p = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
                            "-movflags", "+faststart", str(out_mp4)], capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError("ffmpeg concat failed: " + p.stderr[-300:])
    return out_mp4

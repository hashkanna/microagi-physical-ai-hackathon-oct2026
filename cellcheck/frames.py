"""Sample a handful of sharp, evenly spread frames from a phone video with FFmpeg.

FFmpeg applies the stream's rotation metadata (display matrix) when decoding, so portrait
phone videos come out upright; frames are written as JPEGs with the long side <= max_side.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image

MIN_DURATION_S = 3.0


class VideoError(RuntimeError):
    """The video cannot be read or is unsuitable for sampling."""


def _run(cmd: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError:
        raise VideoError(f"{cmd[0]} is not installed") from None
    except subprocess.TimeoutExpired:
        raise VideoError(f"{cmd[0]} timed out after {timeout:.0f} s") from None


def probe(video_path: str | Path) -> dict:
    """Duration (s) and displayed (rotation-corrected) width/height of the first video stream."""
    p = Path(video_path)
    if not p.is_file():
        raise VideoError(f"video not found: {p}")
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
              "format=duration:stream=width,height,duration:stream_side_data=rotation", "-of", "json", str(p)])
    try:
        info = json.loads(r.stdout or b"{}")
    except json.JSONDecodeError:
        info = {}
    streams = info.get("streams") or []
    if r.returncode != 0 or not streams:
        msg = r.stderr.decode(errors="replace").strip().splitlines()
        raise VideoError(f"not a readable video ({msg[-1] if msg else 'no video stream'})")
    s = streams[0]
    dur = info.get("format", {}).get("duration") or s.get("duration")
    try:
        duration = float(dur)
    except (TypeError, ValueError):
        raise VideoError("could not determine video duration") from None
    rotation = int(next((float(d["rotation"]) for d in s.get("side_data_list", []) if "rotation" in d), 0))
    w, h = int(s.get("width", 0)), int(s.get("height", 0))
    if rotation % 180:
        w, h = h, w
    return {"duration": duration, "width": w, "height": h, "rotation": rotation}


def _grab(video: Path, t: float, out: Path, max_side: int) -> bool:
    vf = (f"scale=w='min({max_side},iw)':h='min({max_side},ih)'"
          ":force_original_aspect_ratio=decrease:force_divisible_by=2")
    r = _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
              "-vf", vf, "-q:v", "3", str(out)])
    return r.returncode == 0 and out.is_file() and out.stat().st_size > 0


def sharpness(img: Image.Image, side: int = 384) -> float:
    """Variance of the 4-neighbour Laplacian on a downscaled grayscale copy (higher = sharper)."""
    g = img.convert("L")
    g.thumbnail((side, side))
    a = np.asarray(g, dtype=np.float32)
    lap = a[1:-1, :-2] + a[1:-1, 2:] + a[:-2, 1:-1] + a[2:, 1:-1] - 4 * a[1:-1, 1:-1]
    return float(lap.var())


def sample_frames(video_path: str | Path, out_dir: str | Path, n: int = 10, *, max_side: int = 1280,
                  candidates_per_window: int = 3) -> list[dict]:
    """Write the sharpest frame of each of `n` equal time windows to out_dir/frame_XX.jpg.

    Returns [{"index", "path", "t", "width", "height", "sharpness"}] in time order.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    video = Path(video_path)
    meta = probe(video)
    if meta["duration"] < MIN_DURATION_S:
        raise VideoError(f"video is {meta['duration']:.1f} s long; need at least {MIN_DURATION_S:.0f} s")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for stale in out.glob("frame_*.jpg"):
        stale.unlink()
    tmp = out / ".candidates"
    tmp.mkdir(exist_ok=True)

    lo, hi = 0.05 * meta["duration"], 0.95 * meta["duration"]
    k = n * candidates_per_window
    times = [lo + (hi - lo) * (i + 0.5) / k for i in range(k)]

    def score(i_t):
        i, t = i_t
        path = tmp / f"c_{i:03d}.jpg"
        if not _grab(video, t, path, max_side):
            return None
        with Image.open(path) as im:
            return {"t": t, "tmp": path, "sharpness": sharpness(im), "width": im.width, "height": im.height}

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            cands = list(pool.map(score, enumerate(times)))
        frames = []
        for w in range(n):
            window = [c for c in cands[w * candidates_per_window:(w + 1) * candidates_per_window] if c]
            if not window:
                continue
            best = max(window, key=lambda c: c["sharpness"])
            dest = out / f"frame_{len(frames):02d}.jpg"
            shutil.move(best["tmp"], dest)
            frames.append({"index": len(frames), "path": str(dest), "t": round(best["t"], 2),
                           "width": best["width"], "height": best["height"],
                           "sharpness": round(best["sharpness"], 1)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if not frames:
        raise VideoError("ffmpeg could not decode any frames from the video")
    return frames


def frame_bytes(path: str | Path) -> bytes:
    """JPEG bytes of a frame (re-encoded only if it is not already a JPEG)."""
    p = Path(path)
    if p.suffix.lower() in (".jpg", ".jpeg"):
        return p.read_bytes()
    buf = io.BytesIO()
    with Image.open(p) as im:
        im.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()

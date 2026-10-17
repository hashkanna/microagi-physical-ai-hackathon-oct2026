import shutil
import subprocess

import pytest
from PIL import Image, ImageFilter

from cellcheck.frames import VideoError, probe, sample_frames, sharpness

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg not installed")


def _video(path, seconds=6.0, size="320x240", rotate=None):
    src = path.with_suffix(".src.mp4") if rotate else path
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=15", "-t", str(seconds),
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)], check=True)
    if rotate:  # phone-style rotation metadata, pixels stay landscape
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-display_rotation", str(rotate), "-i", str(src), "-c", "copy", str(path)], check=True)
    return path


def test_samples_n_sharp_frames_in_time_order(tmp_path):
    video = _video(tmp_path / "v.mp4")
    frames = sample_frames(video, tmp_path / "frames", n=5)
    assert [f["index"] for f in frames] == list(range(5))
    assert [f["path"].rsplit("/", 1)[1] for f in frames] == [f"frame_{i:02d}.jpg" for i in range(5)]
    ts = [f["t"] for f in frames]
    assert ts == sorted(ts) and 0.3 - 1e-6 <= ts[0] and ts[-1] <= 5.7 + 1e-6
    assert sorted(p.name for p in (tmp_path / "frames").iterdir()) == [f"frame_{i:02d}.jpg" for i in range(5)]
    with Image.open(frames[0]["path"]) as im:
        assert im.size == (320, 240) == (frames[0]["width"], frames[0]["height"])


def test_portrait_rotation_metadata_is_applied(tmp_path):
    video = _video(tmp_path / "r.mp4", seconds=4, rotate=90)
    assert probe(video)["rotation"] in (90, -90)
    frames = sample_frames(video, tmp_path / "frames", n=2)
    with Image.open(frames[0]["path"]) as im:
        assert im.size == (240, 320)


def test_long_side_is_capped(tmp_path):
    video = _video(tmp_path / "big.mp4", seconds=3.5, size="1920x1080")
    frames = sample_frames(video, tmp_path / "frames", n=2, max_side=640)
    with Image.open(frames[0]["path"]) as im:
        assert max(im.size) == 640 and im.size[1] == 360


def test_rejects_short_unreadable_and_missing(tmp_path):
    with pytest.raises(VideoError, match="at least 3"):
        sample_frames(_video(tmp_path / "short.mp4", seconds=2), tmp_path / "f")
    bad = tmp_path / "bad.mp4"
    bad.write_text("not a video")
    with pytest.raises(VideoError, match="not a readable video"):
        sample_frames(bad, tmp_path / "f")
    with pytest.raises(VideoError, match="not found"):
        probe(tmp_path / "missing.mp4")


def test_sharpness_prefers_sharp_images(tmp_path):
    video = _video(tmp_path / "v.mp4", seconds=3.5)
    with Image.open(sample_frames(video, tmp_path / "f", n=1)[0]["path"]) as im:
        im.load()
    assert sharpness(im) > 3 * sharpness(im.filter(ImageFilter.GaussianBlur(3)))

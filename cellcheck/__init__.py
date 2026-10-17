"""CellCheck: phone capture -> editable approximate workcell -> robot task validation."""

import os
import platform

# Headless rendering must be configured before mujoco is first imported. Linux: EGL preferred,
# MUJOCO_GL=osmesa for pure software rendering. macOS: CGL (headless, no window needed).
os.environ.setdefault("MUJOCO_GL", "cgl" if platform.system() == "Darwin" else "egl")
if os.environ["MUJOCO_GL"] in ("egl", "osmesa"):
    os.environ.setdefault("PYOPENGL_PLATFORM", os.environ["MUJOCO_GL"])

__version__ = "0.1.0"

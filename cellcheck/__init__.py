"""CellCheck: phone capture -> editable approximate workcell -> robot task validation."""

import os

# Headless rendering must be configured before mujoco is first imported. EGL is preferred;
# set MUJOCO_GL=osmesa for pure software rendering where EGL is unavailable.
os.environ.setdefault("MUJOCO_GL", "egl")
os.environ.setdefault("PYOPENGL_PLATFORM", os.environ["MUJOCO_GL"])

__version__ = "0.1.0"

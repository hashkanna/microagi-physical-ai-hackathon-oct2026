"""Exit 0 if MuJoCo can render headlessly with the given backend (egl | osmesa | glfw)."""

import os
import sys

os.environ["MUJOCO_GL"] = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("MUJOCO_GL", "egl")
if os.environ["MUJOCO_GL"] in ("egl", "osmesa"):
    os.environ.setdefault("PYOPENGL_PLATFORM", os.environ["MUJOCO_GL"])

import mujoco  # noqa: E402

m = mujoco.MjModel.from_xml_string("<mujoco><worldbody><light pos='0 0 2'/><geom type='box' size='.1 .1 .1'/></worldbody></mujoco>")
d = mujoco.MjData(m)
mujoco.mj_forward(m, d)
r = mujoco.Renderer(m, 64, 64)
r.update_scene(d)
img = r.render()
print(os.environ["MUJOCO_GL"], "ok", img.shape, float(img.mean()))
sys.stdout.flush()
os._exit(0 if img.mean() > 0 else 1)

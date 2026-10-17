"""Deterministic Scene (JSON) -> MJCF compiler.

The vision model never writes MJCF. This module turns a validated `Scene` into XML with ordinary
code. World frame == table frame shifted up by the table height, so world (x, y) equals table
(x, y) and the tabletop sits at world z = table.height.

Physical guarantees enforced here:
* table, bins and robot base are fixed (no joints); only the block gets a free joint;
* bins are open boxes made of one floor and four walls;
* sizes in the scene are FULL extents; MuJoCo box `size` is HALF extents (halved below);
* friction and mass come from the scene (explicit defaults are tagged as assumptions there).
"""

from __future__ import annotations

import copy
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .schema import Scene, SceneObject, footprint_corners, point_in_footprint

ROOT = Path(__file__).resolve().parent.parent
PANDA_DIR = ROOT / "assets" / "franka_emika_panda"
PANDA_XML = PANDA_DIR / "panda.xml"

# Distance from the hand body origin to the centre between the fingertip pads.
TCP_OFFSET = 0.1034
# Approximate Panda base footprint radius (link0), used for overlap checks only.
ROBOT_BASE_RADIUS = 0.11
# Menagerie's hand actuator is a soft position servo (kp = 100 N/m on the finger tendon) that squeezes a
# 4.5 cm block with only ~2 N, far below the real Franka Hand's 70 N continuous grasp force. We stiffen
# that servo 5x (~11 N squeeze, force limit unchanged at 100 N). This is a disclosed actuator assumption,
# not a weld: the block is still held purely by finger contact friction.
GRIP_GAIN_SCALE = 5.0
GRIPPER_MAX_OPENING = 0.08


class SceneError(ValueError):
    """A scene that cannot be compiled or is physically invalid, with a user-facing message."""


@dataclass
class Compiled:
    xml: str
    block_start_z: float
    warnings: list[str] = field(default_factory=list)


def _f(*vals: float) -> str:
    return " ".join(f"{v:.6g}" for v in vals)


def yaw_quat(yaw_deg: float) -> tuple[float, float, float, float]:
    h = math.radians(yaw_deg) / 2
    return (math.cos(h), 0.0, 0.0, math.sin(h))


def support_height(scene: Scene, x: float, y: float) -> tuple[float, str]:
    """Top surface (table frame z) under a point and what provides it."""
    for o in scene.objects:
        if o.kind == "bin" and point_in_footprint(o, x, y):
            return o.wall_thickness, o.id
    return 0.0, "table"


def precheck(scene: Scene) -> list[str]:
    """Geometric sanity checks before compiling. Raises SceneError on hard failures."""
    t = scene.table
    warnings: list[str] = []
    inside = lambda px, py, m=0.0: m <= px <= t.width - m and m <= py <= t.depth - m  # noqa: E731

    if not inside(scene.robot.x, scene.robot.y, ROBOT_BASE_RADIUS * 0.5):
        raise SceneError(
            f"Robot base at ({scene.robot.x:.2f}, {scene.robot.y:.2f}) m is not on the "
            f"{t.width:.2f} × {t.depth:.2f} m tabletop."
        )
    for o in scene.objects:
        corners = footprint_corners(o)
        out = [c for c in corners if not inside(*c)]
        if o.kind == "block" and not inside(o.x, o.y):
            raise SceneError(f"{o.id} centre ({o.x:.2f}, {o.y:.2f}) m is off the tabletop; it would fall.")
        if out:
            msg = f"{o.id} overhangs the table edge"
            if o.kind == "block":
                raise SceneError(msg + "; the block would be unstable.")
            warnings.append(msg + " (bins are fixed, so this is allowed but probably a measurement error).")
        if math.hypot(o.x - scene.robot.x, o.y - scene.robot.y) < ROBOT_BASE_RADIUS + min(o.size[:2]) / 2:
            raise SceneError(f"{o.id} overlaps the robot base footprint.")

    bins = [o for o in scene.objects if o.kind == "bin"]
    a, b = bins
    if _footprints_overlap(a, b):
        raise SceneError(f"Bins {a.id} and {b.id} overlap (initial penetration).")

    block = next(o for o in scene.objects if o.kind == "block")
    for bn in bins:
        if _footprints_overlap(block, bn) and not point_in_footprint(bn, block.x, block.y):
            raise SceneError(f"{block.id} straddles the wall of {bn.id}; it would start in penetration.")
        if point_in_footprint(bn, block.x, block.y):
            # must fit inside the walls
            for cx, cy in footprint_corners(block):
                if not point_in_footprint(bn, cx, cy, margin=bn.wall_thickness):
                    raise SceneError(f"{block.id} does not fit between the walls of {bn.id}.")
    return warnings


def _footprints_overlap(a: SceneObject, b: SceneObject) -> bool:
    """Separating-axis test on two yawed rectangles."""
    pa, pb = footprint_corners(a), footprint_corners(b)
    for poly in (pa, pb):
        for i in range(4):
            x1, y1 = poly[i]
            x2, y2 = poly[(i + 1) % 4]
            nx, ny = y2 - y1, x1 - x2
            pra = [nx * x + ny * y for x, y in pa]
            prb = [nx * x + ny * y for x, y in pb]
            if max(pra) < min(prb) or max(prb) < min(pra):
                return False
    return True


def _panda_tree(meshdir: str, base_pos, base_quat) -> ET.Element:
    tree = ET.parse(PANDA_XML)
    root = tree.getroot()
    root.find("compiler").set("meshdir", meshdir)
    link0 = root.find(".//body[@name='link0']")
    link0.set("pos", _f(*base_pos))
    link0.set("quat", _f(*base_quat))
    act = root.find(".//general[@name='actuator8']")
    g = [float(v) for v in act.get("gainprm").split()]
    b = [float(v) for v in act.get("biasprm").split()]
    act.set("gainprm", _f(g[0] * GRIP_GAIN_SCALE, *g[1:]))
    act.set("biasprm", _f(b[0], b[1] * GRIP_GAIN_SCALE, b[2] * GRIP_GAIN_SCALE))
    hand = root.find(".//body[@name='hand']")
    ET.SubElement(hand, "site", name="tcp", pos=_f(0, 0, TCP_OFFSET), size="0.006", rgba="1 0.3 0.1 0.6", group="4")
    return root


def compile_scene(scene: Scene, *, meshdir: str | None = None, robot_only: bool = False,
                  reach: dict | None = None) -> Compiled:
    """Return MJCF text for a scene. `robot_only` builds the IK model (robot at the same base pose)."""
    warnings = precheck(scene)
    t = scene.table
    H = t.height
    meshdir = meshdir or str(PANDA_DIR / "assets")
    root = _panda_tree(meshdir, (scene.robot.x, scene.robot.y, H), yaw_quat(scene.robot.yaw_deg))
    root.set("model", f"cellcheck_{scene.name}")

    opt = root.find("option")
    opt.set("timestep", "0.002")
    opt.set("cone", "elliptic")
    opt.set("impratio", "10")
    opt.set("noslip_iterations", "4")  # removes soft-contact creep in the grasp; no welds or extra forces

    # Visual / assets
    visual = ET.Element("visual")
    ET.SubElement(visual, "headlight", diffuse="0.55 0.55 0.55", ambient="0.35 0.35 0.38", specular="0 0 0")
    ET.SubElement(visual, "global", azimuth="150", elevation="-28", offwidth="1280", offheight="960")
    ET.SubElement(visual, "quality", shadowsize="2048")
    root.insert(1, visual)
    asset = root.find("asset")
    ET.SubElement(asset, "texture", name="grid", type="2d", builtin="checker", rgb1="0.13 0.15 0.18",
                  rgb2="0.10 0.12 0.14", width="512", height="512", mark="edge", markrgb="0.22 0.25 0.3")
    ET.SubElement(asset, "material", name="floor", texture="grid", texrepeat="8 8", texuniform="true", reflectance="0")
    ET.SubElement(asset, "material", name="tabletop", rgba="0.78 0.70 0.58 1", specular="0.1")
    ET.SubElement(asset, "material", name="tableleg", rgba="0.35 0.35 0.38 1")
    ET.SubElement(asset, "texture", name="sky", type="skybox", builtin="gradient", rgb1="0.16 0.19 0.24",
                  rgb2="0.05 0.06 0.08", width="256", height="1536")

    wb = root.find("worldbody")
    world_items: list[ET.Element] = []

    def add(parent, tag, **attrs):
        el = ET.Element(tag, {k: str(v) for k, v in attrs.items()})
        if parent is None:
            world_items.append(el)
        else:
            parent.append(el)
        return el

    add(None, "light", name="key", pos=_f(t.width / 2, -0.6, H + 2.2), dir=_f(0, 0.35, -1), directional="false",
        diffuse="0.55 0.55 0.55", castshadow="true", cutoff="70", attenuation="1 0 0")
    add(None, "light", name="fill", pos=_f(t.width / 2, t.depth + 1, H + 1.5), dir=_f(0, -0.6, -1),
        directional="true", diffuse="0.25 0.25 0.28", castshadow="false")

    if not robot_only:
        add(None, "geom", name="floor", type="plane", size="0 0 0.05", material="floor", pos=_f(t.width / 2, t.depth / 2, 0))
        # Table: collision top + visual legs
        tb = add(None, "body", name="table", pos=_f(t.width / 2, t.depth / 2, H - t.thickness / 2))
        add(tb, "geom", name="table_top", type="box", size=_f(t.width / 2, t.depth / 2, t.thickness / 2),
            material="tabletop", friction=_f(t.friction, 0.005, 0.0001))
        leg_h = H - t.thickness
        for i, (sx, sy) in enumerate(((-1, -1), (1, -1), (1, 1), (-1, 1))):
            add(tb, "geom", name=f"table_leg{i}", type="box", size=_f(0.025, 0.025, leg_h / 2), material="tableleg",
                pos=_f(sx * (t.width / 2 - 0.05), sy * (t.depth / 2 - 0.05), -t.thickness / 2 - leg_h / 2),
                contype="0", conaffinity="0")
        # Edge labels as thin coloured strips: front edge (y=0) blue, left edge (x=0) orange.
        add(tb, "geom", name="edge_front", type="box", size=_f(t.width / 2, 0.004, 0.0005), rgba="0.2 0.55 1 1",
            pos=_f(0, -t.depth / 2 + 0.004, t.thickness / 2 + 0.0005), contype="0", conaffinity="0")
        add(tb, "geom", name="edge_left", type="box", size=_f(0.004, t.depth / 2, 0.0005), rgba="1 0.6 0.1 1",
            pos=_f(-t.width / 2 + 0.004, 0, t.thickness / 2 + 0.0005), contype="0", conaffinity="0")

        if reach:
            _add_reach_overlay(add, reach, H)
        block_z = 0.0
        for o in scene.objects:
            if o.kind == "bin":
                _add_bin(add, o, H)
        for o in scene.objects:
            if o.kind == "block":
                sup, _ = support_height(scene, o.x, o.y)
                block_z = sup + o.size[2] / 2 + 0.0005
                bb = add(None, "body", name=o.id, pos=_f(o.x, o.y, H + block_z), quat=_f(*yaw_quat(o.yaw_deg)))
                add(bb, "freejoint", name=f"{o.id}_free")
                rgba = o.color or (0.85, 0.18, 0.15)
                add(bb, "geom", name=f"{o.id}_geom", type="box", size=_f(*(s / 2 for s in o.size)), mass=f"{o.mass:.6g}",
                    rgba=_f(*rgba, 1), friction=_f(o.friction, 0.01, 0.0002), condim="4", solref="0.01 1")

        # Cameras
        cx, cy = t.width / 2, t.depth / 2
        add(None, "camera", name="overview", mode="targetbody", target="table",
            pos=_f(cx + 0.95 * t.width, -0.95 * t.depth - 0.6, H + 1.0))
        add(None, "camera", name="front", mode="targetbody", target="table", pos=_f(cx - 0.2, -1.25, H + 0.75))
        add(None, "camera", name="topdown", pos=_f(cx, cy, H + 2.2), quat="1 0 0 0", fovy="40")
    else:
        block_z = 0.0

    for el in world_items:
        wb.append(el)

    # Remove the panda keyframe if objects add DOFs (keyframe qpos length would mismatch).
    kf = root.find("keyframe")
    if kf is not None:
        root.remove(kf)

    ET.indent(root, space="  ")
    xml = ET.tostring(root, encoding="unicode")
    return Compiled(xml=xml, block_start_z=block_z, warnings=warnings)


def _add_reach_overlay(add, reach: dict, H: float) -> None:
    """Visual-only translucent tiles marking where a top-down grasp is reachable (no collision, no mass)."""
    body = add(None, "body", name="reach_overlay", pos=_f(0, 0, H + 0.0025))  # above z-buffer precision
    step = reach["step"]
    for j, row in enumerate(reach["reachable"]):
        i = 0
        while i < len(row):
            if not row[i]:
                i += 1
                continue
            k = i
            while k + 1 < len(row) and row[k + 1]:
                k += 1
            x0 = reach["x0"] + i * step - step / 2
            x1 = reach["x0"] + k * step + step / 2
            y = reach["y0"] + j * step
            add(body, "geom", type="box", size=_f((x1 - x0) / 2, step / 2, 0.0004), pos=_f((x0 + x1) / 2, y, 0),
                rgba="0.2 0.85 0.4 0.22", contype="0", conaffinity="0", mass="0", group="1")
            i = k + 1


def _add_bin(add, o: SceneObject, H: float) -> None:
    sx, sy, sz = o.size
    w = o.wall_thickness
    rgba = _f(*(o.color or (0.25, 0.45, 0.85)), 1)
    body = add(None, "body", name=o.id, pos=_f(o.x, o.y, H), quat=_f(*yaw_quat(o.yaw_deg)))
    fr = "0.8 0.005 0.0001"
    add(body, "geom", name=f"{o.id}_floor", type="box", size=_f(sx / 2, sy / 2, w / 2), pos=_f(0, 0, w / 2), rgba=rgba, friction=fr)
    hz = (sz - w) / 2
    zc = w + hz
    add(body, "geom", name=f"{o.id}_wall_front", type="box", size=_f(sx / 2, w / 2, hz), pos=_f(0, -sy / 2 + w / 2, zc), rgba=rgba, friction=fr)
    add(body, "geom", name=f"{o.id}_wall_back", type="box", size=_f(sx / 2, w / 2, hz), pos=_f(0, sy / 2 - w / 2, zc), rgba=rgba, friction=fr)
    add(body, "geom", name=f"{o.id}_wall_left", type="box", size=_f(w / 2, sy / 2 - w, hz), pos=_f(-sx / 2 + w / 2, 0, zc), rgba=rgba, friction=fr)
    add(body, "geom", name=f"{o.id}_wall_right", type="box", size=_f(w / 2, sy / 2 - w, hz), pos=_f(sx / 2 - w / 2, 0, zc), rgba=rgba, friction=fr)


def compile_bundle_xml(scene: Scene) -> str:
    """MJCF with a relative meshdir for export bundles (assets/ copied alongside)."""
    return compile_scene(copy.deepcopy(scene), meshdir="assets").xml

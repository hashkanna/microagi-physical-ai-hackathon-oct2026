"""Physics rollout of the scripted pick-and-place task, with honest checks.

* Planning uses a separate, robot-only IK model (Mink) placed at the same base pose.
* Execution only writes actuator `ctrl` targets and calls `mj_step`. The block moves purely
  through contact physics: no teleporting, no welds, no disabled collisions, normal gravity.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import mink
import mujoco
import numpy as np

from .compiler import compile_scene, support_height
from .schema import Scene, point_in_footprint

HOME_Q = np.array([0.0, -0.3, 0.0, -2.2, 0.0, 1.9, 0.7853])
ARM_JOINTS = [f"joint{i}" for i in range(1, 8)]
GRIP_OPEN, GRIP_CLOSED = 255.0, 0.0
CTRL_DT = 0.02  # controller tick (50 Hz); physics runs at 500 Hz underneath
POS_TOL = 0.006  # m, IK position tolerance for a waypoint to count as reachable
HAND_BELOW_TCP = 0.033  # m, underside of the Panda hand housing above the TCP (from the collision mesh AABB)
ORI_TOL = 0.08  # rad

STAGES = ["approach", "descend", "close gripper", "lift", "transfer", "descend to bin", "open gripper", "retreat", "observe"]


@dataclass
class Check:
    id: str
    label: str
    status: str = "skip"  # pass | fail | skip
    detail: str = ""

    def as_dict(self):
        return {"id": self.id, "label": self.label, "status": self.status, "detail": self.detail}


@dataclass
class Result:
    checks: dict[str, Check]
    success: bool = False
    failure_reason: str | None = None
    suggestion: dict | None = None
    metrics: dict = field(default_factory=dict)
    frames_qpos: list[np.ndarray] = field(default_factory=list)  # for rendering replay
    frame_labels: list[str] = field(default_factory=list)
    waypoints: list[dict] = field(default_factory=list)
    xml: str = ""

    def as_dict(self):
        return {
            "checks": [c.as_dict() for c in self.checks.values()],
            "success": self.success,
            "failure_reason": self.failure_reason,
            "suggestion": self.suggestion,
            "metrics": self.metrics,
            "waypoints": self.waypoints,
        }


def new_checks() -> dict[str, Check]:
    return {
        "compiled": Check("compiled", "Scene compiled"),
        "settled": Check("settled", "Scene settled"),
        "reachable": Check("reachable", "Required targets reachable"),
        "collisions": Check("collisions", "No unexpected collisions"),
        "placement": Check("placement", "Block placed in target bin"),
    }


# ----------------------------------------------------------------------------------------------
# Geometry helpers


def _rotz(yaw: float) -> np.ndarray:
    c, s = math.cos(yaw), math.sin(yaw)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1.0]])


def grasp_rotation(block_yaw_deg: float, robot_yaw_deg: float) -> np.ndarray:
    """Gripper pointing straight down, closing across the block faces.

    Candidate yaws are block_yaw + k*90deg; pick the one closest to the robot's natural wrist yaw
    to stay away from joint-7 limits.
    """
    flip = np.diag([1.0, -1.0, -1.0])  # Rx(pi): tool z points down
    natural = math.radians(robot_yaw_deg) - math.pi / 4  # hand is mounted at -45deg about link7
    best, best_d = None, 9.0
    for k in range(4):
        yaw = math.radians(block_yaw_deg) + k * math.pi / 2
        d = abs((yaw - natural + math.pi) % (2 * math.pi) - math.pi)
        if d < best_d:
            best, best_d = yaw, d
    return _rotz(best) @ flip


def _yaw_of(R: np.ndarray) -> float:
    return math.atan2(R[1, 0], R[0, 0])


def _arc_path(p0, R0, p1, R1, base_xy):
    """TCP path around the robot base: radius, bearing, height and tool yaw interpolate linearly."""
    bx, by = base_xy
    r0, r1 = math.hypot(p0[0] - bx, p0[1] - by), math.hypot(p1[0] - bx, p1[1] - by)
    a0, a1 = math.atan2(p0[1] - by, p0[0] - bx), math.atan2(p1[1] - by, p1[0] - bx)
    da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
    y0, y1 = _yaw_of(R0), _yaw_of(R1)
    dy = (y1 - y0 + math.pi) % (2 * math.pi) - math.pi
    flip = np.diag([1.0, -1.0, -1.0])

    def path(s: float):
        r, a = r0 + s * (r1 - r0), a0 + s * da
        p = np.array([bx + r * math.cos(a), by + r * math.sin(a), p0[2] + s * (p1[2] - p0[2])])
        return p, _rotz(y0 + s * dy) @ flip

    return path


def min_jerk(s: float) -> float:
    return 10 * s**3 - 15 * s**4 + 6 * s**5


# ----------------------------------------------------------------------------------------------
# IK planning on a robot-only model


class Planner:
    def __init__(self, scene: Scene):
        self.model = mujoco.MjModel.from_xml_string(compile_scene(scene, robot_only=True).xml)
        self.cfg = mink.Configuration(self.model)
        self.ee = mink.FrameTask("tcp", "site", position_cost=1.0, orientation_cost=0.5, lm_damping=1e-2)
        self.posture = mink.PostureTask(self.model, cost=1e-3)
        self.limits = [mink.ConfigurationLimit(self.model)]
        q0 = np.zeros(self.model.nq)
        q0[:7] = HOME_Q
        q0[7:9] = 0.04
        self.cfg.update(q0)
        self.posture.set_target(q0)
        self.home = q0.copy()

    def tcp_pose(self, q: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        self.cfg.update(q)
        T = self.cfg.get_transform_frame_to_world("tcp", "site")
        return T.translation(), T.rotation().as_matrix()

    def solve(self, q_init: np.ndarray, pos: np.ndarray, rot: np.ndarray, iters: int = 200):
        """Return (q, pos_err, ori_err). Warm-started; respects joint limits."""
        self.cfg.update(q_init)
        target = mink.SE3.from_rotation_and_translation(mink.SO3.from_matrix(rot), pos)
        self.ee.set_target(target)
        dt = 0.05
        for _ in range(iters):
            v = mink.solve_ik(self.cfg, [self.ee, self.posture], dt, "daqp", damping=1e-3, limits=self.limits)
            self.cfg.integrate_inplace(v, dt)
            p, e_o = self._err(pos, rot)
            if p < 5e-4 and e_o < 5e-3:
                break
        p, e_o = self._err(pos, rot)
        return self.cfg.q.copy(), p, e_o

    def _err(self, pos, rot):
        T = self.cfg.get_transform_frame_to_world("tcp", "site")
        perr = float(np.linalg.norm(T.translation() - pos))
        R = T.rotation().as_matrix().T @ rot
        oerr = float(math.acos(max(-1.0, min(1.0, (np.trace(R) - 1) / 2))))
        return perr, oerr


@dataclass
class Segment:
    stage: str
    kind: str  # "cart" | "joint" | "grip" | "hold"
    duration: float
    pos: np.ndarray | None = None
    rot: np.ndarray | None = None
    grip: float | None = None
    q_end: np.ndarray | None = None
    reachable: bool = True
    err: float = 0.0
    trajectory: list[np.ndarray] = field(default_factory=list)


def plan_task(scene: Scene, planner: Planner) -> tuple[list[Segment], list[dict]]:
    """Build the waypoint sequence from the CURRENT scene coordinates and solve IK for each step."""
    H = scene.table.height
    block = scene.obj(scene.task.object_id)
    target = scene.obj(scene.task.target_id)
    sup, _ = support_height(scene, block.x, block.y)
    block_c = np.array([block.x, block.y, H + sup + block.size[2] / 2])
    rim = max(o.size[2] for o in scene.objects if o.kind == "bin")
    safe_z = H + max(rim, sup + block.size[2]) + block.size[2] + 0.12
    R_pick = grasp_rotation(block.yaw_deg, scene.robot.yaw_deg)
    R_place = grasp_rotation(target.yaw_deg, scene.robot.yaw_deg)
    grasp_z = block_c[2] + min(0.01, block.size[2] * 0.2)
    # Release low enough that the block ends below the rim, but keep the hand housing (its underside is
    # HAND_BELOW_TCP above the TCP, and it is ~20 cm long) at least 3 cm above the rim.
    place_z = max(H + target.wall_thickness + block.size[2] / 2 + 0.02 + (grasp_z - block_c[2]),
                  H + target.size[2] + 0.03 - HAND_BELOW_TCP)
    pre_pick = np.array([block.x, block.y, safe_z])
    pre_place = np.array([target.x, target.y, safe_z])

    segs = [
        Segment("approach", "joint", 2.0, pre_pick, R_pick),
        Segment("descend", "cart", 1.4, np.array([block.x, block.y, grasp_z]), R_pick),
        Segment("close gripper", "grip", 0.9, grip=GRIP_CLOSED),
        Segment("lift", "cart", 1.4, pre_pick, R_pick),
        Segment("transfer", "arc", 2.2, pre_place, R_place),
        Segment("descend to bin", "cart", 1.4, np.array([target.x, target.y, place_z]), R_place),
        Segment("open gripper", "grip", 0.7, grip=GRIP_OPEN),
        Segment("retreat", "cart", 1.2, pre_place, R_place),
        Segment("observe", "hold", 1.5),
    ]

    q = planner.home.copy()
    report = []
    for seg in segs:
        if seg.kind in ("grip", "hold"):
            seg.q_end = q.copy()
            continue
        if seg.kind in ("cart", "arc"):
            p0, R0 = planner.tcp_pose(q)
            path = _arc_path(p0, R0, seg.pos, seg.rot, (scene.robot.x, scene.robot.y)) if seg.kind == "arc" \
                else (lambda s, p0=p0, p1=seg.pos, R=seg.rot: (p0 + s * (p1 - p0), R))
            if seg.kind == "arc":  # keep the payload slow: ~0.25 m/s average along the arc
                length = sum(np.linalg.norm(path((i + 1) / 20)[0] - path(i / 20)[0]) for i in range(20))
                seg.duration = max(seg.duration, length / 0.25)
            n = max(2, int(seg.duration / CTRL_DT))
            traj = []
            qi = q.copy()
            worst = 0.0
            for i in range(1, n + 1):
                pi, Ri = path(min_jerk(i / n))
                qi, perr, oerr = planner.solve(qi, pi, Ri, iters=40)
                worst = max(worst, perr)
                traj.append(qi[:7].copy())
            qf, perr, oerr = planner.solve(qi, seg.pos, seg.rot)
            traj[-1] = qf[:7].copy()
            perr = max(perr, worst if worst > 3 * POS_TOL else 0.0)  # mid-path misses also count
        else:
            n = max(2, int(seg.duration / CTRL_DT))
            qf, perr, oerr = planner.solve(q, seg.pos, seg.rot, iters=400)
            traj = [q[:7] + min_jerk(i / n) * (qf[:7] - q[:7]) for i in range(1, n + 1)]
            worst = perr
        seg.trajectory = traj
        seg.q_end = qf
        seg.err = perr
        seg.reachable = bool(perr < POS_TOL and oerr < ORI_TOL)
        report.append({
            "stage": seg.stage,
            "target": [round(float(v), 4) for v in seg.pos],
            "pos_error_m": round(perr, 4),
            "ori_error_rad": round(oerr, 4),
            "reachable": seg.reachable,
        })
        q = qf
    return segs, report


# ----------------------------------------------------------------------------------------------
# Physics rollout


class Physics:
    def __init__(self, xml: str, scene: Scene):
        self.model = mujoco.MjModel.from_xml_string(xml)
        self.data = mujoco.MjData(self.model)
        m = self.model
        self.block_id = scene.task.object_id
        self.block_body = m.body(self.block_id).id
        self.block_qadr = m.jnt_qposadr[m.joint(f"{self.block_id}_free").id]
        self.block_vadr = m.jnt_dofadr[m.joint(f"{self.block_id}_free").id]
        self.arm_qadr = [m.jnt_qposadr[m.joint(j).id] for j in ARM_JOINTS]
        # Geom groups for contact classification
        link0 = m.body("link0").id
        robot_bodies = {b for b in range(m.nbody) if self._descends(b, link0)}
        finger_bodies = {m.body("left_finger").id, m.body("right_finger").id}
        hand_body = m.body("hand").id
        self.geom_role = {}
        for g in range(m.ngeom):
            b = m.geom_bodyid[g]
            if b in finger_bodies:
                role = "finger"
            elif b == hand_body:
                role = "hand"
            elif b in robot_bodies:
                role = "arm"
            elif b == self.block_body:
                role = "block"
            elif b == 0:
                role = "floor"
            else:
                role = "fixture"  # table, bins
            self.geom_role[g] = role
        self.contacts_unexpected: dict[tuple[str, str], dict] = {}
        self.contacts_intended: set[tuple[str, str]] = set()
        self.max_force = 0.0
        self.t = 0.0
        self.stage = "settle"

    def geom_name(self, g: int) -> str:
        return self.model.geom(g).name or self.model.body(self.model.geom_bodyid[g]).name

    def _descends(self, b: int, root: int) -> bool:
        while b != 0:
            if b == root:
                return True
            b = self.model.body_parentid[b]
        return False

    def set_home(self):
        d = self.data
        for i, a in enumerate(self.arm_qadr):
            d.qpos[a] = HOME_Q[i]
        d.qpos[self.model.joint("finger_joint1").qposadr[0]] = 0.04
        d.qpos[self.model.joint("finger_joint2").qposadr[0]] = 0.04
        d.ctrl[:7] = HOME_Q
        d.ctrl[7] = GRIP_OPEN
        mujoco.mj_forward(self.model, d)

    def block_pos(self) -> np.ndarray:
        return self.data.qpos[self.block_qadr:self.block_qadr + 3].copy()

    def block_speed(self) -> float:
        return float(np.linalg.norm(self.data.qvel[self.block_vadr:self.block_vadr + 3]))

    def step(self, n: int = 1):
        m, d = self.model, self.data
        f6 = np.zeros(6)
        for _ in range(n):
            mujoco.mj_step(m, d)
            self.t += m.opt.timestep
            for i in range(d.ncon):
                c = d.contact[i]
                r1, r2 = self.geom_role[c.geom1], self.geom_role[c.geom2]
                roles = tuple(sorted((r1, r2)))
                if roles in {("block", "fixture"), ("block", "floor"), ("fixture", "fixture")}:
                    continue
                if roles in {("block", "finger"), ("block", "hand")}:
                    self.contacts_intended.add(roles)
                    continue
                mujoco.mj_contactForce(m, d, i, f6)
                fn = abs(f6[0])
                if fn < 0.5:  # ignore grazing contacts below 0.5 N
                    continue
                n1, n2 = self.geom_name(c.geom1), self.geom_name(c.geom2)
                key = tuple(sorted((n1, n2)))
                rec = self.contacts_unexpected.setdefault(key, {"pair": list(key), "roles": list(roles),
                                                                  "first_t": round(self.t, 2), "stage": self.stage, "max_force_n": 0.0})
                rec["max_force_n"] = round(max(rec["max_force_n"], fn), 1)
                self.max_force = max(self.max_force, fn)


def run_task(scene: Scene, *, fps: int = 25, progress=None, reach: dict | None = None) -> Result:
    """Compile, settle, plan, execute and evaluate. Never raises for task failures."""
    res = Result(checks=new_checks())
    ck = res.checks
    t0 = time.time()
    say = progress or (lambda stage: None)

    # 1. Compile
    say("compiling")
    try:
        comp = compile_scene(scene, reach=reach)
        phys = Physics(comp.xml, scene)
        res.xml = comp.xml
        ck["compiled"].status = "pass"
        ck["compiled"].detail = f"{phys.model.nbody} bodies, {phys.model.ngeom} geoms, 1 free body ({scene.task.object_id})"
        if comp.warnings:
            ck["compiled"].detail += "; warnings: " + "; ".join(comp.warnings)
    except Exception as e:  # SceneError or MuJoCo XML error
        ck["compiled"].status = "fail"
        ck["compiled"].detail = str(e)
        res.failure_reason = f"Scene did not compile: {e}"
        return res

    frame_every = max(1, int(round(1.0 / fps / phys.model.opt.timestep)))
    steps_per_tick = int(round(CTRL_DT / phys.model.opt.timestep))

    def record(label: str):
        res.frames_qpos.append(phys.data.qpos.copy())
        res.frame_labels.append(label)

    # 2. Initial penetration + settle
    say("settling")
    phys.set_home()
    pen = [(phys.model.geom(c.geom1).name, phys.model.geom(c.geom2).name, c.dist)
           for c in phys.data.contact[: phys.data.ncon] if c.dist < -0.002]
    start = phys.block_pos()
    settle_steps = int(0.8 / phys.model.opt.timestep)
    for i in range(settle_steps):
        phys.step()
        if i % frame_every == 0:
            record("settle")
    drift = float(np.linalg.norm(phys.block_pos() - start))
    speed = phys.block_speed()
    if pen:
        ck["settled"].status = "fail"
        ck["settled"].detail = "initial penetration: " + ", ".join(f"{a}/{b} {d * 1000:.1f} mm" for a, b, d in pen[:3])
    elif drift > 0.005 or speed > 0.01:
        ck["settled"].status = "fail"
        ck["settled"].detail = f"block moved {drift * 1000:.1f} mm while settling (speed {speed:.3f} m/s) — unstable placement"
    else:
        ck["settled"].status = "pass"
        ck["settled"].detail = f"block drift {drift * 1000:.2f} mm over 0.8 s"
    phys.contacts_unexpected.clear()
    if ck["settled"].status == "fail":
        res.failure_reason = "Scene is not physically stable before the task starts: " + ck["settled"].detail
        return _finish(res, phys, scene, t0)

    # 3. Plan (IK on the separate robot-only model)
    say("planning")
    from .compiler import GRIPPER_MAX_OPENING

    blk = scene.obj(scene.task.object_id)
    if min(blk.size[:2]) > GRIPPER_MAX_OPENING - 0.005:
        ck["reachable"].status = "fail"
        ck["reachable"].detail = (f"grasp infeasible: {blk.id} is {min(blk.size[:2]) * 100:.1f} cm across its narrowest side; "
                                  f"the Panda hand opens {GRIPPER_MAX_OPENING * 100:.0f} cm")
        res.failure_reason = "Block is too wide for the Panda hand: " + ck["reachable"].detail
        return _finish(res, phys, scene, t0)
    planner = Planner(scene)
    segs, report = plan_task(scene, planner)
    res.waypoints = report
    bad = [r for r in report if not r["reachable"]]
    worst = max(report, key=lambda r: r["pos_error_m"])
    if bad:
        ck["reachable"].status = "fail"
        b = max(bad, key=lambda r: r["pos_error_m"])
        ck["reachable"].detail = (f"{len(report) - len(bad)}/{len(report)} motion targets reachable; '{b['stage']}' "
                                  f"misses its target by {b['pos_error_m'] * 100:.1f} cm")
    else:
        ck["reachable"].status = "pass"
        ck["reachable"].detail = f"{len(report)}/{len(report)} motion targets reachable, worst IK error {worst['pos_error_m'] * 1000:.1f} mm"

    # 4. Execute (actuator commands + physics only). Like a real controller, execution halts
    #    before any motion whose target IK cannot reach; the arm holds its last commanded pose.
    say("executing")
    grip = GRIP_OPEN

    def run_ticks(n: int, label: str, on_tick=None):
        for i in range(n):
            if on_tick:
                on_tick(i)
            for _ in range(steps_per_tick):
                phys.step()
                if int(round(phys.t / phys.model.opt.timestep)) % frame_every == 0:
                    record(label)

    for seg in segs:
        phys.stage = seg.stage
        if seg.kind in ("cart", "joint", "arc") and not seg.reachable:
            phys.stage = f"halted before {seg.stage}"
            res.metrics["halted_before_stage"] = seg.stage
            run_ticks(int(1.8 / CTRL_DT), f"HALTED: {seg.stage} target out of reach")
            break
        n = max(1, int(seg.duration / CTRL_DT))
        if seg.kind in ("cart", "joint", "arc"):
            run_ticks(n, seg.stage, lambda i: phys.data.ctrl.__setitem__(slice(0, 7), seg.trajectory[min(i, len(seg.trajectory) - 1)]))
        elif seg.kind == "grip":
            g0 = grip
            run_ticks(n, seg.stage, lambda i: phys.data.ctrl.__setitem__(7, g0 + min_jerk((i + 1) / n) * (seg.grip - g0)))
            grip = seg.grip
        else:
            run_ticks(n, seg.stage)
        if seg.stage == "lift":
            b = scene.obj(scene.task.object_id)
            rest_z = scene.table.height + support_height(scene, b.x, b.y)[0] + b.size[2] / 2
            res.metrics["block_lifted_m"] = round(float(phys.block_pos()[2] - rest_z), 4)

    return _finish(res, phys, scene, t0)


def _finish(res: Result, phys: Physics, scene: Scene, t0: float) -> Result:
    ck = res.checks
    # Collisions
    if ck["settled"].status == "pass":
        if phys.contacts_unexpected:
            ck["collisions"].status = "fail"
            worst = sorted(phys.contacts_unexpected.values(), key=lambda r: -r["max_force_n"])
            ck["collisions"].detail = "; ".join(f"{r['pair'][0]} ↔ {r['pair'][1]} during {r['stage']} ({r['max_force_n']} N)" for r in worst[:3])
        else:
            ck["collisions"].status = "pass"
            intended = ", ".join(f"{a}–{b}" for a, b in sorted(phys.contacts_intended)) or "none"
            ck["collisions"].detail = f"only intended contacts (gripper–block: {intended}; block–supports)"
        res.metrics["unexpected_contacts"] = list(phys.contacts_unexpected.values())

    # Placement
    if ck["reachable"].status != "skip" and ck["settled"].status == "pass":
        H = scene.table.height
        target = scene.obj(scene.task.target_id)
        block = scene.obj(scene.task.object_id)
        p = phys.block_pos()
        # observe for a further 0.5 s and measure speed
        speeds = []
        for _ in range(int(0.5 / phys.model.opt.timestep)):
            phys.step()
            speeds.append(phys.block_speed())
        p = phys.block_pos()
        inside = point_in_footprint(target, p[0], p[1], margin=target.wall_thickness)
        below_rim = p[2] - H < target.size[2]
        resting = max(speeds) < 0.02
        lifted = res.metrics.get("block_lifted_m", 0.0) > 0.03
        res.metrics.update({
            "block_final_table_frame": [round(float(p[0]), 3), round(float(p[1]), 3), round(float(p[2] - H), 3)],
            "block_final": [round(float(p[0]), 3), round(float(p[1]), 3), round(float(p[2] - H), 3)],
            "block_max_speed_last_0_5s": round(max(speeds), 4),
            "sim_time_s": round(phys.t, 2),
        })
        ok = bool(inside and below_rim and resting)
        ck["placement"].status = "pass" if ok else "fail"
        where = (f"block at ({p[0]:.2f}, {p[1]:.2f}) m, {(p[2] - H) * 100:.1f} cm above tabletop")
        if ok:
            ck["placement"].detail = f"{where}: inside {target.id}, below rim, speed < 0.02 m/s for 0.5 s"
        else:
            why = []
            if "halted_before_stage" in res.metrics:
                why.append(f"execution halted before '{res.metrics['halted_before_stage']}' (target out of reach)")
            elif not lifted:
                why.append("block was never lifted (grasp failed)")
            if not inside:
                why.append(f"not inside {target.id}")
            if not below_rim:
                why.append("above the rim")
            if not resting:
                why.append("still moving")
            ck["placement"].detail = f"{where}: " + ", ".join(why)
        res.success = bool(ok and ck["collisions"].status == "pass")
        if not res.success and res.failure_reason is None:
            if ck["reachable"].status == "fail":
                res.failure_reason = "Required targets are out of reach: " + ck["reachable"].detail
            elif not ok:
                res.failure_reason = "Placement failed: " + ck["placement"].detail
            else:
                res.failure_reason = "Unexpected collision: " + ck["collisions"].detail
    res.metrics["wall_time_s"] = round(time.time() - t0, 2)
    res.metrics.setdefault("sim_time_s", round(phys.t, 2))
    return res

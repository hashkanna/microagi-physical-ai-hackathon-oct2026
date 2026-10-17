"""CellCheck scene schema (cellcheck.scene/v1).

Coordinate frame (the "table frame"), stated explicitly so every value is unambiguous:

* Stand at the table's FRONT edge (the edge the camera operator faces) looking at the table.
* Origin: the FRONT-LEFT corner of the tabletop surface.
* +x: along the front edge, from the LEFT edge towards the RIGHT edge.
* +y: from the FRONT edge towards the BACK edge.
* +z: up; z = 0 is the tabletop surface.

So the tabletop spans x in [0, width], y in [0, depth]. "Toward the left edge" means -x,
"toward the back edge" means +y. All lengths are metres, all object sizes are FULL extents
(the compiler halves them for MuJoCo), and orientation is limited to yaw about +z (degrees).

Every editable quantity carries a provenance tag so the UI can distinguish what the vision
model guessed, what the user measured, what the user confirmed, and what is a default.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "cellcheck.scene/v1"

Provenance = Literal["inferred", "measured", "user_confirmed", "default"]

# Hard bounds. Anything outside these is rejected rather than clamped.
MIN_DIM, MAX_TABLE_DIM = 0.01, 3.0
MAX_OBJECT_DIM = 0.8
MIN_MASS, MAX_MASS = 0.005, 5.0


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Table(_Strict):
    width: float = Field(..., gt=0.2, le=MAX_TABLE_DIM, description="Full extent along x (left→right), metres")
    depth: float = Field(..., gt=0.2, le=MAX_TABLE_DIM, description="Full extent along y (front→back), metres")
    height: float = Field(0.75, ge=0.3, le=1.3, description="Floor to tabletop, metres")
    thickness: float = Field(0.04, ge=0.01, le=0.1)
    friction: float = Field(0.8, gt=0.05, le=2.0)
    provenance: dict[str, Provenance] = Field(
        default_factory=lambda: {"width": "default", "depth": "default", "height": "default"}
    )


class Robot(_Strict):
    model: Literal["franka_panda"] = "franka_panda"
    x: float = Field(..., description="Base centre, table frame, metres")
    y: float
    yaw_deg: float = Field(-90.0, description="0 = arm's forward axis points +x. -90 = faces the front edge (-y).")
    provenance: dict[str, Provenance] = Field(default_factory=lambda: {"x": "default", "y": "default", "yaw_deg": "default"})


class SceneObject(_Strict):
    id: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,31}$", description="Stable ID, never reused for another object")
    kind: Literal["block", "bin"]
    label: str = Field("", max_length=60, description="Human-readable name, e.g. 'blue tray'")
    x: float = Field(..., description="Footprint centre, table frame, metres")
    y: float
    yaw_deg: float = 0.0
    size: tuple[float, float, float] = Field(..., description="FULL extents [sx, sy, sz] in metres, object frame before yaw")
    # block-only physical parameters (explicit defaults, labelled as assumptions via provenance)
    mass: float | None = Field(None, description="kg; blocks only")
    friction: float | None = Field(None, description="sliding friction coefficient; blocks only")
    # bin-only geometry
    wall_thickness: float | None = Field(None, description="metres; bins only")
    color: tuple[float, float, float] | None = None
    provenance: dict[str, Provenance] = Field(default_factory=dict)

    @field_validator("size")
    @classmethod
    def _size_bounds(cls, v):
        for s in v:
            if not (MIN_DIM <= s <= MAX_OBJECT_DIM):
                raise ValueError(f"each size component must be in [{MIN_DIM}, {MAX_OBJECT_DIM}] m, got {v}")
        return v

    @field_validator("yaw_deg")
    @classmethod
    def _wrap_yaw(cls, v):
        if not math.isfinite(v):
            raise ValueError("yaw must be finite")
        return ((v + 180.0) % 360.0) - 180.0

    @model_validator(mode="after")
    def _kind_defaults(self):
        if self.kind == "block":
            if self.mass is None:
                self.mass = 0.1
                self.provenance.setdefault("mass", "default")
            if self.friction is None:
                self.friction = 1.0
                self.provenance.setdefault("friction", "default")
            if not (MIN_MASS <= self.mass <= MAX_MASS):
                raise ValueError(f"block mass must be in [{MIN_MASS}, {MAX_MASS}] kg")
            if not (0.1 <= self.friction <= 2.0):
                raise ValueError("block friction must be in [0.1, 2.0]")
            if self.wall_thickness is not None:
                raise ValueError("wall_thickness only applies to bins")
            if max(self.size) > 0.2:
                raise ValueError("block is too large for the Panda gripper scenario (max 0.2 m per side)")
        else:
            if self.wall_thickness is None:
                self.wall_thickness = 0.008
                self.provenance.setdefault("wall_thickness", "default")
            if not (0.003 <= self.wall_thickness <= 0.05):
                raise ValueError("bin wall_thickness must be in [0.003, 0.05] m")
            if 2 * self.wall_thickness >= min(self.size[0], self.size[1]):
                raise ValueError("bin walls leave no interior")
            if self.mass is not None or self.friction is not None:
                raise ValueError("bins are fixed; mass/friction do not apply")
        for k in ("x", "y", "yaw_deg", "size"):
            self.provenance.setdefault(k, "default")
        return self


class Task(_Strict):
    type: Literal["pick_and_place"] = "pick_and_place"
    object_id: str
    target_id: str


class Scene(_Strict):
    schema_version: Literal["cellcheck.scene/v1"] = SCHEMA_VERSION
    name: str = Field("workcell", max_length=80)
    notes: list[str] = Field(default_factory=list, description="Assumptions and caveats shown to the user")
    table: Table
    robot: Robot
    objects: list[SceneObject]
    task: Task

    @model_validator(mode="after")
    def _structure(self):
        ids = [o.id for o in self.objects]
        if len(ids) != len(set(ids)):
            raise ValueError("object ids must be unique")
        blocks = [o for o in self.objects if o.kind == "block"]
        bins = [o for o in self.objects if o.kind == "bin"]
        if len(blocks) != 1:
            raise ValueError(f"MVP supports exactly one block, got {len(blocks)}")
        if len(bins) != 2:
            raise ValueError(f"MVP supports exactly two bins, got {len(bins)}")
        by_id = {o.id: o for o in self.objects}
        if self.task.object_id not in by_id or by_id[self.task.object_id].kind != "block":
            raise ValueError("task.object_id must reference the block")
        if self.task.target_id not in by_id or by_id[self.task.target_id].kind != "bin":
            raise ValueError("task.target_id must reference a bin")
        return self

    def obj(self, oid: str) -> SceneObject:
        for o in self.objects:
            if o.id == oid:
                return o
        raise KeyError(oid)


# ---- Patches (typed corrections) -------------------------------------------------------

PatchField = Literal["x", "y", "yaw_deg", "size", "mass", "friction", "wall_thickness", "label"]


class PatchOp(_Strict):
    """One validated change against a stable object id (or the table/robot/task)."""

    target: str = Field(..., description="object id, or 'table', 'robot', 'task'")
    field: str
    op: Literal["set", "add"] = "set"
    value: float | str | list[float]


class Patch(_Strict):
    ops: list[PatchOp]
    summary: str = ""


def footprint_corners(o: SceneObject) -> list[tuple[float, float]]:
    """Corners of the object's yawed footprint, table frame."""
    c, s = math.cos(math.radians(o.yaw_deg)), math.sin(math.radians(o.yaw_deg))
    hx, hy = o.size[0] / 2, o.size[1] / 2
    pts = []
    for dx, dy in ((-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)):
        pts.append((o.x + c * dx - s * dy, o.y + s * dx + c * dy))
    return pts


def point_in_footprint(o: SceneObject, px: float, py: float, margin: float = 0.0) -> bool:
    c, s = math.cos(math.radians(-o.yaw_deg)), math.sin(math.radians(-o.yaw_deg))
    dx, dy = px - o.x, py - o.y
    lx, ly = c * dx - s * dy, s * dx + c * dy
    return abs(lx) <= o.size[0] / 2 - margin and abs(ly) <= o.size[1] / 2 - margin

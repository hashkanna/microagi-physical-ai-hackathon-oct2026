"""Table-plane calibration: four clicked tabletop corners + measured width/depth -> homography.

The homography maps image pixels (u right, v down) of ONE frame to the table frame defined in
`cellcheck.schema` (metres, origin at the front-left corner, +x to the right edge, +y to the
back edge). It is only valid for points ON the tabletop plane (z = 0), which is why the vision
model is asked for the pixel centre of each object's *footprint*, not of its visible bulk.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np

from .schema import Scene

CORNER_NAMES = ("front-left", "front-right", "back-right", "back-left")
MIN_SIDE_PX = 8.0


class CalibrationError(ValueError):
    """Corner clicks or table dimensions that cannot give a trustworthy mapping."""


def table_corners(width: float, depth: float) -> np.ndarray:
    """Table-frame corners in CORNER_NAMES order."""
    return np.array([[0.0, 0.0], [width, 0.0], [width, depth], [0.0, depth]])


def _as_corners(corners_px: Sequence[Sequence[float]]) -> np.ndarray:
    try:
        pts = np.asarray(corners_px, dtype=float)
    except (TypeError, ValueError) as e:
        raise CalibrationError(f"corners must be 4 [u, v] pixel pairs: {e}") from None
    if pts.shape != (4, 2) or not np.all(np.isfinite(pts)):
        raise CalibrationError(f"need exactly 4 finite [u, v] corners ({', '.join(CORNER_NAMES)}), got shape {pts.shape}")
    return pts


def check_quad(corners_px: Sequence[Sequence[float]]) -> np.ndarray:
    """Validate the clicked quadrilateral; returns it as a (4, 2) array or raises CalibrationError."""
    pts = _as_corners(corners_px)
    sides = np.linalg.norm(np.roll(pts, -1, axis=0) - pts, axis=1)
    if sides.min() < MIN_SIDE_PX:
        i = int(sides.argmin())
        raise CalibrationError(f"corners {CORNER_NAMES[i]} and {CORNER_NAMES[(i + 1) % 4]} are only {sides[i]:.1f} px apart")
    e1, e2 = np.roll(pts, -1, axis=0) - pts, np.roll(pts, -2, axis=0) - np.roll(pts, -1, axis=0)
    cross = e1[:, 0] * e2[:, 1] - e1[:, 1] * e2[:, 0]
    # Image v points down, so front-left -> front-right -> back-right -> back-left turns with cross < 0.
    if np.all(cross > 0):
        raise CalibrationError(
            "corners are mirrored: click them in the order front-left, front-right, back-right, back-left "
            "(front = the edge nearest the camera)")
    if not np.all(cross < 0):
        raise CalibrationError("corners do not form a convex quadrilateral (order must be front-left, front-right, back-right, back-left)")
    area = 0.5 * abs(np.dot(pts[:, 0], np.roll(pts[:, 1], -1)) - np.dot(pts[:, 1], np.roll(pts[:, 0], -1)))
    if area < MIN_SIDE_PX ** 2 * 4:
        raise CalibrationError(f"table quadrilateral is degenerate ({area:.0f} px²)")
    return pts


def _normaliser(pts: np.ndarray) -> np.ndarray:
    c = pts.mean(axis=0)
    s = np.sqrt(2) / max(np.mean(np.linalg.norm(pts - c, axis=1)), 1e-12)
    return np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1]])


def _dlt(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Normalised 4-point direct linear transform: dst ~ H @ src."""
    ts, td = _normaliser(src), _normaliser(dst)
    s = (ts @ np.c_[src, np.ones(4)].T).T
    d = (td @ np.c_[dst, np.ones(4)].T).T
    rows = []
    for (u, v, _), (x, y, _) in zip(s, d):
        rows.append([-u, -v, -1, 0, 0, 0, u * x, v * x, x])
        rows.append([0, 0, 0, -u, -v, -1, u * y, v * y, y])
    _, sv, vt = np.linalg.svd(np.array(rows))
    if sv[-2] < 1e-9:
        raise CalibrationError("corner configuration is degenerate (three corners are collinear?)")
    h = np.linalg.inv(td) @ vt[-1].reshape(3, 3) @ ts
    return h / h[2, 2] if abs(h[2, 2]) > 1e-12 else h


def homography(corners_px: Sequence[Sequence[float]], width: float, depth: float, *, tol_m: float = 1e-4) -> np.ndarray:
    """3x3 homography H with [x, y, 1] ~ H @ [u, v, 1] (pixels -> table-frame metres).

    `corners_px` order: front-left (0,0), front-right (W,0), back-right (W,D), back-left (0,D).
    """
    if not (0.2 < width <= 3.0 and 0.2 < depth <= 3.0):
        raise CalibrationError(f"table width/depth must be in (0.2, 3.0] m, got {width} x {depth}")
    src, dst = check_quad(corners_px), table_corners(width, depth)
    H = _dlt(src, dst)
    w = (H @ np.c_[src, np.ones(4)].T)[2]
    if not (np.all(w > 0) or np.all(w < 0)):
        raise CalibrationError("table corners straddle the horizon; pick a frame that shows the whole table")
    if w[0] < 0:
        H = -H  # keep w > 0 on the tabletop so pixel_to_table can detect points beyond the horizon
    err = max(np.hypot(*(np.array(pixel_to_table(H, *p)) - q)) for p, q in zip(src, dst))
    if err > tol_m:
        raise CalibrationError(f"homography reprojection error {err * 1000:.2f} mm exceeds {tol_m * 1000:.2f} mm")
    return H


def pixel_to_table(H: np.ndarray, u: float, v: float) -> tuple[float, float]:
    """Map one pixel (on the tabletop plane) to table-frame metres."""
    x, y, w = np.asarray(H) @ np.array([u, v, 1.0])
    if w <= 1e-12:
        raise CalibrationError(f"pixel ({u:.0f}, {v:.0f}) lies beyond the table plane's horizon")
    return float(x / w), float(y / w)


def table_to_pixel(H: np.ndarray, x: float, y: float) -> tuple[float, float]:
    """Inverse mapping, e.g. to draw table-frame points over the overview frame."""
    u, v, w = np.linalg.inv(np.asarray(H)) @ np.array([x, y, 1.0])
    return float(u / w), float(v / w)


def apply_calibration(
    scene: Scene,
    *,
    table_width: float | None = None,
    table_depth: float | None = None,
    corners_px: Sequence[Sequence[float]] | None = None,
    footprints_px: Mapping[str, Sequence[float]] | None = None,
) -> Scene:
    """Return a copy of `scene` with measured table dimensions (provenance "measured").

    If `corners_px` and `footprints_px` ({object_id: [u, v]} footprint centres on the same
    frame) are given, those objects are re-projected through the new homography. Fields the
    user already confirmed are never overwritten. Objects without pixels are left untouched:
    their table-frame coordinates stay as they were, and a note says so.
    """
    data = scene.model_dump()
    t = data["table"]
    if table_width is not None:
        t["width"], t["provenance"]["width"] = float(table_width), "measured"
    if table_depth is not None:
        t["depth"], t["provenance"]["depth"] = float(table_depth), "measured"
    notes = data["notes"]
    notes.append(f"Table measured as {t['width']:.3f} x {t['depth']:.3f} m.")
    r = data["robot"]
    if r["provenance"].get("x") == "default" and r["provenance"].get("y") == "default":
        r["x"], r["y"] = t["width"] / 2, t["depth"] - 0.1
        notes.append("Robot kept at its default spot (back-edge centre, 0.1 m in) for the measured table.")
    if corners_px is not None and footprints_px:
        H = homography(corners_px, t["width"], t["depth"])
        for o in data["objects"]:
            px = footprints_px.get(o["id"])
            if px is None:
                continue
            if "user_confirmed" in (o["provenance"].get("x"), o["provenance"].get("y")):
                notes.append(f"{o['id']}: position was user-confirmed, not re-projected.")
                continue
            o["x"], o["y"] = pixel_to_table(H, *px)
            o["provenance"]["x"] = o["provenance"]["y"] = "inferred"
        notes.append("Object positions re-projected from footprint pixels using the clicked table corners.")
    return Scene.model_validate(data)

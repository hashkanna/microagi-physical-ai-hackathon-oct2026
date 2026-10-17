import numpy as np
import pytest

from cellcheck.calibrate import CalibrationError, apply_calibration, homography, pixel_to_table, table_to_pixel
from cellcheck.schema import Scene

CORNERS = [[170, 660], [1110, 660], [935, 255], [345, 255]]  # FL, FR, BR, BL in a 1280x720 frame


def test_corners_map_exactly_and_round_trip():
    H = homography(CORNERS, 1.0, 0.7)
    for (u, v), (x, y) in zip(CORNERS, [(0, 0), (1.0, 0), (1.0, 0.7), (0, 0.7)]):
        assert pixel_to_table(H, u, v) == pytest.approx((x, y), abs=1e-9)
    for x in np.linspace(0, 1.0, 5):
        for y in np.linspace(0, 0.7, 5):
            assert pixel_to_table(H, *table_to_pixel(H, x, y)) == pytest.approx((x, y), abs=1e-9)


def test_perspective_is_not_affine():
    H = homography(CORNERS, 1.0, 0.7)
    # Foreshortening: the near half of the table fills more of the image, so the image midpoint
    # of the left edge lies in the near half of the table (y < depth / 2).
    u, v = (np.array(CORNERS[0]) + CORNERS[3]) / 2
    assert pixel_to_table(H, u, v)[1] < 0.3


@pytest.mark.parametrize("corners, msg", [
    ([CORNERS[1], CORNERS[0], CORNERS[3], CORNERS[2]], "mirrored"),
    ([CORNERS[0], CORNERS[2], CORNERS[1], CORNERS[3]], "convex"),
    ([[0, 0], [100, 0], [200, 0], [50, -1]], "convex|degenerate"),
    ([[0, 0], [2, 0], [2, -2], [0, -2]], "apart"),
    ([[0, 0], [1, 1]], "exactly 4"),
])
def test_bad_corner_sets_raise(corners, msg):
    with pytest.raises(CalibrationError, match=msg):
        homography(corners, 1.0, 0.7)


def test_bad_dimensions_raise():
    with pytest.raises(CalibrationError, match="width/depth"):
        homography(CORNERS, 0.1, 0.7)


def test_point_beyond_horizon_raises():
    H = homography(CORNERS, 1.0, 0.7)
    with pytest.raises(CalibrationError, match="horizon"):
        pixel_to_table(H, 640, -5000)


def _scene():
    return Scene.model_validate({
        "table": {"width": 1.0, "depth": 0.7},
        "robot": {"x": 0.5, "y": 0.6},
        "objects": [
            {"id": "block_1", "kind": "block", "x": 0.2, "y": 0.3, "size": [0.05, 0.05, 0.05]},
            {"id": "bin_a", "kind": "bin", "x": 0.5, "y": 0.3, "size": [0.25, 0.18, 0.08]},
            {"id": "bin_b", "kind": "bin", "x": 0.8, "y": 0.3, "size": [0.25, 0.18, 0.08], "provenance": {"x": "user_confirmed"}},
        ],
        "task": {"object_id": "block_1", "target_id": "bin_b"},
    })


def test_apply_calibration_sets_measured_and_reprojects_only_given_objects():
    s = _scene()
    H = homography(CORNERS, 1.2, 0.8)
    out = apply_calibration(s, table_width=1.2, table_depth=0.8, corners_px=CORNERS,
                            footprints_px={"bin_a": table_to_pixel(H, 0.6, 0.4), "bin_b": table_to_pixel(H, 0.1, 0.1)})
    assert (out.table.width, out.table.depth) == (1.2, 0.8)
    assert out.table.provenance["width"] == out.table.provenance["depth"] == "measured"
    assert (out.robot.x, out.robot.y) == pytest.approx((0.6, 0.7))
    assert (out.obj("bin_a").x, out.obj("bin_a").y) == pytest.approx((0.6, 0.4), abs=1e-6)
    assert out.obj("bin_b") == s.obj("bin_b")  # user-confirmed position kept
    assert out.obj("block_1") == s.obj("block_1")  # no pixels -> untouched
    assert s.table.width == 1.0  # input not mutated

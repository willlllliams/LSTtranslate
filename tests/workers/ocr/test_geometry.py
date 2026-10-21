"""坐标换算与预处理缩放（需求 CAP-05）。

旋转的逆变换用一份独立的正向实现来验证：按 OpenCV getRotationMatrix2D 的定义构造旋转矩阵，加上 PaddleX
rotate_image 的画布扩展，把原图上的框转到旋转后的图上，再用 CoordinateMap 转回来，应得到原来的框。
"""

import math

import numpy as np
import pytest

from lst.workers.ocr_worker.geometry import (
    Box,
    CoordinateMap,
    box_area,
    box_from,
    clamp_to_pixels,
    intersection_area,
    iou,
    plan_resize,
    union_box,
)

# ---------------------------------------------------------------- 预处理缩放


@pytest.mark.parametrize(
    ("size", "max_side", "expected"),
    [
        ((1920, 1080), 0, (1920, 1080)),
        ((1920, 1080), 1920, (1920, 1080)),
        ((1920, 1080), 4000, (1920, 1080)),
        ((1920, 1080), 960, (960, 540)),
        ((1080, 1920), 960, (540, 960)),
        ((1000, 333), 500, (500, 166)),  # 166.5 四舍五入到偶数
        ((3000, 1), 300, (300, 1)),  # 短边至少 1 像素
        ((2000, 2000), 256, (256, 256)),
    ],
)
def test_plan_resize(size: tuple[int, int], max_side: int, expected: tuple[int, int]) -> None:
    assert plan_resize(*size, max_side) == expected


def test_resize_keeps_long_side_exact() -> None:
    for width in range(1001, 1200, 7):
        for height in range(300, 1000, 97):
            new = plan_resize(width, height, 1000)
            assert max(new) == 1000
            assert abs(new[0] / new[1] - width / height) < 0.01


# ---------------------------------------------------------------- 包围盒运算


def test_box_from_points_and_corners() -> None:
    assert box_from([10, 20, 5, 40]) == (5.0, 20.0, 10.0, 40.0)
    assert box_from([3, 1, 9, 2, 8, 7, 2, 6]) == (2.0, 1.0, 9.0, 7.0)
    with pytest.raises(ValueError):
        box_from([1, 2, 3])


def test_area_iou_union() -> None:
    a: Box = (0, 0, 10, 10)
    b: Box = (5, 5, 15, 15)
    assert box_area(a) == 100
    assert box_area((5, 5, 1, 1)) == 0
    assert intersection_area(a, b) == 25
    assert iou(a, b) == pytest.approx(25 / 175)
    assert iou((0, 0, 0, 0), (0, 0, 0, 0)) == 0.0
    assert union_box([a, b]) == (0, 0, 15, 15)
    assert union_box([]) is None


@pytest.mark.parametrize(
    ("box", "expected"),
    [
        ((10.2, 20.7, 30.1, 40.0), (10, 20, 31, 40)),
        ((2.9999999, 0.0, 5.0000001, 1.0), (3, 0, 5, 1)),  # 浮点误差不扩出一个像素
        ((-5, -5, 50, 50), (0, 0, 50, 50)),
        ((90, 90, 150, 150), (90, 90, 100, 100)),
        ((100, 0, 120, 10), None),  # 完全在右侧之外
        ((10, 10, 10, 20), None),  # 宽为零
        ((math.nan, 0, 1, 1), None),
    ],
)
def test_clamp_to_pixels(box: Box, expected: tuple[int, int, int, int] | None) -> None:
    assert clamp_to_pixels(box, 100, 100) == expected


# ---------------------------------------------------------------- 坐标换算


def test_identity_map() -> None:
    cmap = CoordinateMap.identity(640, 480)
    assert cmap.to_original((10.5, 20.5, 30.5, 40.5)) == (10.5, 20.5, 30.5, 40.5)
    assert cmap.to_pixels((10.5, 20.5, 30.5, 40.5)) == (10, 20, 31, 41)


def test_resize_is_scaled_back_to_original() -> None:
    # 原图 3000×2000 按长边 1500 缩成 1500×1000，产线输出的坐标乘 2 回到原图。
    cmap = CoordinateMap((3000, 2000), plan_resize(3000, 2000, 1500))
    assert cmap.to_original((100, 50, 700, 400)) == (200, 100, 1400, 800)
    assert cmap.to_pixels((1499.6, 999.6, 1500, 1000)) == (2999, 1999, 3000, 2000)


def test_non_uniform_rounding_uses_each_axis() -> None:
    cmap = CoordinateMap((1000, 333), (500, 166))
    x0, y0, x1, y1 = cmap.to_original((0, 0, 500, 166))
    assert (x0, y0) == (0, 0)
    assert x1 == pytest.approx(1000)
    assert y1 == pytest.approx(333)


def _opencv_rotate_forward(box: Box, width: int, height: int, angle: int) -> Box:
    """按 cv2.getRotationMatrix2D 与 PaddleX rotate_image 的画布扩展，把原图上的框转到旋转后的图上。"""
    theta = math.radians(angle)
    alpha, beta = math.cos(theta), math.sin(theta)
    cx, cy = width / 2, height / 2
    matrix = np.array(
        [[alpha, beta, (1 - alpha) * cx - beta * cy], [-beta, alpha, beta * cx + (1 - alpha) * cy]]
    )
    new_w = int(height * abs(beta) + width * abs(alpha))
    new_h = int(height * abs(alpha) + width * abs(beta))
    matrix[0, 2] += (new_w - width) / 2
    matrix[1, 2] += (new_h - height) / 2
    x0, y0, x1, y1 = box
    corners = np.array([[x0, y0, 1], [x1, y0, 1], [x1, y1, 1], [x0, y1, 1]], dtype=float)
    moved = corners @ matrix.T
    return (moved[:, 0].min(), moved[:, 1].min(), moved[:, 0].max(), moved[:, 1].max())


@pytest.mark.parametrize("angle", [0, 90, 180, 270])
@pytest.mark.parametrize("box", [(10, 20, 110, 60), (0, 0, 640, 480), (600, 400, 640, 480)])
def test_rotation_inverse_matches_opencv(angle: int, box: Box) -> None:
    width, height = 640, 480
    rotated = _opencv_rotate_forward(box, width, height, angle)
    cmap = CoordinateMap((width, height), (width, height), angle)
    assert cmap.to_original(rotated) == pytest.approx(box, abs=1e-6)


def test_rotation_combined_with_resize() -> None:
    # 原图 1280×960 缩成 640×480，产线再把它旋转 90 度。
    original: Box = (200, 100, 600, 300)
    resized: Box = (100, 50, 300, 150)
    rotated = _opencv_rotate_forward(resized, 640, 480, 90)
    cmap = CoordinateMap((1280, 960), (640, 480), 90)
    assert cmap.to_original(rotated) == pytest.approx(original, abs=1e-6)


def test_unwarped_output_is_scaled_to_rotated_size() -> None:
    # 去畸变后的图像是 320×240，旋转后的图像是 640×480：坐标先放大 2 倍。
    cmap = CoordinateMap((640, 480), (640, 480), -1, output_size=(320, 240))
    assert cmap.to_original((10, 10, 20, 20)) == (20, 20, 40, 40)


def test_angle_minus_one_means_no_rotation() -> None:
    assert CoordinateMap((10, 10), (10, 10), -1).to_original((1, 2, 3, 4)) == (1, 2, 3, 4)


def test_invalid_angle() -> None:
    with pytest.raises(ValueError):
        CoordinateMap((10, 10), (10, 10), 45).to_original((1, 2, 3, 4))

"""覆盖窗口的纯函数：坐标与缩放、单击判断、亮度、字号适配（需求 OV-02、OV-03、OV-05、UI-04）。"""

import math

import pytest

from lst.contracts.config import AppConfig
from lst.contracts.ocr import BBox
from lst.ui.overlay import char_units, chart_cell_bbox, estimate_font_size, fit_font_size, overlay_markers
from lst.ui.overlay.fitting import MAX_FONT_PX, MIN_FONT_PX
from lst.ui.overlay.geometry import (
    MAX_ZOOM,
    MIN_ZOOM,
    bbox_to_view,
    device_per_pixel,
    is_light,
    logical_per_pixel,
    luminance,
    view_to_pixel,
    wheel_zoom,
    window_size,
    zoom_about,
)


@pytest.mark.parametrize(
    ("pixels", "dpr", "zoom", "expected"),
    [
        ((600, 300), 1.0, 1.0, (600, 300)),
        ((600, 300), 1.25, 1.0, (480, 240)),
        ((601, 301), 1.5, 1.0, (401, 201)),
        ((2560, 1440), 1.75, 1.0, (1463, 823)),
        ((3840, 2160), 2.0, 1.0, (1920, 1080)),
        ((600, 300), 1.5, 2.0, (800, 400)),
        ((1, 1), 2.0, 0.25, (1, 1)),  # 至少 1×1
    ],
)
def test_window_size(pixels: tuple[int, int], dpr: float, zoom: float, expected: tuple[int, int]) -> None:
    assert window_size(*pixels, dpr, zoom) == expected


@pytest.mark.parametrize("capture_dpr", [1.0, 1.25, 1.5, 1.75, 2.0])
@pytest.mark.parametrize("device_dpr", [1.0, 1.25, 1.5, 1.75, 2.0])
@pytest.mark.parametrize("zoom", [0.5, 1.0, 1.1, 2.0])
def test_scales_are_consistent(capture_dpr: float, device_dpr: float, zoom: float) -> None:
    """k = s × r_d；在截屏的屏幕上、倍数为 1 时，一个截图像素恰好是一个设备像素。"""
    s = logical_per_pixel(capture_dpr, zoom)
    k = device_per_pixel(capture_dpr, zoom, device_dpr)
    assert k == pytest.approx(s * device_dpr)
    if zoom == 1.0 and device_dpr == capture_dpr:
        assert k == pytest.approx(1.0)
    box = bbox_to_view(BBox(10, 20, 110, 70), k)
    assert view_to_pixel(box.x, box.y, k) == pytest.approx((10, 20))
    assert box.width == pytest.approx(100 * k)


@pytest.mark.parametrize("old", [0.5, 1.0, 1.7])
@pytest.mark.parametrize("new", [0.25, 1.1, 3.0])
@pytest.mark.parametrize("cursor", [(0.0, 0.0), (120.0, 45.0), (399.0, 199.0)])
def test_zoom_about_keeps_point_under_cursor(old: float, new: float, cursor: tuple[float, float]) -> None:
    """缩放前后，光标下的截图点在屏幕上的位置不变。"""
    position = (-300.0, 250.0)
    capture_dpr = 1.5
    pixel = (cursor[0] / logical_per_pixel(capture_dpr, old), cursor[1] / logical_per_pixel(capture_dpr, old))
    moved = zoom_about(position, cursor, old, new)
    s_new = logical_per_pixel(capture_dpr, new)
    screen_after = (moved[0] + pixel[0] * s_new, moved[1] + pixel[1] * s_new)
    assert screen_after == pytest.approx((position[0] + cursor[0], position[1] + cursor[1]))


def test_wheel_zoom_steps_and_clamps() -> None:
    assert wheel_zoom(1.0, 120) == pytest.approx(1.1)
    assert wheel_zoom(1.0, -120) == pytest.approx(1 / 1.1)
    assert wheel_zoom(1.0, 240) == pytest.approx(1.21)
    assert wheel_zoom(3.9, 1200) == MAX_ZOOM
    assert wheel_zoom(0.3, -1200) == MIN_ZOOM


def test_light_and_dark() -> None:
    assert is_light(luminance(255, 255, 255))
    assert not is_light(luminance(0, 0, 0))
    assert is_light(128)
    assert not is_light(127.9)
    assert luminance(255, 0, 0) == pytest.approx(0.299 * 255)


# ---------------------------------------------------------------- 字号适配


@pytest.mark.parametrize("best", [MIN_FONT_PX, 7, 13, 50, 99, MAX_FONT_PX])
def test_fit_font_size_finds_largest_fitting(best: int) -> None:
    calls: list[int] = []

    def fits(px: int) -> bool:
        calls.append(px)
        return px <= best

    assert fit_font_size(fits) == best
    # 二分查找：调用次数是对数级。
    assert len(calls) <= math.ceil(math.log2(MAX_FONT_PX - MIN_FONT_PX + 2)) + 2


def test_fit_font_size_returns_lower_bound_when_nothing_fits() -> None:
    assert fit_font_size(lambda _px: False) == MIN_FONT_PX
    assert fit_font_size(lambda _px: True, 6, 3) == 6


def test_fit_with_text_metric_model() -> None:
    """用“每字宽 0.55 字号、行高 1.35 字号”的简化排版模型检查：结果放得下，大一号就放不下。"""
    text = "The quick brown fox jumps over the lazy dog"
    width, height = 300.0, 60.0

    def fits(px: int) -> bool:
        per_line = max(1, int(width // (0.55 * px)))
        lines = math.ceil(len(text) / per_line)
        return lines * 1.35 * px <= height

    size = fit_font_size(fits)
    assert fits(size)
    assert not fits(size + 1)
    # 估算值与真正的最大值同一量级。
    assert 0.5 * size <= estimate_font_size(text, width, height) <= 2 * size


def test_char_units_counts_wide_characters() -> None:
    assert char_units("ab") == pytest.approx(1.1)
    assert char_units("你好") == 2
    assert char_units("こんにちは") == 5
    assert char_units("ＡＢ") == 2  # 全角


@pytest.mark.parametrize(
    ("text", "width", "height"),
    [("Hello", 200, 40), ("你好世界" * 10, 300, 100), ("x" * 500, 100, 20), ("", 50, 50), ("a", 0, 10)],
)
def test_estimate_font_size_bounds(text: str, width: float, height: float) -> None:
    size = estimate_font_size(text, width, height)
    assert MIN_FONT_PX <= size <= MAX_FONT_PX
    if height > 0:
        assert size <= max(MIN_FONT_PX, height / 1.35)


def test_chart_cell_bbox_splits_evenly() -> None:
    block = BBox(320, 170, 580, 290)
    cells = [chart_cell_bbox(block, r, c, 2, 2) for r in range(2) for c in range(2)]
    assert cells[0] == BBox(320, 170, 450, 230)
    assert cells[3] == BBox(450, 230, 580, 290)


def test_overlay_markers_reads_config() -> None:
    assert not overlay_markers(AppConfig({}, 1))
    assert overlay_markers(AppConfig({"race": {"show_source_marker": {"overlay": True}}}, 1))
    assert not overlay_markers(AppConfig({"race": {"show_source_marker": {"overlay": "yes"}}}, 1))
    assert not overlay_markers(AppConfig({"race": []}, 1))

"""截屏坐标换算的纯函数测试（需求 CAP-02、CAP-03、CAP-04、CAP-07）。

屏幕布局按 Windows 的原生坐标（物理像素）虚构，再按 Qt 6 的规则换算成逻辑坐标（geometry.screen_from_native）。
每个用例都从“用户想框住的物理像素矩形”出发：把矩形的两个角按 Qt 的公式换算成鼠标的逻辑位置，交给
selection_from_drag，检查得到的物理像素矩形与原矩形一致（或误差不超过 1 像素）。

布局覆盖：单屏的 100%、125%、150%、175%、200%；副屏在左侧、上方、左上方（坐标为负）；
混合缩放；竖屏（旋转 90 度后宽小于高）；三块屏幕；不同分辨率。刷新率不参与坐标换算，不单独列出。
"""

import random
import zlib
from collections.abc import Iterator

import pytest

from lst.ui.capture.geometry import (
    MIN_SELECTION,
    Rect,
    ScreenGeometry,
    clamp_to_screen,
    logical_rect,
    logical_to_pixel,
    native_to_logical,
    pixel_to_logical,
    screen_from_native,
    selection_from_drag,
)

# 每个布局：名称 → [(屏幕名, 原生矩形, 设备像素比)]
LAYOUTS: dict[str, list[tuple[str, Rect, float]]] = {
    "单屏 100%": [("A", Rect(0, 0, 1920, 1080), 1.0)],
    "单屏 125%": [("A", Rect(0, 0, 1920, 1080), 1.25)],
    "单屏 150% 2K": [("A", Rect(0, 0, 2560, 1440), 1.5)],
    "单屏 175%": [("A", Rect(0, 0, 2560, 1600), 1.75)],
    "单屏 200% 4K": [("A", Rect(0, 0, 3840, 2160), 2.0)],
    "左侧副屏 150% + 主屏 100%": [
        ("main", Rect(0, 0, 1920, 1080), 1.0),
        ("left", Rect(-2560, 0, 2560, 1440), 1.5),
    ],
    "上方副屏 100% + 主屏 125%": [
        ("main", Rect(0, 0, 2560, 1440), 1.25),
        ("top", Rect(0, -1080, 1920, 1080), 1.0),
    ],
    "左上副屏 100% + 笔记本 200%": [
        ("laptop", Rect(0, 0, 2880, 1800), 2.0),
        ("corner", Rect(-1920, -1080, 1920, 1080), 1.0),
    ],
    "主屏 150% + 右侧竖屏 125%（错位）": [
        ("main", Rect(0, 0, 3840, 2160), 1.5),
        ("portrait", Rect(3840, -400, 1080, 1920), 1.25),
    ],
    "三屏：左竖屏 175% + 主屏 125% + 右 4K 200%": [
        ("left", Rect(-1440, -560, 1440, 2560), 1.75),
        ("main", Rect(0, 0, 1920, 1200), 1.25),
        ("right", Rect(1920, 0, 3840, 2160), 2.0),
    ],
    "小屏 100% + 右侧 4K 175%": [
        ("small", Rect(0, 0, 1366, 768), 1.0),
        ("big", Rect(1366, -300, 3840, 2160), 1.75),
    ],
    "上方竖屏 150%（负坐标）": [
        ("main", Rect(0, 0, 1920, 1080), 1.0),
        ("above", Rect(200, -2560, 1440, 2560), 1.5),
    ],
}


def _screens(layout: str) -> list[ScreenGeometry]:
    return [screen_from_native(name, native, dpr) for name, native, dpr in LAYOUTS[layout]]


def _cases() -> Iterator[tuple[str, str]]:
    for layout, screens in LAYOUTS.items():
        for name, _native, _dpr in screens:
            yield layout, name


def _screen(layout: str, name: str) -> ScreenGeometry:
    return next(s for s in _screens(layout) if s.name == name)


def _mouse(screen: ScreenGeometry, px: int, py: int) -> tuple[float, float]:
    """屏幕内物理像素 (px, py)（相对屏幕左上角）处，Qt 报告的鼠标逻辑位置。"""
    rect = screen.logical
    return native_to_logical(screen, rect.x + px, rect.y + py)


def _targets(screen: ScreenGeometry, count: int, seed: int) -> list[Rect]:
    """要框住的物理像素矩形：整屏、四角各一块、随机若干块。"""
    w, h = screen.pixel_width, screen.pixel_height
    fixed = [
        Rect(0, 0, w, h),
        Rect(0, 0, 37, 41),
        Rect(w - 53, 0, 53, 29),
        Rect(0, h - 31, 47, 31),
        Rect(w - 61, h - 59, 61, 59),
        Rect(1, 1, w - 2, h - 2),
    ]
    rng = random.Random(seed)
    out = list(fixed)
    for _ in range(count):
        x0 = rng.randrange(0, w - 20)
        y0 = rng.randrange(0, h - 20)
        x1 = rng.randrange(x0 + 20, w + 1)
        y1 = rng.randrange(y0 + 20, h + 1)
        out.append(Rect(x0, y0, x1 - x0, y1 - y0))
    return out


# ---------------------------------------------------------------- Qt 6 的屏幕几何


@pytest.mark.parametrize(
    ("native", "dpr", "logical"),
    [
        (Rect(0, 0, 1920, 1080), 1.0, Rect(0, 0, 1920, 1080)),
        (Rect(0, 0, 2560, 1440), 1.5, Rect(0, 0, 1707, 960)),  # 1706.67 取整，本机实际值
        (Rect(-2560, 0, 2560, 1440), 1.5, Rect(-2560, 0, 1707, 960)),  # 左上角保持原生坐标
        (Rect(0, -1080, 1920, 1080), 1.25, Rect(0, -1080, 1536, 864)),
        (Rect(3840, 0, 2560, 1600), 1.75, Rect(3840, 0, 1463, 914)),
        (Rect(0, 0, 1080, 1920), 1.25, Rect(0, 0, 864, 1536)),  # 竖屏
    ],
)
def test_screen_from_native_follows_qt_rule(native: Rect, dpr: float, logical: Rect) -> None:
    screen = screen_from_native("s", native, dpr)
    assert screen.logical == logical
    assert (screen.pixel_width, screen.pixel_height) == (native.width, native.height)


def test_mixed_scaling_leaves_logical_gap() -> None:
    """左侧 150% 副屏的逻辑范围到 -853 为止，与主屏之间有缝隙；这是每块屏幕单独一个蒙版的原因。"""
    left, main = _screens("左侧副屏 150% + 主屏 100%")[1], _screens("左侧副屏 150% + 主屏 100%")[0]
    assert left.logical.right == -853
    assert main.logical.x == 0
    assert not left.contains(-500, 10)
    assert not main.contains(-500, 10)


# ---------------------------------------------------------------- 选区 → 物理像素（CAP-03）


@pytest.mark.parametrize(("layout", "name"), list(_cases()))
def test_exact_mouse_positions_map_to_exact_pixels(layout: str, name: str) -> None:
    """鼠标位置由整数原生坐标换算而来（真实情况）时，物理像素与目标完全一致。"""
    screen = _screen(layout, name)
    for target in _targets(screen, 40, seed=zlib.crc32(f"{layout}/{name}".encode())):
        start = _mouse(screen, target.x, target.y)
        end = _mouse(screen, target.right, target.bottom)
        selection = selection_from_drag(screen, start, end)
        if target.width / screen.dpr < MIN_SELECTION or target.height / screen.dpr < MIN_SELECTION:
            continue
        assert selection is not None, target
        assert selection.pixels == target
        # 反向拖动（从右下到左上）结果相同。
        reverse = selection_from_drag(screen, end, start)
        assert reverse is not None
        assert reverse.pixels == target


@pytest.mark.parametrize(("layout", "name"), list(_cases()))
@pytest.mark.parametrize("jitter", [-0.49, -0.25, 0.25, 0.49])
def test_sub_pixel_jitter_stays_within_one_pixel(layout: str, name: str, jitter: float) -> None:
    """逻辑坐标带有不超过半个物理像素的偏差时，每条边的误差不超过 1 物理像素（CAP-03）。"""
    screen = _screen(layout, name)
    offset = jitter / screen.dpr
    for target in _targets(screen, 15, seed=7):
        sx, sy = _mouse(screen, target.x, target.y)
        ex, ey = _mouse(screen, target.right, target.bottom)
        selection = selection_from_drag(screen, (sx + offset, sy - offset), (ex - offset, ey + offset))
        assert selection is not None
        pixels = selection.pixels
        assert abs(pixels.x - target.x) <= 1
        assert abs(pixels.y - target.y) <= 1
        assert abs(pixels.right - target.right) <= 1
        assert abs(pixels.bottom - target.bottom) <= 1


@pytest.mark.parametrize(("layout", "name"), list(_cases()))
def test_logical_rect_round_trips_within_one_pixel(layout: str, name: str) -> None:
    """覆盖窗口按选区的逻辑矩形摆放：逻辑矩形换算回物理像素，与选区的偏差不超过设备像素比的一半加取整误差。"""
    screen = _screen(layout, name)
    for target in _targets(screen, 20, seed=11):
        rect = logical_rect(screen, target)
        left, top = logical_to_pixel(screen, rect.x, rect.y)
        assert abs(left - target.x) <= max(1, screen.dpr / 2 + 0.5)
        assert abs(top - target.y) <= max(1, screen.dpr / 2 + 0.5)
        # 全局逻辑坐标确实落在这块屏幕上。
        assert screen.logical.x <= rect.x <= screen.logical.right
        assert screen.logical.y <= rect.y <= screen.logical.bottom


@pytest.mark.parametrize(("layout", "name"), list(_cases()))
def test_pixel_logical_round_trip(layout: str, name: str) -> None:
    screen = _screen(layout, name)
    for px, py in [(0, 0), (1, 1), (screen.pixel_width, screen.pixel_height), (123, 457), (999, 3)]:
        px, py = min(px, screen.pixel_width), min(py, screen.pixel_height)
        assert logical_to_pixel(screen, *pixel_to_logical(screen, px, py)) == (px, py)


# ---------------------------------------------------------------- 限制在按下的屏幕内（CAP-02）


@pytest.mark.parametrize(("layout", "name"), list(_cases()))
def test_drag_into_other_screen_is_clamped(layout: str, name: str) -> None:
    """从屏幕中部拖到屏幕之外（另一块屏幕或缝隙里），选区截止在本屏边缘。"""
    screen = _screen(layout, name)
    mid = _mouse(screen, screen.pixel_width // 2, screen.pixel_height // 2)
    rect = screen.logical
    far_right_bottom = (rect.x + 10_000, rect.y + 10_000)
    selection = selection_from_drag(screen, mid, far_right_bottom)
    assert selection is not None
    assert selection.pixels.right == screen.pixel_width
    assert selection.pixels.bottom == screen.pixel_height
    far_left_top = (rect.x - 10_000, rect.y - 10_000)
    selection = selection_from_drag(screen, mid, far_left_top)
    assert selection is not None
    assert (selection.pixels.x, selection.pixels.y) == (0, 0)


def test_clamp_uses_true_right_edge() -> None:
    """175% 下逻辑宽度取整为 1463，真实右边缘是 2560 / 1.75 = 1462.857；限制按真实边缘，右边恰好是 2560。"""
    screen = screen_from_native("s", Rect(0, 0, 2560, 1600), 1.75)
    assert screen.logical.width == 1463
    x, _y = clamp_to_screen(screen, 1463.0, 0)
    assert x == pytest.approx(2560 / 1.75)
    assert logical_to_pixel(screen, 1463.0, 0) == (2560, 0)


# ---------------------------------------------------------------- 过小视为取消（CAP-04）


@pytest.mark.parametrize("dpr", [1.0, 1.25, 1.5, 1.75, 2.0])
@pytest.mark.parametrize(
    ("width", "height", "kept"),
    [(4.99, 100, False), (100, 4.99, False), (0, 0, False), (5, 5, True), (5, 100, True), (300, 200, True)],
)
def test_small_selection_is_cancel(dpr: float, width: float, height: float, kept: bool) -> None:
    screen = screen_from_native("s", Rect(-1000, -500, 3840, 2160), dpr)
    start = (screen.logical.x + 50.0, screen.logical.y + 60.0)
    end = (start[0] + width, start[1] + height)
    assert (selection_from_drag(screen, start, end) is not None) is kept


def test_selection_reports_logical_rect_for_overlay() -> None:
    """150% 屏幕上框住 (300, 150)–(901, 451) 物理像素：逻辑矩形 (200, 100, 401, 201)。"""
    screen = screen_from_native("s", Rect(0, 0, 2560, 1440), 1.5)
    selection = selection_from_drag(screen, _mouse(screen, 300, 150), _mouse(screen, 901, 451))
    assert selection is not None
    assert selection.pixels == Rect(300, 150, 601, 301)
    assert selection.logical == Rect(200, 100, 401, 201)


def test_negative_screen_selection_logical_rect() -> None:
    """左侧副屏（原生 x 从 -2560 起，150%）：逻辑坐标同样以 -2560 为起点。"""
    screen = screen_from_native("left", Rect(-2560, 0, 2560, 1440), 1.5)
    selection = selection_from_drag(screen, _mouse(screen, 0, 0), _mouse(screen, 150, 300))
    assert selection is not None
    assert selection.pixels == Rect(0, 0, 150, 300)
    assert selection.logical == Rect(-2560, 0, 100, 200)


def test_screen_geometry_validates() -> None:
    with pytest.raises(ValueError, match="设备像素比"):
        ScreenGeometry("s", Rect(0, 0, 10, 10), 0, 10, 10)

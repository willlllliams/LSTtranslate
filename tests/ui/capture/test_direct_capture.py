"""不经过蒙版、按原生坐标的矩形直接截取（自动化接口的 capture direct，需求 DEV-05）。

原生坐标是全局物理像素，屏幕在其中的矩形是“逻辑左上角 + 冻结画面的像素尺寸”（geometry.native_rect）。
左侧副屏 150% 缩放、原点 (-2560, -200)，主屏 100%、原点 (0, 0)，两屏在原生坐标里首尾相接。
"""

import pytest
from PySide6.QtGui import QColor

from lst.ui.capture import (
    NativePiece,
    Rect,
    anchor_piece,
    intersect,
    logical_rect,
    native_pieces,
    native_rect,
    screen_from_native,
)
from lst.ui.capture.controller import to_raw_image
from tests.ui.capture.conftest import FakeScreens, MakeRig, coded_color, pixel_at


def rgb(color: QColor) -> tuple[int, int, int]:
    return color.red(), color.green(), color.blue()


TWO_SCREENS = [
    ("main", Rect(0, 0, 1920, 1080), 1.0),
    ("left", Rect(-2560, -200, 2560, 1440), 1.5),
]
GEOMETRIES = [screen_from_native(name, native, dpr) for name, native, dpr in TWO_SCREENS]


def test_native_rect_keeps_the_origin_and_uses_pixel_size() -> None:
    assert native_rect(GEOMETRIES[1]) == Rect(-2560, -200, 2560, 1440)
    assert GEOMETRIES[1].logical == Rect(-2560, -200, 1707, 960)


def test_intersect() -> None:
    assert intersect(Rect(0, 0, 10, 10), Rect(5, 5, 10, 10)) == Rect(5, 5, 5, 5)
    assert intersect(Rect(0, 0, 10, 10), Rect(10, 0, 5, 5)) is None


def test_pieces_split_a_rect_across_screens() -> None:
    pieces = native_pieces(GEOMETRIES, Rect(-100, 10, 300, 50))
    assert pieces == [
        NativePiece(0, Rect(0, 10, 200, 50), Rect(100, 0, 200, 50)),
        NativePiece(1, Rect(2460, 210, 100, 50), Rect(0, 0, 100, 50)),
    ]
    assert anchor_piece(pieces) == pieces[0]
    assert native_pieces(GEOMETRIES, Rect(5000, 5000, 10, 10)) == []
    assert anchor_piece([]) is None


async def test_single_screen_rect_matches_the_frozen_pixels(make_rig: MakeRig) -> None:
    screens = FakeScreens(TWO_SCREENS)
    rig = await make_rig(screens)
    session = rig.controller.capture_rect(Rect(-2560 + 90, -200 + 210, 1260, 360))
    await rig.bus.drain()
    [event] = rig.completed
    assert event.session_id == session == rig.controller.last_session
    assert not rig.controller.active and screens.calls == 1
    left = screens._cache["left"]  # pyright: ignore[reportPrivateUsage]
    assert event.image.data == to_raw_image(left, Rect(90, 210, 1260, 360)).data
    # 与在蒙版上框出同一块的几何信息相同（见 test_capture.py 与完整交互测试）。
    assert event.geometry.screen_name == "left" and event.geometry.dpr == 1.5
    assert event.geometry.selection == (-2500, -60, 840, 240)
    assert event.geometry.selection == logical_rect(GEOMETRIES[1], Rect(90, 210, 1260, 360)).as_tuple()


async def test_cross_screen_rect_is_stitched_in_native_coordinates(make_rig: MakeRig) -> None:
    rig = await make_rig(FakeScreens(TWO_SCREENS))
    rig.controller.capture_rect(Rect(-100, 10, 300, 50))
    await rig.bus.drain()
    image = rig.completed[-1].image
    assert (image.width, image.height) == (300, 50)
    assert pixel_at(image, 0, 0) == rgb(coded_color(2460, 210))
    assert pixel_at(image, 99, 49) == rgb(coded_color(2559, 259))
    assert pixel_at(image, 100, 0) == rgb(coded_color(0, 10))
    assert pixel_at(image, 299, 49) == rgb(coded_color(199, 59))
    geometry = rig.completed[-1].geometry
    assert geometry.screen_name == "main"  # 主屏上的面积更大
    assert geometry.selection == (-100, 10, 300, 50)


async def test_gaps_between_screens_stay_black(make_rig: MakeRig) -> None:
    rig = await make_rig(FakeScreens([("main", Rect(0, 0, 1920, 1080), 1.0)]))
    rig.controller.capture_rect(Rect(1900, 1070, 40, 20))
    await rig.bus.drain()
    image = rig.completed[-1].image
    assert pixel_at(image, 0, 0) == rgb(coded_color(1900, 1070))
    assert pixel_at(image, 39, 19) == (0, 0, 0)


async def test_rejects_rects_outside_screens_and_while_selecting(make_rig: MakeRig) -> None:
    screens = FakeScreens(TWO_SCREENS)
    rig = await make_rig(screens)
    with pytest.raises(ValueError):
        rig.controller.capture_rect(Rect(9000, 9000, 10, 10))
    with pytest.raises(ValueError):
        rig.controller.capture_rect(Rect(0, 0, 0, 10))
    rig.controller.start()
    with pytest.raises(RuntimeError):
        rig.controller.capture_rect(Rect(0, 0, 10, 10))
    rig.controller.cancel()
    screens.fail = True
    with pytest.raises(RuntimeError):
        rig.controller.capture_rect(Rect(0, 0, 10, 10))
    await rig.bus.drain()
    assert rig.completed == [] and rig.controller.last_session is None

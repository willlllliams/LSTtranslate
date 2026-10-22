"""截屏模块：蒙版窗口的创建与取消、框选、CaptureCompleted 的内容、屏幕变化（需求 CAP-01 至 CAP-07）。"""

import logging
import uuid
from collections.abc import Sequence

import pytest
from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QGuiApplication, QScreen
from PySide6.QtTest import QTest

from lst.contracts.events import NoticeLevel, StartCapture
from lst.ui.capture import Rect, screen_from_native
from lst.ui.capture import controller as controller_module
from lst.ui.capture.controller import to_raw_image
from tests.ui.capture.conftest import (
    FakeScreens,
    MakeRig,
    Rig,
    coded_color,
    coded_pixmap,
    drag,
    gone,
    mouse,
    pixel_at,
)

TWO_SCREENS = [
    ("main", Rect(0, 0, 1920, 1080), 1.0),
    ("left", Rect(-2560, -200, 2560, 1440), 1.5),
]


async def _start(rig: Rig) -> None:
    rig.bus.send(StartCapture())
    await rig.bus.drain()


async def test_start_capture_freezes_each_screen_and_shows_one_mask_per_screen(make_rig: MakeRig) -> None:
    screens = FakeScreens(TWO_SCREENS)
    rig = await make_rig(screens)
    await _start(rig)
    assert screens.calls == 1
    assert rig.controller.active
    assert {m.screen_geometry.name for m in rig.masks} == {"main", "left"}
    left = rig.mask("left")
    assert left.geometry() == QRect(-2560, -200, 1707, 960)
    assert left.frozen.devicePixelRatio() == 1.5
    for mask in rig.masks:
        assert mask.isVisible()
        assert mask.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
        flags = mask.windowFlags()
        assert flags & Qt.WindowType.FramelessWindowHint
        assert flags & Qt.WindowType.WindowStaysOnTopHint
    timing = rig.controller.last_timing
    assert timing is not None and timing.screens == 2


async def test_second_start_while_capturing_does_not_refreeze(make_rig: MakeRig) -> None:
    screens = FakeScreens(TWO_SCREENS)
    rig = await make_rig(screens)
    await _start(rig)
    masks = rig.masks
    await _start(rig)
    assert screens.calls == 1
    assert rig.masks == masks


@pytest.mark.parametrize("how", ["escape", "right_click"])
async def test_escape_and_right_click_cancel(make_rig: MakeRig, how: str) -> None:
    rig = await make_rig(FakeScreens(TWO_SCREENS))
    await _start(rig)
    masks = rig.masks
    mask = rig.mask("main")
    if how == "escape":
        QTest.keyClick(mask, Qt.Key.Key_Escape)
    else:
        mouse(mask, QEvent.Type.MouseButtonPress, 100, 100, Qt.MouseButton.RightButton)
    await rig.bus.drain()
    assert not rig.controller.active
    assert all(gone(m) for m in masks)
    assert rig.completed == []


async def test_right_click_during_drag_cancels(make_rig: MakeRig) -> None:
    rig = await make_rig(FakeScreens(TWO_SCREENS))
    await _start(rig)
    mask = rig.mask("main")
    mouse(mask, QEvent.Type.MouseButtonPress, 10, 10, Qt.MouseButton.LeftButton)
    mouse(mask, QEvent.Type.MouseMove, 300, 300, Qt.MouseButton.LeftButton)
    mouse(mask, QEvent.Type.MouseButtonPress, 300, 300, Qt.MouseButton.RightButton)
    await rig.bus.drain()
    assert not rig.controller.active
    assert rig.completed == []


@pytest.mark.parametrize(("dx", "dy"), [(4.9, 200), (200, 4.9), (0, 0)])
async def test_too_small_selection_cancels(make_rig: MakeRig, dx: float, dy: float) -> None:
    rig = await make_rig(FakeScreens(TWO_SCREENS))
    await _start(rig)
    drag(rig.mask("main"), (100, 100), (100 + dx, 100 + dy))
    await rig.bus.drain()
    assert not rig.controller.active
    assert rig.completed == []


async def test_selection_is_shown_while_dragging(make_rig: MakeRig) -> None:
    rig = await make_rig(FakeScreens(TWO_SCREENS))
    await _start(rig)
    mask = rig.mask("left")
    left = Qt.MouseButton.LeftButton
    mouse(mask, QEvent.Type.MouseButtonPress, 10, 20, left)
    mouse(mask, QEvent.Type.MouseMove, 110, 220, left)
    selection = mask.selection
    assert selection is not None
    assert selection.pixels == Rect(15, 30, 150, 300)
    # 尺寸标签显示物理像素，也就是截图的尺寸。
    image = mask.grab().toImage()
    assert not image.isNull()


async def test_capture_completed_carries_physical_pixels_and_geometry(make_rig: MakeRig) -> None:
    """150% 的左侧副屏（原生 x 从 -2560 起）上框选 (10, 20)–(110, 220) 逻辑像素。"""
    rig = await make_rig(FakeScreens(TWO_SCREENS))
    await _start(rig)
    masks = rig.masks
    drag(rig.mask("left"), (10, 20), (110, 220))
    await rig.bus.drain()
    assert not rig.controller.active
    assert all(gone(m) for m in masks)
    [event] = rig.completed
    image, geometry = event.image, event.geometry
    assert (image.width, image.height) == (150, 300)
    assert image.stride >= image.width * 4
    assert geometry.screen_name == "left"
    assert geometry.screen_rect == (-2560, -200, 1707, 960)
    assert geometry.selection == (-2550, -180, 100, 200)
    assert geometry.dpr == 1.5
    # 像素来自冻结画面的 (15, 30) 起的区域。
    for x, y in [(0, 0), (149, 0), (0, 299), (149, 299), (77, 123)]:
        expected = coded_color(15 + x, 30 + y)
        assert pixel_at(image, x, y) == (expected.red(), expected.green(), expected.blue())
    parsed = uuid.UUID(event.session_id)
    assert parsed.version == 4


async def test_drag_beyond_the_pressed_screen_is_clamped(make_rig: MakeRig) -> None:
    """在主屏按下后拖到左侧副屏的位置（窗口坐标为负），选区止于主屏左边缘（CAP-02）。"""
    rig = await make_rig(FakeScreens(TWO_SCREENS))
    await _start(rig)
    drag(rig.mask("main"), (300, 400), (-900, 50))
    await rig.bus.drain()
    [event] = rig.completed
    assert event.geometry.screen_name == "main"
    assert (event.image.width, event.image.height) == (300, 350)
    assert event.geometry.selection == (0, 50, 300, 350)


async def test_each_capture_gets_a_new_session(make_rig: MakeRig) -> None:
    rig = await make_rig(FakeScreens(TWO_SCREENS))
    for _ in range(2):
        await _start(rig)
        drag(rig.mask("main"), (10, 10), (60, 60))
        await rig.bus.drain()
    first, second = rig.completed
    assert first.session_id != second.session_id


@pytest.mark.parametrize(
    ("native", "dpr", "start", "end", "pixels"),
    [
        (Rect(0, 0, 2560, 1600), 1.75, (100.0, 100.0), (300.0, 200.0), (175, 175, 350, 175)),
        (Rect(0, 0, 3840, 2160), 2.0, (0.5, 0.5), (1920.0, 1080.0), (1, 1, 3839, 2159)),
        (Rect(0, -1920, 1080, 1920), 1.25, (8.0, 16.0), (808.0, 1536.0), (10, 20, 1000, 1900)),
    ],
)
async def test_non_integer_scaling_and_portrait(
    make_rig: MakeRig,
    native: Rect,
    dpr: float,
    start: tuple[float, float],
    end: tuple[float, float],
    pixels: tuple[int, int, int, int],
) -> None:
    rig = await make_rig(FakeScreens([("s", native, dpr)]))
    await _start(rig)
    drag(rig.mask("s"), start, end)
    await rig.bus.drain()
    [event] = rig.completed
    x, y, w, h = pixels
    assert (event.image.width, event.image.height) == (w, h)
    expected = coded_color(x, y)
    assert pixel_at(event.image, 0, 0) == (expected.red(), expected.green(), expected.blue())
    assert event.geometry.dpr == dpr
    assert event.geometry.screen_rect == screen_from_native("s", native, dpr).logical.as_tuple()


async def test_freeze_failure_notifies(make_rig: MakeRig, caplog: pytest.LogCaptureFixture) -> None:
    screens = FakeScreens(TWO_SCREENS, fail=True)
    rig = await make_rig(screens)
    with caplog.at_level(logging.ERROR):
        await _start(rig)
    await rig.bus.drain()
    assert not rig.controller.active
    [notice] = rig.notices
    assert notice.title_key == "capture.failed.title"
    assert notice.level is NoticeLevel.ERROR


def test_to_raw_image_crops_by_pixels() -> None:
    pixmap = coded_pixmap(600, 400)
    pixmap.setDevicePixelRatio(1.5)
    image = to_raw_image(pixmap, Rect(300, 260, 257, 140))
    assert (image.width, image.height) == (257, 140)
    expected = coded_color(300 + 256, 260 + 139)
    assert pixel_at(image, 256, 139) == (expected.red(), expected.green(), expected.blue())
    # BGRA 的透明度字节恒为 0xFF。
    assert image.data[3] == 255


# ---------------------------------------------------------------- 真实屏幕与屏幕变化（CAP-07）


def _screen() -> QScreen:
    return QGuiApplication.screens()[0]


async def test_real_screens_are_frozen_in_physical_pixels(make_rig: MakeRig) -> None:
    rig = await make_rig(real=True)
    await _start(rig)
    assert len(rig.masks) == len(QGuiApplication.screens())
    for mask in rig.masks:
        geometry = mask.screen_geometry
        screen = next(s for s in QGuiApplication.screens() if s.name() == geometry.name)
        assert geometry.dpr == screen.devicePixelRatio()
        assert abs(geometry.pixel_width - screen.geometry().width() * geometry.dpr) <= 1
        assert mask.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    QTest.keyClick(rig.masks[0], Qt.Key.Key_Escape)
    await rig.bus.drain()
    assert not rig.controller.active


@pytest.mark.parametrize("signal", ["screenAdded", "screenRemoved", "dpi", "geometry", "orientation"])
async def test_screen_change_during_capture_cancels_and_notifies(make_rig: MakeRig, signal: str) -> None:
    rig = await make_rig(real=True)
    await _start(rig)
    masks = rig.masks
    app = QGuiApplication.instance()
    assert isinstance(app, QGuiApplication)
    screen = _screen()
    match signal:
        case "screenAdded":
            app.screenAdded.emit(screen)
        case "screenRemoved":
            app.screenRemoved.emit(screen)
        case "dpi":
            screen.logicalDotsPerInchChanged.emit(144.0)
        case "geometry":
            screen.geometryChanged.emit(QRect(0, 0, 100, 100))
        case _:
            screen.orientationChanged.emit(Qt.ScreenOrientation.PortraitOrientation)
    await rig.bus.drain()
    assert not rig.controller.active
    assert all(gone(m) for m in masks)
    assert rig.completed == []
    [notice] = rig.notices
    assert notice.title_key == "capture.screens_changed.title"
    assert notice.message_key == "capture.screens_changed.message"
    assert notice.level is NoticeLevel.WARNING


async def test_signals_are_disconnected_after_capture(make_rig: MakeRig) -> None:
    rig = await make_rig(real=True)
    await _start(rig)
    QTest.keyClick(rig.masks[0], Qt.Key.Key_Escape)
    await rig.bus.drain()
    _screen().logicalDotsPerInchChanged.emit(144.0)
    await rig.bus.drain()
    assert rig.notices == []


async def test_layout_change_detected_at_release(make_rig: MakeRig, monkeypatch: pytest.MonkeyPatch) -> None:
    """信号漏发时，松开鼠标前再比较一次屏幕几何：不一致就取消并提示。"""
    rig = await make_rig(real=True)
    await _start(rig)

    def changed(screens: Sequence[QScreen]) -> tuple[tuple[str, Rect, float], ...]:
        del screens
        return (("changed", Rect(0, 0, 1, 1), 9.0),)

    monkeypatch.setattr(controller_module, "live_signature", changed)
    drag(rig.masks[0], (10, 10), (200, 200))
    await rig.bus.drain()
    assert rig.completed == []
    [notice] = rig.notices
    assert notice.title_key == "capture.screens_changed.title"


async def test_real_capture_completes(make_rig: MakeRig) -> None:
    rig = await make_rig(real=True)
    await _start(rig)
    mask = rig.masks[0]
    dpr = mask.screen_geometry.dpr
    drag(mask, (10, 10), (110, 60))
    await rig.bus.drain()
    [event] = rig.completed
    assert abs(event.image.width - 100 * dpr) <= 1
    assert abs(event.image.height - 50 * dpr) <= 1


async def test_stop_cancels_active_capture(make_rig: MakeRig) -> None:
    rig = await make_rig(FakeScreens(TWO_SCREENS))
    await _start(rig)
    masks = rig.masks
    await rig.module.stop()
    assert all(gone(m) for m in masks)
    assert not rig.controller.active

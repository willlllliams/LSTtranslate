"""截屏蒙版的键位（需求 UI-09、CAP-04，架构文档 11.8 节）：改键后旧键位失效、新键位生效，
拖动途中按其他键不出错，配置变化对正在显示的蒙版立即生效。默认键位下的操作见 test_capture.py。"""

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from lst.contracts.config import AppConfig
from lst.contracts.events import StartCapture
from lst.contracts.worker_protocol import JsonValue
from lst.ui.capture import Rect
from tests.ui.capture.conftest import FakeScreens, MakeRig, Rig, drag, mouse

ONE_SCREEN = [("main", Rect(0, 0, 1920, 1080), 1.0)]
NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
LEFT, RIGHT, MIDDLE = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton


def bindings(revision: int = 1, /, **fields: JsonValue) -> AppConfig:
    return AppConfig({"bindings": dict(fields)}, revision)


async def _started(make_rig: MakeRig, *configs: AppConfig) -> Rig:
    rig = await make_rig(FakeScreens(ONE_SCREEN))
    for config in configs:
        rig.bus.publish(config)
    rig.bus.send(StartCapture())
    await rig.bus.drain()
    return rig


def send_mouse(
    widget: QWidget,
    kind: QEvent.Type,
    x: float,
    y: float,
    button: Qt.MouseButton,
    modifiers: Qt.KeyboardModifier,
) -> None:
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    local = QPointF(x, y)
    QApplication.sendEvent(
        widget, QMouseEvent(kind, local, widget.mapToGlobal(local), button, buttons, modifiers)
    )


async def test_cancel_rebound_to_middle_click(make_rig: MakeRig) -> None:
    rig = await _started(make_rig, bindings(capture_cancel=["MiddleClick"]))
    mask = rig.mask("main")
    mouse(mask, QEvent.Type.MouseButtonPress, 100, 100, RIGHT)
    mouse(mask, QEvent.Type.MouseButtonRelease, 100, 100, RIGHT)
    QTest.keyClick(mask, Qt.Key.Key_Escape)
    await rig.bus.drain()
    assert rig.controller.active, "右键与 Esc 不再取消"
    mouse(mask, QEvent.Type.MouseButtonPress, 100, 100, MIDDLE)
    await rig.bus.drain()
    assert not rig.controller.active
    assert rig.completed == []


async def test_cancel_rebound_to_a_key(make_rig: MakeRig) -> None:
    rig = await _started(make_rig, bindings(capture_cancel=["Q"]))
    QTest.keyClick(rig.mask("main"), Qt.Key.Key_Q)
    await rig.bus.drain()
    assert not rig.controller.active


async def test_select_rebound_to_ctrl_left_drag(make_rig: MakeRig) -> None:
    rig = await _started(make_rig, bindings(capture_select=["Ctrl+LeftDrag"]))
    mask = rig.mask("main")
    drag(mask, (100, 100), (300, 250))
    assert mask.selection is None, "不带 Ctrl 的左键拖动不再框选"
    assert rig.controller.active
    send_mouse(mask, QEvent.Type.MouseButtonPress, 100, 100, LEFT, CTRL)
    send_mouse(mask, QEvent.Type.MouseMove, 200, 180, LEFT, CTRL)
    send_mouse(mask, QEvent.Type.MouseMove, 300, 250, LEFT, CTRL)
    send_mouse(mask, QEvent.Type.MouseButtonRelease, 300, 250, LEFT, CTRL)
    await rig.bus.drain()
    [event] = rig.completed
    assert event.geometry.selection == (100, 100, 200, 150)


async def test_selection_starts_at_the_press_point(make_rig: MakeRig) -> None:
    """移动超过 4 个逻辑像素才开始框选，选区仍然从按下的位置算起。"""
    rig = await _started(make_rig)
    mask = rig.mask("main")
    mouse(mask, QEvent.Type.MouseButtonPress, 100, 100, LEFT)
    mouse(mask, QEvent.Type.MouseMove, 102, 102, LEFT)
    assert mask.selection is None
    mouse(mask, QEvent.Type.MouseMove, 160, 140, LEFT)
    selection = mask.selection
    assert selection is not None
    assert selection.pixels == Rect(100, 100, 60, 40)


async def test_other_buttons_during_drag_do_not_break_it(make_rig: MakeRig) -> None:
    """取消改到 Esc 后，框选途中按下、松开右键和中键没有效果，框选照常完成。"""
    rig = await _started(make_rig, bindings(capture_cancel=["Esc"]))
    mask = rig.mask("main")
    mouse(mask, QEvent.Type.MouseButtonPress, 100, 100, LEFT)
    mouse(mask, QEvent.Type.MouseMove, 200, 200, LEFT)
    for button in (RIGHT, MIDDLE):
        mouse(mask, QEvent.Type.MouseButtonPress, 200, 200, button)
        mouse(mask, QEvent.Type.MouseButtonRelease, 200, 200, button)
    mouse(mask, QEvent.Type.MouseMove, 300, 300, LEFT)
    mouse(mask, QEvent.Type.MouseButtonRelease, 300, 300, LEFT)
    await rig.bus.drain()
    [event] = rig.completed
    assert event.geometry.selection == (100, 100, 200, 200)


async def test_double_click_binding(make_rig: MakeRig) -> None:
    rig = await _started(make_rig, bindings(capture_cancel=["RightDoubleClick"]))
    mask = rig.mask("main")
    mouse(mask, QEvent.Type.MouseButtonPress, 100, 100, RIGHT)
    mouse(mask, QEvent.Type.MouseButtonRelease, 100, 100, RIGHT)
    assert rig.controller.active
    mouse(mask, QEvent.Type.MouseButtonDblClick, 100, 100, RIGHT)
    await rig.bus.drain()
    assert not rig.controller.active


async def test_config_change_applies_to_the_open_mask(make_rig: MakeRig) -> None:
    rig = await _started(make_rig)
    mask = rig.mask("main")
    assert mask.hint_text() == "框选区域：左键拖动　　取消截屏：右键单击 / Esc"
    rig.bus.publish(bindings(capture_cancel=["MiddleClick"]))
    await rig.bus.drain()
    assert mask.hint_text() == "框选区域：左键拖动　　取消截屏：中键单击"
    mouse(mask, QEvent.Type.MouseButtonPress, 100, 100, RIGHT)
    await rig.bus.drain()
    assert rig.controller.active, "正在显示的蒙版立即换用新键位"
    mouse(mask, QEvent.Type.MouseButtonPress, 100, 100, MIDDLE)
    await rig.bus.drain()
    assert not rig.controller.active

    # 之后的截屏也用新键位。
    rig.bus.send(StartCapture())
    await rig.bus.drain()
    QTest.keyClick(rig.mask("main"), Qt.Key.Key_Escape)
    await rig.bus.drain()
    assert rig.controller.active


async def test_hint_omits_unbound_actions(make_rig: MakeRig) -> None:
    rig = await _started(make_rig, bindings(capture_cancel=[]))
    assert rig.mask("main").hint_text() == "框选区域：左键拖动"
    QTest.keyClick(rig.mask("main"), Qt.Key.Key_Escape)
    await rig.bus.drain()
    assert rig.controller.active
    rig.controller.cancel()

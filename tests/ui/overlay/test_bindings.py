"""覆盖窗口的键位（需求 UI-09、OV-05，架构文档 11.8 节）：改键后旧键位失效、新键位生效，键盘放大缩小还原，
配置变化对已经打开的窗口立即生效。默认键位下的操作见 test_overlay.py。"""

import pytest
from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from lst.contracts.config import AppConfig
from lst.contracts.ids import SegmentId
from lst.contracts.worker_protocol import JsonValue
from lst.ui.overlay.geometry import MAX_ZOOM, window_size
from lst.ui.overlay.window import OverlayWindow
from tests.ui.overlay.conftest import MakeRig, Rig, capture, layout_result, text_result, view

P1, P2 = SegmentId("p1"), SegmentId("p2")
NONE = Qt.KeyboardModifier.NoModifier
CTRL = Qt.KeyboardModifier.ControlModifier
SHIFT = Qt.KeyboardModifier.ShiftModifier
LEFT, RIGHT, MIDDLE = Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton, Qt.MouseButton.MiddleButton


def bindings(revision: int = 1, /, **fields: JsonValue) -> AppConfig:
    return AppConfig({"bindings": dict(fields)}, revision)


async def _with_text(make_rig: MakeRig) -> Rig:
    rig = await make_rig()
    await rig.publish(capture("s1"), text_result("s1"))
    rig.window.fit_all_now()
    return rig


def send_mouse(
    widget: QWidget,
    kind: QEvent.Type,
    local: QPointF,
    button: Qt.MouseButton,
    modifiers: Qt.KeyboardModifier = NONE,
    *,
    global_pos: QPointF | None = None,
) -> None:
    buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else button
    position = global_pos if global_pos is not None else widget.mapToGlobal(local)
    QApplication.sendEvent(widget, QMouseEvent(kind, local, position, button, buttons, modifiers))


def click(widget: QWidget, button: Qt.MouseButton, modifiers: Qt.KeyboardModifier = NONE) -> None:
    point = QPointF(10, 10)
    send_mouse(widget, QEvent.Type.MouseButtonPress, point, button, modifiers)
    send_mouse(widget, QEvent.Type.MouseButtonRelease, point, button, modifiers)


def drag_window(window: OverlayWindow, button: Qt.MouseButton, modifiers: Qt.KeyboardModifier = NONE) -> None:
    """按住 button 把窗口往右下拖 (120, 45)：窗口内的坐标不变，全局坐标变化，与真实拖动窗口时一致。"""
    local = QPointF(50, 20)
    origin = QPointF(window.x() + 50, window.y() + 20)
    send_mouse(window, QEvent.Type.MouseButtonPress, local, button, modifiers, global_pos=origin)
    for step in (QPointF(30, 0), QPointF(120, 45)):
        send_mouse(window, QEvent.Type.MouseMove, local, button, modifiers, global_pos=origin + step)
    send_mouse(
        window, QEvent.Type.MouseButtonRelease, local, button, modifiers, global_pos=origin + QPointF(120, 45)
    )


def drag_select(window: OverlayWindow, button: Qt.MouseButton, modifiers: Qt.KeyboardModifier = NONE) -> None:
    """拖选只盖住 p2（截图坐标 y 100–180）。"""
    s = window.scale_logical()
    send_mouse(window, QEvent.Type.MouseButtonPress, QPointF(30 * s, 110 * s), button, modifiers)
    send_mouse(window, QEvent.Type.MouseMove, QPointF(300 * s, 150 * s), button, modifiers)
    send_mouse(window, QEvent.Type.MouseButtonRelease, QPointF(300 * s, 150 * s), button, modifiers)


def wheel(window: OverlayWindow, modifiers: Qt.KeyboardModifier = NONE, delta: int = 120) -> None:
    cursor = QPointF(100, 50)
    event = QWheelEvent(
        cursor,
        window.mapToGlobal(cursor),
        QPoint(0, 0),
        QPoint(0, delta),
        Qt.MouseButton.NoButton,
        modifiers,
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(window, event)


# ---------------------------------------------------------------- 改键


async def test_toggle_source_rebound_to_space(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    await rig.publish(bindings(overlay_toggle_source=["Space"]))
    click(window, RIGHT)
    assert not window.showing_source, "右键单击不再切换"
    QTest.keyClick(window, Qt.Key.Key_Space)
    assert window.showing_source
    # 右键拖动仍然移动窗口。
    start = window.pos()
    drag_window(window, RIGHT)
    assert window.pos() == start + QPoint(120, 45)


async def test_move_rebound_to_ctrl_left_drag(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    await rig.publish(bindings(overlay_move=["Ctrl+LeftDrag"]))
    start = window.pos()
    drag_window(window, RIGHT)
    assert window.pos() == start, "右键拖动不再移动窗口"
    # 右键没有拖动操作了，单击在按下时就切换；这次拖动只切换了一次。
    assert window.showing_source
    window.toggle_source()

    drag_window(window, LEFT, CTRL)
    assert window.pos() == start + QPoint(120, 45)
    assert window.selected == frozenset(), "Ctrl+左键拖动移动窗口，不框选"
    # 不带 Ctrl 的左键拖动仍然框选。
    drag_select(window, LEFT)
    assert window.selected == {P2}


async def test_zoom_rebound_to_ctrl_wheel(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    await rig.publish(bindings(overlay_zoom=["Ctrl+Wheel"]))
    wheel(window)
    assert window.zoom == 1.0, "不带 Ctrl 的滚轮不再缩放"
    wheel(window, CTRL)
    assert window.zoom == pytest.approx(1.1)


async def test_select_rebound_keeps_click_behaviour_on_the_new_button(make_rig: MakeRig) -> None:
    """框选改到 Shift+左键拖动后，单击清除选择、点开图表也跟着到 Shift+左键单击。"""
    rig = await make_rig()
    await rig.publish(capture("s1"), layout_result("s1"))
    window = rig.window
    await rig.publish(bindings(overlay_select=["Shift+LeftDrag"]))
    s = window.scale_logical()
    chart = QPointF(450 * s, 230 * s)
    send_mouse(window, QEvent.Type.MouseButtonPress, chart, LEFT)
    send_mouse(window, QEvent.Type.MouseButtonRelease, chart, LEFT)
    assert window.expanded_charts == frozenset(), "不带 Shift 的左键单击不再点开图表"
    send_mouse(window, QEvent.Type.MouseButtonPress, chart, LEFT, SHIFT)
    send_mouse(window, QEvent.Type.MouseButtonRelease, chart, LEFT, SHIFT)
    assert window.expanded_charts == {"4"}


async def test_pin_rebound_to_middle_double_click(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    await rig.publish(bindings(overlay_pin=["MiddleDoubleClick"]))
    click(window, MIDDLE)
    assert not window.pinned
    point = QPointF(10, 10)
    send_mouse(window, QEvent.Type.MouseButtonPress, point, MIDDLE)
    send_mouse(window, QEvent.Type.MouseButtonRelease, point, MIDDLE)
    send_mouse(window, QEvent.Type.MouseButtonDblClick, point, MIDDLE)
    send_mouse(window, QEvent.Type.MouseButtonRelease, point, MIDDLE)
    assert window.pinned


async def test_close_rebound_and_standard_copy_stays(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    window.apply_views([view("p1", "Hello world", "你好世界")])
    await rig.publish(bindings(overlay_close=["Ctrl+W"]))
    window.select_range((P1, 0), (P1, 2))
    QTest.keyClick(window, Qt.Key.Key_C, CTRL)
    assert rig.copied == ["你好"], "复制用标准复制键，改键不影响它"
    QTest.keyClick(window, Qt.Key.Key_Escape)
    assert window.isVisible(), "Esc 不再关闭"
    QTest.keyClick(window, Qt.Key.Key_W, CTRL)
    await rig.bus.drain()
    assert rig.module.window is None


async def test_binding_on_the_copy_key_takes_precedence(make_rig: MakeRig) -> None:
    """用户把 Ctrl+C 绑定给别的操作时按键位表执行，标准复制让位；Ctrl+Insert 仍然复制。"""
    rig = await _with_text(make_rig)
    window = rig.window
    await rig.publish(bindings(overlay_pin=["Ctrl+C"]))
    window.select_range((P1, 0), (P1, 5))
    QTest.keyClick(window, Qt.Key.Key_C, CTRL)
    assert window.pinned
    assert rig.copied == []
    QTest.keyClick(window, Qt.Key.Key_Insert, CTRL)
    assert rig.copied == ["Hello"], "Ctrl+Insert 也是标准复制键"


async def test_old_copy_fields_do_not_invalidate_the_section(make_rig: MakeRig) -> None:
    """旧配置里还有 overlay_copy、bilingual_copy：丢掉它们，同一节里改过的键位照常生效。"""
    rig = await _with_text(make_rig)
    window = rig.window
    await rig.publish(
        bindings(
            overlay_copy=["Ctrl+C", "Ctrl+Ins"],
            bilingual_copy=["Ctrl+Shift+C"],
            overlay_toggle_source=["Space"],
        )
    )
    QTest.keyClick(window, Qt.Key.Key_Space)
    assert window.showing_source, "整节没有退回默认值"
    window.select_range((P1, 0), (P1, 5))
    QTest.keyClick(window, Qt.Key.Key_C, CTRL)
    assert rig.copied == ["Hello"]


async def test_unbound_action_does_nothing(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    await rig.publish(bindings(overlay_toggle_source=[], overlay_pin=[]))
    click(window, RIGHT)
    click(window, MIDDLE)
    assert not window.showing_source
    assert not window.pinned


# ---------------------------------------------------------------- 键盘放大、缩小、还原


async def test_keyboard_zoom_in_out_reset_about_the_center(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    before = window.geometry()
    QTest.keyClick(window, Qt.Key.Key_Equal, CTRL)
    assert window.zoom == pytest.approx(1.1), "与滚轮一格的步长相同"
    after = window.geometry()
    assert (after.width(), after.height()) == window_size(600, 300, 1.5, 1.1)
    # 以窗口中心为锚点：新左上角 = 旧左上角 + c − c × 1.1，c 是旧窗口尺寸的一半。
    assert after.x() == round(before.x() + before.width() / 2 * (1 - 1.1))
    assert after.y() == round(before.y() + before.height() / 2 * (1 - 1.1))
    # 缩放的是整张图：一个截图像素对应的逻辑长度随倍数变化，底图与段的范围一起放大。
    assert window.scale_logical() == pytest.approx(1.1 / 1.5)

    QTest.keyClick(window, Qt.Key.Key_Minus, CTRL)
    assert window.zoom == pytest.approx(1.0)
    QTest.keyClick(window, Qt.Key.Key_Minus, CTRL)
    assert window.zoom == pytest.approx(1 / 1.1)

    for _ in range(40):
        QTest.keyClick(window, Qt.Key.Key_Equal, CTRL)
    assert window.zoom == pytest.approx(MAX_ZOOM), "放大到上限为止"
    QTest.keyClick(window, Qt.Key.Key_0, CTRL)
    assert window.zoom == 1.0
    assert (window.width(), window.height()) == (before.width(), before.height())


async def test_keyboard_zoom_rebound(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    await rig.publish(
        bindings(overlay_zoom_in=["Ctrl+Up"], overlay_zoom_out=["Ctrl+Down"], overlay_zoom_reset=["Home"])
    )
    QTest.keyClick(window, Qt.Key.Key_Equal, CTRL)
    assert window.zoom == 1.0
    QTest.keyClick(window, Qt.Key.Key_Up, CTRL)
    QTest.keyClick(window, Qt.Key.Key_Up, CTRL)
    assert window.zoom == pytest.approx(1.21)
    QTest.keyClick(window, Qt.Key.Key_Down, CTRL)
    assert window.zoom == pytest.approx(1.1)
    QTest.keyClick(window, Qt.Key.Key_Home)
    assert window.zoom == 1.0


# ---------------------------------------------------------------- 配置变化


async def test_config_change_applies_to_open_and_new_windows(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    first = rig.window
    await rig.publish(bindings(overlay_toggle_source=["Space"]))
    QTest.keyClick(first, Qt.Key.Key_Space)
    assert first.showing_source, "已经打开的窗口立即换用新键位"

    await rig.publish(capture("s2"), text_result("s2"))
    second = rig.window
    assert second is not first
    QTest.keyClick(second, Qt.Key.Key_Space)
    assert second.showing_source, "之后新开的窗口也用新键位"

    await rig.publish(bindings(2))
    click(second, RIGHT)
    assert not second.showing_source, "恢复默认后右键单击又切换回来"


async def test_invalid_section_falls_back_to_defaults(make_rig: MakeRig) -> None:
    rig = await _with_text(make_rig)
    window = rig.window
    await rig.publish(bindings(overlay_pin=["Esc"]))  # 与关闭冲突，整节无效
    click(window, RIGHT)
    assert window.showing_source

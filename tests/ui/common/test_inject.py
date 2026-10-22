"""按键位文字合成的事件（lst.ui.common.inject，需求 DEV-05）：事件的种类、顺序、按键与修饰键，
以及拖动中窗口移动时坐标仍按真实鼠标的方式换算。"""

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QWidget

from lst.ui.common import inject
from lst.ui.common.bindings import BindingsConfig, Keymap, MouseGestures, Scope


class Recorder(QWidget):
    """记录收到的输入事件；move_with_drag 为真时像“拖动窗口”那样跟着全局位移移动自己。"""

    def __init__(self, *, move_with_drag: bool = False) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        self.setGeometry(100, 200, 400, 300)
        self.events: list[tuple[object, ...]] = []
        self._move = move_with_drag
        self._press: tuple[QPointF, QPoint] | None = None

    def keyPressEvent(self, event: QKeyEvent) -> None:
        self.events.append(("key", event.key(), event.modifiers()))

    def keyReleaseEvent(self, event: QKeyEvent) -> None:
        self.events.append(("key-up", event.key(), event.modifiers()))

    def _mouse(self, name: str, event: QMouseEvent) -> None:
        self.events.append(
            (
                name,
                event.button(),
                event.buttons(),
                event.modifiers(),
                event.position(),
                event.globalPosition(),
            )
        )

    def mousePressEvent(self, event: QMouseEvent) -> None:
        self._press = (event.globalPosition(), self.pos())
        self._mouse("press", event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._move and self._press is not None:
            start, origin = self._press
            self.move(origin + (event.globalPosition() - start).toPoint())
        self._mouse("move", event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._mouse("release", event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        self._mouse("double", event)

    def wheelEvent(self, event: QWheelEvent) -> None:
        self.events.append(("wheel", event.angleDelta().y(), event.modifiers(), event.position()))


def kinds(widget: Recorder) -> list[str]:
    return [str(event[0]) for event in widget.events]


def test_key_press_and_release_with_modifiers() -> None:
    widget = Recorder()
    inject.send_key(widget, "Ctrl+Shift+T")
    ctrl_shift = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
    assert widget.events == [
        ("key", int(Qt.Key.Key_T.value), ctrl_shift),
        ("key-up", int(Qt.Key.Key_T.value), ctrl_shift),
    ]
    with pytest.raises(ValueError):
        inject.send_key(widget, "RightClick")


def test_key_events_resolve_through_the_keymap() -> None:
    """合成的按键经 Keymap 查出的操作与真实按键相同。"""
    config = BindingsConfig.model_validate({"overlay_toggle_source": ["Ctrl+T"]})
    keymap = Keymap.for_scope(config, Scope.OVERLAY)
    seen: list[str | None] = []

    class Target(QWidget):
        def keyPressEvent(self, event: QKeyEvent) -> None:
            seen.append(keymap.key_action(event))

    target = Target()
    inject.send_key(target, "Ctrl+T")
    inject.send_key(target, "Esc")
    assert seen == ["overlay.toggle_source", "overlay.close"]


def test_click_and_double_click_sequences() -> None:
    widget = Recorder()
    inject.send_mouse(widget, "Ctrl+RightClick", (10, 20))
    assert kinds(widget) == ["press", "release"]
    press = widget.events[0]
    assert press[1] == Qt.MouseButton.RightButton and press[3] == Qt.KeyboardModifier.ControlModifier
    assert press[4] == QPointF(10, 20) and press[5] == widget.mapToGlobal(QPointF(10, 20))
    widget.events.clear()
    inject.send_mouse(widget, "MiddleDoubleClick", (5, 5))
    assert kinds(widget) == ["press", "release", "double", "release"]


def test_drag_follows_mouse_gestures() -> None:
    """拖动经过 MouseGestures：第一次移动就超过 4 个逻辑像素，开始拖动，松开时结束。"""
    gestures = MouseGestures(Keymap.defaults(Scope.OVERLAY))
    steps: list[object] = []

    class Target(QWidget):
        def mousePressEvent(self, event: QMouseEvent) -> None:
            steps.append(gestures.press(event.button(), event.modifiers(), event.position()))

        def mouseMoveEvent(self, event: QMouseEvent) -> None:
            steps.append(gestures.move(event.position()))

        def mouseReleaseEvent(self, event: QMouseEvent) -> None:
            steps.append(gestures.release(event.button()))

    inject.send_mouse(Target(), "LeftDrag", (10, 10), (90, 10))
    assert steps[0] is None and steps[1] == ("start", "overlay.select")
    assert steps[2:-1] == [("move", "overlay.select")] * (inject.DRAG_STEPS - 1)
    assert steps[-1] == ("end", "overlay.select")
    with pytest.raises(ValueError):
        inject.send_mouse(Target(), "LeftDrag", (0, 0))


def test_drag_keeps_global_positions_when_the_window_moves() -> None:
    widget = Recorder(move_with_drag=True)
    start = widget.pos()
    inject.send_mouse(widget, "RightDrag", (50, 50), (130, 90))
    assert widget.pos() == start + QPoint(80, 40)
    globals_ = [event[5] for event in widget.events]
    origin = globals_[0]
    assert isinstance(origin, QPointF)
    assert globals_[-1] == origin + QPointF(80, 40)


def test_press_and_move_then_release() -> None:
    widget = Recorder()
    button, point = inject.press_and_move(widget, "LeftDrag", (10, 10), (60, 40))
    assert kinds(widget) == ["press", *["move"] * inject.DRAG_STEPS]
    inject.release(widget, button, point)
    assert widget.events[-1][0] == "release" and widget.events[-1][4] == QPointF(60, 40)
    with pytest.raises(ValueError):
        inject.press_and_move(widget, "LeftClick", (0, 0), (1, 1))


def test_wheel_and_any_binding() -> None:
    widget = Recorder()
    inject.send_mouse(widget, "Ctrl+Wheel", (30, 40), steps=-2)
    assert widget.events == [("wheel", -240, Qt.KeyboardModifier.ControlModifier, QPointF(30, 40))]
    widget.events.clear()
    inject.send_binding(widget, "MiddleClick")
    assert widget.events[0][4] == QPointF(200, 150)  # 缺省在窗口中心
    inject.send_binding(widget, "Esc")
    assert widget.events[-1][0] == "key-up"
    assert inject.current_bindings(BindingsConfig(), "capture.cancel") == ("RightClick", "Esc")

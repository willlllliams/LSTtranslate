"""按键位文字合成键盘、鼠标与滚轮事件，交给窗口自己的事件处理（需求 DEV-05，架构文档 16.4 节）。

自动化接口用它验证窗口里的操作与改过的键位：事件用 ``QApplication.sendEvent`` 直接交给目标窗口，
经过窗口的 ``event`` → ``mousePressEvent`` 等处理函数，走的是用户操作时同一套 ``Keymap`` 与
``MouseGestures``。不经过操作系统（不用 SendInput），不移动真实的鼠标指针，也不需要窗口在前台。

键位文字的写法见 ``lst.ui.common.bindings``：

- 键盘：``Ctrl+C``、``Esc``，发出按下与松开；
- 单击：``RightClick``，在 at 处按下再松开；双击：按下、松开、双击事件、松开，与 Qt 收到真实双击时的顺序相同；
- 拖动：``LeftDrag``，在 at 处按下，分几步移到 to，在 to 处松开；
- 滚轮：``Ctrl+Wheel``，在 at 处转 steps 格（正数向上）。

坐标是窗口内的逻辑坐标。拖动中窗口本身可能移动（例如“拖动窗口”），所以全局坐标在按下时按窗口的位置一次算好，
之后每个事件的窗口内坐标由全局坐标减去窗口当前的位置得到，与真实鼠标的行为一致。
"""

from collections.abc import Sequence
from typing import Final

from PySide6.QtCore import QEvent, QKeyCombination, QPoint, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QWidget

from lst.ui.common.bindings import ACTIONS_BY_ID, Binding, BindingsConfig, parse_binding

_BUTTONS: Final = {
    "Left": Qt.MouseButton.LeftButton,
    "Right": Qt.MouseButton.RightButton,
    "Middle": Qt.MouseButton.MiddleButton,
}
DRAG_STEPS: Final = 8
"""拖动时从起点到终点分几步移动。第一步就超过 4 个逻辑像素时，第一次移动即开始拖动。"""


def current_bindings(config: BindingsConfig, action: str) -> tuple[str, ...]:
    """操作 action 当前的键位；操作不存在时抛 KeyError。"""
    return config.bindings_of(ACTIONS_BY_ID[action])


def _modifiers(binding: Binding) -> Qt.KeyboardModifier:
    return Qt.KeyboardModifier(binding.modifiers)


def send_key(widget: QWidget, text: str) -> None:
    """按下并松开一个键盘键位。不是键盘键位时抛 ValueError。"""
    binding = parse_binding(text)
    if binding.kind != "key":
        raise ValueError(f"{text} 不是键盘键位")
    combination = QKeyCombination.fromCombined(binding.key)
    key, modifiers = combination.key(), combination.keyboardModifiers()
    for kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
        QApplication.sendEvent(widget, QKeyEvent(kind, int(key.value), modifiers))


class _Pointer:
    """一次鼠标操作：按下时记下窗口的位置，之后的全局坐标都按它换算。"""

    def __init__(self, widget: QWidget, modifiers: Qt.KeyboardModifier) -> None:
        self._widget = widget
        self._modifiers = modifiers
        self._origin = widget.mapToGlobal(QPointF(0, 0))

    def global_point(self, local: tuple[float, float]) -> QPointF:
        return QPointF(self._origin.x() + local[0], self._origin.y() + local[1])

    def send(
        self,
        kind: QEvent.Type,
        point: QPointF,
        button: Qt.MouseButton,
        buttons: Qt.MouseButton,
    ) -> None:
        local = point - self._widget.mapToGlobal(QPointF(0, 0))
        event = QMouseEvent(kind, local, point, button, buttons, self._modifiers)
        QApplication.sendEvent(self._widget, event)


def send_mouse(
    widget: QWidget,
    text: str,
    at: tuple[float, float],
    to: tuple[float, float] | None = None,
    *,
    steps: int = 1,
) -> None:
    """执行一个鼠标或滚轮键位。拖动必须给出 to；滚轮转 steps 格。不是鼠标键位时抛 ValueError。"""
    binding = parse_binding(text)
    if binding.kind == "wheel":
        _wheel(widget, binding, at, steps)
        return
    if binding.kind != "mouse" or binding.button is None or binding.gesture is None:
        raise ValueError(f"{text} 不是鼠标键位")
    button = _BUTTONS[binding.button]
    pointer = _Pointer(widget, _modifiers(binding))
    start = pointer.global_point(at)
    none = Qt.MouseButton.NoButton
    match binding.gesture:
        case "Click":
            pointer.send(QEvent.Type.MouseButtonPress, start, button, button)
            pointer.send(QEvent.Type.MouseButtonRelease, start, button, none)
        case "DoubleClick":
            pointer.send(QEvent.Type.MouseButtonPress, start, button, button)
            pointer.send(QEvent.Type.MouseButtonRelease, start, button, none)
            pointer.send(QEvent.Type.MouseButtonDblClick, start, button, button)
            pointer.send(QEvent.Type.MouseButtonRelease, start, button, none)
        case "Drag":
            if to is None:
                raise ValueError("拖动键位需要终点 to")
            pointer.send(QEvent.Type.MouseButtonPress, start, button, button)
            for point in drag_path(at, to):
                pointer.send(QEvent.Type.MouseMove, pointer.global_point(point), none, button)
            pointer.send(QEvent.Type.MouseButtonRelease, pointer.global_point(to), button, none)


def press_and_move(
    widget: QWidget, text: str, at: tuple[float, float], to: tuple[float, float]
) -> tuple[Qt.MouseButton, QPointF]:
    """拖动键位的前半段：按下并移到 to，不松开。返回按着的按键与 to 的全局坐标，交给 release。"""
    binding = parse_binding(text)
    if binding.kind != "mouse" or binding.button is None or binding.gesture != "Drag":
        raise ValueError(f"{text} 不是拖动键位")
    button = _BUTTONS[binding.button]
    pointer = _Pointer(widget, _modifiers(binding))
    pointer.send(QEvent.Type.MouseButtonPress, pointer.global_point(at), button, button)
    for point in drag_path(at, to):
        pointer.send(QEvent.Type.MouseMove, pointer.global_point(point), Qt.MouseButton.NoButton, button)
    return button, pointer.global_point(to)


def release(widget: QWidget, button: Qt.MouseButton, point: QPointF) -> None:
    """松开 press_and_move 按着的按键。point 是全局坐标。"""
    local = point - widget.mapToGlobal(QPointF(0, 0))
    event = QMouseEvent(
        QEvent.Type.MouseButtonRelease,
        local,
        point,
        button,
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    )
    QApplication.sendEvent(widget, event)


def drag_path(at: tuple[float, float], to: tuple[float, float]) -> Sequence[tuple[float, float]]:
    """拖动经过的点：从起点到终点均分 DRAG_STEPS 步，最后一点是终点。"""
    return [
        (at[0] + (to[0] - at[0]) * i / DRAG_STEPS, at[1] + (to[1] - at[1]) * i / DRAG_STEPS)
        for i in range(1, DRAG_STEPS + 1)
    ]


def _wheel(widget: QWidget, binding: Binding, at: tuple[float, float], steps: int) -> None:
    pointer = _Pointer(widget, _modifiers(binding))
    point = pointer.global_point(at)
    local = point - widget.mapToGlobal(QPointF(0, 0))
    event = QWheelEvent(
        local,
        point,
        QPoint(0, 0),
        QPoint(0, 120 * steps),
        Qt.MouseButton.NoButton,
        _modifiers(binding),
        Qt.ScrollPhase.NoScrollPhase,
        False,
    )
    QApplication.sendEvent(widget, event)


def send_binding(
    widget: QWidget,
    text: str,
    at: tuple[float, float] | None = None,
    to: tuple[float, float] | None = None,
    *,
    steps: int = 1,
) -> None:
    """执行任意一个键位：键盘键位不需要坐标，鼠标与滚轮键位在 at 处（缺省为窗口中心）。"""
    binding = parse_binding(text)
    if binding.kind == "key":
        send_key(widget, text)
        return
    point = at if at is not None else (widget.width() / 2, widget.height() / 2)
    send_mouse(widget, text, point, to, steps=steps)


__all__ = [
    "DRAG_STEPS",
    "current_bindings",
    "drag_path",
    "press_and_move",
    "release",
    "send_binding",
    "send_key",
    "send_mouse",
]

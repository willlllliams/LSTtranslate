"""“热键与键位”下三个窗口的键位子页（需求 UI-09，架构文档 11.7、11.8 节）。

截屏蒙版、覆盖窗口、双语窗口各一个子页（``BindingsPage``，页面标识 ``hotkeys.capture``、``hotkeys.overlay``、
``hotkeys.bilingual``），只列出 ``lst.ui.common.bindings.ACTIONS`` 里属于这个窗口（``Scope``）的操作。
每个操作一张卡片（``ActionRow``）。卡片第一行是操作名、当前键位（每个键位一个带删除按钮的小标签）、
“添加”与“恢复默认”两个图标按钮；下面按需要出现三块内容，同一时间一个子页只有一张卡片展开其中一块：

- 录制区（``BindingRecorder``）：点“添加”后出现并获得焦点，按操作类型只接受合适的输入；
- 手动输入：录制区右边的“改为手动输入”打开，用来绑定 Esc，或直接写键位文字；
- 冲突提示：新键位已经分给同一个窗口里的另一个操作时出现，“替换”把它从那个操作上移除再加到这里。
  冲突只在同一个窗口里判断，而同一个窗口的操作都在同一个子页上，所以冲突提示与替换不会跨页。

子页右上角的“恢复本页默认”先确认，再把这个窗口的全部键位恢复成默认值，其他窗口的键位不动。

修改经由 ``BindingsSection`` 提交：先在本地用 ``BindingsConfig`` 校验整节，冲突时给出冲突提示，其他错误显示在
卡片下方；通过后发一条 UpdateConfig，只含改动的字段。提交之后、新配置到达之前，卡片先显示提交的键位；
配置服务拒绝时（ConfigRejected）卡片回到配置里的键位，并在下方显示原因。拒绝的字段属于别的子页时，
窗口按各页的 ``claims`` 切到那个子页（``window.SettingsWindow``）。

配置里存规范写法，界面显示本地化的名称（``binding_display``）：鼠标与滚轮按当前语言命名，键盘组合用
``QKeySequence`` 的 NativeText。
"""

import math
from collections.abc import Mapping, Sequence
from typing import Final, cast

from pydantic import ValidationError
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QFocusEvent,
    QHideEvent,
    QKeyEvent,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QApplication,
    QBoxLayout,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from lst.contracts.worker_protocol import JsonValue
from lst.ui.common.bindings import (
    ACTIONS,
    DRAG_SLOP,
    MAX_BINDINGS,
    ActionKind,
    ActionSpec,
    BindingsConfig,
    Gesture,
    Scope,
    key_binding_text,
    mouse_binding_text,
    parse_binding,
    wheel_binding_text,
)
from lst.ui.i18n import tr
from lst.ui.settings import strings_bindings  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
from lst.ui.settings.cards import CardBuilder
from lst.ui.settings.controls import SettingCard, ThemedLabel
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.nav import nav_name
from lst.ui.settings.pages import SettingsPage
from lst.ui.settings.presenter import Location, SettingsPresenter, Topic
from lst.ui.settings.schema import describe_error
from lst.ui.theme import TOKENS, Glyph, fluent_icon, theme_colors

SECTION: Final = "bindings"
"""键位所在的配置节。"""

_MODIFIER_NAMES: Final = (
    ("Ctrl", Qt.KeyboardModifier.ControlModifier),
    ("Alt", Qt.KeyboardModifier.AltModifier),
    ("Shift", Qt.KeyboardModifier.ShiftModifier),
)

RECORDER_HEIGHT: Final = 56
"""录制区的高度：要容得下单击、双击和一小段拖动。"""


def action_name(spec: ActionSpec) -> str:
    return tr(f"bindings.action.{spec.id}")


def binding_display(text: str) -> str:
    """键位的显示名：鼠标与滚轮按当前语言命名（“Ctrl+左键拖动”“滚轮”），键盘组合用系统的写法。

    写法无效时原样返回，配置里的值总能显示出来。
    """
    try:
        binding = parse_binding(text)
    except ValueError:
        return text
    if binding.kind == "key":
        return QKeySequence(binding.key).toString(QKeySequence.SequenceFormat.NativeText)
    prefix = "".join(f"{name}+" for name, flag in _MODIFIER_NAMES if binding.modifiers & flag.value)
    if binding.kind == "wheel":
        return prefix + tr("settings.bindings.wheel")
    button = tr(f"settings.bindings.button.{binding.button}")
    return prefix + tr(f"settings.bindings.gesture.{binding.gesture}", button=button)


def displays(texts: Sequence[str]) -> str:
    return tr("settings.bindings.separator").join(binding_display(text) for text in texts)


def _strip_reason(reason: str) -> str:
    """配置服务转述的 pydantic 错误带着“Value error, ”前缀，界面上去掉。"""
    return reason.removeprefix("Value error, ")


# ---------------------------------------------------------------- 录制区


class BindingRecorder(QFrame):
    """录制键位的区域。``start`` 进入录制状态并获得焦点，录到合适的输入后发出 ``recorded``。

    按操作类型只接受对应的输入，其余输入忽略、继续等待：

    - 触发：键盘组合键（只按修饰键时继续等待主键）、单击、双击。单击在松开后等一个系统双击间隔，
      期间又按下同一个键就算双击；
    - 拖动：按住鼠标按键移动超过 ``DRAG_SLOP`` 后松开；
    - 滚轮：滚动滚轮。

    鼠标与滚轮带上当时按住的 Ctrl、Alt、Shift。录制中不带修饰键的 Esc 取消录制；失去焦点或被隐藏也取消。
    录制触发类操作时接管全部按键，包括 Tab 与带 Alt 的组合（不触发助记键）；
    录制拖动、滚轮时 Tab 照常移动焦点，焦点离开即取消，键盘用户不会被困住。
    进入和退出录制状态时发出 ``recording_changed``，展示器据此暂停全局热键。
    """

    recorded = Signal(str)
    cancelled = Signal(bool)
    """录制被取消；参数表示是否应当把焦点还给“添加”按钮（失去焦点导致的取消为 False）。"""
    recording_changed = Signal(bool)

    def __init__(self, kind: ActionKind, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self._recording = False
        self._press: tuple[Qt.MouseButton, Qt.KeyboardModifier, QPointF] | None = None
        self._dragged = False
        self._pending_click: tuple[Qt.MouseButton, Qt.KeyboardModifier] | None = None
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.setInterval(QApplication.doubleClickInterval())
        self.click_timer.timeout.connect(self._finish_click)
        self.label = ThemedLabel("secondary", TOKENS.caption_px)
        self.label.setWordWrap(True)
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(TOKENS.spacing_m, TOKENS.spacing_s, TOKENS.spacing_m, TOKENS.spacing_s)
        layout.addWidget(self.label)
        self.setMinimumHeight(RECORDER_HEIGHT)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.retranslate()

    @property
    def recording(self) -> bool:
        return self._recording

    def retranslate(self) -> None:
        text = tr(f"settings.bindings.record.{self.kind.value}")
        self.label.setText(text)
        self.setAccessibleDescription(text)

    def start(self) -> None:
        if self._recording:
            return
        self._recording = True
        self._reset_gesture()
        self.setFocus(Qt.FocusReason.OtherFocusReason)
        self.recording_changed.emit(True)

    def cancel(self, *, restore_focus: bool = True) -> None:
        if self._stop():
            self.cancelled.emit(restore_focus)

    def _stop(self) -> bool:
        if not self._recording:
            return False
        self._recording = False
        self._reset_gesture()
        self.recording_changed.emit(False)
        return True

    def _reset_gesture(self) -> None:
        self._press = None
        self._dragged = False
        self._pending_click = None
        self.click_timer.stop()

    def _emit(self, text: str | None) -> None:
        if text is not None and self._stop():
            self.recorded.emit(text)

    # ------------------------------------------------------------------ 键盘

    def event(self, event: QEvent) -> bool:
        if self._recording:
            if event.type() == QEvent.Type.ShortcutOverride:
                event.accept()  # 录制时的按键不触发窗口里的快捷键和助记键
                return True
            if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
                moves_focus = event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab)
                if self.kind is ActionKind.TRIGGER or not moves_focus:
                    self.keyPressEvent(event)  # 绕过 QWidget 对 Tab 的焦点切换
                    return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._recording:
            super().keyPressEvent(event)
            return
        event.accept()
        if event.isAutoRepeat():
            return
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if event.key() == Qt.Key.Key_Escape and modifiers == Qt.KeyboardModifier.NoModifier:
            self.cancel()
            return
        if self.kind is ActionKind.TRIGGER:
            self._emit(key_binding_text(event))

    # ------------------------------------------------------------------ 鼠标

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if not self._recording:
            super().mousePressEvent(event)
            return
        event.accept()
        pending = self._pending_click
        if pending is not None:
            if pending[0] == event.button():  # 双击间隔内又按下同一个键：双击
                self._gesture(pending[0], pending[1], "DoubleClick")
            else:  # 换了一个键：前一次单击已经确定
                self._finish_click()
            return
        if self._press is None:
            self._press = (event.button(), event.modifiers(), event.position())
            self._dragged = False

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        press = self._press
        if not self._recording or press is None:
            super().mouseMoveEvent(event)
            return
        event.accept()
        origin = press[2]
        position = event.position()
        if math.hypot(position.x() - origin.x(), position.y() - origin.y()) > DRAG_SLOP:
            self._dragged = True

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        press = self._press
        if not self._recording or press is None or event.button() != press[0]:
            super().mouseReleaseEvent(event)
            return
        event.accept()
        button, modifiers, _ = press
        self._press = None
        dragged, self._dragged = self._dragged, False
        if dragged:
            if self.kind is ActionKind.DRAG:
                self._emit(mouse_binding_text(button, modifiers, "Drag"))
        elif self.kind is ActionKind.TRIGGER and mouse_binding_text(button, modifiers, "Click") is not None:
            self._pending_click = (button, modifiers)
            self.click_timer.start()

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:
        if not self._recording:
            super().mouseDoubleClickEvent(event)
            return
        pending = self._pending_click
        if pending is not None and pending[0] == event.button():
            event.accept()
            self._gesture(pending[0], pending[1], "DoubleClick")
            return
        self.mousePressEvent(event)  # 不是双击（例如拖动类操作）：当作一次按下

    def _finish_click(self) -> None:
        pending = self._pending_click
        if pending is not None:
            self._gesture(pending[0], pending[1], "Click")

    def _gesture(self, button: Qt.MouseButton, modifiers: Qt.KeyboardModifier, gesture: Gesture) -> None:
        self._reset_gesture()
        self._emit(mouse_binding_text(button, modifiers, gesture))

    def wheelEvent(self, event: QWheelEvent) -> None:
        if not self._recording:
            super().wheelEvent(event)
            return
        event.accept()  # 录制中滚轮不滚动页面，录制区留在原处
        if self.kind is ActionKind.WHEEL and not event.angleDelta().isNull():
            self._emit(wheel_binding_text(event.modifiers()))

    # ------------------------------------------------------------------ 焦点与绘制

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.cancel(restore_focus=False)
        super().focusOutEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        # 窗口隐藏时不一定先发失去焦点的事件；录制随之结束，全局热键恢复。
        self.cancel(restore_focus=False)
        super().hideEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        colors = theme_colors()
        pen = colors.accent if self.hasFocus() else colors.card_border
        painter.setPen(pen)
        painter.setBrush(colors.card_hover)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.drawRoundedRect(rect, TOKENS.radius_m, TOKENS.radius_m)


class ManualEdit(QLineEdit):
    """手动输入键位的文本框。Esc 只取消输入，不关闭设置窗口。"""

    cancelled = Signal()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            event.accept()
            self.cancelled.emit()
            return
        super().keyPressEvent(event)


# ---------------------------------------------------------------- 键位标签


class BindingChip(QFrame):
    """一个键位：显示名加一个删除按钮。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.text = ""
        self.label = ThemedLabel("primary", TOKENS.caption_px)
        self.remove_button = QToolButton()
        self.remove_button.setIcon(fluent_icon(Glyph.CANCEL))
        self.remove_button.setAutoRaise(True)
        self.remove_button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(TOKENS.spacing_s, 1, 1, 1)
        layout.setSpacing(TOKENS.spacing_xs)
        layout.addWidget(self.label)
        layout.addWidget(self.remove_button)

    def set_binding(self, text: str) -> None:
        self.text = text
        self.retranslate()

    def retranslate(self) -> None:
        shown = binding_display(self.text)
        self.label.setText(shown)
        self.label.setToolTip(self.text)
        name = tr("settings.bindings.remove.name", binding=shown)
        self.remove_button.setAccessibleName(name)
        self.remove_button.setToolTip(name)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        colors = theme_colors()
        painter.setPen(colors.card_border)
        painter.setBrush(colors.card_hover)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.drawRoundedRect(rect, TOKENS.radius_s, TOKENS.radius_s)


# ---------------------------------------------------------------- 一个操作一张卡片


def _icon_button(glyph: str) -> QToolButton:
    button = QToolButton()
    button.setIcon(fluent_icon(glyph))
    button.setAutoRaise(True)
    button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
    return button


def _row(*widgets: QWidget, stretch_first: bool = True) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(TOKENS.spacing_s)
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if index == 0 and stretch_first else 0)
    return container


class RowControls(QWidget):
    """卡片第一行右边的控件：键位标签、“未绑定”、“添加”与“恢复默认”。

    控件按从左到右的顺序创建，Tab 顺序与之一致。
    """

    def __init__(self) -> None:
        super().__init__()
        top = QHBoxLayout(self)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(TOKENS.spacing_xs)
        self.empty_label = ThemedLabel("secondary", TOKENS.caption_px)
        top.addWidget(self.empty_label)
        self.chips: list[BindingChip] = []
        for _ in range(MAX_BINDINGS):
            chip = BindingChip()
            top.addWidget(chip)
            self.chips.append(chip)
        top.addSpacing(TOKENS.spacing_s)
        self.add_button = _icon_button(Glyph.ADD)
        self.reset_button = _icon_button(Glyph.REFRESH)
        top.addWidget(self.add_button)
        top.addWidget(self.reset_button)


class ActionRow(SettingCard):
    """一个操作的卡片。键位的增删经由 ``BindingsSection`` 校验并提交。"""

    def __init__(self, spec: ActionSpec, section: "BindingsSection") -> None:
        controls = RowControls()
        super().__init__(controls, location=(SECTION, spec.field))
        self.spec = spec
        self._section = section
        self.pending: tuple[str, ...] | None = None
        """已经提交、配置里还没有出现的键位；None 表示显示配置里的键位。"""
        self._sent_from: tuple[str, ...] = ()
        self._local_error = ""
        self._conflict: tuple[tuple[str, ActionSpec], ...] = ()
        self._proposed: tuple[str, ...] = ()
        self.controls = controls
        self.empty_label = controls.empty_label
        self.chips = controls.chips
        self.add_button = controls.add_button
        self.reset_button = controls.reset_button
        for chip in self.chips:
            chip.remove_button.clicked.connect(lambda _=False, c=chip: self._remove(c.text))

        self.recorder = BindingRecorder(spec.kind)
        self.manual_button = QPushButton()
        self.manual_button.setFlat(True)
        # 不接受单击焦点：点它时录制区不失去焦点，录制状态由按钮自己结束。
        self.manual_button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.recorder_row = _row(self.recorder, self.manual_button)

        self.manual_edit = ManualEdit()
        self.manual_ok = QPushButton()
        self.manual_cancel = QPushButton()
        self.manual_row = _row(self.manual_edit, self.manual_ok, self.manual_cancel)

        self.conflict_label = ThemedLabel("critical", TOKENS.caption_px)
        self.conflict_label.setWordWrap(True)
        self.replace_button = QPushButton()
        self.conflict_cancel = QPushButton()
        self.conflict_row = _row(self.conflict_label, self.replace_button, self.conflict_cancel)

        root = self.layout()
        if isinstance(root, QBoxLayout):
            for index, row in enumerate((self.recorder_row, self.manual_row, self.conflict_row), start=1):
                root.insertWidget(index, row)
        for row in (self.recorder_row, self.manual_row, self.conflict_row):
            row.hide()

        self.add_button.clicked.connect(self.start_recording)
        self.reset_button.clicked.connect(self._reset)
        self.recorder.recorded.connect(self._recorded)
        self.recorder.cancelled.connect(self._recording_cancelled)
        self.manual_button.clicked.connect(self.open_manual)
        self.manual_ok.clicked.connect(self._manual_confirmed)
        self.manual_edit.returnPressed.connect(self._manual_confirmed)
        self.manual_edit.cancelled.connect(self._close_manual)
        self.manual_cancel.clicked.connect(self._close_manual)
        self.replace_button.clicked.connect(self._replace)
        self.conflict_cancel.clicked.connect(self._close_conflict)

    # ------------------------------------------------------------------ 显示

    def values(self) -> tuple[str, ...]:
        """卡片上显示的键位：提交中的，或配置里的。"""
        return self.pending if self.pending is not None else self._section.config_value(self.spec)

    def retranslate(self) -> None:
        name = action_name(self.spec)
        self.set_texts(name, "")
        self.empty_label.setText(tr("settings.bindings.none"))
        self.add_button.setToolTip(tr("settings.bindings.add.tip"))
        self.add_button.setAccessibleName(tr("settings.bindings.add.name", action=name))
        self.reset_button.setToolTip(tr("settings.bindings.reset.tip", defaults=displays(self.spec.defaults)))
        self.reset_button.setAccessibleName(tr("settings.bindings.reset.name", action=name))
        self.recorder.retranslate()
        self.recorder.setAccessibleName(tr("settings.bindings.record.name", action=name))
        self.manual_button.setText(tr("settings.bindings.manual"))
        self.manual_button.setToolTip(tr("settings.bindings.manual.tip"))
        self.manual_edit.setPlaceholderText(tr("settings.bindings.manual.placeholder"))
        self.manual_edit.setAccessibleName(tr("settings.bindings.manual.name", action=name))
        self.manual_edit.setToolTip(tr("settings.bindings.manual.tip"))
        self.manual_ok.setText(tr("settings.bindings.manual.ok"))
        self.manual_cancel.setText(tr("settings.bindings.cancel"))
        self.replace_button.setText(tr("settings.bindings.replace"))
        self.replace_button.setToolTip(tr("settings.bindings.conflict.hint"))
        self.conflict_cancel.setText(tr("settings.bindings.cancel"))
        self._show_conflict_text()
        self.refresh_values()

    def refresh_values(self) -> None:
        config = self._section.config_value(self.spec)
        if self.pending is not None and (config == self.pending or config != self._sent_from):
            self.pending = None  # 新配置到了，或者配置被别处改过
        values = self.values()
        for chip, text in zip(self.chips, (*values, *[""] * MAX_BINDINGS), strict=False):
            chip.set_binding(text)
            chip.setVisible(bool(text))
        self.empty_label.setVisible(not values)
        self.add_button.setEnabled(len(values) < MAX_BINDINGS)
        self.reset_button.setEnabled(values != self.spec.defaults)

    def refresh_error(self) -> None:
        rejected = self._section.presenter.rejection_for(self.location)
        if rejected is not None and self.pending is not None:
            self.pending = None  # 被拒绝：回到配置里的键位
            self.refresh_values()
        reason = self._local_error or (_strip_reason(rejected) if rejected is not None else None)
        self.set_error(reason or None)

    def set_local_error(self, text: str) -> None:
        self._local_error = text
        self.refresh_error()

    def set_pending(self, values: tuple[str, ...]) -> None:
        self._sent_from = self._section.config_value(self.spec)
        self.pending = values
        self.refresh_values()

    # ------------------------------------------------------------------ 交互状态

    @property
    def busy(self) -> bool:
        """录制、手动输入或冲突提示中的一种正在显示。"""
        return self.recorder.recording or not self.manual_row.isHidden() or not self.conflict_row.isHidden()

    def close_interaction(self) -> None:
        """收起录制区、手动输入与冲突提示（另一张卡片开始操作时调用）。"""
        self.recorder.cancel(restore_focus=False)
        self.recorder_row.hide()
        self.manual_row.hide()
        self._close_conflict(restore_focus=False)

    def _begin(self) -> None:
        self._section.begin(self)
        self._local_error = ""
        self._section.presenter.clear_rejection(self.location)
        self.refresh_error()

    def start_recording(self) -> None:
        self._begin()
        self.recorder_row.show()
        self.recorder.start()

    def _recording_cancelled(self, restore_focus: bool) -> None:
        self.recorder_row.hide()
        if restore_focus:
            self.add_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _recorded(self, text: str) -> None:
        self.recorder_row.hide()
        self.add_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self.add_binding(text)

    def open_manual(self) -> None:
        self.recorder.cancel(restore_focus=False)
        self._begin()
        self.manual_edit.clear()
        self.manual_row.show()
        self.manual_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    def _close_manual(self) -> None:
        self.manual_row.hide()
        self.add_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _manual_confirmed(self) -> None:
        text = self.manual_edit.text()
        try:
            binding = parse_binding(text)
        except ValueError as exc:
            self.set_local_error(str(exc))
            return
        if not binding.accepts(self.spec.kind):
            self.set_local_error(tr(f"bindings.invalid.kind.{self.spec.kind.value}", text=binding.text))
            return
        self.manual_row.hide()
        self.add_button.setFocus(Qt.FocusReason.OtherFocusReason)
        self.add_binding(binding.text)

    # ------------------------------------------------------------------ 修改

    def add_binding(self, text: str) -> None:
        """加一个键位（录制或手动输入的结果）。"""
        try:
            binding = parse_binding(text)
        except ValueError as exc:
            self.set_local_error(str(exc))
            return
        values = self.values()
        if binding.text in values:
            self.set_local_error(tr("settings.bindings.exists", binding=binding_display(binding.text)))
            return
        if len(values) >= MAX_BINDINGS:
            self.set_local_error(tr("bindings.invalid.too_many", count=MAX_BINDINGS))
            return
        self._section.propose(self, (*values, binding.text))

    def _remove(self, text: str) -> None:
        self._begin()
        self._section.propose(self, tuple(value for value in self.values() if value != text))

    def _reset(self) -> None:
        self._begin()
        self._section.propose(self, self.spec.defaults)

    def show_conflict(self, proposed: tuple[str, ...], conflicts: Sequence[tuple[str, ActionSpec]]) -> None:
        self._proposed = proposed
        self._conflict = tuple(conflicts)
        self._show_conflict_text()
        self.conflict_row.show()
        self.replace_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _show_conflict_text(self) -> None:
        lines = [
            tr("settings.bindings.conflict", action=action_name(other), binding=binding_display(text))
            for text, other in self._conflict
        ]
        self.conflict_label.setText("\n".join(lines))

    def _replace(self) -> None:
        proposed, conflicts = self._proposed, self._conflict
        self._close_conflict()
        self._section.replace(self, proposed, conflicts)

    def _close_conflict(self, *, restore_focus: bool = True) -> None:
        if self.conflict_row.isHidden():
            return
        self.conflict_row.hide()
        self._conflict = ()
        self._proposed = ()
        if restore_focus:
            self.add_button.setFocus(Qt.FocusReason.OtherFocusReason)


# ---------------------------------------------------------------- 整节


class BindingsSection:
    """一个窗口（scope）全部操作的卡片与提交逻辑。页面负责把卡片放进布局。"""

    def __init__(self, presenter: SettingsPresenter, dialogs: Dialogs, scope: Scope) -> None:
        self.presenter = presenter
        self.scope = scope
        self._dialogs = dialogs
        self.specs = tuple(spec for spec in ACTIONS if spec.scope is scope)
        self.available = all(presenter.spec(SECTION, (spec.field,)) is not None for spec in self.specs)
        """组合根注入了 [bindings] 节；没有时页面只显示说明。"""
        self.rows: dict[str, ActionRow] = {}
        self.reset_button = QPushButton()
        self.reset_button.setIcon(fluent_icon(Glyph.REFRESH))
        self.reset_button.clicked.connect(self.reset_page)
        if not self.available:
            self.reset_button.hide()
            return
        for spec in self.specs:
            row = ActionRow(spec, self)
            row.recorder.recording_changed.connect(presenter.set_hotkey_recording)
            self.rows[spec.id] = row

    def row(self, action: str) -> ActionRow:
        return self.rows[action]

    # ------------------------------------------------------------------ 取值

    def config_value(self, spec: ActionSpec) -> tuple[str, ...]:
        value = self.presenter.value(SECTION, (spec.field,))
        if isinstance(value, list):
            return tuple(str(item) for item in value)
        return spec.defaults

    def current(self, spec: ActionSpec) -> tuple[str, ...]:
        """卡片上显示的键位。其他窗口的操作不在这一节里，取配置里的值。"""
        row = self.rows.get(spec.id)
        return row.values() if row is not None else self.config_value(spec)

    # ------------------------------------------------------------------ 刷新

    def retranslate(self) -> None:
        self.reset_button.setText(tr("settings.bindings.reset_page"))
        for row in self.rows.values():
            row.retranslate()

    def refresh(self) -> None:
        for row in self.rows.values():
            row.refresh_values()
        self.reset_button.setEnabled(
            self.available and any(self.current(spec) != spec.defaults for spec in self.specs)
        )

    def refresh_errors(self) -> None:
        for row in self.rows.values():
            row.refresh_error()
        self.refresh()

    def claims(self, location: Location) -> bool:
        return any(location[: len(row.location)] == row.location for row in self.rows.values())

    # ------------------------------------------------------------------ 修改

    def begin(self, active: ActionRow) -> None:
        """一张卡片开始录制、手动输入或删改：其他卡片收起它们的操作，一个子页同一时间只有一处在操作。"""
        for row in self.rows.values():
            if row is not active and row.busy:
                row.close_interaction()

    def conflicts(self, spec: ActionSpec, values: Sequence[str]) -> list[tuple[str, ActionSpec]]:
        """values 里已经分给同一个窗口里其他操作的键位：(键位, 那个操作)。"""
        found: list[tuple[str, ActionSpec]] = []
        for text in values:
            for other in self.specs:
                if other is not spec and text in self.current(other):
                    found.append((text, other))
        return found

    def propose(self, row: ActionRow, values: tuple[str, ...]) -> None:
        """把 row 的键位改成 values：与同窗口的其他操作冲突时给出冲突提示，否则校验并提交。"""
        conflicts = self.conflicts(row.spec, values)
        if conflicts:
            row.show_conflict(values, conflicts)
            return
        self.commit({row.spec: values}, row)

    def replace(
        self, row: ActionRow, values: tuple[str, ...], conflicts: Sequence[tuple[str, ActionSpec]]
    ) -> None:
        """冲突时的“替换”：从其他操作上移除冲突的键位，与 row 的修改一起提交。"""
        changes: dict[ActionSpec, tuple[str, ...]] = {}
        for text, other in conflicts:
            current = changes.get(other, self.current(other))
            changes[other] = tuple(value for value in current if value != text)
        changes[row.spec] = values  # 被移除键位的操作先写，配置服务按这个顺序逐项尝试时也不会冲突
        self.commit(changes, row)

    def commit(self, changes: Mapping[ActionSpec, tuple[str, ...]], row: ActionRow | None = None) -> bool:
        """用 BindingsConfig 校验修改后的整节，通过就在一条 UpdateConfig 里提交改动的字段。

        校验失败时原因显示在 row 下方（没有 row 时显示在第一个改动的操作下方），不提交。
        """
        candidate = {spec.field: list(self.current(spec)) for spec in ACTIONS}
        for spec, values in changes.items():
            candidate[spec.field] = list(values)
        try:
            BindingsConfig.model_validate(candidate)
        except ValidationError as exc:
            target = row or self.rows[next(iter(changes)).id]
            target.set_local_error(describe_error(exc.errors(include_url=False)[0]))
            return False
        for spec, values in changes.items():
            self.rows[spec.id].set_pending(values)
        patch = {spec.field: cast(JsonValue, list(values)) for spec, values in changes.items()}
        self.presenter.set_values(SECTION, patch)
        self.refresh()
        return True

    def reset_page(self) -> None:
        """把这个窗口的全部键位恢复默认（先确认）。只提交与默认值不同的字段，其他窗口的键位不动。

        同一个窗口的默认键位互不冲突，恢复后的整节一定能通过校验。
        """
        changes = {spec: spec.defaults for spec in self.specs if self.current(spec) != spec.defaults}
        if not changes:
            return
        question = tr("settings.bindings.reset_page.confirm", window=nav_name(page_id(self.scope)))
        if not self._dialogs.confirm(tr("settings.confirm.title"), question):
            return
        for row in self.rows.values():
            row.close_interaction()
            row.set_local_error("")
        self.commit(changes)


def page_id(scope: Scope) -> str:
    """一个窗口的键位子页的页面标识，例如 hotkeys.overlay。"""
    return f"hotkeys.{scope.value}"


class BindingsPage(SettingsPage):
    """热键与键位 › 某个窗口：这个窗口的全部操作，每个操作一张卡片（需求 UI-09）。

    最上面是窗口的说明，下面一行是键位规则与“恢复本页默认”按钮，再往下是操作卡片。组合根没有注入
    [bindings] 节时只显示窗口的说明与“键位不可用”。
    """

    icon = Glyph.KEYBOARD

    def __init__(
        self, scope: Scope, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs
    ) -> None:
        super().__init__(presenter, builder, dialogs)
        self.page_id = page_id(scope)
        self.scope = scope
        self.add_intro(f"settings.bindings.scope.{scope.value}.intro")
        self.keymap = BindingsSection(presenter, dialogs, scope)
        self.rules_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.rules_label.setWordWrap(True)
        header = QWidget()
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOKENS.spacing_l)
        row.addWidget(self.rules_label, 1)
        row.addWidget(self.keymap.reset_button, 0, Qt.AlignmentFlag.AlignTop)
        self.body.addWidget(header)
        self.body.addSpacing(TOKENS.spacing_s)
        for action_row in self.keymap.rows.values():
            self.add_card(action_row)

    def retranslate(self) -> None:
        super().retranslate()
        key = "settings.bindings.intro" if self.keymap.available else "settings.bindings.unavailable"
        self.rules_label.setText(tr(key, count=MAX_BINDINGS))
        self.keymap.retranslate()

    def refresh_all(self) -> None:
        super().refresh_all()
        self.keymap.refresh_errors()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        match topic:
            case Topic.CONFIG:
                self.keymap.refresh()
            case Topic.REJECTIONS:
                self.keymap.refresh_errors()
            case _:
                pass

    def claims(self, location: Location) -> bool:
        return super().claims(location) or self.keymap.claims(location)

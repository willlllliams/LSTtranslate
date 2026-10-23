"""设置窗口的控件：各种取值的编辑器与“设置卡片”（需求 UI-01、UI-06、UI-07，架构文档 11.7 节）。

编辑器统一的约定（``ValueEditor``）：

- ``json_value``：当前取值，已经是配置里的规范 JSON 值；``set_json``：把配置里的值显示出来，不触发提交；
- ``edited``：用户修改了控件，每次修改都发出，展示器据此让这个设置项旁边的错误提示消失；
- ``committed``：该提交了。开关、下拉框在操作完成时立即发出；文本框与数值框在停止输入
  ``debounce_ms`` 毫秒后发出，按 Enter 或离开控件时立即发出（需求 UI-06，架构文档 11.7 节）；
  ``finish_only`` 的编辑器（API key 输入框）不按停顿提交，只在按 Enter、离开控件或窗口隐藏时提交，
  输入中途的停顿不会把半截内容提交出去；
- ``flush``：窗口隐藏时调用，把还没发出的修改立即提交，不丢用户的输入。

用户修改过、配置里的值还没有变成这个值时，控件保持用户输入的样子，不被别的设置项触发的配置刷新覆盖：
被配置服务拒绝的输入因此会留在控件里，旁边显示原因，用户可以直接改正。

所有控件都可以只用键盘操作：开关用空格，下拉框用方向键，热键用 Enter 或空格进入录入。
"""

from collections.abc import Sequence
from typing import Final, cast

from PySide6.QtCore import QEvent, QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QDoubleValidator,
    QEnterEvent,
    QFocusEvent,
    QGuiApplication,
    QHideEvent,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from lst.contracts.worker_protocol import JsonValue
from lst.ui.common.text import LANGUAGES, language_name
from lst.ui.i18n import tr
from lst.ui.settings.dialogs import Dialogs
from lst.ui.theme import TOKENS, Glyph, fluent_icon, fluent_icon_font, fluent_icon_text, theme_colors, ui_font

DEFAULT_DEBOUNCE_MS: Final = 500
"""文本框停止输入后提交的等待时间（需求 UI-06）。"""

EDITOR_WIDTH: Final = 280
"""文字类编辑器的宽度（逻辑像素）。"""


def critical_color() -> QColor:
    """错误提示的颜色，深色主题下用更亮的红色。取自 ui.theme。"""
    return theme_colors().critical


def accent_color() -> QColor:
    """强调色，跟随系统。取自 ui.theme。"""
    return theme_colors().accent


def make_transparent(view: QAbstractItemView) -> None:
    """列表、表格的背景改成透明，露出卡片的背景；条目的绘制仍由系统样式完成（复选框等照常显示）。"""
    palette = view.palette()
    palette.setColor(QPalette.ColorRole.Base, Qt.GlobalColor.transparent)
    view.setPalette(palette)
    view.viewport().setAutoFillBackground(False)


class ThemedLabel(QLabel):
    """文字颜色和字号取自 ui.theme 的标签。

    role 取 primary（正文色）、secondary（次要色，说明文字）、critical（错误色）或 accent（用作文字的强调色，
    例如“当前引擎”标识）。
    深浅色或强调色变化时（调色板变化事件）重新取色，页面不需要自己刷新。
    """

    def __init__(
        self,
        role: str = "primary",
        pixel_size: int | None = None,
        *,
        semibold: bool = False,
        display: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._role = role
        self._applying = False
        if pixel_size is not None:
            self.setFont(ui_font(pixel_size, semibold=semibold, display=display))
        self._apply_color()
        # 显式设置过调色板的子标签不会总是收到应用调色板变化事件。
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._scheme_changed)

    def set_role(self, role: str) -> None:
        """换成另一种文字色（例如提示变成错误）。"""
        if role != self._role:
            self._role = role
            self._apply_color()

    def _scheme_changed(self, scheme: Qt.ColorScheme) -> None:
        # 信号发出时系统调色板还在切换，按信号的新值立即更新已有标签。
        self._apply_color(dark=scheme == Qt.ColorScheme.Dark)

    def _apply_color(self, *, dark: bool | None = None) -> None:
        colors = theme_colors(dark=dark)
        color = {
            "secondary": colors.text_secondary,
            "critical": colors.critical,
            "accent": colors.accent_strong,
        }.get(self._role, colors.text)
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.WindowText, color)
        # 不可用的卡片（例如当前引擎不支持的设置项）文字变淡，与系统控件的禁用样式一致。
        faded = QColor(color)
        faded.setAlphaF(color.alphaF() * 0.45)
        palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, faded)
        self._applying = True
        try:
            self.setPalette(palette)
        finally:
            self._applying = False

    def changeEvent(self, event: QEvent) -> None:
        if not self._applying and event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.ThemeChange,
        ):
            self._apply_color()
        super().changeEvent(event)


# ---------------------------------------------------------------- 编辑器基类


class ValueEditor(QWidget):
    """取值编辑器的基类。子类实现 ``json_value`` 与 ``_write``，用户修改时调用 ``_user_changed``。"""

    committed = Signal(object)
    edited = Signal()

    def __init__(
        self, *, debounce_ms: int | None = None, finish_only: bool = False, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._debounce_ms = debounce_ms
        self._finish_only = finish_only
        self._held = False
        self._dirty = False
        self._loading = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._commit)

    # ---- 子类实现

    def json_value(self) -> JsonValue:
        raise NotImplementedError

    def _write(self, value: JsonValue) -> None:
        raise NotImplementedError

    def focus_widget(self) -> QWidget:
        """键盘焦点落在哪个控件上（用来设置可访问名称）。默认是编辑器自己。"""
        return self

    # ---- 公共接口

    @property
    def dirty(self) -> bool:
        """用户修改过，配置里的值还没有变成这个值。"""
        return self._dirty

    @property
    def pending(self) -> bool:
        """有一次提交还没有发出（防抖计时中，或 finish_only 的编辑器等待编辑结束）。"""
        return self._timer.isActive() or self._held

    def set_json(self, value: JsonValue) -> None:
        """显示配置里的值。用户修改过且配置里还不是这个值时保持用户的输入。"""
        if self._dirty:
            if value == self.json_value():
                self._dirty = False
            return
        self._load(value)

    def reload(self, value: JsonValue) -> None:
        """无条件显示这个值，丢弃用户的未提交输入。"""
        self._timer.stop()
        self._held = False
        self._dirty = False
        self._load(value)

    def flush(self) -> None:
        """还有没发出的提交时立即发出。"""
        if self.pending:
            self._commit()

    # ---- 内部

    def _load(self, value: JsonValue) -> None:
        self._loading = True
        try:
            self._write(value)
        finally:
            self._loading = False

    def _user_changed(self) -> None:
        if self._loading:
            return
        self._dirty = True
        self.edited.emit()
        if self._finish_only:
            self._held = True  # 只在编辑结束（Enter、离开控件、窗口隐藏）时由 flush 提交
        elif self._debounce_ms is None:
            self._commit()
        else:
            self._timer.start(self._debounce_ms)

    def _commit(self) -> None:
        self._timer.stop()
        self._held = False
        self.committed.emit(self.json_value())


# ---------------------------------------------------------------- 开关


class ToggleSwitch(QAbstractButton):
    """Fluent 风格的开关：圆角轨道加圆形滑块，开启时轨道用系统强调色。空格键切换，有焦点框。"""

    _WIDTH: Final = 40
    _HEIGHT: Final = 20

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def sizeHint(self) -> QSize:
        return QSize(self._WIDTH + 6, self._HEIGHT + 6)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        colors = theme_colors()
        enabled = self.isEnabled()
        track = QRectF(
            (self.width() - self._WIDTH) / 2, (self.height() - self._HEIGHT) / 2, self._WIDTH, self._HEIGHT
        )
        radius = self._HEIGHT / 2
        text = colors.text
        if self.isChecked():
            fill = QColor(colors.accent)
            fill.setAlphaF(1.0 if enabled else 0.4)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(track, radius, radius)
            knob_color = theme_colors().accent_text
        else:
            border = QColor(text)
            border.setAlphaF(0.6 if enabled else 0.25)
            painter.setPen(border)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), radius - 0.5, radius - 0.5)
            knob_color = QColor(text)
            knob_color.setAlphaF(0.75 if enabled else 0.3)
        diameter = 12.0 if not self.isDown() else 14.0
        gap = (self._HEIGHT - 12.0) / 2
        x = track.right() - gap - 12.0 if self.isChecked() else track.left() + gap
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(knob_color)
        painter.drawEllipse(QPointF(x + 6.0, track.center().y()), diameter / 2, diameter / 2)
        if self.hasFocus():
            focus = QPainterPath()
            focus.addRoundedRect(track.adjusted(-3, -3, 3, 3), radius + 3, radius + 3)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(colors.accent)
            painter.drawPath(focus)


class SwitchEditor(ValueEditor):
    """布尔值：开关加“开”“关”文字。开关在操作完成时立即提交。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(debounce_ms=None, parent=parent)
        self.switch = ToggleSwitch()
        self.state_label = ThemedLabel("primary", TOKENS.title_px)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOKENS.spacing_s)
        layout.addWidget(self.state_label)
        layout.addWidget(self.switch)
        self.setFocusProxy(self.switch)
        self.switch.clicked.connect(self._user_changed)
        self.retranslate()

    def focus_widget(self) -> QWidget:
        return self.switch

    def retranslate(self) -> None:
        self.state_label.setText(
            tr("settings.switch.on" if self.switch.isChecked() else "settings.switch.off")
        )

    def json_value(self) -> JsonValue:
        return self.switch.isChecked()

    def _write(self, value: JsonValue) -> None:
        self.switch.setChecked(value is True)
        self.retranslate()

    def _user_changed(self) -> None:
        self.retranslate()
        super()._user_changed()


# ---------------------------------------------------------------- 下拉框


class ChoiceEditor(ValueEditor):
    """从几个取值里选一个：下拉框，选定时立即提交。options 是 (取值, 显示名)。"""

    def __init__(self, options: Sequence[tuple[str, str]], parent: QWidget | None = None) -> None:
        super().__init__(debounce_ms=None, parent=parent)
        self.combo = QComboBox()
        self.combo.setMinimumWidth(EDITOR_WIDTH // 2)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.combo)
        self.setFocusProxy(self.combo)
        self.set_options(options)
        self.combo.currentIndexChanged.connect(self._user_changed)

    def focus_widget(self) -> QWidget:
        return self.combo

    def set_options(self, options: Sequence[tuple[str, str]]) -> None:
        """换一组可选项或显示名（界面语言切换时），保持当前选中的取值。"""
        current = self.combo.currentData() if self.combo.count() else None
        self._loading = True
        try:
            self.combo.clear()
            for value, label in options:
                self.combo.addItem(label, value)
            index = self.combo.findData(current) if current is not None else -1
            self.combo.setCurrentIndex(max(0, index))
        finally:
            self._loading = False

    def json_value(self) -> JsonValue:
        data = self.combo.currentData()
        return data if isinstance(data, str) else None

    def _write(self, value: JsonValue) -> None:
        index = self.combo.findData(value)
        if index < 0 and isinstance(value, str):
            # 配置里的取值不在可选项里（例如列表里没有的语言）：照实显示，不改动它。
            self.combo.addItem(value, value)
            index = self.combo.count() - 1
        self.combo.setCurrentIndex(index)


class PairEditor(ValueEditor):
    """一对语言（互译对）：两个下拉框，任何一个改变都提交整对。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(debounce_ms=None, parent=parent)
        self.first = QComboBox()
        self.second = QComboBox()
        self.arrow = QLabel()
        self.arrow.setFont(fluent_icon_font(14))
        self.arrow.setText(fluent_icon_text(Glyph.SWITCH))
        for combo in (self.first, self.second):
            combo.setMinimumWidth(EDITOR_WIDTH // 2 - 20)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOKENS.spacing_s)
        layout.addWidget(self.first)
        layout.addWidget(self.arrow)
        layout.addWidget(self.second)
        self.setFocusProxy(self.first)
        self.retranslate()
        self.first.currentIndexChanged.connect(self._user_changed)
        self.second.currentIndexChanged.connect(self._user_changed)

    def focus_widget(self) -> QWidget:
        return self.first

    def retranslate(self) -> None:
        self.second.setAccessibleName(tr("settings.pair.second"))
        for combo in (self.first, self.second):
            current = combo.currentData() if combo.count() else None
            self._loading = True
            try:
                combo.clear()
                for code in LANGUAGES:
                    combo.addItem(language_name(code), code)
                combo.setCurrentIndex(max(0, combo.findData(current)) if current is not None else 0)
            finally:
                self._loading = False

    def json_value(self) -> JsonValue:
        return [str(self.first.currentData()), str(self.second.currentData())]

    def _write(self, value: JsonValue) -> None:
        pair = value if isinstance(value, list) and len(value) == 2 else ["zh-Hans", "en"]
        for combo, code in zip((self.first, self.second), pair, strict=True):
            index = combo.findData(code)
            if index < 0 and isinstance(code, str):
                combo.addItem(code, code)
                index = combo.count() - 1
            combo.setCurrentIndex(index)


# ---------------------------------------------------------------- 数值与文字


class IntEditor(ValueEditor):
    """整数：数值框，停止输入后提交；special_zero 非空时 0 显示为这段文字（例如“不缩放”）。"""

    def __init__(
        self,
        minimum: int,
        maximum: int,
        *,
        suffix: str = "",
        special_zero: str = "",
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(debounce_ms=debounce_ms, parent=parent)
        self.spin = QSpinBox()
        self.spin.setRange(minimum, maximum)
        self.spin.setMinimumWidth(EDITOR_WIDTH // 2)
        self.spin.setAccelerated(True)
        self.set_texts(suffix, special_zero)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.spin)
        self.setFocusProxy(self.spin)
        self.spin.valueChanged.connect(self._user_changed)
        self.spin.editingFinished.connect(self.flush)

    def set_texts(self, suffix: str, special_zero: str) -> None:
        self.spin.setSuffix(suffix)
        self.spin.setSpecialValueText(special_zero if self.spin.minimum() == 0 else "")

    def focus_widget(self) -> QWidget:
        return self.spin

    def json_value(self) -> JsonValue:
        return self.spin.value()

    def _write(self, value: JsonValue) -> None:
        number = value if isinstance(value, int) and not isinstance(value, bool) else self.spin.minimum()
        self.spin.setValue(number)


class FloatEditor(ValueEditor):
    """小数：数值框，停止输入后提交。"""

    def __init__(
        self,
        minimum: float,
        maximum: float,
        *,
        decimals: int = 2,
        step: float = 0.1,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(debounce_ms=debounce_ms, parent=parent)
        self.spin = QDoubleSpinBox()
        self.spin.setDecimals(decimals)
        self.spin.setRange(minimum, maximum)
        self.spin.setSingleStep(step)
        self.spin.setMinimumWidth(EDITOR_WIDTH // 2)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.spin)
        self.setFocusProxy(self.spin)
        self.spin.valueChanged.connect(self._user_changed)
        self.spin.editingFinished.connect(self.flush)

    def focus_widget(self) -> QWidget:
        return self.spin

    def json_value(self) -> JsonValue:
        return round(self.spin.value(), self.spin.decimals())

    def _write(self, value: JsonValue) -> None:
        number = (
            value if isinstance(value, int | float) and not isinstance(value, bool) else self.spin.minimum()
        )
        self.spin.setValue(float(number))


class TextEditor(ValueEditor):
    """文字：单行文本框，停止输入后提交，按 Enter 或离开控件时立即提交。

    - optional：空文本表示 None（配置里没有这个值）；
    - as_list：用逗号分隔的列表，空文本表示 None；
    - number：可选的小数，空文本表示 None；
    - password：输入时显示圆点（API key）；
    - finish_only：不按停顿提交，只在按 Enter、离开控件或 flush 时提交（API key 输入框）。
    """

    def __init__(
        self,
        *,
        optional: bool = False,
        as_list: bool = False,
        number: bool = False,
        password: bool = False,
        finish_only: bool = False,
        placeholder: str = "",
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(debounce_ms=debounce_ms, finish_only=finish_only, parent=parent)
        self._optional = optional or as_list or number
        self._as_list = as_list
        self._number = number
        self.edit = QLineEdit()
        self.edit.setMinimumWidth(EDITOR_WIDTH)
        self.edit.setPlaceholderText(placeholder)
        if password:
            self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        if number:
            validator = QDoubleValidator(0.0, 100000.0, 3, self.edit)
            validator.setNotation(QDoubleValidator.Notation.StandardNotation)
            self.edit.setValidator(validator)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)
        self.setFocusProxy(self.edit)
        self.edit.textEdited.connect(self._user_changed)
        self.edit.editingFinished.connect(self.flush)

    def focus_widget(self) -> QWidget:
        return self.edit

    def json_value(self) -> JsonValue:
        text = self.edit.text()
        if self._as_list:
            items = [part.strip() for part in text.split(",") if part.strip()]
            return cast(JsonValue, items) if items else None
        if self._number:
            try:
                return float(text) if text.strip() else None
            except ValueError:
                return None
        if self._optional and not text.strip():
            return None
        return text

    def _write(self, value: JsonValue) -> None:
        if value is None:
            text = ""
        elif isinstance(value, list):
            text = ", ".join(str(item) for item in value)
        elif isinstance(value, float) and value.is_integer():
            text = str(int(value))
        else:
            text = str(value)
        self.edit.setText(text)


class TextAreaEditor(ValueEditor):
    """多行文字（提示词）：停止输入后提交，Tab 键移动焦点而不是插入制表符。空文本表示 None。"""

    def __init__(self, *, debounce_ms: int = DEFAULT_DEBOUNCE_MS, parent: QWidget | None = None) -> None:
        super().__init__(debounce_ms=debounce_ms, parent=parent)
        self.edit = QPlainTextEdit()
        self.edit.setTabChangesFocus(True)
        self.edit.setMinimumWidth(EDITOR_WIDTH)
        self.edit.setFixedHeight(96)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit)
        self.setFocusProxy(self.edit)
        self.edit.textChanged.connect(self._user_changed)

    def focus_widget(self) -> QWidget:
        return self.edit

    def json_value(self) -> JsonValue:
        text = self.edit.toPlainText()
        return text if text.strip() else None

    def _write(self, value: JsonValue) -> None:
        self.edit.setPlainText(value if isinstance(value, str) else "")


class DirectoryEditor(ValueEditor):
    """目录：文本框加“浏览”按钮。留空表示默认位置。选好目录后立即提交。"""

    def __init__(
        self, dialogs: Dialogs, *, debounce_ms: int = DEFAULT_DEBOUNCE_MS, parent: QWidget | None = None
    ) -> None:
        super().__init__(debounce_ms=debounce_ms, parent=parent)
        self._dialogs = dialogs
        self.edit = QLineEdit()
        self.edit.setMinimumWidth(EDITOR_WIDTH)
        self.browse_button = QPushButton()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOKENS.spacing_s)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.browse_button)
        self.setFocusProxy(self.edit)
        self.edit.textEdited.connect(self._user_changed)
        self.edit.editingFinished.connect(self.flush)
        self.browse_button.clicked.connect(self.browse)
        self.retranslate()

    def focus_widget(self) -> QWidget:
        return self.edit

    def retranslate(self) -> None:
        self.browse_button.setText(tr("settings.browse"))
        self.browse_button.setIcon(fluent_icon(Glyph.FOLDER))
        self.edit.setPlaceholderText(tr("settings.dir.placeholder"))

    def browse(self) -> None:
        chosen = self._dialogs.choose_directory(tr("settings.browse.title"), self.edit.text())
        if chosen:
            self.edit.setText(chosen)
            self._user_changed()
            self.flush()

    def json_value(self) -> JsonValue:
        return self.edit.text().strip()

    def _write(self, value: JsonValue) -> None:
        self.edit.setText(value if isinstance(value, str) else "")


# ---------------------------------------------------------------- 热键录入

_MODIFIER_KEYS: Final = frozenset(
    {Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Shift, Qt.Key.Key_Meta, Qt.Key.Key_AltGr}
)

_NAMED_KEYS: Final = {
    Qt.Key.Key_Space: "Space",
    Qt.Key.Key_Tab: "Tab",
    Qt.Key.Key_Return: "Enter",
    Qt.Key.Key_Enter: "Enter",
    Qt.Key.Key_Escape: "Esc",
    Qt.Key.Key_Print: "PrintScreen",
    Qt.Key.Key_Insert: "Insert",
    Qt.Key.Key_Delete: "Delete",
    Qt.Key.Key_Home: "Home",
    Qt.Key.Key_End: "End",
    Qt.Key.Key_PageUp: "PageUp",
    Qt.Key.Key_PageDown: "PageDown",
    Qt.Key.Key_QuoteLeft: "`",
}


_VIRTUAL_KEY_NAMES: Final = {
    0x20: "Space",
    0x09: "Tab",
    0x0D: "Enter",
    0x1B: "Esc",
    0x2C: "PrintScreen",
    0x2D: "Insert",
    0x2E: "Delete",
    0x24: "Home",
    0x23: "End",
    0x21: "PageUp",
    0x22: "PageDown",
    0xC0: "`",
}
"""Windows 虚拟键码 → 热键写法里的键名（与 ``[hotkeys]`` 节的规范写法一致）。"""


def virtual_key_name(virtual_key: int) -> str | None:
    """Windows 虚拟键码对应的键名：字母、数字、F1 至 F24 与几个命名键；其他键码返回 None。

    全局热键按虚拟键码注册，键名也按虚拟键码取：按住 Shift 时 Qt 给出的是 Shift 之后的字符
    （Shift+1 是 Key_Exclam），而热键要的是键位本身。
    """
    if 0x30 <= virtual_key <= 0x39 or 0x41 <= virtual_key <= 0x5A:
        return chr(virtual_key)
    if 0x70 <= virtual_key <= 0x87:
        return f"F{virtual_key - 0x70 + 1}"
    return _VIRTUAL_KEY_NAMES.get(virtual_key)


def hotkey_text(key: int, modifiers: Qt.KeyboardModifier, virtual_key: int = 0) -> str | None:
    """一次按键 → 热键的规范写法，例如 Ctrl+Alt+Q；只按了修饰键时返回 None。

    修饰键的顺序与 ``[hotkeys]`` 节的规范写法一致：Ctrl、Alt、Shift、Win。virtual_key 是按键事件的
    Windows 虚拟键码（``QKeyEvent.nativeVirtualKey``），能识别时按它取键名；没有虚拟键码的平台（offscreen）
    按 Qt 的键取名。按键的名称不在热键支持的范围内时仍然给出 Qt 的名称，由配置服务拒绝并说明原因。
    """
    if key in {int(k.value) for k in _MODIFIER_KEYS} or key == 0:
        return None
    parts: list[str] = []
    for flag, name in (
        (Qt.KeyboardModifier.ControlModifier, "Ctrl"),
        (Qt.KeyboardModifier.AltModifier, "Alt"),
        (Qt.KeyboardModifier.ShiftModifier, "Shift"),
        (Qt.KeyboardModifier.MetaModifier, "Win"),
    ):
        if modifiers & flag:
            parts.append(name)
    named = virtual_key_name(virtual_key) if virtual_key else None
    if named is None:
        named = next((name for known, name in _NAMED_KEYS.items() if int(known.value) == key), None)
    if named is not None:
        key_name = named
    elif int(Qt.Key.Key_A.value) <= key <= int(Qt.Key.Key_Z.value) or int(Qt.Key.Key_0.value) <= key <= int(
        Qt.Key.Key_9.value
    ):
        key_name = chr(key)
    elif int(Qt.Key.Key_F1.value) <= key <= int(Qt.Key.Key_F35.value):
        key_name = f"F{key - int(Qt.Key.Key_F1.value) + 1}"
    else:
        key_name = chr(key) if 0x20 < key < 0x7F else f"0x{key:X}"
    return "+".join([*parts, key_name])


class HotkeyButton(QPushButton):
    """显示当前热键的按钮。点击、Enter 或空格进入录入状态，按下组合键后退出，Esc 或失去焦点取消。

    录入状态下按键都被它接管，包括 Tab 和带 Alt 的组合（不会触发助记键）。
    只有不在录入状态时 Tab 才移动焦点，所以键盘用户不会被困在这个控件里。
    进入和退出录入状态时发出 ``recording_changed``：已经注册的全局热键会先于窗口收到按键，
    展示器据此请热键模块在录入期间暂停全局热键。
    """

    hotkey_chosen = Signal(str)
    recording_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = ""
        self._recording = False
        self.setMinimumWidth(EDITOR_WIDTH // 2)
        self.clicked.connect(self.start_recording)

    @property
    def recording(self) -> bool:
        return self._recording

    def set_hotkey(self, text: str) -> None:
        self._text = text
        self.refresh_text()

    def hotkey(self) -> str:
        return self._text

    def refresh_text(self) -> None:
        if self._recording:
            self.setText(tr("settings.hotkey.record"))
        else:
            self.setText(self._text or tr("settings.hotkey.empty"))

    def start_recording(self) -> None:
        if not self._recording:
            self._recording = True
            self.refresh_text()
            self.recording_changed.emit(True)

    def stop_recording(self) -> None:
        if self._recording:
            self._recording = False
            self.refresh_text()
            self.recording_changed.emit(False)

    def event(self, event: QEvent) -> bool:
        if self._recording:
            if event.type() == QEvent.Type.ShortcutOverride:
                event.accept()  # 录入时的按键不触发窗口里的快捷键和助记键
                return True
            if event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
                self.keyPressEvent(event)  # 绕过 QWidget 对 Tab 的焦点切换
                return True
        return super().event(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if not self._recording:
            if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not event.isAutoRepeat():
                self.start_recording()
                event.accept()
                return
            super().keyPressEvent(event)
            return
        event.accept()
        if event.isAutoRepeat():
            return
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if event.key() == Qt.Key.Key_Escape and modifiers == Qt.KeyboardModifier.NoModifier:
            self.stop_recording()
            return
        text = hotkey_text(event.key(), modifiers, event.nativeVirtualKey())
        if text is None:
            return  # 只按了修饰键，等待主键
        self.stop_recording()
        self.hotkey_chosen.emit(text)

    def focusOutEvent(self, event: QFocusEvent) -> None:
        self.stop_recording()
        super().focusOutEvent(event)

    def hideEvent(self, event: QHideEvent) -> None:
        # 窗口隐藏时不一定先发失去焦点的事件；录入状态随之结束，全局热键恢复。
        self.stop_recording()
        super().hideEvent(event)


class HotkeyEditor(ValueEditor):
    """热键：录入完成时立即提交。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(debounce_ms=None, parent=parent)
        self.button = HotkeyButton()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.button)
        self.setFocusProxy(self.button)
        self.button.hotkey_chosen.connect(self._chosen)

    def focus_widget(self) -> QWidget:
        return self.button

    def retranslate(self) -> None:
        self.button.refresh_text()
        self.button.setToolTip(tr("settings.hotkey.tip"))

    def _chosen(self, text: str) -> None:
        self.button.set_hotkey(text)
        self._user_changed()

    def json_value(self) -> JsonValue:
        return self.button.hotkey()

    def _write(self, value: JsonValue) -> None:
        self.button.set_hotkey(value if isinstance(value, str) else "")


# ---------------------------------------------------------------- 设置卡片


class SettingCard(QFrame):
    """设置卡片：左侧图标，中间标题与说明，右侧控件，错误提示在最下面（版式与 Windows 11 的“设置”一致）。

    stacked 为真时控件放在文字下方、占满整行（表格、多行文字等较宽的控件）。
    背景是半透明的圆角矩形，叠在 Mica 或窗口底色上，比底色略亮（深色主题下略浅），悬停时略有变化；
    颜色在每次绘制时取自 ui.theme，切换主题后立即正确。错误提示是下方的错误色小字，不改变控件的位置。
    """

    def __init__(
        self,
        editor: QWidget | None,
        *,
        location: tuple[str | int, ...] = (),
        stacked: bool = False,
        icon: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.location = location
        self.editor = editor
        self._hover = False
        self._hoverable = not stacked
        self.setMinimumHeight(TOKENS.card_min_height)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        self.icon_label = ThemedLabel("primary")
        self.icon_label.setFont(fluent_icon_font(TOKENS.icon_px))
        self.icon_label.setFixedWidth(TOKENS.icon_px + TOKENS.spacing_xs)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.title_label = ThemedLabel("primary", TOKENS.title_px)
        self.title_label.setWordWrap(True)
        self.desc_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.desc_label.setWordWrap(True)
        self.error_label = ThemedLabel("critical", TOKENS.caption_px)
        self.error_label.setWordWrap(True)
        self.error_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.error_label.hide()
        self._error_icon = ThemedLabel("critical")
        self._error_icon.setFont(fluent_icon_font(TOKENS.caption_px))
        self._error_icon.setText(fluent_icon_text(Glyph.WARNING))
        self._error_icon.hide()

        text_column = QVBoxLayout()
        text_column.setSpacing(2)
        text_column.addWidget(self.title_label)
        text_column.addWidget(self.desc_label)
        # 第一行放在自己的容器里，最小高度恰好让整张卡片达到最小高度：错误提示出现时这一行的高度不变，
        # 控件和标题的位置也就不变。
        top_row = QWidget()
        self._top_row = top_row
        self._stacked = stacked
        top_row.setMinimumHeight(TOKENS.card_min_height - 2 * TOKENS.spacing_m)
        top = QHBoxLayout(top_row)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(TOKENS.spacing_l)
        top.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)
        top.addLayout(text_column, 1)
        if editor is not None and not stacked:
            top.addWidget(editor, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self._error_row = QHBoxLayout()
        self._error_row.setSpacing(TOKENS.spacing_s)
        self._error_row.addWidget(self._error_icon, 0, Qt.AlignmentFlag.AlignTop)
        self._error_row.addWidget(self.error_label, 1)

        root = QVBoxLayout(self)
        root.setContentsMargins(TOKENS.card_padding, TOKENS.spacing_m, TOKENS.card_padding, TOKENS.spacing_m)
        root.setSpacing(TOKENS.spacing_s)
        root.addWidget(top_row)
        if editor is not None and stacked:
            root.addWidget(editor)
        root.addLayout(self._error_row)
        self.set_icon(icon)

    def hide_header(self) -> None:
        """不要标题、说明和图标：卡片只作为一块容器（列表、表格、按钮行放在里面）。"""
        for label in (self.title_label, self.desc_label, self.icon_label):
            label.hide()
        if self._stacked:
            self._top_row.hide()  # 第一行只有标题区，没有它就不占高度
        self._error_row.setContentsMargins(0, 0, 0, 0)

    def set_icon(self, glyph: str) -> None:
        """卡片左侧的 Fluent 图标；glyph 为空串时不显示图标，文字靠左。"""
        self.icon_label.setText(fluent_icon_text(glyph))
        self.icon_label.setVisible(bool(glyph))
        indent = self.icon_label.width() + TOKENS.spacing_l if glyph else 0
        self._error_row.setContentsMargins(indent, 0, 0, 0)

    def set_texts(self, title: str, description: str) -> None:
        self.title_label.setText(title)
        self.desc_label.setText(description)
        self.desc_label.setVisible(bool(description))
        if isinstance(self.editor, ValueEditor):
            focus = self.editor.focus_widget()
            focus.setAccessibleName(title)
            focus.setAccessibleDescription(description)

    def set_error(self, text: str | None) -> None:
        """在控件下方显示错误原因；None 表示清除。"""
        shown = bool(text)
        self.error_label.setText(tr("settings.error.prefix", reason=text) if text else "")
        self.error_label.setVisible(shown)
        self._error_icon.setVisible(shown)
        self.error_label.setAccessibleName(self.error_label.text())

    @property
    def error_text(self) -> str:
        """当前显示的错误提示，没有时为空串。"""
        return self.error_label.text() if not self.error_label.isHidden() else ""

    def enterEvent(self, event: QEnterEvent) -> None:
        self._hover = self._hoverable
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        colors = theme_colors()
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(colors.card_border)
        painter.setBrush(colors.card_hover if self._hover else colors.card)
        painter.drawRoundedRect(rect, TOKENS.card_radius, TOKENS.card_radius)

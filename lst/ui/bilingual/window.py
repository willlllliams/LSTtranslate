"""双语窗口的视图：顶层 QWidget，只负责绘制和收集输入（架构文档 11.1、11.6 节）。

顶栏从左到右是源语言、交换、目标语言、翻译服务下拉框和置顶按钮；中间是可调大小的输入区与译文区；
译文区下面是一行状态文字，说明服务回退、翻译失败或服务不支持语言对这类情况（需求 BI-04）。

窗口不在任务栏占按钮（需求 BI-01）：它有一个隐藏的所有者窗口，Windows 不为有所有者的窗口创建任务栏按钮，
Alt+Tab 列表、激活、置顶与 Mica 背景照常；窗口没有最小化按钮，关闭按钮与 Esc 只隐藏窗口。
规则与取舍见 ``lst.ui.common.window.SecondaryWindow`` 与架构文档 11.6.4 节。

窗口内的操作按键位表（``Keymap``，配置的 [bindings] 节，需求 UI-09，架构文档 11.8 节）执行，括号里是默认键位：
立即翻译（Ctrl+Return）、交换语言（Ctrl+Shift+S）、置顶（Ctrl+Shift+T）、清空（Ctrl+Shift+L）、
关闭窗口（Esc，只隐藏窗口，状态由展示器保存）。交换、置顶、清空调用按钮的 click，
与鼠标点击按钮的路径完全相同。

复制不进键位表：复制全部译文用“复制译文”按钮；译文区的文字可以用鼠标或键盘选中，选中的部分用系统标准的
复制键（Ctrl+C、Ctrl+Insert）复制，由控件自己处理。事件过滤器只拦截匹配到键位的按键，默认键位里没有 Ctrl+C
与 Ctrl+Insert，这两个键照常交给输入框和译文区。

按键要在子控件之前拦下：用户可以把操作改到输入框自己处理的组合上，例如 Ctrl+A。窗口显示期间在应用程序上
装一个事件过滤器，发给本窗口及其子控件的按键与鼠标按下先查键位表，匹配到操作才拦截，其余照常交给控件。
焦点在可编辑的文字控件或下拉框里时，不带 Ctrl、Alt 的可打印字符一律交给控件，打字不会被键位吃掉。
这样也不依赖 QShortcut 的“窗口处于激活状态”条件，键盘操作在测试里可以直接验证。
按钮的提示与输入框的占位文字按当前键位显示。
"""

from collections.abc import Collection, Sequence
from itertools import pairwise
from typing import Final, cast

from PySide6.QtCore import QEvent, QObject, QSize, Qt
from PySide6.QtGui import (
    QHideEvent,
    QKeyEvent,
    QMouseEvent,
    QShowEvent,
    QStandardItem,
    QStandardItemModel,
    QWindow,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from lst.contracts.ids import EngineId, Lang
from lst.ui.bilingual.presenter import BilingualActions, EngineChoice
from lst.ui.bilingual.strings import LANGUAGES, language_name
from lst.ui.bilingual.widgets import TranslationPane
from lst.ui.common.bindings import BindingsConfig, Keymap, Scope, action_label
from lst.ui.common.window import SecondaryWindow
from lst.ui.i18n import tr
from lst.ui.theme import TOKENS

_ENGINE_ROLE: Final = Qt.ItemDataRole.UserRole

DEFAULT_SIZE = QSize(640, 600)

_COMMAND_MODIFIERS: Final = Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier
_FILTERED: Final = frozenset(
    {
        QEvent.Type.KeyPress,
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonDblClick,
        QEvent.Type.MouseButtonRelease,
    }
)


class _BindingFilter(QObject):
    """装在应用程序上的事件过滤器，把发给窗口及其子控件的按键与鼠标按键交给窗口查键位表。"""

    def __init__(self, window: "BilingualWindow") -> None:
        super().__init__(window)
        self._window = window

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() not in _FILTERED:
            return False
        window = self._window
        if not isinstance(watched, QWidget) or not (watched is window or window.isAncestorOf(watched)):
            return False
        return window.handle_binding_event(watched, event)


def _typing(widget: QWidget, event: QKeyEvent) -> bool:
    """可编辑的文字控件或下拉框里不带 Ctrl、Alt 的可打印字符：这是在打字，不当作键位。"""
    if event.modifiers() & _COMMAND_MODIFIERS:
        return False
    text = event.text()
    if not text or not text.isprintable():
        return False
    if isinstance(widget, QPlainTextEdit | QTextEdit):
        return not widget.isReadOnly()
    return isinstance(widget, QLineEdit | QComboBox)


class BilingualWindow(SecondaryWindow):
    """双语窗口。控件以公开属性暴露，供展示器测试检查。"""

    def __init__(self) -> None:
        super().__init__(taskbar=False)
        self._actions: BilingualActions | None = None
        self._bindings = BindingsConfig()
        self._keymap = Keymap.for_scope(self._bindings, Scope.BILINGUAL)
        self._filter = _BindingFilter(self)
        self._filter_installed = False
        self._swallow_release: Qt.MouseButton | None = None

        self.source_combo = QComboBox()
        self.target_combo = QComboBox()
        for combo in (self.source_combo, self.target_combo):
            combo.addItem("", "")
            for lang in LANGUAGES:
                combo.addItem("", lang)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.swap_button = QPushButton()
        self.engine_combo = QComboBox()
        self.engine_combo.setModel(QStandardItemModel(self.engine_combo))
        self.engine_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.engine_label = QLabel()
        self.engine_label.setBuddy(self.engine_combo)
        self.pin_button = QPushButton()
        self.pin_button.setCheckable(True)
        self.input_edit = QPlainTextEdit()
        # Tab 用来移动焦点（需求 UI-07），输入框里不插入制表符。
        self.input_edit.setTabChangesFocus(True)
        self.clear_button = QPushButton()
        self.translation = TranslationPane()
        self.copy_button = QPushButton()
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.TextFormat.PlainText)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.status_label.setVisible(False)

        top = QHBoxLayout()
        top.setSpacing(TOKENS.spacing_s)
        top.addWidget(self.source_combo)
        top.addWidget(self.swap_button)
        top.addWidget(self.target_combo)
        top.addStretch(1)
        top.addWidget(self.engine_label)
        top.addWidget(self.engine_combo)
        top.addWidget(self.pin_button)

        input_panel = QWidget()
        input_layout = QVBoxLayout(input_panel)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(TOKENS.spacing_xs)
        input_layout.addWidget(self.input_edit, 1)
        input_buttons = QHBoxLayout()
        input_buttons.addStretch(1)
        input_buttons.addWidget(self.clear_button)
        input_layout.addLayout(input_buttons)

        output_panel = QWidget()
        output_layout = QVBoxLayout(output_panel)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.setSpacing(TOKENS.spacing_xs)
        output_layout.addWidget(self.translation, 1)
        output_buttons = QHBoxLayout()
        output_buttons.addWidget(self.status_label, 1)
        output_buttons.addWidget(self.copy_button, 0, Qt.AlignmentFlag.AlignTop)
        output_layout.addLayout(output_buttons)

        # 输入区与译文区的排列方向可设置（需求 BI-02），用分隔条让用户调整两区大小。
        self.content_splitter = QSplitter(Qt.Orientation.Vertical)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.addWidget(input_panel)
        self.content_splitter.addWidget(output_panel)

        root = QVBoxLayout(self)
        root.setContentsMargins(TOKENS.spacing_l, TOKENS.spacing_l, TOKENS.spacing_l, TOKENS.spacing_l)
        root.setSpacing(TOKENS.spacing_m)
        root.addLayout(top)
        root.addWidget(self.content_splitter, 1)

        self.retranslate()
        self._fix_tab_order()
        self.resize(DEFAULT_SIZE)

    # ------------------------------------------------------------------ 绑定展示器

    def bind(self, actions: BilingualActions) -> None:
        self._actions = actions
        self.input_edit.textChanged.connect(lambda: actions.input_changed(self.input_edit.toPlainText()))
        self.source_combo.currentIndexChanged.connect(self._languages_edited)
        self.target_combo.currentIndexChanged.connect(self._languages_edited)
        self.engine_combo.currentIndexChanged.connect(self._engine_edited)
        self.swap_button.clicked.connect(actions.swap_languages)
        self.pin_button.toggled.connect(actions.pin_toggled)
        self.clear_button.clicked.connect(actions.clear)
        self.copy_button.clicked.connect(actions.copy_translation)

    def _languages_edited(self, _index: int) -> None:
        if self._actions is not None:
            self._actions.languages_changed(*self.languages())

    def _engine_edited(self, index: int) -> None:
        engine_id = self.engine_combo.itemData(index, _ENGINE_ROLE)
        if self._actions is not None and isinstance(engine_id, str) and engine_id:
            self._actions.engine_chosen(EngineId(engine_id))

    # ------------------------------------------------------------------ 文案

    def retranslate(self) -> None:
        self.setWindowTitle(tr("bilingual.window.title"))
        for combo in (self.source_combo, self.target_combo):
            for index in range(combo.count()):
                code = combo.itemData(index)
                combo.setItemText(
                    index, language_name(Lang(code) if isinstance(code, str) and code else None)
                )
        self.source_combo.setAccessibleName(tr("bilingual.source.label"))
        self.source_combo.setToolTip(tr("bilingual.source.label"))
        self.target_combo.setAccessibleName(tr("bilingual.target.label"))
        self.target_combo.setToolTip(tr("bilingual.target.label"))
        self.engine_label.setText(tr("bilingual.engine.label"))
        self.engine_combo.setAccessibleName(tr("bilingual.engine.label"))
        self.engine_combo.setToolTip(tr("bilingual.engine.tip"))
        self.swap_button.setText(tr("bilingual.swap"))
        self.swap_button.setToolTip(self._tip("bilingual.swap.tip", "bilingual.swap"))
        self.pin_button.setText(tr("bilingual.pin"))
        self.pin_button.setToolTip(self._tip("bilingual.pin.tip", "bilingual.pin"))
        self.clear_button.setText(tr("bilingual.clear"))
        self.clear_button.setToolTip(self._tip("bilingual.clear.tip", "bilingual.clear"))
        self.copy_button.setText(tr("bilingual.copy"))
        self.copy_button.setToolTip(tr("bilingual.copy.tip"))
        self.input_edit.setPlaceholderText(self._placeholder())
        self.input_edit.setAccessibleName(tr("bilingual.input.label"))
        self.translation.setPlaceholderText(tr("bilingual.output.placeholder"))
        self.translation.setAccessibleName(tr("bilingual.output.label"))

    def _tip(self, key: str, action: str) -> str:
        """按钮提示：说明文字后面括上当前键位；没有绑定时只有说明文字。"""
        keys = action_label(self._bindings, action)
        return tr("bilingual.tip.keys", tip=tr(key), keys=keys) if keys else tr(key)

    def _placeholder(self) -> str:
        """输入框的占位文字：自动翻译的说明，加上立即翻译与关闭窗口的当前键位。"""
        parts = [tr("bilingual.input.placeholder")]
        translate = action_label(self._bindings, "bilingual.translate")
        if translate:
            parts.append(tr("bilingual.input.placeholder.translate", keys=translate))
        close = action_label(self._bindings, "bilingual.close")
        if close:
            parts.append(tr("bilingual.input.placeholder.close", keys=close))
        return "".join(parts)  # 英文的后两句自带前导空格

    # ------------------------------------------------------------------ 键位

    @property
    def bindings(self) -> BindingsConfig:
        return self._bindings

    def set_bindings(self, bindings: BindingsConfig) -> None:
        """换用新的键位（UI-09），按钮提示与占位文字随之更新。"""
        self._bindings = bindings
        self._keymap = Keymap.for_scope(bindings, Scope.BILINGUAL)
        self.retranslate()

    def handle_binding_event(self, widget: QWidget, event: QEvent) -> bool:
        """发给 widget 的按键或鼠标事件匹配到键位时执行操作并返回真，事件不再交给控件。"""
        if self._actions is None:
            return False
        if isinstance(event, QKeyEvent):
            action = self._keymap.key_action(event)
            if action is None or _typing(widget, event):
                return False
            self._trigger(action)
            return True
        if not isinstance(event, QMouseEvent):
            return False
        button = event.button()
        if event.type() == QEvent.Type.MouseButtonRelease:
            # 按下时已经执行了操作的按键，松开也不交给控件，按钮不会因为这次松开被点击。
            if button == self._swallow_release:
                self._swallow_release = None
                return True
            return False
        action = None
        if event.type() == QEvent.Type.MouseButtonDblClick:
            action = self._keymap.mouse_action(button, event.modifiers(), "DoubleClick")
        if action is None:
            # 本窗口没有拖动操作，单击在按下时就生效；没有绑定双击时，双击的第二次按下当作单击。
            action = self._keymap.mouse_action(button, event.modifiers(), "Click")
        if action is None:
            return False
        self._swallow_release = button
        self._trigger(action)
        return True

    def _trigger(self, action: str) -> None:
        actions = self._actions
        if actions is None:
            return
        match action:
            case "bilingual.translate":
                actions.translate_now()
            # 交换、置顶、清空走按钮本身，与鼠标点击的路径完全相同（架构文档 11.6.4 节）。
            case "bilingual.swap":
                self.swap_button.click()
            case "bilingual.pin":
                self.pin_button.click()
            case "bilingual.clear":
                self.clear_button.click()
            case "bilingual.close":
                actions.hide_requested()
            case _:
                pass

    # ------------------------------------------------------------------ 输入与语言

    def input_text(self) -> str:
        return self.input_edit.toPlainText()

    def set_input_text(self, text: str) -> None:
        self.input_edit.blockSignals(True)
        try:
            self.input_edit.setPlainText(text)
        finally:
            self.input_edit.blockSignals(False)

    def focus_input(self) -> None:
        self.input_edit.setFocus(Qt.FocusReason.OtherFocusReason)

    @staticmethod
    def _combo_lang(combo: QComboBox) -> Lang | None:
        code = combo.currentData()
        return Lang(code) if isinstance(code, str) and code else None

    def languages(self) -> tuple[Lang | None, Lang | None]:
        return (self._combo_lang(self.source_combo), self._combo_lang(self.target_combo))

    def set_languages(self, source: Lang | None, target: Lang | None) -> None:
        for combo, lang in ((self.source_combo, source), (self.target_combo, target)):
            combo.blockSignals(True)
            try:
                combo.setCurrentIndex(max(0, combo.findData(lang or "")))
            finally:
                combo.blockSignals(False)

    def set_engines(self, choices: Sequence[EngineChoice], current: EngineId | None) -> None:
        """重建服务下拉框并选中 current，不触发 engine_chosen。出错的服务列出但不能选。"""
        combo = self.engine_combo
        model = combo.model()
        assert isinstance(model, QStandardItemModel)
        combo.blockSignals(True)
        try:
            model.clear()
            for choice in choices:
                item = QStandardItem(choice.label)
                item.setData(choice.engine_id, _ENGINE_ROLE)
                item.setEnabled(choice.available)
                model.appendRow(item)
            combo.setCurrentIndex(combo.findData(current, _ENGINE_ROLE) if current is not None else -1)
        finally:
            combo.blockSignals(False)
        combo.setEnabled(any(choice.available for choice in choices))

    def current_engine(self) -> EngineId | None:
        """下拉框当前选中的服务。"""
        data = self.engine_combo.currentData(_ENGINE_ROLE)
        return EngineId(data) if isinstance(data, str) and data else None

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setVisible(bool(text))

    # ------------------------------------------------------------------ 外观设置

    def set_pinned(self, pinned: bool) -> None:
        self.pin_button.blockSignals(True)
        try:
            self.pin_button.setChecked(pinned)
        finally:
            self.pin_button.blockSignals(False)
        flags = self.windowFlags()
        hint = Qt.WindowType.WindowStaysOnTopHint
        # 去掉标志位要按整数运算：PySide6 的枚举取反（~hint）只覆盖 0x1FFFFFF 以内的位，
        # flags & ~hint 会顺带清掉 WindowCloseButtonHint（0x08000000），标题栏的关闭按钮因此变灰。
        bits = int(flags.value)
        wanted = Qt.WindowType(bits | int(hint.value) if pinned else bits & ~int(hint.value))
        if wanted == flags:
            return
        # 类型桩把 windowHandle 标成非空，实际上原生窗口创建之前它返回 None。
        handle = cast(QWindow | None, self.windowHandle())
        if handle is None:
            self.setWindowFlags(wanted)
        else:
            # 原生窗口已经创建时直接改它的标志。setWindowFlags 会先隐藏窗口再重建，用户会看到窗口闪一下。
            self.overrideWindowFlags(wanted)
            handle.setFlags(wanted)

    def set_horizontal(self, horizontal: bool) -> None:
        orientation = Qt.Orientation.Horizontal if horizontal else Qt.Orientation.Vertical
        if self.content_splitter.orientation() != orientation:
            self.content_splitter.setOrientation(orientation)

    # ------------------------------------------------------------------ 译文区

    def show_segments(self, texts: Sequence[str], fade: Collection[int]) -> None:
        self.translation.set_segments(texts, fade)

    def update_segments(self, changes: Sequence[tuple[int, str]], fade: Collection[int]) -> None:
        self.translation.update_segments(changes, fade)

    def _fix_tab_order(self) -> None:
        """Tab 顺序与视觉顺序一致（需求 UI-07）：顶栏从左到右，输入区，译文区，复制按钮。"""
        chain: list[QWidget] = [
            self.source_combo,
            self.swap_button,
            self.target_combo,
            self.engine_combo,
            self.pin_button,
            self.input_edit,
            self.clear_button,
            self.translation,
            self.copy_button,
        ]
        for first, second in pairwise(chain):
            QWidget.setTabOrder(first, second)

    # ------------------------------------------------------------------ 显示与隐藏

    def present(self) -> None:
        """显示并激活窗口，焦点放到输入框（位置不在任何屏幕上时先移到主屏中央，需求 BI-01）。"""
        self.present_window(self.input_edit)

    # ------------------------------------------------------------------ 事件

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        app = QApplication.instance()
        if app is not None and not self._filter_installed:
            app.installEventFilter(self._filter)
            self._filter_installed = True

    def hideEvent(self, event: QHideEvent) -> None:
        # 窗口隐藏期间不过滤，其他窗口的事件不经过这里。
        app = QApplication.instance()
        if app is not None and self._filter_installed:
            app.removeEventFilter(self._filter)
            self._filter_installed = False
        self._swallow_release = None
        super().hideEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        # 窗口显示时按键已经由事件过滤器处理；这里处理直接发给窗口、过滤器还没有装上的情况。
        if self.handle_binding_event(self, event):
            event.accept()
            return
        super().keyPressEvent(event)

    def close_requested(self) -> None:
        # 标题栏的关闭按钮与 Alt+F4 只隐藏窗口，窗口状态由展示器保存。
        if self._actions is not None:
            self._actions.hide_requested()
        else:
            self.hide()

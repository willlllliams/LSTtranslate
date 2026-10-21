"""双语窗口的视图：顶层 QWidget，只负责绘制和收集输入（架构文档 11.1、11.6 节）。

快捷键在窗口的 keyPressEvent 里处理：输入框、按钮和下拉框不处理 Ctrl+Enter、Ctrl+Shift+C 和 Esc，
这些按键沿父控件链传到窗口。这样不依赖 QShortcut 的“窗口处于激活状态”条件，键盘操作在测试里也能直接验证。
"""

from collections.abc import Collection, Mapping, Sequence
from itertools import pairwise
from typing import cast

from PySide6.QtCore import QPoint, QSize, Qt
from PySide6.QtGui import QCloseEvent, QColor, QGuiApplication, QKeyEvent, QScreen, QWindow
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from lst.contracts.ids import EngineId, Lang
from lst.ui.bilingual.presenter import BilingualActions, EngineRow, SegmentItem
from lst.ui.bilingual.state import fit_on_screens
from lst.ui.bilingual.strings import LANGUAGES, language_name
from lst.ui.bilingual.widgets import EngineResultList, TranslationPane
from lst.ui.i18n import tr
from lst.ui.theme import TOKENS, enable_mica, is_dark

DEFAULT_SIZE = QSize(640, 600)

_CTRL = Qt.KeyboardModifier.ControlModifier
_SHIFT = Qt.KeyboardModifier.ShiftModifier


def _primary_screen() -> QScreen | None:
    # 类型桩把 primaryScreen 标成非空，没有屏幕时（例如显示器全部断开）它返回 None。
    return cast(QScreen | None, QGuiApplication.primaryScreen())


class BilingualWindow(QWidget):
    """双语窗口。控件以公开属性暴露，供展示器测试检查。"""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window)
        # 这是常驻托盘程序的次要窗口，关闭它不应让程序退出。
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self._actions: BilingualActions | None = None
        self._allow_close = False
        self._backdrop_tried = False

        self.source_combo = QComboBox()
        self.target_combo = QComboBox()
        for combo in (self.source_combo, self.target_combo):
            combo.addItem("", "")
            for lang in LANGUAGES:
                combo.addItem("", lang)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.swap_button = QPushButton()
        self.pin_button = QPushButton()
        self.pin_button.setCheckable(True)
        self.input_edit = QPlainTextEdit()
        # Tab 用来移动焦点（需求 UI-07），输入框里不插入制表符。
        self.input_edit.setTabChangesFocus(True)
        self.clear_button = QPushButton()
        self._shortcut_buttons: dict[int, QPushButton] = {
            Qt.Key.Key_S: self.swap_button,
            Qt.Key.Key_T: self.pin_button,
            Qt.Key.Key_L: self.clear_button,
        }
        self.translation = TranslationPane()
        self.copy_button = QPushButton()
        self.engine_list = EngineResultList(self._copy_engine)
        self._engines_title = QLabel()

        top = QHBoxLayout()
        top.setSpacing(TOKENS.spacing_s)
        top.addWidget(self.source_combo)
        top.addWidget(self.swap_button)
        top.addWidget(self.target_combo)
        top.addStretch(1)
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
        output_buttons.addStretch(1)
        output_buttons.addWidget(self.copy_button)
        output_layout.addLayout(output_buttons)

        # 输入区与译文区的排列方向可设置（需求 BI-02），用分隔条让用户调整两区大小。
        self.content_splitter = QSplitter(Qt.Orientation.Vertical)
        self.content_splitter.setChildrenCollapsible(False)
        self.content_splitter.addWidget(input_panel)
        self.content_splitter.addWidget(output_panel)

        engines_panel = QWidget()
        engines_layout = QVBoxLayout(engines_panel)
        engines_layout.setContentsMargins(0, 0, 0, 0)
        engines_layout.setSpacing(TOKENS.spacing_xs)
        engines_layout.addWidget(self._engines_title)
        engines_layout.addWidget(self.engine_list, 1)

        outer = QSplitter(Qt.Orientation.Vertical)
        outer.setChildrenCollapsible(False)
        outer.addWidget(self.content_splitter)
        outer.addWidget(engines_panel)
        outer.setStretchFactor(0, 3)
        outer.setStretchFactor(1, 1)

        root = QVBoxLayout(self)
        root.setContentsMargins(TOKENS.spacing_l, TOKENS.spacing_l, TOKENS.spacing_l, TOKENS.spacing_l)
        root.setSpacing(TOKENS.spacing_m)
        root.addLayout(top)
        root.addWidget(outer, 1)

        self.retranslate()
        self._fix_tab_order()
        self.resize(DEFAULT_SIZE)

    # ------------------------------------------------------------------ 绑定展示器

    def bind(self, actions: BilingualActions) -> None:
        self._actions = actions
        self.input_edit.textChanged.connect(lambda: actions.input_changed(self.input_edit.toPlainText()))
        self.source_combo.currentIndexChanged.connect(self._languages_edited)
        self.target_combo.currentIndexChanged.connect(self._languages_edited)
        self.swap_button.clicked.connect(actions.swap_languages)
        self.pin_button.toggled.connect(actions.pin_toggled)
        self.clear_button.clicked.connect(actions.clear)
        self.copy_button.clicked.connect(actions.copy_translation)

    def _languages_edited(self, _index: int) -> None:
        if self._actions is not None:
            self._actions.languages_changed(*self.languages())

    def _copy_engine(self, engine_id: EngineId) -> None:
        if self._actions is not None:
            self._actions.copy_engine(engine_id)

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
        self.swap_button.setText(tr("bilingual.swap"))
        self.swap_button.setToolTip(tr("bilingual.swap.tip"))
        self.pin_button.setText(tr("bilingual.pin"))
        self.pin_button.setToolTip(tr("bilingual.pin.tip"))
        self.clear_button.setText(tr("bilingual.clear"))
        self.clear_button.setToolTip(tr("bilingual.clear.tip"))
        self.copy_button.setText(tr("bilingual.copy"))
        self.copy_button.setToolTip(tr("bilingual.copy.tip"))
        self.input_edit.setPlaceholderText(tr("bilingual.input.placeholder"))
        self.input_edit.setAccessibleName(tr("bilingual.input.label"))
        self.translation.setPlaceholderText(tr("bilingual.output.placeholder"))
        self.translation.setAccessibleName(tr("bilingual.output.label"))
        self._engines_title.setText(tr("bilingual.engines.title"))
        self.engine_list.retranslate()

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

    # ------------------------------------------------------------------ 外观设置

    def set_pinned(self, pinned: bool) -> None:
        self.pin_button.blockSignals(True)
        try:
            self.pin_button.setChecked(pinned)
        finally:
            self.pin_button.blockSignals(False)
        flags = self.windowFlags()
        hint = Qt.WindowType.WindowStaysOnTopHint
        wanted = flags | hint if pinned else flags & ~hint
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

    def set_markers_visible(self, visible: bool) -> None:
        self.translation.set_markers_visible(visible)

    # ------------------------------------------------------------------ 译文区与引擎列表

    def show_segments(self, items: Sequence[SegmentItem], fade: Collection[int]) -> None:
        self.translation.set_segments(items, fade)

    def update_segments(self, changes: Sequence[tuple[int, SegmentItem]], fade: Collection[int]) -> None:
        self.translation.update_segments(changes, fade)

    def set_segment_colors(self, colors: Mapping[int, QColor | None]) -> None:
        self.translation.set_colors(colors)

    def show_engine_rows(self, order: Sequence[EngineId], changed: Sequence[EngineRow]) -> None:
        self.engine_list.show_rows(order, changed)
        self._fix_tab_order()

    def clear_engine_rows(self) -> None:
        self.engine_list.clear_rows()

    def _fix_tab_order(self) -> None:
        """Tab 顺序与视觉顺序一致（需求 UI-07）：顶栏从左到右，输入区，译文区，再到各行的复制按钮。"""
        chain: list[QWidget] = [
            self.source_combo,
            self.swap_button,
            self.target_combo,
            self.pin_button,
            self.input_edit,
            self.clear_button,
            self.translation,
            self.copy_button,
        ]
        chain.extend(row.copy_button for row in self.engine_list.rows())
        for first, second in pairwise(chain):
            QWidget.setTabOrder(first, second)

    # ------------------------------------------------------------------ 显示与隐藏

    def is_shown(self) -> bool:
        return self.isVisible() and not self.isMinimized()

    def window_geometry(self) -> tuple[QPoint, QSize]:
        return (self.pos(), self.size())

    def restore_geometry(self, position: QPoint | None, size: QSize | None) -> None:
        if size is not None:
            self.resize(size)
        if position is not None:
            self.move(position)
            return
        primary = _primary_screen()
        if primary is not None:
            area = primary.availableGeometry()
            frame = self.frameGeometry()
            self.move(area.center() - frame.center() + frame.topLeft())

    def ensure_on_screen(self) -> None:
        """位置不在任何屏幕上时移到主屏中央（需求 BI-01）。"""
        primary = _primary_screen()
        if primary is None:
            return
        screens = [screen.availableGeometry() for screen in QGuiApplication.screens()]
        frame = self.frameGeometry()
        target = fit_on_screens(frame, screens, primary.availableGeometry())
        if target.size() != frame.size():
            # 外框与客户区的尺寸差就是边框和标题栏，缩小时保持不变。
            self.resize(self.size() - (frame.size() - target.size()))
        if target.topLeft() != frame.topLeft():
            self.move(target.topLeft())

    def present(self) -> None:
        self.ensure_on_screen()
        self._apply_backdrop()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        # 不显示在屏幕上的窗口（WA_DontShowOnScreen，例如原生平台上的测试）不去抢前台。
        if not self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            self.raise_()
            self.activateWindow()
        self.input_edit.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def conceal(self) -> None:
        self.hide()

    def _apply_backdrop(self) -> None:
        """首次显示前尝试 Mica 背景（需求 UI-01）。失败时 enable_mica 自己退回不透明背景。"""
        if self._backdrop_tried:
            return
        self._backdrop_tried = True
        if QGuiApplication.platformName() != "windows":
            return
        app = QApplication.instance()
        dark = is_dark(app) if isinstance(app, QApplication) else False
        enable_mica(self, dark=dark)

    def close_for_good(self) -> None:
        """模块停止时真正关闭窗口；其他情况下关闭按钮只隐藏窗口。"""
        self._allow_close = True
        self.close()

    # ------------------------------------------------------------------ 事件

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        actions = self._actions
        if actions is not None:
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and modifiers == _CTRL:
                actions.translate_now()
                event.accept()
                return
            if key == Qt.Key.Key_C and modifiers == _CTRL | _SHIFT:
                actions.copy_translation()
                event.accept()
                return
            # 交换、置顶、清空走按钮本身，与鼠标点击的路径完全相同（架构文档 11.6.4 节）。
            button = self._shortcut_buttons.get(key) if modifiers == _CTRL | _SHIFT else None
            if button is not None:
                button.click()
                event.accept()
                return
            if key == Qt.Key.Key_Escape and modifiers == Qt.KeyboardModifier.NoModifier:
                actions.hide_requested()
                event.accept()
                return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        # 标题栏的关闭按钮与 Alt+F4 只隐藏窗口，窗口状态由展示器保存。
        event.ignore()
        if self._actions is not None:
            self._actions.hide_requested()

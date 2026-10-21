"""首次运行引导的视图：顶层 QWidget，只负责绘制和收集输入（架构文档 11.1、11.7 节）。

视图按展示器给出的 OnboardingScreen 更新控件。模型行的集合不变时只更新文字和进度，不重建控件，
下载进度每秒刷新几次也不会闪烁，焦点也不会丢。
"""

from collections.abc import Callable, Sequence
from itertools import pairwise
from typing import cast

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QCloseEvent, QFont, QGuiApplication, QScreen
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from lst.ui.i18n import tr
from lst.ui.onboarding.presenter import ModelRow, OnboardingIntents, OnboardingScreen
from lst.ui.theme import TOKENS, enable_mica, is_dark

DEFAULT_SIZE = QSize(600, 520)


def _primary_screen() -> QScreen | None:
    # 类型桩把 primaryScreen 标成非空，没有屏幕时它返回 None。
    return cast(QScreen | None, QGuiApplication.primaryScreen())


class ModelRowWidget(QFrame):
    """模型列表的一行：名称、大小、状态、失败详情、进度条，以及“取消”“重试”按钮。"""

    def __init__(
        self, model_id: str, on_cancel: Callable[[str], None], on_retry: Callable[[str], None]
    ) -> None:
        super().__init__()
        self.model_id = model_id
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.title_label = QLabel()
        title_font = QFont(self.title_label.font())
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.size_label = QLabel()
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setMaximumHeight(TOKENS.spacing_s)
        self.cancel_button = QPushButton()
        self.retry_button = QPushButton()
        self.cancel_button.clicked.connect(lambda: on_cancel(self.model_id))
        self.retry_button.clicked.connect(lambda: on_retry(self.model_id))

        header = QHBoxLayout()
        header.setSpacing(TOKENS.spacing_s)
        header.addWidget(self.title_label)
        header.addStretch(1)
        header.addWidget(self.size_label)

        status = QHBoxLayout()
        status.setSpacing(TOKENS.spacing_s)
        status.addWidget(self.status_label, 1)
        status.addWidget(self.cancel_button)
        status.addWidget(self.retry_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(TOKENS.spacing_m, TOKENS.spacing_s, TOKENS.spacing_m, TOKENS.spacing_s)
        layout.setSpacing(TOKENS.spacing_xs)
        layout.addLayout(header)
        layout.addLayout(status)
        layout.addWidget(self.progress_bar)

    def update_row(self, row: ModelRow, cancel_text: str, retry_text: str) -> None:
        self.title_label.setText(row.title)
        self.size_label.setText(row.size)
        self.status_label.setText(row.status)
        self.progress_bar.setVisible(row.show_progress)
        if row.progress is None:
            self.progress_bar.setRange(0, 0)  # 没有刻度的忙碌指示
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(row.progress)
        self.progress_bar.setAccessibleName(tr("onboarding.row.progress.name", model=row.title))
        self.cancel_button.setText(cancel_text)
        self.cancel_button.setAccessibleName(tr("onboarding.row.cancel.name", model=row.title))
        self.cancel_button.setVisible(row.can_cancel)
        self.retry_button.setText(retry_text)
        self.retry_button.setAccessibleName(tr("onboarding.row.retry.name", model=row.title))
        self.retry_button.setVisible(row.can_retry)


class OnboardingWindow(QWidget):
    """首次运行引导窗口。控件以公开属性暴露，供测试检查。"""

    def __init__(self) -> None:
        super().__init__(None, Qt.WindowType.Window)
        # 常驻托盘程序的次要窗口，关闭它不应让程序退出。
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self._intents: OnboardingIntents | None = None
        self._allow_close = False
        self._backdrop_tried = False
        self._placed = False
        self._rows: list[ModelRowWidget] = []

        self.heading_label = QLabel()
        heading_font = QFont(self.heading_label.font())
        heading_font.setPointSizeF(heading_font.pointSizeF() * 1.6)
        heading_font.setBold(True)
        self.heading_label.setFont(heading_font)
        self.intro_label = QLabel()
        self.intro_label.setWordWrap(True)
        self.pair_label = QLabel()
        self.models_title = QLabel()
        models_font = QFont(self.models_title.font())
        models_font.setBold(True)
        self.models_title.setFont(models_font)
        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(TOKENS.spacing_s)
        self.notice_label = QLabel()
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.retry_index_button = QPushButton()
        self.download_button = QPushButton()
        self.ocr_note = QLabel()
        self.ocr_note.setWordWrap(True)
        self.finish_button = QPushButton()

        notice = QHBoxLayout()
        notice.setSpacing(TOKENS.spacing_s)
        notice.addWidget(self.notice_label, 1)
        notice.addWidget(self.retry_index_button, 0, Qt.AlignmentFlag.AlignTop)

        download = QHBoxLayout()
        download.addStretch(1)
        download.addWidget(self.download_button)

        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.finish_button)

        root = QVBoxLayout(self)
        root.setContentsMargins(TOKENS.spacing_xl, TOKENS.spacing_xl, TOKENS.spacing_xl, TOKENS.spacing_l)
        root.setSpacing(TOKENS.spacing_m)
        root.addWidget(self.heading_label)
        root.addWidget(self.intro_label)
        root.addWidget(self.pair_label)
        root.addSpacing(TOKENS.spacing_s)
        root.addWidget(self.models_title)
        root.addLayout(self.rows_layout)
        root.addLayout(notice)
        root.addLayout(download)
        root.addWidget(self.ocr_note)
        root.addStretch(1)
        root.addLayout(bottom)

        self.retry_index_button.hide()
        self.resize(DEFAULT_SIZE)

    # ------------------------------------------------------------------ 绑定展示器

    def bind(self, intents: OnboardingIntents) -> None:
        self._intents = intents
        self.download_button.clicked.connect(intents.download)
        self.retry_index_button.clicked.connect(intents.retry_index)
        self.finish_button.clicked.connect(intents.finish)

    def _cancel(self, model_id: str) -> None:
        if self._intents is not None:
            self._intents.cancel(model_id)

    def _retry(self, model_id: str) -> None:
        if self._intents is not None:
            self._intents.retry(model_id)

    # ------------------------------------------------------------------ 绘制

    def rows(self) -> list[ModelRowWidget]:
        """模型行，按显示顺序排列。"""
        return list(self._rows)

    def row(self, model_id: str) -> ModelRowWidget | None:
        return next((row for row in self._rows if row.model_id == model_id), None)

    def show_screen(self, screen: OnboardingScreen) -> None:
        self.setWindowTitle(screen.window_title)
        self.heading_label.setText(screen.heading)
        self.intro_label.setText(screen.intro)
        self.pair_label.setText(screen.pair)
        self.models_title.setText(screen.models_title)
        self._render_rows(screen.rows, screen.cancel_text, screen.retry_text)
        self.notice_label.setText(screen.notice)
        self.notice_label.setVisible(bool(screen.notice))
        self.notice_label.setProperty("error", screen.notice_is_error)
        self.retry_index_button.setText(screen.retry_index_text)
        self.retry_index_button.setVisible(screen.show_retry_index)
        self.download_button.setText(screen.download_text)
        self.download_button.setEnabled(screen.download_enabled)
        self.ocr_note.setText(screen.ocr_note)
        self.finish_button.setText(screen.finish_text)
        self.finish_button.setToolTip(screen.finish_tip)

    def _render_rows(self, rows: Sequence[ModelRow], cancel_text: str, retry_text: str) -> None:
        if [row.model_id for row in rows] != [widget.model_id for widget in self._rows]:
            for widget in self._rows:
                self.rows_layout.removeWidget(widget)
                widget.deleteLater()
            self._rows = [ModelRowWidget(row.model_id, self._cancel, self._retry) for row in rows]
            for widget in self._rows:
                self.rows_layout.addWidget(widget)
                # 窗口已经显示时，布局只会在下一轮 Qt 事件循环里显示新加入的控件，这里直接显示。
                widget.show()
            self._fix_tab_order()
        for widget, row in zip(self._rows, rows, strict=True):
            widget.update_row(row, cancel_text, retry_text)

    def _fix_tab_order(self) -> None:
        """Tab 顺序与视觉顺序一致（需求 UI-07）：各行的按钮、重试获取、下载、完成。"""
        chain: list[QWidget] = []
        for widget in self._rows:
            chain.extend((widget.cancel_button, widget.retry_button))
        chain.extend((self.retry_index_button, self.download_button, self.finish_button))
        for first, second in pairwise(chain):
            QWidget.setTabOrder(first, second)

    # ------------------------------------------------------------------ 显示与隐藏

    def is_shown(self) -> bool:
        return self.isVisible() and not self.isMinimized()

    def present(self) -> None:
        if not self._placed:
            self._placed = True
            primary = _primary_screen()
            if primary is not None:
                area = primary.availableGeometry()
                frame = self.frameGeometry()
                self.move(area.center() - frame.center() + frame.topLeft())
        self._apply_backdrop()
        if self.isMinimized():
            self.showNormal()
        else:
            self.show()
        # 不显示在屏幕上的窗口（WA_DontShowOnScreen，例如原生平台上的测试）不去抢前台。
        if not self.testAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen):
            self.raise_()
            self.activateWindow()
        target = self.download_button if self.download_button.isEnabled() else self.finish_button
        target.setFocus(Qt.FocusReason.ActiveWindowFocusReason)

    def conceal(self) -> None:
        self.hide()

    def _apply_backdrop(self) -> None:
        """首次显示前尝试 Mica 背景（需求 UI-01）。失败时退回不透明背景，否则窗口会渲染成黑色。"""
        if self._backdrop_tried:
            return
        self._backdrop_tried = True
        if QGuiApplication.platformName() != "windows":
            return
        app = QApplication.instance()
        dark = is_dark(app) if isinstance(app, QApplication) else False
        if not enable_mica(self, dark=dark):
            self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)

    def close_for_good(self) -> None:
        """模块停止时真正关闭窗口；其他情况下关闭按钮只隐藏窗口。"""
        self._allow_close = True
        self.close()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._allow_close:
            event.accept()
            return
        # 标题栏的关闭按钮只隐藏窗口，不记录“已完成”：下次启动时引导会再次出现。
        event.ignore()
        self.hide()

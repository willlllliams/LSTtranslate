"""设置窗口的视图：左侧导航、右侧页面、Mica 背景（需求 UI-01、UI-05、UI-07，架构文档 11.7 节）。

窗口只负责绘制和收集输入：页面从展示器读数据，用户的操作交给展示器，数据变化时展示器通过 ``Topic``
通知窗口，窗口再转给各页面。窗口在模块启动时创建并保持隐藏，收到 OpenSettings 时只做显示
（架构文档 11.2 节“窗口首次创建”，需求 6.1 节“设置窗口打开 ≤ 300 毫秒”）。

键盘（需求 UI-07）：Tab 顺序是导航列表、当前页面里从上到下的控件；Ctrl+1 至 Ctrl+7 切换页面，
Ctrl+Tab 与 Ctrl+Shift+Tab 依次切换；Esc 关闭窗口（只是隐藏）。显示、关闭、Mica 背景与屏幕位置的规则
与其他次要窗口相同（``lst.ui.common.window.SecondaryWindow``）。这些按键在 keyPressEvent 里处理，
不依赖 QShortcut 要求的“窗口处于激活状态”。
"""

from typing import Final

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QHideEvent,
    QIcon,
    QKeyEvent,
    QPainter,
    QPaintEvent,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
)

from lst.ui.common.window import SecondaryWindow
from lst.ui.i18n import tr
from lst.ui.settings.cards import CardBuilder
from lst.ui.settings.controls import ThemedLabel
from lst.ui.settings.dialogs import Dialogs, QtDialogs
from lst.ui.settings.page_about import AboutPage
from lst.ui.settings.page_capture import CapturePage
from lst.ui.settings.page_engines import EnginesPage
from lst.ui.settings.page_models import ModelsPage
from lst.ui.settings.pages import GeneralPage, HotkeysPage, RacePage, SettingsPage
from lst.ui.settings.presenter import Location, SettingsPresenter, Topic
from lst.ui.settings.state import WindowStateStore
from lst.ui.theme import TOKENS, fluent_icon, theme_colors, ui_font

DEFAULT_SIZE: Final = QSize(1120, 760)
MINIMUM_SIZE: Final = QSize(820, 520)

_CTRL = Qt.KeyboardModifier.ControlModifier
_SHIFT = Qt.KeyboardModifier.ShiftModifier


def describe_location(location: Location) -> str:
    """配置位置的显示文字，例如 race › mode、engines › 第 3 项 › priority。"""
    if not location:
        return tr("settings.location.root")
    parts = [
        tr("settings.location.index", n=part + 1) if isinstance(part, int) else part for part in location
    ]
    return " › ".join(parts)


class NavDelegate(QStyledItemDelegate):
    """导航项的绘制：图标加文字，悬停与选中有圆角背景，选中项左侧有强调色竖条（与 Windows 11 的“设置”一致）。

    颜色取自 ui.theme，每次绘制时读取，切换深浅色或强调色后立即正确。
    """

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:
        del index
        return QSize(option.rect.width(), TOKENS.nav_item_height)

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        colors = theme_colors()
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(option.rect).adjusted(TOKENS.spacing_s, 2, -TOKENS.spacing_s, -2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colors.nav_selected if selected else colors.nav_hover)
            painter.drawRoundedRect(rect, TOKENS.radius_s, TOKENS.radius_s)
        if selected:
            bar = QRectF(rect.left(), rect.center().y() - 8, TOKENS.nav_indicator_width, 16)
            painter.setBrush(colors.accent)
            painter.drawRoundedRect(bar, TOKENS.nav_indicator_width / 2, TOKENS.nav_indicator_width / 2)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        left = rect.left() + TOKENS.spacing_l
        if isinstance(icon, QIcon):
            size = TOKENS.nav_icon_px
            target = QRect(int(left), int(rect.center().y() - size / 2), size, size)
            icon.paint(painter, target, Qt.AlignmentFlag.AlignCenter, QIcon.Mode.Normal)
        painter.setFont(ui_font(TOKENS.title_px))
        painter.setPen(colors.text)
        text_rect = QRectF(
            left + TOKENS.nav_icon_px + TOKENS.spacing_m, rect.top(), rect.right() - left, rect.height()
        )
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
        )
        painter.restore()


class SettingsWindow(SecondaryWindow):
    """设置窗口。控件以公开属性暴露，供测试检查。"""

    def __init__(
        self,
        presenter: SettingsPresenter,
        dialogs: Dialogs,
        store: WindowStateStore,
        *,
        debounce_ms: int,
    ) -> None:
        super().__init__()
        self._presenter = presenter
        self._store = store
        self._placed = False
        self._shown_once = False
        if isinstance(dialogs, QtDialogs):
            dialogs.parent = self

        self.title_label = ThemedLabel("primary", TOKENS.title_px + 6, semibold=True, display=True)
        self.nav = QListWidget()
        self.nav.setFrameShape(QFrame.Shape.NoFrame)
        self.nav.setIconSize(QSize(TOKENS.nav_icon_px, TOKENS.nav_icon_px))
        self.nav.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.nav.setFixedWidth(TOKENS.nav_width)
        self.nav.viewport().setAutoFillBackground(False)
        self.nav.setMouseTracking(True)
        self.nav.setItemDelegate(NavDelegate(self.nav))
        self.nav.setStyleSheet("QListWidget { background: transparent; outline: none; }")
        self.stack = QStackedWidget()
        self.stack.setAutoFillBackground(False)
        self.notice_label = ThemedLabel("critical", TOKENS.caption_px)
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notice_label.hide()

        builder = CardBuilder(presenter, dialogs, debounce_ms)
        self.pages: list[SettingsPage] = [
            GeneralPage(presenter, builder, dialogs),
            HotkeysPage(presenter, builder, dialogs),
            CapturePage(presenter, builder, dialogs),
            EnginesPage(presenter, builder, dialogs),
            RacePage(presenter, builder, dialogs),
            ModelsPage(presenter, builder, dialogs),
            AboutPage(presenter, builder, dialogs),
        ]
        for page in self.pages:
            page.finish()  # 页面构造完成之后才设置文案和取值，见 SettingsPage.finish
            self.stack.addWidget(page)
            self.nav.addItem(QListWidgetItem(fluent_icon(page.icon), ""))

        nav_column = QVBoxLayout()
        nav_column.setContentsMargins(0, TOKENS.spacing_xl, 0, TOKENS.spacing_m)
        nav_column.setSpacing(TOKENS.spacing_l)
        nav_column.addWidget(self.title_label)
        nav_column.addWidget(self.nav, 1)
        self.title_label.setContentsMargins(TOKENS.spacing_xl, 0, 0, 0)
        content_column = QVBoxLayout()
        content_column.setContentsMargins(0, 0, 0, TOKENS.spacing_s)
        content_column.setSpacing(0)
        content_column.addWidget(self.stack, 1)
        content_column.addWidget(self.notice_label)
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addLayout(nav_column)
        root.addLayout(content_column, 1)

        self.nav.currentRowChanged.connect(self._page_changed)
        presenter.subscribe(self._on_topic)
        self.retranslate()
        self.nav.setCurrentRow(0)
        self.setMinimumSize(MINIMUM_SIZE)
        self.resize(DEFAULT_SIZE)

    # ------------------------------------------------------------------ 页面

    @property
    def current_page(self) -> SettingsPage:
        return self.pages[max(0, self.stack.currentIndex())]

    def page(self, page_id: str) -> SettingsPage:
        """按页面标识取页面：general、hotkeys、capture、engines、race、models、about。"""
        return next(page for page in self.pages if page.page_id == page_id)

    def show_page(self, page_id: str) -> None:
        self.nav.setCurrentRow(self.pages.index(self.page(page_id)))

    def _page_changed(self, row: int) -> None:
        if not 0 <= row < len(self.pages):
            return
        self.stack.setCurrentIndex(row)
        self.pages[row].on_shown()
        if self._shown_once:
            self._store.save_page(row)

    # ------------------------------------------------------------------ 文案

    def retranslate(self) -> None:
        self.setWindowTitle(tr("settings.window.title"))
        self.title_label.setText(tr("settings.window.title"))
        self.nav.setAccessibleName(tr("settings.nav.label"))
        for number, page in enumerate(self.pages, start=1):
            item = self.nav.item(number - 1)
            name = tr(f"settings.nav.{page.page_id}")
            item.setText(name)
            item.setToolTip(tr("settings.nav.tip", name=name, number=number))
        self._update_notice()

    def _on_topic(self, topic: Topic) -> None:
        match topic:
            case Topic.OPEN:
                self.present()
                return
            case Topic.LANGUAGE:
                self.retranslate()
            case Topic.REJECTIONS:
                self._update_notice()
            case _:
                pass
        for page in self.pages:
            page.refresh(topic)
        if topic is Topic.CONFIG:
            self._update_notice()

    def unclaimed_rejections(self) -> list[tuple[Location, str]]:
        """没有任何卡片负责显示的 ConfigRejected（例如整节出错），由窗口底部的提示条显示。"""
        return [
            (location, reason)
            for location, reason in self._presenter.rejections.items()
            if not any(page.claims(location) for page in self.pages)
        ]

    def _update_notice(self) -> None:
        lines = [
            tr("settings.notice.rejected", where=describe_location(location), reason=reason)
            for location, reason in self.unclaimed_rejections()
        ]
        self.notice_label.setText("\n".join(lines))
        self.notice_label.setContentsMargins(TOKENS.page_margin, TOKENS.spacing_xs, TOKENS.page_margin, 0)
        self.notice_label.setVisible(bool(lines))

    # ------------------------------------------------------------------ 显示与隐藏

    def present(self) -> None:
        """显示并激活窗口。窗口已经显示时再次打开只是把它带到前面并给导航列表焦点。"""
        if not self._placed:
            self._placed = True
            self._restore_state()
        self.present_window(self.nav)
        self._shown_once = True

    def _restore_state(self) -> None:
        state = self._store.load()
        self.restore_geometry(state.position, state.size)
        if state.page is not None and 0 <= state.page < len(self.pages):
            self.nav.setCurrentRow(state.page)

    def flush(self) -> None:
        """把还没提交的输入提交出去。"""
        for page in self.pages:
            page.flush()

    # ------------------------------------------------------------------ 事件

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if modifiers == _CTRL and int(Qt.Key.Key_1.value) <= key <= int(Qt.Key.Key_9.value):
            row = key - int(Qt.Key.Key_1.value)
            if row < len(self.pages):
                self.nav.setCurrentRow(row)
                event.accept()
                return
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and modifiers & _CTRL:
            step = -1 if (key == Qt.Key.Key_Backtab or modifiers & _SHIFT) else 1
            self.nav.setCurrentRow((self.nav.currentRow() + step) % len(self.pages))
            event.accept()
            return
        if key == Qt.Key.Key_Escape and modifiers == Qt.KeyboardModifier.NoModifier:
            self.hide()
            event.accept()
            return
        super().keyPressEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        """Mica 生效时内容区透明，由系统绘制背景；不可用时用 ui.theme 的底色填充。"""
        if self.mica_active:
            return
        painter = QPainter(self)
        painter.fillRect(self.rect(), theme_colors().window)

    def hideEvent(self, event: QHideEvent) -> None:
        # 窗口隐藏时提交还在防抖等待中的输入，并记住位置和大小。
        if self._shown_once:
            self.flush()
            self._store.save_geometry(self.pos(), self.size())
        super().hideEvent(event)

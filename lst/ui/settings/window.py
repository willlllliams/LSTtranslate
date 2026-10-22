"""设置窗口的视图：左侧两级导航、右侧页面、Mica 背景（需求 UI-01、UI-05、UI-07，架构文档 11.7 节）。

窗口只负责绘制和收集输入：页面从展示器读数据，用户的操作交给展示器，数据变化时展示器通过 ``Topic``
通知窗口，窗口再转给各页面。窗口在模块启动时创建并保持隐藏，收到 OpenSettings 时只做显示
（架构文档 11.2 节“窗口首次创建”，需求 6.1 节“设置窗口打开 ≤ 300 毫秒”）。

导航是两级菜单（``nav.NavTree``）：七个一级项，其中“热键与键位”“截屏与识别”“模型”有子页。
点有子项的一级项展开它并显示第一个子页，同一时间只展开当前页所在的分支。页面标识：一级页面是 ``general``
这样的名字，二级页面是 ``capture.paddle``、``hotkeys.overlay`` 这样的“一级.二级”。
``show_page`` 接受二级标识，也接受旧版本的一级标识（``hotkeys``、``capture``、``models``），
映射到该分支的第一个子页，例如 ``hotkeys`` 映射到 ``hotkeys.global``。
上次打开的页面按页面标识保存，旧版本保存的页面序号读取时同样映射。

键盘（需求 UI-07）：Tab 顺序是导航树、当前页面里从上到下的控件；Ctrl+1 至 Ctrl+7 切到第 n 个一级项
（有子项时到它的第一个子页）；Ctrl+Tab 与 Ctrl+Shift+Tab 按子页的显示顺序循环切换（没有子项的一级项算一页）；
在导航树里上下键移动、右键展开、左键收起；Esc 关闭窗口（只是隐藏）。显示、关闭、Mica 背景与屏幕位置的规则
与其他次要窗口相同（``lst.ui.common.window.SecondaryWindow``）。这些按键在 keyPressEvent 里处理，
不依赖 QShortcut 要求的“窗口处于激活状态”。

配置被拒绝（ConfigRejected）时，错误显示在负责那个位置的卡片上。窗口可见、而当前页没有卡片负责它时，
自动切到负责它的子页，例如 [bindings] 节里覆盖窗口操作的字段被拒绝时切到 ``hotkeys.overlay``。
"""

from typing import Final

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QHideEvent, QKeyEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout

from lst.contracts.ocr_engines import OcrEngineKind
from lst.ui.common.bindings import Scope
from lst.ui.common.window import SecondaryWindow
from lst.ui.i18n import tr
from lst.ui.settings.cards import Binding, CardBuilder
from lst.ui.settings.controls import ThemedLabel
from lst.ui.settings.dialogs import Dialogs, QtDialogs
from lst.ui.settings.nav import (
    NAV_GROUPS,
    NavDelegate,
    NavTree,
    leaf_ids,
    legacy_page_id,
    resolve_page_id,
)
from lst.ui.settings.page_about import AboutPage
from lst.ui.settings.page_bindings import BindingsPage
from lst.ui.settings.page_capture import CaptureEnginesPage, CaptureOptionsPage
from lst.ui.settings.page_engines import EnginesPage
from lst.ui.settings.page_models import DictsPage, GpuPage, NmtModelsPage
from lst.ui.settings.page_ocr_engine import NormalizePage, OcrEnginePage
from lst.ui.settings.pages import GeneralPage, GlobalHotkeysPage, RacePage, SettingsPage
from lst.ui.settings.presenter import Location, SettingsPresenter, Topic
from lst.ui.settings.state import WindowStateStore
from lst.ui.theme import TOKENS, theme_colors

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
        self._current_id = ""
        self._revealed: set[Location] = set()
        if isinstance(dialogs, QtDialogs):
            dialogs.parent = self

        self.title_label = ThemedLabel("primary", TOKENS.title_px + 6, semibold=True, display=True)
        self.nav = NavTree()
        self.nav.setFixedWidth(TOKENS.nav_width)
        self.stack = QStackedWidget()
        self.stack.setAutoFillBackground(False)
        self.notice_label = ThemedLabel("critical", TOKENS.caption_px)
        self.notice_label.setWordWrap(True)
        self.notice_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.notice_label.hide()

        builder = CardBuilder(presenter, dialogs, debounce_ms)
        self.pages: list[SettingsPage] = [
            GeneralPage(presenter, builder, dialogs),
            GlobalHotkeysPage(presenter, builder, dialogs),
            *(BindingsPage(scope, presenter, builder, dialogs) for scope in Scope),
            CaptureEnginesPage(presenter, builder, dialogs),
            *(OcrEnginePage(kind, presenter, builder, dialogs) for kind in OcrEngineKind),
            NormalizePage(presenter, builder, dialogs),
            CaptureOptionsPage(presenter, builder, dialogs),
            EnginesPage(presenter, builder, dialogs),
            RacePage(presenter, builder, dialogs),
            NmtModelsPage(presenter, builder, dialogs),
            GpuPage(presenter, builder, dialogs),
            DictsPage(presenter, builder, dialogs),
            AboutPage(presenter, builder, dialogs),
        ]
        if [page.page_id for page in self.pages] != leaf_ids():
            raise RuntimeError("设置页面的顺序与导航菜单不一致")
        self._by_id = {page.page_id: page for page in self.pages}
        for page in self.pages:
            page.navigate = self.show_page
            page.finish()  # 页面构造完成之后才设置文案和取值，见 SettingsPage.finish
            self.stack.addWidget(page)

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

        self.nav.page_chosen.connect(self._activate)
        presenter.subscribe(self._on_topic)
        self.retranslate()
        self._activate(self.pages[0].page_id)
        self.setMinimumSize(MINIMUM_SIZE)
        self.resize(DEFAULT_SIZE)

    # ------------------------------------------------------------------ 页面

    @property
    def current_page(self) -> SettingsPage:
        return self._by_id[self._current_id]

    def page(self, page_id: str) -> SettingsPage:
        """按页面标识取页面，如 general、hotkeys.overlay、capture.paddle、models.gpu。

        旧的一级标识 hotkeys、capture、models 给出该分支的第一个子页。
        """
        resolved = resolve_page_id(page_id)
        if resolved is None:
            raise KeyError(page_id)
        return self._by_id[resolved]

    def show_page(self, page_id: str) -> None:
        self._activate(self.page(page_id).page_id)

    def _activate(self, page_id: str) -> None:
        """切换到页面 page_id，并让导航树的展开与选中状态与它一致。"""
        self.nav.show_leaf(page_id)
        if page_id == self._current_id:
            return
        self._current_id = page_id
        page = self._by_id[page_id]
        self.stack.setCurrentWidget(page)
        page.on_shown()
        if self._shown_once:
            self._store.save_page(page_id)

    def page_for_location(self, location: Location) -> SettingsPage | None:
        """负责显示这个配置位置的页面。

        当前页负责时就是当前页。否则在负责的页面里优先取当前 OCR 引擎的子页（几个引擎共用的 [ocr] 节字段，
        例如文本行方向，同时出现在 RapidOCR 与 PaddleOCR 子页上），再按菜单顺序取第一个。
        """
        if self.current_page.claims(location):
            return self.current_page
        claimants = [page for page in self.pages if page.claims(location)]
        engine = self._presenter.current_ocr_engine
        preferred = [page for page in claimants if isinstance(page, OcrEnginePage) and page.kind is engine]
        return next(iter(preferred or claimants), None)

    def show_location(self, location: Location) -> Binding | None:
        """切到负责这个配置位置的页面，并把对应的卡片滚动到可见处。返回那张卡片的绑定，没有页面负责时不动。"""
        page = self.page_for_location(location)
        if page is None:
            return None
        self.show_page(page.page_id)
        binding = next((b for b in page.all_bindings() if b.matches(location)), None)
        if binding is not None:
            page.ensureWidgetVisible(binding.card)
        return binding

    # ------------------------------------------------------------------ 文案

    def retranslate(self) -> None:
        self.setWindowTitle(tr("settings.window.title"))
        self.title_label.setText(tr("settings.window.title"))
        self.nav.setAccessibleName(tr("settings.nav.label"))
        self.nav.retranslate()
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
        if topic is Topic.REJECTIONS:
            self._reveal_rejections()

    def _reveal_rejections(self) -> None:
        """新出现的 ConfigRejected 落在别的子页上时，切到那个子页（窗口可见时）。"""
        fresh = [location for location in self._presenter.rejections if location not in self._revealed]
        self._revealed = set(self._presenter.rejections)
        if not self.isVisible() or not fresh:
            return
        if any(self.current_page.claims(location) for location in fresh):
            return
        for location in fresh:
            if self.page_for_location(location) is not None:
                self.show_location(location)
                return

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
        """显示并激活窗口。窗口已经显示时再次打开只是把它带到前面并给导航树焦点。"""
        if not self._placed:
            self._placed = True
            self._restore_state()
        self.present_window(self.nav)
        self._shown_once = True

    def _restore_state(self) -> None:
        state = self._store.load()
        self.restore_geometry(state.position, state.size)
        page_id = resolve_page_id(state.page_id) if state.page_id is not None else None
        if page_id is None and state.page is not None:
            page_id = legacy_page_id(state.page)
        if page_id is not None:
            self.show_page(page_id)

    def flush(self) -> None:
        """把还没提交的输入提交出去。"""
        for page in self.pages:
            page.flush()

    # ------------------------------------------------------------------ 事件

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if modifiers == _CTRL and int(Qt.Key.Key_1.value) <= key <= int(Qt.Key.Key_9.value):
            number = key - int(Qt.Key.Key_1.value)
            if number < len(NAV_GROUPS):
                self.show_page(NAV_GROUPS[number].first)
                event.accept()
                return
        if key in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab) and modifiers & _CTRL:
            step = -1 if (key == Qt.Key.Key_Backtab or modifiers & _SHIFT) else 1
            order = leaf_ids()
            self.show_page(order[(order.index(self._current_id) + step) % len(order)])
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


__all__ = ["NavDelegate", "SettingsWindow", "describe_location"]

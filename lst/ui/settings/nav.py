"""设置窗口左侧的两级导航：菜单结构、旧页面标识的映射和树控件（需求 UI-05、UI-07，架构文档 11.7 节）。

菜单分两级。一级项有图标；有子项的一级项（热键与键位、截屏与识别、模型）不对应页面，点击后展开并显示它的第一个子页。
没有子项的一级项直接对应一个页面。页面标识：一级页面用 ``general`` 这样的名字，二级页面用
``<一级>.<二级>`` 的形式，例如 ``capture.paddle``、``hotkeys.overlay``。

同一时间只展开当前页所在的分支。树里的键盘操作：

- 上下键在“可停留的行”之间移动：已展开的一级项由它的子项代表，不单独停留；
- 右键展开当前分支，左键收起当前分支并回到它的一级项（页面保持不变）；
- 移到一个收起的一级项上，等同于点它：展开并显示第一个子页。

旧版本只有七个一级页面，保存的页面序号与旧的页面标识（``hotkeys``、``capture``、``models`` 等）仍然有效：
``resolve_page_id`` 把它们映射到该分支的第一个子页。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QIcon, QKeyEvent, QMouseEvent, QPainter
from PySide6.QtWidgets import (
    QFrame,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
)

from lst.ui.i18n import tr
from lst.ui.settings import strings_nav  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
from lst.ui.theme import TOKENS, Glyph, fluent_icon, fluent_icon_font, theme_colors, ui_font

CHEVRON_RIGHT: Final = "\ue76c"
"""Fluent 图标里向右的箭头：分支收起。"""
CHEVRON_DOWN: Final = "\ue70d"
"""向下的箭头：分支展开。"""


@dataclass(frozen=True, slots=True)
class NavGroup:
    """一个一级项。children 为空时它自己就是页面。"""

    id: str
    icon: str
    children: tuple[str, ...] = ()

    @property
    def first(self) -> str:
        """点击这个一级项时显示的页面。"""
        return self.children[0] if self.children else self.id


NAV_GROUPS: Final = (
    NavGroup("general", Glyph.SETTINGS),
    NavGroup(
        "hotkeys",
        Glyph.KEYBOARD,
        ("hotkeys.global", "hotkeys.capture", "hotkeys.overlay", "hotkeys.bilingual"),
    ),
    NavGroup(
        "capture",
        Glyph.SCREEN_CAPTURE,
        (
            "capture.engines",
            "capture.windows",
            "capture.rapid",
            "capture.paddle",
            "capture.normalize",
            "capture.options",
        ),
    ),
    NavGroup("engines", Glyph.TRANSLATE),
    NavGroup("race", Glyph.LIGHTNING),
    NavGroup("models", Glyph.PACKAGE, ("models.nmt", "models.gpu", "models.dicts")),
    NavGroup("about", Glyph.INFO),
)
"""菜单的一级项，按显示顺序。Ctrl+1 至 Ctrl+7 对应这里的第 1 至第 7 项。"""

LEGACY_PAGE_ORDER: Final = ("general", "hotkeys", "capture", "engines", "race", "models", "about")
"""只有七个一级页面的旧版本里，页面序号对应的页面标识。"""


def leaf_ids() -> list[str]:
    """全部页面的标识，按菜单从上到下的顺序（Ctrl+Tab 循环的顺序）。"""
    return [leaf for group in NAV_GROUPS for leaf in (group.children or (group.id,))]


def group_of(page_id: str) -> NavGroup | None:
    """页面所属的一级项；page_id 是一级项自己的标识时也返回它。"""
    for group in NAV_GROUPS:
        if page_id == group.id or page_id in group.children:
            return group
    return None


def resolve_page_id(page_id: str) -> str | None:
    """把页面标识（二级页面或旧的一级标识）映射成页面的标识；不认识的标识返回 None。"""
    group = group_of(page_id)
    if group is None:
        return None
    return page_id if page_id in group.children else group.first


def legacy_page_id(index: int) -> str | None:
    """旧版本保存的页面序号对应的页面标识；序号越界时为 None。"""
    if 0 <= index < len(LEGACY_PAGE_ORDER):
        return resolve_page_id(LEGACY_PAGE_ORDER[index])
    return None


def nav_name(page_id: str) -> str:
    """菜单里的名称。"""
    return tr(f"settings.nav.{page_id}")


def nav_title(page_id: str) -> str:
    """页面标题：二级页面写成“一级 › 二级”，一级页面只写自己的名称。"""
    group = group_of(page_id)
    if group is None or page_id == group.id:
        return nav_name(page_id)
    return f"{nav_name(group.id)} › {nav_name(page_id)}"


class NavDelegate(QStyledItemDelegate):
    """导航项的绘制：一级项是图标加文字，二级项缩进、没有图标；悬停与选中有圆角背景，选中项左侧有强调色竖条
    （与 Windows 11 的“设置”一致）。有子项的一级项右侧画一个展开或收起的箭头。

    颜色取自 ui.theme，每次绘制时读取，切换深浅色或强调色后立即正确。
    """

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:
        height = TOKENS.nav_item_height - (6 if index.parent().isValid() else 0)
        return QSize(option.rect.width(), height)

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        colors = theme_colors()
        child = index.parent().isValid()
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
        left = rect.left() + TOKENS.spacing_l
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon) and not child:
            size = TOKENS.nav_icon_px
            target = QRectF(left, rect.center().y() - size / 2, size, size).toRect()
            icon.paint(painter, target, Qt.AlignmentFlag.AlignCenter, QIcon.Mode.Normal)
        text_left = left + TOKENS.nav_icon_px + TOKENS.spacing_m + (TOKENS.spacing_m if child else 0)
        painter.setFont(ui_font(TOKENS.title_px - 1 if child else TOKENS.title_px))
        painter.setPen(colors.text if not child or selected else colors.text_secondary)
        text_rect = QRectF(text_left, rect.top(), rect.right() - text_left - TOKENS.spacing_xl, rect.height())
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            str(index.data(Qt.ItemDataRole.DisplayRole) or ""),
        )
        if index.model().hasChildren(index):
            expanded = bool(option.state & QStyle.StateFlag.State_Open)
            painter.setFont(fluent_icon_font(TOKENS.caption_px))
            painter.setPen(colors.text_secondary)
            arrow = QRectF(rect.right() - TOKENS.spacing_xl, rect.top(), TOKENS.spacing_l, rect.height())
            painter.drawText(
                arrow,
                int(Qt.AlignmentFlag.AlignCenter),
                CHEVRON_DOWN if expanded else CHEVRON_RIGHT,
            )
        painter.restore()


class NavTree(QTreeWidget):
    """两级导航树。用户通过点击或键盘选了一个页面时发出 ``page_chosen``（页面标识）。

    窗口收到后切换页面，再调用 ``show_leaf`` 把树的展开与选中状态对齐；``show_leaf`` 自己不发信号。
    """

    page_chosen = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._items: dict[str, QTreeWidgetItem] = {}
        self._leaf = ""
        self._syncing = False
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setRootIsDecorated(False)
        self.setIndentation(0)
        self.setItemsExpandable(False)
        self.setExpandsOnDoubleClick(False)
        self.setUniformRowHeights(False)
        self.setAnimated(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setIconSize(QSize(TOKENS.nav_icon_px, TOKENS.nav_icon_px))
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.viewport().setAutoFillBackground(False)
        self.setMouseTracking(True)
        self.setItemDelegate(NavDelegate(self))
        self.setStyleSheet("QTreeWidget { background: transparent; outline: none; }")
        for group in NAV_GROUPS:
            top = QTreeWidgetItem(self)
            top.setData(0, Qt.ItemDataRole.UserRole, group.id)
            top.setIcon(0, fluent_icon(group.icon))
            self._items[group.id] = top
            for leaf in group.children:
                item = QTreeWidgetItem(top)
                item.setData(0, Qt.ItemDataRole.UserRole, leaf)
                self._items[leaf] = item
        self.currentItemChanged.connect(self._current_changed)

    # ------------------------------------------------------------------ 查询

    def item_of(self, page_id: str) -> QTreeWidgetItem:
        return self._items[page_id]

    @property
    def current_leaf(self) -> str:
        return self._leaf

    def top_items(self) -> list[QTreeWidgetItem]:
        return [
            item for row in range(self.topLevelItemCount()) if (item := self.topLevelItem(row)) is not None
        ]

    @staticmethod
    def children_of(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
        return [child for row in range(item.childCount()) if (child := item.child(row)) is not None]

    def visible_ids(self) -> list[str]:
        """当前看得见的行的标识，从上到下。"""
        ids: list[str] = []
        for top in self.top_items():
            ids.append(_id(top))
            if top.isExpanded():
                ids.extend(_id(child) for child in self.children_of(top))
        return ids

    def is_expanded(self, page_id: str) -> bool:
        return self._items[page_id].isExpanded()

    # ------------------------------------------------------------------ 状态

    def retranslate(self) -> None:
        for number, group in enumerate(NAV_GROUPS, start=1):
            name = nav_name(group.id)
            top = self._items[group.id]
            top.setText(0, name)
            top.setToolTip(0, tr("settings.nav.tip", name=name, number=number))
            for leaf in group.children:
                self._items[leaf].setText(0, nav_name(leaf))
                self._items[leaf].setToolTip(0, nav_title(leaf))

    def show_leaf(self, page_id: str) -> None:
        """选中一个页面并只展开它所在的分支。不发 ``page_chosen``。"""
        item = self._items[page_id]
        self._syncing = True
        try:
            for top in self.top_items():
                top.setExpanded(top is item.parent())
            self._leaf = page_id
            self.setCurrentItem(item)
        finally:
            self._syncing = False

    def _collapse_branch(self, top: QTreeWidgetItem) -> None:
        """收起当前分支；高亮回到它的一级项，页面不变。"""
        self._syncing = True
        try:
            top.setExpanded(False)
            self.setCurrentItem(top)
        finally:
            self._syncing = False

    def _current_changed(self, current: QTreeWidgetItem | None, _previous: QTreeWidgetItem | None) -> None:
        if self._syncing or current is None:
            return
        page_id = _id(current)
        if current.childCount() == 0:
            self.page_chosen.emit(page_id)
            return
        children = self.children_of(current)
        branch = _id(children[0])
        shown = self._items.get(self._leaf)
        if shown is not None and shown.parent() is current:
            # 选中了当前页所在分支的一级项：已展开时高亮回到当前页，已收起时保持在一级项上。
            if current.isExpanded():
                self.show_leaf(self._leaf)
            return
        self.page_chosen.emit(branch)

    # ------------------------------------------------------------------ 键盘与鼠标

    def _stops(self) -> list[QTreeWidgetItem]:
        stops: list[QTreeWidgetItem] = []
        for top in self.top_items():
            if top.childCount() and top.isExpanded():
                stops.extend(self.children_of(top))
            else:
                stops.append(top)
        return stops

    def _move(self, target: int | None = None, step: int = 0) -> None:
        stops = self._stops()
        current = self.currentItem()
        if not stops:
            return
        position = stops.index(current) if current in stops else 0
        index = target if target is not None else position + step
        index = max(0, min(len(stops) - 1, index))
        if stops[index] is not current:
            self.setCurrentItem(stops[index])

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier:
            super().keyPressEvent(event)
            return
        key = event.key()
        current = self.currentItem()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            self._move(step=-1 if key == Qt.Key.Key_Up else 1)
        elif key in (Qt.Key.Key_Home, Qt.Key.Key_End):
            self._move(target=0 if key == Qt.Key.Key_Home else len(self._stops()) - 1)
        elif key == Qt.Key.Key_Left and current is not None:
            top = current.parent()
            if top is not None:
                self._collapse_branch(top)
        elif key == Qt.Key.Key_Right and current is not None:
            if current.childCount() and not current.isExpanded():
                self.show_leaf(self._leaf)
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        item = self.itemAt(event.position().toPoint())
        if item is not None and item.childCount() and event.button() == Qt.MouseButton.LeftButton:
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            self.page_chosen.emit(_id(self.children_of(item)[0]))
            event.accept()
            return
        super().mousePressEvent(event)


def _id(item: QTreeWidgetItem) -> str:
    return str(item.data(0, Qt.ItemDataRole.UserRole))


def nav_ids(groups: Sequence[NavGroup] = NAV_GROUPS) -> list[str]:
    """菜单里全部项（一级与二级）的标识，按显示顺序。"""
    return [item for group in groups for item in (group.id, *group.children)]


__all__ = [
    "CHEVRON_DOWN",
    "CHEVRON_RIGHT",
    "LEGACY_PAGE_ORDER",
    "NAV_GROUPS",
    "NavDelegate",
    "NavGroup",
    "NavTree",
    "group_of",
    "leaf_ids",
    "legacy_page_id",
    "nav_ids",
    "nav_name",
    "nav_title",
    "resolve_page_id",
]

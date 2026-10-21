"""托盘的视图：系统托盘图标、右键菜单、通知（架构文档 11.1 节）。

视图只负责 Qt 对象：按展示器给出的 TrayMenu 更新菜单条目，把用户操作交给 TrayIntents。
状态区出错的条目带一个子菜单，列出原因和处理建议：鼠标悬停时子菜单自动展开（需求 TR-03），
用键盘打开托盘菜单时也能用方向键进入。
"""

import logging
from collections.abc import Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Final

from PySide6.QtCore import QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from lst.contracts.events import NoticeLevel
from lst.ui.tray.presenter import MenuEntry, Notice, StatusLine, TrayAction, TrayIntents, TrayMenu

NOTICE_DURATION_MS: Final = 10_000
"""通知的显示时长提示。Windows 按系统的辅助功能设置决定实际时长，这个值只是建议。"""

MESSAGE_ICONS: Final = {
    NoticeLevel.INFO: QSystemTrayIcon.MessageIcon.Information,
    NoticeLevel.WARNING: QSystemTrayIcon.MessageIcon.Warning,
    NoticeLevel.ERROR: QSystemTrayIcon.MessageIcon.Critical,
}
"""通知级别到托盘通知图标的映射。"""

_FALLBACK_SIZES: Final = (16, 20, 24, 32, 40, 48, 64, 128, 256)
_FALLBACK_COLOR: Final = "#0F6CBD"


def _escape(text: str) -> str:
    """转义 Qt 菜单文本里的 &，避免引擎名之类的纯文本被当成助记符。"""
    return text.replace("&", "&&")


def _entry_text(entry: MenuEntry) -> str:
    # 制表符后面的部分由 QMenu 右对齐显示在快捷键位置，只作提示，不绑定快捷键。
    return f"{entry.text}\t{_escape(entry.shortcut)}" if entry.shortcut else entry.text


# ---------------------------------------------------------------- 图标


def load_icon(path: Path | None, logger: logging.Logger) -> QIcon:
    """读取托盘图标；文件不存在或无法解码时改用程序绘制的图标。"""
    if path is not None and path.is_file():
        icon = QIcon(str(path))
        if not icon.isNull() and icon.availableSizes():
            return icon
        logger.warning("托盘图标 %s 无法读取，改用内置图标", path)
    else:
        logger.warning("托盘图标文件不存在：%s，改用内置图标", path)
    return fallback_icon()


def fallback_icon() -> QIcon:
    """程序绘制的托盘图标：蓝色圆角方块上三条白色横线，表示一页文字。各常用尺寸分别绘制，缩放时不模糊。"""
    icon = QIcon()
    for size in _FALLBACK_SIZES:
        icon.addPixmap(_draw_fallback(size))
    return icon


def _draw_fallback(size: int) -> QPixmap:
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(_FALLBACK_COLOR))
        margin = size * 0.06
        side = size - 2 * margin
        painter.drawRoundedRect(QRectF(margin, margin, side, side), size * 0.2, size * 0.2)
        painter.setBrush(QColor("white"))
        bar = max(size * 0.1, 1.5)
        left = size * 0.24
        for index, width in enumerate((0.52, 0.52, 0.34)):
            top = size * (0.27 + 0.19 * index)
            painter.drawRoundedRect(QRectF(left, top, size * width, bar), bar / 2, bar / 2)
    finally:
        painter.end()
    return pixmap


# ---------------------------------------------------------------- 计时器


class QtOneShotTimer:
    """基于 QTimer 的单次计时器，满足展示器的 OneShotTimer 协议。回调在主线程执行。"""

    def __init__(self, callback: Callable[[], None]) -> None:
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(callback)

    def start(self, msec: int, /) -> None:
        self._timer.start(msec)

    def stop(self) -> None:
        self._timer.stop()


# ---------------------------------------------------------------- 视图


class QtTrayView:
    """托盘图标与菜单。满足展示器的 TrayView 协议。

    系统托盘不可用时（例如 offscreen 平台）对象照常创建，菜单照常更新，只是图标和通知不会显示。
    菜单结构不变时，状态更新只改条目文字，不重建菜单，菜单打开期间刷新也不会闪烁。
    """

    def __init__(
        self,
        app_name: str,
        icon: QIcon,
        *,
        tray_available: bool,
        logger: logging.Logger,
    ) -> None:
        self._logger = logger
        self._tray_available = tray_available
        self._intents: TrayIntents | None = None
        self._menu = QMenu()
        self._menu.aboutToShow.connect(self._on_about_to_show)
        # 状态区与命令区之间的分隔线；状态条目都插在它前面。
        self._status_separator = self._menu.addSeparator()
        self._status_actions: list[QAction] = []
        self._status_submenus: list[QMenu | None] = []
        self._status_shape: tuple[tuple[str, int], ...] = ()
        self._command_items: list[QAction] = []
        self._command_actions: dict[TrayAction, QAction] = {}
        self._layout: tuple[TrayAction | None, ...] = ()
        self._tray = QSystemTrayIcon(icon)
        self._tray.setToolTip(app_name)
        self._tray.setContextMenu(self._menu)
        self._tray.activated.connect(self._on_activated)

    @property
    def tray(self) -> QSystemTrayIcon:
        return self._tray

    @property
    def menu(self) -> QMenu:
        return self._menu

    @property
    def tray_available(self) -> bool:
        return self._tray_available

    def command_action(self, kind: TrayAction) -> QAction:
        """命令区里 kind 对应的菜单条目。第一次 render 之前调用会抛出 KeyError。"""
        return self._command_actions[kind]

    def status_actions(self) -> list[QAction]:
        """状态区的菜单条目，按显示顺序排列。"""
        return list(self._status_actions)

    def bind(self, intents: TrayIntents) -> None:
        """登记接收用户操作的展示器。"""
        self._intents = intents

    def show(self) -> None:
        """显示托盘图标。系统托盘不可用时 Qt 不会创建原生图标，调用没有副作用。"""
        self._tray.show()

    def close(self) -> None:
        """隐藏托盘图标并销毁托盘对象与菜单。之后不再把任何操作交给展示器。"""
        self._intents = None
        self._tray.hide()
        self._tray.deleteLater()
        self._menu.deleteLater()

    # ------------------------------------------------------------ TrayView

    def render(self, menu: TrayMenu) -> None:
        self._render_status(menu.status)
        self._render_entries(menu.entries)
        self._tray.setToolTip(menu.tooltip)

    def notify(self, notice: Notice) -> None:
        if not self._tray_available:
            self._logger.warning("系统托盘不可用，通知没有显示：%s", notice.title)
        self._tray.showMessage(notice.title, notice.message, MESSAGE_ICONS[notice.level], NOTICE_DURATION_MS)

    def open_folder(self, path: Path) -> bool:
        if not path.is_dir():
            return False
        return QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    # ------------------------------------------------------------ 状态区

    def _render_status(self, lines: Sequence[StatusLine]) -> None:
        shape = tuple((line.key, len(line.details)) for line in lines)
        if shape != self._status_shape:
            self._rebuild_status(lines)
            self._status_shape = shape
        for action, submenu, line in zip(self._status_actions, self._status_submenus, lines, strict=True):
            action.setText(_escape(line.text))
            if submenu is not None:
                for detail_action, text in zip(submenu.actions(), line.details, strict=True):
                    detail_action.setText(_escape(text))

    def _rebuild_status(self, lines: Sequence[StatusLine]) -> None:
        for action, submenu in zip(self._status_actions, self._status_submenus, strict=True):
            self._menu.removeAction(action)
            action.deleteLater()
            if submenu is not None:
                submenu.deleteLater()
        self._status_actions = []
        self._status_submenus = []
        for line in lines:
            action = QAction(self._menu)
            submenu: QMenu | None = None
            if line.details:
                # 出错的条目保持可用，悬停或按方向键时展开原因与处理建议。
                submenu = QMenu(self._menu)
                for _ in line.details:
                    detail = QAction(submenu)
                    detail.setEnabled(False)
                    submenu.addAction(detail)
                action.setMenu(submenu)
            else:
                action.setEnabled(False)
            self._menu.insertAction(self._status_separator, action)
            self._status_actions.append(action)
            self._status_submenus.append(submenu)

    # ------------------------------------------------------------ 命令区

    def _render_entries(self, entries: Sequence[MenuEntry | None]) -> None:
        layout = tuple(None if entry is None else entry.action for entry in entries)
        if layout != self._layout:
            self._rebuild_entries(layout)
            self._layout = layout
        for entry in entries:
            if entry is None:
                continue
            action = self._command_actions[entry.action]
            action.setText(_entry_text(entry))
            action.setEnabled(entry.enabled)

    def _rebuild_entries(self, layout: Sequence[TrayAction | None]) -> None:
        for item in self._command_items:
            self._menu.removeAction(item)
            item.deleteLater()
        self._command_items = []
        self._command_actions = {}
        for kind in layout:
            if kind is None:
                self._command_items.append(self._menu.addSeparator())
                continue
            action = QAction(self._menu)
            action.triggered.connect(partial(self._on_triggered, kind))
            self._menu.addAction(action)
            self._command_items.append(action)
            self._command_actions[kind] = action

    # ------------------------------------------------------------ 用户操作

    def _on_triggered(self, kind: TrayAction, checked: bool = False) -> None:
        if self._intents is not None:
            self._intents.choose(kind)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        # 只响应左键单击。双击时 Qt 先发 Trigger 再发 DoubleClick，只处理前者，双语窗口不会被切换两次。
        if reason is QSystemTrayIcon.ActivationReason.Trigger and self._intents is not None:
            self._intents.click_icon()

    def _on_about_to_show(self) -> None:
        if self._intents is not None:
            self._intents.menu_opening()

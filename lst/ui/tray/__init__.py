"""系统托盘：状态显示、命令菜单与通知（需求 TR-01 至 TR-05，架构文档 11.1 节）。

- strings：本模块的中英文文案，导入时登记到 ui.i18n；
- presenter：展示器与菜单模型，纯逻辑，不依赖 Qt；
- view：QSystemTrayIcon 与 QMenu；
- module：TrayModule，实现 kernel 的 Module 协议。
"""

from lst.ui.tray.module import TrayModule
from lst.ui.tray.presenter import (
    MENU_LAYOUT,
    WORKER_HEALTH,
    MenuEntry,
    Notice,
    StatusLine,
    TrayAction,
    TrayMenu,
    TrayPresenter,
    build_menu,
)

__all__ = [
    "MENU_LAYOUT",
    "WORKER_HEALTH",
    "MenuEntry",
    "Notice",
    "StatusLine",
    "TrayAction",
    "TrayMenu",
    "TrayModule",
    "TrayPresenter",
    "build_menu",
]

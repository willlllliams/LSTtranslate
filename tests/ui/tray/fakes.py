"""托盘测试用的假对象：展示器测试用的假视图与假计时器，Qt 测试用的通知与打开目录记录。"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QSystemTrayIcon

from lst.ui.tray.presenter import Notice, TrayMenu


class FakeView:
    """记录展示器对视图的全部调用。open_result 是 open_folder 的返回值。"""

    def __init__(self) -> None:
        self.menus: list[TrayMenu] = []
        self.notices: list[Notice] = []
        self.opened: list[Path] = []
        self.open_result = True

    @property
    def menu(self) -> TrayMenu:
        """最近一次渲染的菜单。"""
        return self.menus[-1]

    def render(self, menu: TrayMenu) -> None:
        self.menus.append(menu)

    def notify(self, notice: Notice) -> None:
        self.notices.append(notice)

    def open_folder(self, path: Path) -> bool:
        self.opened.append(path)
        return self.open_result


class FakeTimer:
    """手动触发的单次计时器：start 只记录时长，fire 模拟到时。"""

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self.starts: list[int] = []
        self.active = False

    def start(self, msec: int, /) -> None:
        self.starts.append(msec)
        self.active = True

    def stop(self) -> None:
        self.active = False

    def fire(self) -> None:
        assert self.active, "计时器没有在计时"
        self.active = False
        self._callback()


@dataclass(frozen=True, slots=True)
class ShownMessage:
    """QSystemTrayIcon.showMessage 的一次调用。"""

    title: str
    message: str
    icon: QSystemTrayIcon.MessageIcon
    msecs: int


@dataclass
class DesktopRecorder:
    """记录 openUrl 的调用；result 是假函数的返回值。"""

    urls: list[QUrl] = field(default_factory=list[QUrl])
    result: bool = True

    def open_url(self, url: QUrl | str) -> bool:
        self.urls.append(QUrl(url))
        return self.result

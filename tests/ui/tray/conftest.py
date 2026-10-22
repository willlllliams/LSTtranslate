"""托盘测试的公共夹具。

Qt 平台、QApplication、qasync 事件循环、事件总线与界面语言的夹具在 tests/ui/conftest.py 里；offscreen 平台上
测试不会在桌面上显示托盘图标。另有两个自动夹具把 QSystemTrayIcon.showMessage 和
QDesktopServices.openUrl 换成记录调用的假函数：测试不会弹出系统通知，也不会打开资源管理器。
"""

import pytest
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QSystemTrayIcon

from lst.contracts.events import (
    OpenSettings,
    QuitRequested,
    RestartWorker,
    ShowModelDownloads,
    StartCapture,
    ToggleBilingualWindow,
)
from lst.contracts.messages import Message
from lst.contracts.updates import CheckForUpdates, DownloadUpdate, InstallUpdate
from lst.kernel import EventBus
from tests.ui.tray.fakes import DesktopRecorder, ShownMessage


@pytest.fixture
async def received(bus: EventBus) -> list[Message]:
    """托盘会发出的命令和事件。每个命令恰好登记一个处理者，满足 send 的要求。"""
    sink: list[Message] = []
    for kind in (
        StartCapture,
        ToggleBilingualWindow,
        OpenSettings,
        ShowModelDownloads,
        QuitRequested,
        CheckForUpdates,
        DownloadUpdate,
        InstallUpdate,
        RestartWorker,
    ):
        bus.subscribe(kind, sink.append)
    return sink


@pytest.fixture(autouse=True)
def shown_messages(monkeypatch: pytest.MonkeyPatch) -> list[ShownMessage]:
    """把 QSystemTrayIcon.showMessage 换成记录调用的假函数，返回记录列表。"""
    shown: list[ShownMessage] = []

    def fake_show_message(
        self: QSystemTrayIcon, title: str, message: str, icon: QSystemTrayIcon.MessageIcon, msecs: int
    ) -> None:
        shown.append(ShownMessage(title, message, icon, msecs))

    monkeypatch.setattr(QSystemTrayIcon, "showMessage", fake_show_message)
    return shown


@pytest.fixture(autouse=True)
def desktop(monkeypatch: pytest.MonkeyPatch) -> DesktopRecorder:
    """把 QDesktopServices.openUrl 换成记录调用的假函数。"""
    recorder = DesktopRecorder()
    monkeypatch.setattr(QDesktopServices, "openUrl", recorder.open_url)
    return recorder

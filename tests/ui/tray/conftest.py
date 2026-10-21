"""托盘测试的公共夹具。

Qt 平台在本文件导入时设为 offscreen（环境里已经指定的不覆盖），QApplication 在之后的测试里才创建，
所以测试不会在桌面上显示托盘图标。另有两个自动夹具把 QSystemTrayIcon.showMessage 和
QDesktopServices.openUrl 换成记录调用的假函数：测试不会弹出系统通知，也不会打开资源管理器。
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QSystemTrayIcon

from lst.contracts.events import (
    OpenSettings,
    QuitRequested,
    ShowModelDownloads,
    StartCapture,
    ToggleBilingualWindow,
)
from lst.contracts.messages import Message
from lst.kernel import EventBus
from lst.ui import i18n
from tests.ui.tray.fakes import DesktopRecorder, ShownMessage

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def zh_language() -> Iterator[None]:
    """每个测试默认用简体中文，结束后恢复原来的界面语言。"""
    previous = i18n.current_language()
    i18n.set_language("zh-Hans")
    yield
    i18n.set_language(previous)


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    """真实的事件总线，raise_errors=True。

    同步处理函数的异常会被总线重新抛给事件循环的异常处理器；这里换上记录用的处理器，
    测试结束时有记录就判失败，处理函数里的错误因此不会只留下一行日志。
    """
    loop = asyncio.get_running_loop()
    contexts: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    instance = EventBus(loop, logger=logging.getLogger("tests.ui.tray.bus"), raise_errors=True)
    try:
        yield instance
        await instance.drain()
    finally:
        await instance.aclose()
        loop.set_exception_handler(None)
    assert not contexts, f"总线处理函数抛出了异常：{contexts}"


@pytest.fixture
async def received(bus: EventBus) -> list[Message]:
    """托盘会发出的命令和事件。每个命令恰好登记一个处理者，满足 send 的要求。"""
    sink: list[Message] = []
    for kind in (StartCapture, ToggleBilingualWindow, OpenSettings, ShowModelDownloads, QuitRequested):
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

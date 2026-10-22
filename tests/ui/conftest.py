"""界面测试共用的夹具：Qt 平台、QApplication、qasync 事件循环、事件总线、界面语言和垃圾回收。

平台：默认使用 offscreen 平台，窗口不会真的弹出来。平台在创建 QApplication 时才读取，本文件导入时设置即可；
环境里已经指定的平台（例如 ``QT_QPA_PLATFORM=windows``）不覆盖。原生平台上的测试窗口一律带
WA_DontShowOnScreen：isVisible 照常为真，但窗口不出现在屏幕上，也不抢前台。

事件循环：tests/ui 下的每个异步测试都运行在 qasync 的 QEventLoop 上
（``pytest_asyncio_loop_factories`` 钩子），与正式程序相同，asyncio 回调和 Qt 的计时器、信号槽
由同一个 Qt 事件循环驱动。

垃圾回收：每个测试新建一个 qasync 循环，旧循环留下的 QObject（计时器、信号转发器、执行器线程的工作对象）
处在 Python 的引用环里，只有循环垃圾回收才能释放它们。自动回收可能在任何线程触发，包括 qasync 执行器的
QThread（例如 ``asyncio.to_thread`` 写配置文件时），在那里析构主线程的 QObject 会让主线程随后在 Qt 事件循环里
访问已经释放的内存而崩溃（access violation）。所以每个测试的夹具全部拆除、循环已经关闭之后，这里在主线程上
立即做一次完整的垃圾回收，旧循环的对象不会留到下一个测试里被别的线程回收。
"""

import asyncio
import gc
import logging
import os
from collections.abc import AsyncIterator, Callable, Generator, Iterator, Mapping
from typing import Any

import pytest
import qasync  # pyright: ignore[reportMissingTypeStubs]
from PySide6.QtWidgets import QApplication

from lst.kernel import EventBus
from lst.ui import i18n

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def _qasync_loop() -> asyncio.AbstractEventLoop:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("创建 qasync 事件循环之前需要 QApplication，见 _qt_app 夹具")
    loop: asyncio.AbstractEventLoop = qasync.QEventLoop(app)  # pyright: ignore[reportUnknownMemberType]
    return loop


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """tests/ui 下的异步测试都运行在 qasync 事件循环上。"""
    del config, item
    return {"qasync": _qasync_loop}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None]:
    """夹具全部拆除（事件循环已关闭）之后，在主线程上做一次完整的垃圾回收。原因见模块说明。"""
    del item, nextitem
    yield
    gc.collect()


@pytest.fixture(autouse=True)
def _qt_app(qapp: QApplication) -> QApplication:
    """保证 QApplication 先于事件循环创建：qapp 是会话级夹具，先于函数级的事件循环夹具建立。"""
    return qapp


@pytest.fixture(autouse=True)
def zh_language() -> Iterator[None]:
    """每个测试默认用简体中文，结束后恢复原来的界面语言。"""
    previous = i18n.current_language()
    i18n.set_language("zh-Hans")
    yield
    i18n.set_language(previous)


def on_native_platform() -> bool:
    return QApplication.platformName() != "offscreen"


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """让事件循环运转，直到条件成立。超时说明被测行为没有发生。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("等待的条件在时限内没有成立")
        await asyncio.sleep(0.01)


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    """真实的事件总线（raise_errors=True）。

    同步处理函数的异常被总线重新抛给事件循环的异常处理器，这里把它们收集起来，测试结束时有记录就判失败，
    处理函数里的错误因此不会只留下一行日志。
    """
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    instance = EventBus(loop, logger=logging.getLogger("tests.ui.bus"), raise_errors=True)
    try:
        yield instance
        await instance.drain()
    finally:
        await instance.aclose()
        loop.set_exception_handler(None)
    assert not errors, [context.get("exception") or context.get("message") for context in errors]

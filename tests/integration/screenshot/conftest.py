"""截屏翻译端到端测试的夹具：Qt 平台、qasync 事件循环与事件总线。

与 tests/ui 相同，异步测试运行在 qasync 的 QEventLoop 上：覆盖窗口的分帧字号测量与淡入动画由 Qt 计时器驱动，
蒙版与覆盖窗口的事件也要由同一个循环处理。平台默认 offscreen，环境里已经指定的平台
（例如 ``QT_QPA_PLATFORM=windows``）不覆盖；窗口一律带 WA_DontShowOnScreen，不出现在桌面上。
主线程上的垃圾回收由 tests/integration/conftest.py 的钩子负责。
"""

import asyncio
import logging
import os
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
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
        raise RuntimeError("创建 qasync 事件循环之前需要 QApplication")
    loop: asyncio.AbstractEventLoop = qasync.QEventLoop(app)  # pyright: ignore[reportUnknownMemberType]
    return loop


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    del config, item
    return {"qasync": _qasync_loop}


@pytest.fixture(autouse=True)
def _qt_app(qapp: QApplication) -> QApplication:
    return qapp


@pytest.fixture(autouse=True)
def zh_language() -> Iterator[None]:
    previous = i18n.current_language()
    i18n.set_language("zh-Hans")
    yield
    i18n.set_language(previous)


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    instance = EventBus(loop, logger=logging.getLogger("tests.integration.screenshot.bus"), raise_errors=True)
    try:
        yield instance
        await instance.drain()
    finally:
        await instance.aclose()
        loop.set_exception_handler(None)
    assert not errors, [context.get("exception") or context.get("message") for context in errors]

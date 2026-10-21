"""双语窗口测试的公共夹具与构造函数。

平台：默认使用 offscreen 平台，窗口不会真的弹出来。用 QT_QPA_PLATFORM=windows 在原生平台上运行时，
窗口带上 WA_DontShowOnScreen：isVisible 照常为真，但窗口不出现在屏幕上，也不抢前台
（见 BilingualWindow.present）。
剪贴板一律换成记录列表，测试不改动用户的系统剪贴板；写系统剪贴板的那一个函数单独在 offscreen 平台上测。

事件循环：每个异步测试都运行在 qasync 的 QEventLoop 上（pytest_asyncio_loop_factories 钩子），与正式程序相同，
asyncio 回调和 Qt 的计时器、动画、信号槽由同一个 Qt 事件循环驱动。测试用 ``await asyncio.sleep`` 让时间流逝，
停顿计时器随之触发；用 ``await bus.drain()`` 等总线把排队的消息分发完。展示器本身只用 Qt 计时器和同步的
``bus.send``，不 await 任何东西。
"""

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import qasync  # pyright: ignore[reportMissingTypeStubs]
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QApplication

from lst.contracts.config import AppConfig
from lst.contracts.errors import ErrorCode
from lst.contracts.events import EngineHealth, EngineStatus, EngineStatuses, SegmentsUpdated, TranslateText
from lst.contracts.ids import EngineId, SegmentId, SessionId
from lst.contracts.translation import AttemptState, AttemptView, SegmentView
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, ModuleContext
from lst.ui import i18n
from lst.ui.bilingual import BilingualModule, BilingualPresenter, BilingualWindow

# 平台在创建 QApplication 时才读取，所以放在导入之后也来得及。显式指定了平台时不覆盖。
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

DEBOUNCE_MS = 150
"""测试里用的输入停顿时长，比默认的 400 毫秒短，测试跑得快。"""


def _qasync_loop() -> asyncio.AbstractEventLoop:
    app = QApplication.instance()
    if app is None:
        raise RuntimeError("创建 qasync 事件循环之前需要 QApplication，见 _qt_app 夹具")
    loop: asyncio.AbstractEventLoop = qasync.QEventLoop(app)  # pyright: ignore[reportUnknownMemberType]
    return loop


def pytest_asyncio_loop_factories(
    config: pytest.Config, item: pytest.Item
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """本目录下的异步测试都运行在 qasync 事件循环上。"""
    del config, item
    return {"qasync": _qasync_loop}


@pytest.fixture(autouse=True)
def _qt_app(qapp: QApplication) -> QApplication:
    """保证 QApplication 先于事件循环创建：qapp 是会话级夹具，先于函数级的事件循环夹具建立。"""
    return qapp


@pytest.fixture(autouse=True)
def _chinese_ui() -> Iterator[None]:
    previous = i18n.current_language()
    i18n.set_language("zh-Hans")
    yield
    i18n.set_language(previous)


def on_native_platform() -> bool:
    return QApplication.platformName() != "offscreen"


# ---------------------------------------------------------------------- 消息构造


def attempt(
    engine: str,
    state: AttemptState = AttemptState.QUEUED,
    text: str | None = None,
    *,
    latency: float | None = None,
    cached: bool = False,
    code: ErrorCode | None = None,
    error: str | None = None,
) -> AttemptView:
    return AttemptView(
        engine_id=EngineId(engine),
        state=state,
        text=text,
        error=error,
        latency_ms=latency,
        cached=cached,
        started=state is not AttemptState.QUEUED and not cached,
        error_code=code,
    )


def segment(
    segment_id: str,
    source: str,
    *attempts: AttemptView,
    winner: str | None = None,
    finished: bool = False,
) -> SegmentView:
    """构造段快照。display_text 按竞速模块的规则取：有胜出者时是它的译文，否则是原文。"""
    display = source
    if winner is not None:
        display = next(a.text for a in attempts if a.engine_id == winner and a.text is not None)
    return SegmentView(
        segment_id=SegmentId(segment_id),
        source_text=source,
        display_text=display,
        winner=None if winner is None else EngineId(winner),
        finished=finished,
        attempts=attempts,
    )


def config(
    *,
    debounce_ms: JsonValue = DEBOUNCE_MS,
    layout: JsonValue = None,
    marker: JsonValue = None,
    revision: int = 1,
) -> AppConfig:
    bilingual: dict[str, JsonValue] = {"pair": ["zh-Hans", "en"], "debounce_ms": debounce_ms}
    if layout is not None:
        bilingual["layout"] = layout
    race: dict[str, JsonValue] = {"mode": "saver"}
    if marker is not None:
        race["show_source_marker"] = {"overlay": False, "bilingual": marker}
    return AppConfig(sections={"bilingual": bilingual, "race": race}, revision=revision)


def statuses(names: Mapping[str, str], colors: Mapping[str, str] | None = None) -> EngineStatuses:
    return EngineStatuses(
        statuses={EngineId(e): EngineStatus(EngineHealth.READY) for e in names},
        names={EngineId(e): n for e, n in names.items()},
        colors=None if colors is None else {EngineId(e): c for e, c in colors.items()},
    )


async def wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """让事件循环运转，直到条件成立。超时说明被测行为没有发生。"""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待的条件在时限内没有成立")
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------- 夹具


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    """真实的事件总线（raise_errors=True）。

    同步处理函数的异常被总线重新抛给事件循环的异常处理器，这里把它们收集起来，测试结束时断言为空。
    """
    loop = asyncio.get_running_loop()
    errors: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    bus = EventBus(loop, raise_errors=True)
    yield bus
    await bus.drain()
    await bus.aclose()
    loop.set_exception_handler(None)
    assert not errors, [context.get("exception") or context.get("message") for context in errors]


@dataclass
class Harness:
    """一个运行中的双语窗口模块，加上扮演竞速模块的命令记录。"""

    bus: EventBus
    module: BilingualModule
    ctx: ModuleContext
    sent: list[TranslateText]
    copied: list[str]
    settings_path: Path
    stopped: bool = field(default=False)

    @property
    def window(self) -> BilingualWindow:
        window = self.module.window
        assert window is not None
        return window

    @property
    def presenter(self) -> BilingualPresenter:
        presenter = self.module.presenter
        assert presenter is not None
        return presenter

    @property
    def last(self) -> TranslateText:
        assert self.sent, "还没有发出任何 TranslateText"
        return self.sent[-1]

    async def flush(self) -> None:
        await self.bus.drain()

    async def publish(self, message: AppConfig | EngineStatuses | SegmentsUpdated) -> None:
        self.bus.publish(message)
        await self.bus.drain()

    async def updates(self, session_id: SessionId, *views: SegmentView) -> None:
        await self.publish(SegmentsUpdated(session_id=session_id, views=views))

    async def type_and_wait(self, text: str) -> TranslateText:
        """替换输入框内容，等停顿后发出的那条命令。"""
        count = len(self.sent)
        edit = self.window.input_edit
        edit.setPlainText(text)
        edit.moveCursor(QTextCursor.MoveOperation.End)
        await wait_until(lambda: len(self.sent) > count)
        return self.last

    async def stop(self) -> None:
        if not self.stopped:
            self.stopped = True
            await self.module.stop()
            self.ctx.close()


type StartHarness = Callable[..., Awaitable[Harness]]


@pytest.fixture
async def start_harness(bus: EventBus, tmp_path: Path) -> AsyncIterator[StartHarness]:
    """返回一个启动函数；可以多次调用（例如模拟重启），共用同一条总线和同一个命令记录。"""
    sent: list[TranslateText] = []
    copied: list[str] = []
    bus.subscribe(TranslateText, sent.append)
    harnesses: list[Harness] = []

    async def start(settings_path: Path | None = None, *, with_config: bool = True) -> Harness:
        path = settings_path or tmp_path / "data" / "ui_state.ini"
        module = BilingualModule(path, clipboard=copied.append)
        ctx = ModuleContext(bus, logging.getLogger("lst.ui.bilingual"))
        await module.start(ctx)
        harness = Harness(bus, module, ctx, sent, copied, path)
        harnesses.append(harness)
        if on_native_platform():
            harness.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        if with_config:
            bus.publish(config())
        await bus.drain()
        return harness

    yield start
    for harness in reversed(harnesses):
        await harness.stop()


@pytest.fixture
async def harness(start_harness: StartHarness) -> Harness:
    return await start_harness()

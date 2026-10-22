"""双语窗口测试的公共夹具与构造函数。

平台：用 QT_QPA_PLATFORM=windows 在原生平台上运行时，窗口带上 WA_DontShowOnScreen
（见 BilingualWindow.present）。
剪贴板一律换成记录列表，测试不改动用户的系统剪贴板；写系统剪贴板的那一个函数单独在 offscreen 平台上测。

事件循环、QApplication、事件总线与界面语言的夹具在 tests/ui/conftest.py 里。
测试用 ``await asyncio.sleep`` 让时间流逝，停顿计时器随之触发；用 ``await bus.drain()``
等总线把排队的消息分发完。展示器本身只用 Qt 计时器和同步的 ``bus.send``，不 await 任何东西。
"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QTextCursor

from lst.contracts.config import AppConfig
from lst.contracts.errors import ErrorCode
from lst.contracts.events import EngineHealth, EngineStatus, EngineStatuses, SegmentsUpdated, TranslateText
from lst.contracts.ids import EngineId, SegmentId, SessionId
from lst.contracts.translation import AttemptState, AttemptView, SegmentView, SkipReason
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, ModuleContext
from lst.ui.bilingual import BilingualModule, BilingualPresenter, BilingualWindow
from tests.ui.conftest import on_native_platform, wait_until

DEBOUNCE_MS = 150
"""测试里用的输入停顿时长，比默认的 400 毫秒短，测试跑得快。"""


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
    shown_engine: str | None = None,
    finished: bool = False,
    skip_reason: SkipReason | None = None,
) -> SegmentView:
    """构造段快照。display_text 按调度模块的规则取：有显示的实例时是它的译文，否则是原文。"""
    display = source
    if shown_engine is not None:
        display = next(a.text for a in attempts if a.engine_id == shown_engine and a.text is not None)
    return SegmentView(
        segment_id=SegmentId(segment_id),
        source_text=source,
        display_text=display,
        shown_engine=None if shown_engine is None else EngineId(shown_engine),
        finished=finished,
        attempts=attempts,
        skip_reason=skip_reason,
    )


def config(
    *,
    debounce_ms: JsonValue = DEBOUNCE_MS,
    layout: JsonValue = None,
    revision: int = 1,
) -> AppConfig:
    bilingual: dict[str, JsonValue] = {"pair": ["zh-Hans", "en"], "debounce_ms": debounce_ms}
    if layout is not None:
        bilingual["layout"] = layout
    return AppConfig(sections={"bilingual": bilingual}, revision=revision)


def statuses(
    names: Mapping[str, str],
    *,
    health: Mapping[str, EngineHealth] | None = None,
    billing: Collection[str] = (),
) -> EngineStatuses:
    """引擎状态：names 的顺序就是优先级（第一个优先级为 1），billing 里列出的实例计费，
    health 给出个别实例的状态（缺省为就绪），未启用的实例不进计费标记。"""
    states = {e: (health or {}).get(e, EngineHealth.READY) for e in names}
    return EngineStatuses(
        statuses={EngineId(e): EngineStatus(h) for e, h in states.items()},
        names={EngineId(e): n for e, n in names.items()},
        priorities={EngineId(e): rank for rank, e in enumerate(names, start=1)},
        billing={EngineId(e): e in billing for e, h in states.items() if h is not EngineHealth.DISABLED},
    )


DEFAULT_ENGINES = {"argos": "Argos"}
"""夹具启动时发布的引擎状态：只有一个就绪的本地实例，它就是双语窗口选中的服务。"""


# ---------------------------------------------------------------------- 夹具


@dataclass
class Harness:
    """一个运行中的双语窗口模块，加上扮演调度模块的命令记录。"""

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

    async def start(
        settings_path: Path | None = None, *, with_config: bool = True, with_engines: bool = True
    ) -> Harness:
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
        if with_engines:
            bus.publish(statuses(DEFAULT_ENGINES))
        await bus.drain()
        return harness

    yield start
    for harness in reversed(harnesses):
        await harness.stop()


@pytest.fixture
async def harness(start_harness: StartHarness) -> Harness:
    return await start_harness()

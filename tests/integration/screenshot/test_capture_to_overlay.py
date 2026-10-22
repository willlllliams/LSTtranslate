"""截屏翻译端到端：StartCapture → 蒙版框选 → CaptureCompleted → OCR → 竞速 → 覆盖窗口显示译文。

参与的都是真实模块：CaptureModule、OcrModule、RaceModule、OverlayModule，经同一条事件总线串联。
替换掉的只有外部依赖：

- 屏幕：虚构的两屏布局（左侧 150% 副屏坐标为负），冻结画面是按坐标编码颜色的合成图像；
- OCR：第一个用例用进程内的假 WorkerChannel，按截图尺寸给出两行文本（文本模式）；第二个用例启动真实的
  OCR 子进程（lst.workers.ocr_worker）并装入测试用的假后端，它把收到的图像尺寸和首末像素写成识别文字，
  据此核对截图的物理像素经过管道后没有错位；
- 翻译引擎：假引擎，把原文加上前缀作为译文；语言检测：一律检测为英文。

窗口全部带 WA_DontShowOnScreen，不出现在桌面上。
"""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from lst.app.supervisor import WorkerSupervisor
from lst.contracts.config import AppConfig
from lst.contracts.events import EngineRoster, SegmentsUpdated, StartCapture
from lst.contracts.ids import EngineId, Lang
from lst.contracts.ocr import CaptureCompleted, OcrCompleted
from lst.contracts.ports import WorkerState
from lst.contracts.translation import EngineInfo, EngineKind, Job, SegmentOutcome
from lst.contracts.worker_protocol import (
    JsonValue,
    OcrRequest,
    OcrResult,
    Started,
    TranslateRequest,
    WorkerReply,
)
from lst.kernel import EventBus, Module, ModuleContext
from lst.services.ocr import OcrModule
from lst.services.race import RaceModule
from lst.ui.capture import CaptureModule, Rect
from lst.ui.overlay import OverlayModule, Phase
from tests.ui.capture.conftest import FakeScreens, coded_color, drag, hide_from_desktop

ROOT = Path(__file__).resolve().parents[3]
OCR_FAKE = (
    sys.executable,
    "-m",
    "lst.workers.ocr_worker",
    "--backend",
    "tests.workers.ocr.fake_backend:FakeOcrBackend",
)
LAYOUT = [("main", Rect(0, 0, 1920, 1080), 1.0), ("left", Rect(-2560, -200, 2560, 1440), 1.5)]


class EnglishDetector:
    async def warm_up(self) -> None:
        return None

    async def detect_many(self, texts: Sequence[str]) -> list[Lang | None]:
        return [Lang("en") for _ in texts]


class PrefixEngine:
    """支持任何语言对的假引擎：译文是“译：”加原文。"""

    def __init__(self) -> None:
        self._info = EngineInfo(
            id=EngineId("fake"), name="Fake", kind=EngineKind.LOCAL_NMT, priority=1, billing=False
        )
        self.jobs: list[Job] = []

    @property
    def info(self) -> EngineInfo:
        return self._info

    def supports(self, source: Lang, target: Lang) -> bool:
        del source, target
        return True

    async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        self.jobs.append(job)
        for segment in job.segments:
            yield SegmentOutcome(segment.id, text=f"译：{segment.text}")


class _Handle:
    def __init__(self, request: OcrRequest, blocks: list[JsonValue]) -> None:
        self._request = request
        self._blocks = blocks

    @property
    def request_id(self) -> str:
        return self._request.request_id

    async def replies(self) -> AsyncIterator[WorkerReply]:
        await asyncio.sleep(0)
        yield Started(self.request_id)
        yield OcrResult(self.request_id, self._blocks)

    def cancel(self) -> None:
        return None


class TwoLineChannel:
    """进程内的假 OCR 通道：按请求里的截图尺寸给出一个文本模式的块，里面两行文字，相隔较远，合并成两段。"""

    def __init__(self) -> None:
        self.state = WorkerState.READY
        self.capabilities: Mapping[str, JsonValue] | None = {"mode": "text"}
        self.requests: list[OcrRequest] = []

    def submit(
        self, request: TranslateRequest | OcrRequest, *, payload: bytes | None = None, timeout_s: float
    ) -> _Handle:
        del timeout_s
        assert isinstance(request, OcrRequest)
        assert payload is not None and len(payload) >= request.stride * request.height
        self.requests.append(request)
        w, h = request.width, request.height
        lines: list[JsonValue] = [
            {"bbox": [10, 10, w - 10, 40], "text": "Hello world", "score": 0.99},
            {"bbox": [10, h - 50, w - 10, h - 20], "text": "Good morning", "score": 0.99},
        ]
        block: JsonValue = {
            "id": "0",
            "category": "text",
            "bbox": [0, 0, w, h],
            "order": 0,
            "score": 0.99,
            "lines": lines,
        }
        return _Handle(request, [block])


async def _wait(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


class Pipeline:
    def __init__(self, bus: EventBus, channel: object) -> None:
        self.bus = bus
        self.engine = PrefixEngine()
        self.copied: list[str] = []
        self.capture = CaptureModule(freezer=FakeScreens(LAYOUT), prepare_window=hide_from_desktop)
        self.overlay = OverlayModule(clipboard=self.copied.append, prepare_window=hide_from_desktop)
        self.race = RaceModule(detector=EnglishDetector(), frame_interval=0)
        self.ocr = OcrModule(channel)  # pyright: ignore[reportArgumentType]
        self.started: list[tuple[Module, ModuleContext]] = []
        self.captures: list[CaptureCompleted] = []
        self.results: list[OcrCompleted] = []
        self.updates: list[SegmentsUpdated] = []
        bus.subscribe(CaptureCompleted, self.captures.append)
        bus.subscribe(OcrCompleted, self.results.append)
        bus.subscribe(SegmentsUpdated, self.updates.append)

    async def start(self) -> None:
        self.bus.publish(AppConfig({"screenshot": {"target_lang": "zh-Hans"}}, 1))
        self.bus.publish(EngineRoster((self.engine,)))
        # 与组合根相同的顺序：业务模块在前，界面模块在后。
        for module in (self.race, self.ocr, self.capture, self.overlay):
            ctx = ModuleContext(self.bus, logging.getLogger(f"tests.e2e.{module.name}"))
            await module.start(ctx)
            self.started.append((module, ctx))
        await self.bus.drain()

    async def stop(self) -> None:
        for module, ctx in reversed(self.started):
            await module.stop()
            ctx.close()
        await self.bus.drain()

    async def select(self, screen: str, start: tuple[float, float], end: tuple[float, float]) -> None:
        self.bus.send(StartCapture())
        await self.bus.drain()
        controller = self.capture.controller
        assert controller is not None and controller.active
        mask = next(m for m in controller.masks if m.screen_geometry.name == screen)
        drag(mask, start, end)
        await self.bus.drain()


@pytest.fixture
async def pipeline(bus: EventBus) -> AsyncIterator[Pipeline]:
    instance = Pipeline(bus, TwoLineChannel())
    await instance.start()
    yield instance
    await instance.stop()


async def test_capture_to_translated_overlay(pipeline: Pipeline) -> None:
    # 在 150% 的左侧副屏上框选 (20, 30)–(220, 130) 逻辑像素，即 300×150 物理像素。
    await pipeline.select("left", (20, 30), (220, 130))
    [event] = pipeline.captures
    assert (event.image.width, event.image.height) == (300, 150)
    assert event.geometry.selection == (-2540, -170, 200, 100)

    overlay = pipeline.overlay

    def translated() -> bool:
        window = overlay.window
        return (
            window is not None
            and all(item.display.startswith("译：") for item in window.items.values())
            and bool(window.items)
        )

    await _wait(translated)
    window = overlay.window
    assert window is not None
    assert window.phase is Phase.RESULT
    assert window.geometry().topLeft().x() == -2540
    assert (window.width(), window.height()) == (200, 100)
    assert [window.shown_text(i) for i in window.visible_items()] == ["译：Hello world", "译：Good morning"]
    await _wait(lambda: window.progress == (2, 2))
    # 两个段在截图中的范围包住两行的范围（文本模式按段渲染，架构文档 8.4 节）。能按字形反推出原文字号时，
    # 单行段的范围在竖直方向扩到行高（OV-03），扩展不越过两段之间的中线；
    # 进程里有没有可用的字体（例如别的测试加载过微软雅黑）决定扩不扩，两种情况都满足下面的关系。
    boxes = [item.bbox for item in window.visible_items()]
    assert boxes[0].y0 <= 10 and boxes[1].y1 >= 130
    assert boxes[0].y1 <= boxes[1].y0
    assert pipeline.engine.jobs[0].session_id == event.session_id

    # 没有选中时 Ctrl+C 不复制；从第一段左边拖到窗口右下角选中两段的全部文字，复制时段与段之间换行。
    QTest.keyClick(window, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert pipeline.copied == []
    s = window.scale_logical()
    first = boxes[0]
    drag(window, (first.x0 * s + 1, (first.y0 + first.y1) / 2 * s), (window.width() - 1, window.height() - 1))
    QTest.keyClick(window, Qt.Key.Key_C, Qt.KeyboardModifier.ControlModifier)
    assert pipeline.copied == ["译：Hello world\n译：Good morning"]


async def test_second_capture_discards_first(pipeline: Pipeline) -> None:
    await pipeline.select("main", (10, 10), (210, 110))
    await pipeline.select("main", (300, 300), (500, 400))
    first, second = pipeline.captures

    def done() -> bool:
        window = pipeline.overlay.window
        return (
            window is not None
            and bool(window.items)
            and all(i.display.startswith("译：") for i in window.items.values())
        )

    await _wait(done)
    presenter = pipeline.overlay.presenter
    assert presenter is not None and presenter.session_id == second.session_id
    sessions = {u.session_id for u in pipeline.updates}
    assert second.session_id in sessions
    window = pipeline.overlay.window
    assert window is not None
    assert window.geometry().topLeft().x() == 300
    del first


@pytest.mark.timeout(120)
async def test_real_ocr_process_receives_exact_pixels(bus: EventBus) -> None:
    """真实的 OCR 子进程（假后端）收到的截图尺寸与首末像素，与蒙版上框选的物理像素一致。"""
    supervisor = WorkerSupervisor("ocr", OCR_FAKE, settings={}, cwd=ROOT)
    await supervisor.start()
    pipeline = Pipeline(bus, supervisor)
    try:
        assert await asyncio.wait_for(
            supervisor.wait_for_state(WorkerState.READY, WorkerState.FAILED), 60
        ) is (WorkerState.READY)
        await pipeline.start()
        # 150% 的左侧副屏上，从 (10, 20) 拖到 (110, 220) 逻辑像素：物理像素 (15, 30) 起 150×300。
        await pipeline.select("left", (10, 20), (110, 220))
        await _wait(lambda: bool(pipeline.results), timeout=60)
        [result] = pipeline.results
        [segment] = result.segments
        first, last = coded_color(15, 30), coded_color(15 + 149, 30 + 299)
        expected = (
            f"150x300 {first.blue()}/{first.green()}/{first.red()} {last.blue()}/{last.green()}/{last.red()}"
        )
        assert segment.segment.text.startswith(expected)

        def translated() -> bool:
            window = pipeline.overlay.window
            return window is not None and any(
                i.display.startswith("译：150x300") for i in window.items.values()
            )

        await _wait(translated, timeout=30)
    finally:
        await pipeline.stop()
        await supervisor.stop()

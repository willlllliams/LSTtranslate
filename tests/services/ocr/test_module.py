"""OCR 模块的测试：真实事件总线（raise_errors=True）→ OcrModule → 假的 WorkerChannel。

OCR 进程与监管器由假的 WorkerChannel 扮演。每个请求的回复放在一个 asyncio.Queue 里，由测试逐条推送，
从而控制回复到达的时机。等待模块处理完时订阅 Signal，每次状态变化后重新检查条件，不靠轮询。
"""

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field

import pytest

from lst.contracts.config import AppConfig
from lst.contracts.errors import ErrorCode
from lst.contracts.events import WorkerStatus, WorkerStatuses
from lst.contracts.ids import SessionId
from lst.contracts.ocr import (
    BBox,
    BlockCategory,
    CaptureCompleted,
    CaptureGeometry,
    OcrCompleted,
    OcrFailed,
    RawImage,
    RetryOcr,
    TextLine,
)
from lst.contracts.ocr_engines import OcrEngineCatalog, OcrEngineKind
from lst.contracts.ports import WorkerState
from lst.contracts.worker_protocol import (
    CANCELLED,
    JsonValue,
    OcrRequest,
    OcrResult,
    RequestDone,
    RequestFailed,
    SegmentResult,
    Started,
    TranslateRequest,
    WorkerReply,
)
from lst.kernel import EventBus, Module, ModuleContext
from lst.services.ocr import (
    OcrConfig,
    OcrModule,
    Paragraph,
    ParagraphMerger,
    request_options,
)
from lst.services.ocr.module import (
    ERROR_CANCELLED,
    ERROR_CONVERT,
    ERROR_NO_RESULT,
    ERROR_STREAM,
    ERROR_SUBMIT,
)

MODULE_LOGGER = "lst.ocr"
GEOMETRY = CaptureGeometry("screen", (0, 0, 1920, 1080), (100, 100, 4, 3), 1.0)
SECRET = "机密原文"
SECRET_EN = "classified source"


def image(width: int = 400, height: int = 300, *, extra: int = 0, seed: int = 1) -> RawImage:
    stride = width * 4
    size = stride * height + extra
    return RawImage(width, height, stride, bytes((seed + i) % 251 for i in range(size)))


def blocks_json() -> list[JsonValue]:
    """一个标题块、一个表格块、一个图片块。"""
    return [
        {
            "id": "0",
            "category": "title",
            "bbox": [10, 10, 390, 40],
            "order": 0,
            "score": 0.9,
            "text": "Report",
        },
        {
            "id": "1",
            "category": "table",
            "bbox": [10, 50, 390, 120],
            "order": 1,
            "score": 0.9,
            "cells": [
                {"row": 0, "col": 0, "bbox": [10, 50, 200, 85], "text": "Region"},
                {"row": 0, "col": 1, "bbox": [200, 50, 390, 85], "text": "Sales"},
            ],
        },
        {"id": "2", "category": "image", "bbox": [10, 130, 390, 290], "order": 2, "score": 0.8},
    ]


def text_mode_json() -> list[JsonValue]:
    """文本模式的输出：一个正文块，里面是两段的文本行（顺序打乱）。"""

    def row(y: int, text: str, width: int = 300) -> JsonValue:
        return {"bbox": [20, y, 20 + width, y + 20], "text": text, "score": 0.95}

    lines: list[JsonValue] = [
        row(120, "four"),
        row(20, "one"),
        row(46, "two"),
        row(72, "three", 120),
        row(146, "five"),
    ]
    return [
        {"id": "0", "category": "text", "bbox": [0, 0, 400, 300], "order": 0, "score": 0.95, "lines": lines}
    ]


# ---------------------------------------------------------------- 测试替身


class Signal:
    """状态变化通知。wait_until 在每次 notify 后重新检查条件，超时则测试失败。"""

    def __init__(self) -> None:
        self._waiters: list[asyncio.Future[None]] = []

    def notify(self) -> None:
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)
        self._waiters.clear()

    async def wait_until(self, predicate: Callable[[], bool], timeout: float = 2.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
                self._waiters.append(waiter)
                await waiter


class StreamEnd:
    """放进回复队列后，回复流不带结果直接结束。"""


END = StreamEnd()

type Item = WorkerReply | StreamEnd | Exception


class FakeHandle:
    """一个已提交的请求。

    回复从 queue 中取出：WorkerReply 原样产出，END 让回复流结束，Exception 从回复流里抛出。
    """

    def __init__(self, request: OcrRequest, signal: Signal) -> None:
        self.request = request
        self.signal = signal
        self.queue: asyncio.Queue[Item] = asyncio.Queue()
        self.reading = False
        self.closed = False
        self.cancel_calls = 0
        self.cancel_error: Exception | None = None

    @property
    def request_id(self) -> str:
        return self.request.request_id

    @property
    def idle(self) -> bool:
        """模块没有待处理的回复：回复流已结束、请求已被模块取消，或者模块正在等下一条回复。"""
        return self.closed or self.cancel_calls > 0 or (self.reading and self.queue.empty())

    def push(self, *items: Item) -> None:
        for item in items:
            self.queue.put_nowait(item)
        self.signal.notify()

    def result(self, blocks: list[JsonValue]) -> None:
        self.push(Started(self.request_id), OcrResult(self.request_id, blocks))

    def fail(self, code: str, error: str = "detail") -> None:
        self.push(RequestFailed(self.request_id, code, error))

    async def replies(self) -> AsyncIterator[WorkerReply]:
        try:
            while True:
                if self.queue.empty():
                    self.reading = True
                    self.signal.notify()
                item = await self.queue.get()
                self.reading = False
                if isinstance(item, StreamEnd):
                    return
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            self.reading = False
            self.closed = True
            self.signal.notify()

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.signal.notify()
        if self.cancel_error is not None:
            raise self.cancel_error


@dataclass(frozen=True, slots=True)
class Submission:
    request: OcrRequest
    payload: bytes | None
    timeout_s: float
    handle: FakeHandle


class FakeChannel:
    """假的 WorkerChannel。fail_submit 为真时 submit 抛出异常，异常信息里带着请求的 repr。"""

    def __init__(self) -> None:
        self.state: WorkerState = WorkerState.READY
        self.capabilities: Mapping[str, JsonValue] | None = None
        self.fail_submit = False
        self.submissions: list[Submission] = []
        self.signal = Signal()

    def submit(
        self, request: TranslateRequest | OcrRequest, *, payload: bytes | None = None, timeout_s: float
    ) -> FakeHandle:
        assert isinstance(request, OcrRequest)
        if self.fail_submit:
            raise RuntimeError(f"监管器拒绝了请求 {request!r}")
        handle = FakeHandle(request, self.signal)
        self.submissions.append(Submission(request, payload, timeout_s, handle))
        self.signal.notify()
        return handle

    @property
    def last(self) -> FakeHandle:
        return self.submissions[-1].handle

    def idle(self) -> bool:
        return all(s.handle.idle for s in self.submissions)


@dataclass
class Recorder:
    completed: list[OcrCompleted] = field(default_factory=lambda: list[OcrCompleted]())
    failed: list[OcrFailed] = field(default_factory=lambda: list[OcrFailed]())


class Harness:
    def __init__(self, bus: EventBus, channel: FakeChannel, module: OcrModule, ctx: ModuleContext) -> None:
        self.bus = bus
        self.channel = channel
        self.module = module
        self.ctx = ctx
        self.recorder = Recorder()
        bus.subscribe(OcrCompleted, self.recorder.completed.append)
        bus.subscribe(OcrFailed, self.recorder.failed.append)
        self._revision = 0

    @property
    def completed(self) -> list[OcrCompleted]:
        return self.recorder.completed

    @property
    def failed(self) -> list[OcrFailed]:
        return self.recorder.failed

    def capture(self, session: str, raw: RawImage | None = None) -> RawImage:
        shot = raw if raw is not None else image()
        self.bus.publish(CaptureCompleted(SessionId(session), shot, GEOMETRY))
        return shot

    def retry(self, session: str) -> None:
        self.bus.send(RetryOcr(SessionId(session)))

    def configure(self, ocr: dict[str, JsonValue] | None) -> None:
        self._revision += 1
        sections: dict[str, JsonValue] = {} if ocr is None else {"ocr": ocr}
        self.bus.publish(AppConfig(sections=sections, revision=self._revision))

    def worker(self, state: WorkerState, detail: str = "") -> None:
        self.bus.publish(WorkerStatuses({"ocr": WorkerStatus(state, detail)}))

    async def settle(self) -> None:
        """等总线分发完、模块读完全部已推送的回复，并把结果交给记录器。"""
        async with asyncio.timeout(5):
            for _ in range(3):
                await self.bus.drain()
                await self.channel.signal.wait_until(self.channel.idle)
            await self.bus.drain()


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), raise_errors=True)
    yield instance
    await instance.aclose()


@dataclass
class Factory:
    bus: EventBus
    harnesses: list[Harness] = field(default_factory=lambda: list[Harness]())

    async def __call__(
        self,
        *,
        merger: ParagraphMerger | None = None,
        start: bool = True,
        clock: Callable[[], float] | None = None,
    ) -> Harness:
        channel = FakeChannel()
        module = OcrModule(channel, merger=merger, clock=clock)
        ctx = ModuleContext(self.bus, logging.getLogger(MODULE_LOGGER))
        harness = Harness(self.bus, channel, module, ctx)
        self.harnesses.append(harness)
        if start:
            await module.start(ctx)
            await harness.settle()
        return harness


@pytest.fixture
async def make(bus: EventBus) -> AsyncIterator[Factory]:
    factory = Factory(bus)
    yield factory
    for harness in factory.harnesses:
        await harness.module.stop()
        harness.ctx.close()


# ---------------------------------------------------------------- 提交请求


async def test_module_satisfies_the_protocol(make: Factory) -> None:
    harness = await make(start=False)
    module: Module = harness.module
    assert module.name == "ocr"


async def test_capture_submits_a_request_with_pixels_and_options(make: Factory) -> None:
    harness = await make()
    shot = harness.capture("s1", image(400, 300))
    await harness.settle()
    (submission,) = harness.channel.submissions
    request = submission.request
    assert request.session_id == "s1"
    assert re.fullmatch(r"[0-9a-f]{32}", request.request_id)
    assert (request.width, request.height, request.stride) == (400, 300, 1600)
    assert request.options == request_options(OcrConfig())
    assert submission.payload == shot.data
    assert submission.timeout_s == 30  # 默认引擎是 Windows OCR，只有文本模式
    assert request.options["mode"] == "text"
    assert harness.module.busy


async def test_payload_is_trimmed_to_stride_times_height(make: Factory) -> None:
    harness = await make()
    shot = harness.capture("s1", image(10, 5, extra=7))
    await harness.settle()
    payload = harness.channel.submissions[0].payload
    assert payload == shot.data[: 40 * 5]


@pytest.mark.parametrize(
    ("ocr", "timeout"),
    [
        ({"mode": "text"}, 30),
        ({"engine": "paddle", "mode": "text"}, 30),
        ({"engine": "paddle", "mode": "structure", "table": False}, 60),
        ({"engine": "paddle", "mode": "structure", "formula": True}, 120),
        ({"engine": "paddle", "mode": "structure", "table": True, "formula": True, "chart": True}, 240),
        ({"engine": "windows", "mode": "structure", "table": True, "formula": True}, 30),
    ],
)
async def test_timeout_follows_mode_and_features(
    make: Factory, ocr: dict[str, JsonValue], timeout: float
) -> None:
    harness = await make()
    harness.configure(ocr)
    await harness.settle()
    harness.capture("s1")
    await harness.settle()
    assert harness.channel.submissions[0].timeout_s == timeout


# ---------------------------------------------------------------- 结果与失败


async def test_result_is_converted_and_published(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    harness.channel.last.result(blocks_json())
    await harness.settle()
    (completed,) = harness.completed
    assert completed.session_id == "s1"
    assert [b.id for b in completed.blocks] == ["0", "1", "2"]
    assert [(s.segment.id, s.segment.text, s.block_id, s.cell) for s in completed.segments] == [
        ("b0", "Report", "0", None),
        ("b1-r0c0", "Region", "1", (0, 0)),
        ("b1-r0c1", "Sales", "1", (0, 1)),
    ]
    assert completed.segments[1].bbox == BBox(10, 50, 200, 85)
    assert harness.failed == []
    assert not harness.module.busy


async def test_text_mode_result_is_merged_into_paragraphs(make: Factory) -> None:
    harness = await make()
    harness.configure({"mode": "text"})
    await harness.settle()
    harness.capture("s1")
    await harness.settle()
    harness.channel.last.result(text_mode_json())
    await harness.settle()
    (completed,) = harness.completed
    assert [(s.segment.id, s.segment.text) for s in completed.segments] == [
        ("p1", "one two three"),
        ("p2", "four five"),
    ]
    assert completed.segments[0].bbox == BBox(20, 20, 320, 92)


async def test_mode_of_the_request_decides_the_conversion(make: Factory) -> None:
    # 请求发出后改成版面模式：结果仍按提交时的文本模式转换。
    harness = await make()
    harness.configure({"engine": "paddle", "mode": "text"})
    await harness.settle()
    harness.capture("s1")
    await harness.settle()
    harness.configure({"engine": "paddle", "mode": "structure"})
    await harness.settle()
    harness.channel.last.result(text_mode_json())
    await harness.settle()
    assert [s.segment.id for s in harness.completed[0].segments] == ["p1", "p2"]


async def test_result_from_a_process_in_another_mode_uses_the_process_mode(make: Factory) -> None:
    """配置已改成版面模式、组合根还没重载完时，请求发给了仍在文本模式的旧进程：按进程实际的模式转换。"""
    harness = await make()
    harness.configure({"engine": "paddle", "mode": "structure"})
    await harness.settle()
    harness.channel.capabilities = {"mode": "text"}  # 正在服务的进程按文本模式加载
    harness.capture("s1")
    await harness.settle()
    assert harness.channel.last.request.options["mode"] == "structure"
    harness.channel.last.result(text_mode_json())
    await harness.settle()
    assert [s.segment.id for s in harness.completed[0].segments] == ["p1", "p2"]


async def test_process_mode_is_taken_when_processing_starts(make: Factory) -> None:
    """进程的模式在收到 Started 时记下；结果到达前进程已经换成新模式，也按处理该请求的进程转换。"""
    harness = await make()
    harness.configure({"engine": "paddle", "mode": "structure"})
    await harness.settle()
    harness.channel.capabilities = {"mode": "text"}
    harness.capture("s1")
    await harness.settle()
    handle = harness.channel.last
    handle.push(Started(handle.request_id))
    await harness.settle()
    harness.channel.capabilities = {"mode": "structure"}
    handle.push(OcrResult(handle.request_id, text_mode_json()))
    await harness.settle()
    assert [s.segment.id for s in harness.completed[0].segments] == ["p1", "p2"]


async def test_windows_engine_result_is_converted_in_text_mode(make: Factory) -> None:
    # 配置里写着版面模式，Windows OCR 下照样按文本模式提交和转换。
    harness = await make()
    harness.configure({"engine": "windows", "mode": "structure"})
    await harness.settle()
    harness.capture("s1")
    await harness.settle()
    assert harness.channel.last.request.options["mode"] == "text"
    harness.channel.last.result(text_mode_json())
    await harness.settle()
    assert [s.segment.id for s in harness.completed[0].segments] == ["p1", "p2"]


async def test_requests_follow_the_engine_the_process_actually_runs(make: Factory) -> None:
    """PaddleOCR 没装好，组合根让进程先跑 Windows OCR：请求按 Windows OCR 生成（OCRE-04）。"""
    harness = await make()
    harness.configure({"engine": "paddle", "mode": "structure"})
    harness.bus.publish(OcrEngineCatalog(active=OcrEngineKind.PADDLE, running=OcrEngineKind.WINDOWS))
    await harness.settle()
    assert harness.module.config.engine is OcrEngineKind.WINDOWS
    harness.capture("s1")
    await harness.settle()
    assert harness.channel.last.request.options["mode"] == "text"

    # PaddleOCR 装好、进程换回后，请求按配置生成。
    harness.bus.publish(OcrEngineCatalog(active=OcrEngineKind.PADDLE, running=OcrEngineKind.PADDLE))
    await harness.settle()
    assert harness.module.config.engine is OcrEngineKind.PADDLE
    assert harness.module.config.mode.value == "structure"


async def test_bad_blocks_do_not_fail_the_result(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    bad: list[JsonValue] = [{"id": "x", "category": "nonsense"}, *blocks_json()]
    harness.channel.last.result(bad)
    await harness.settle()
    assert [b.id for b in harness.completed[0].blocks] == ["0", "1", "2"]


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("engine.timeout", ErrorCode.TIMEOUT),
        ("engine.worker_crashed", ErrorCode.WORKER_CRASHED),
        ("engine.model_missing", ErrorCode.MODEL_MISSING),
        ("engine.internal", ErrorCode.INTERNAL),
    ],
)
async def test_request_failure_publishes_ocr_failed(make: Factory, code: str, expected: ErrorCode) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    harness.channel.last.fail(code, "推理进程处理该请求超过 120 秒")
    await harness.settle()
    assert harness.failed == [OcrFailed(SessionId("s1"), expected, "推理进程处理该请求超过 120 秒")]
    assert harness.completed == []
    assert not harness.module.busy


async def test_unknown_error_code_is_internal(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    harness.channel.last.fail("ocr.gpu_lost", "CUDA error")
    await harness.settle()
    (failed,) = harness.failed
    assert failed.code is ErrorCode.INTERNAL
    assert "CUDA error" in failed.error
    assert "ocr.gpu_lost" in failed.error


async def test_submit_error_publishes_ocr_failed(make: Factory, caplog: pytest.LogCaptureFixture) -> None:
    harness = await make()
    harness.channel.fail_submit = True
    with caplog.at_level(logging.DEBUG):
        harness.capture("s1")
        await harness.settle()
    assert harness.failed == [
        OcrFailed(SessionId("s1"), ErrorCode.INTERNAL, f"{ERROR_SUBMIT}（RuntimeError）")
    ]
    assert "OcrRequest(" not in caplog.text
    assert not harness.module.busy


async def test_reply_stream_error_publishes_ocr_failed(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    harness.channel.last.push(Started(harness.channel.last.request_id), ConnectionError("pipe broken"))
    await harness.settle()
    assert harness.failed == [
        OcrFailed(SessionId("s1"), ErrorCode.INTERNAL, f"{ERROR_STREAM}（ConnectionError）")
    ]


async def test_stream_ending_without_result_publishes_ocr_failed(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    handle = harness.channel.last
    handle.push(RequestDone(handle.request_id), END)
    await harness.settle()
    assert harness.failed == [OcrFailed(SessionId("s1"), ErrorCode.INTERNAL, ERROR_NO_RESULT)]


async def test_foreign_and_unexpected_replies_are_ignored(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    handle = harness.channel.last
    handle.push(
        OcrResult("someone-else", []),
        SegmentResult(handle.request_id, "seg", text="x"),
        RequestFailed("someone-else", "engine.timeout", "x"),
    )
    handle.result(blocks_json())
    await harness.settle()
    assert len(harness.completed) == 1
    assert harness.failed == []


async def test_conversion_error_publishes_ocr_failed(make: Factory, caplog: pytest.LogCaptureFixture) -> None:
    class Exploding:
        def merge(self, lines: object) -> tuple[Paragraph, ...]:
            raise ValueError(f"cannot merge {SECRET}")

    harness = await make(merger=Exploding())
    harness.configure({"mode": "text"})
    await harness.settle()
    with caplog.at_level(logging.DEBUG):
        harness.capture("s1")
        await harness.settle()
        harness.channel.last.result(text_mode_json())
        await harness.settle()
    assert harness.failed == [
        OcrFailed(SessionId("s1"), ErrorCode.INTERNAL, f"{ERROR_CONVERT}（ValueError）")
    ]
    assert "ValueError" in caplog.text
    assert SECRET not in caplog.text


# ---------------------------------------------------------------- 同一时刻只有一个识别任务


async def test_new_capture_cancels_the_previous_request(make: Factory) -> None:
    harness = await make()
    harness.capture("s1", image(seed=1))
    await harness.settle()
    first = harness.channel.last
    second_shot = harness.capture("s2", image(seed=2))
    await harness.settle()
    second = harness.channel.last
    assert first.cancel_calls == 1
    assert first.closed
    assert second is not first
    assert harness.channel.submissions[1].payload == second_shot.data

    # 旧请求的结果迟到：直接丢弃。
    first.result(blocks_json())
    second.result(blocks_json())
    await harness.settle()
    assert [c.session_id for c in harness.completed] == ["s2"]
    assert harness.failed == []


async def test_result_arriving_right_after_a_new_capture_is_dropped(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    first = harness.channel.last
    # 新截屏先到，旧结果紧随其后：截屏的分发排在前面，旧请求被取消，结果不再读取。
    harness.capture("s2")
    first.result(blocks_json())
    await harness.settle()
    assert harness.completed == []
    harness.channel.last.result(blocks_json())
    await harness.settle()
    assert [c.session_id for c in harness.completed] == ["s2"]


async def test_cancel_error_does_not_break_the_new_capture(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    harness.channel.last.cancel_error = RuntimeError("supervisor gone")
    harness.capture("s2")
    await harness.settle()
    assert [s.request.session_id for s in harness.channel.submissions] == ["s1", "s2"]


async def test_supervisor_cancel_resubmits_once(make: Factory) -> None:
    # 监管器重载 OCR 进程时，已发出的请求以 cancelled 结束：自动重新提交一次，第二次再被取消才报告失败。
    harness = await make()
    shot = harness.capture("s1")
    await harness.settle()
    harness.channel.last.fail(CANCELLED, "推理进程正在按新配置重启")
    await harness.settle()
    assert len(harness.channel.submissions) == 2
    resubmitted = harness.channel.submissions[1]
    assert resubmitted.request.session_id == "s1"
    assert resubmitted.request.request_id != harness.channel.submissions[0].request.request_id
    assert resubmitted.payload == shot.data
    assert harness.failed == []

    harness.channel.last.fail(CANCELLED, "推理进程已停止")
    await harness.settle()
    assert len(harness.channel.submissions) == 2
    assert harness.failed == [
        OcrFailed(SessionId("s1"), ErrorCode.WORKER_CRASHED, f"{ERROR_CANCELLED}（推理进程已停止）")
    ]


async def test_resubmitted_request_uses_the_new_configuration(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    harness.configure({"mode": "text"})
    await harness.settle()
    harness.channel.last.fail(CANCELLED, "推理进程正在按新配置重启")
    await harness.settle()
    resubmitted = harness.channel.submissions[1]
    assert resubmitted.request.options["mode"] == "text"
    assert resubmitted.timeout_s == 30
    harness.channel.last.result(text_mode_json())
    await harness.settle()
    assert [s.segment.id for s in harness.completed[0].segments] == ["p1", "p2"]


# ---------------------------------------------------------------- 重试


async def test_retry_resubmits_the_failed_capture(make: Factory) -> None:
    harness = await make()
    shot = harness.capture("s1")
    await harness.settle()
    harness.channel.last.fail("engine.worker_crashed", "推理进程崩溃")
    await harness.settle()
    harness.retry("s1")
    await harness.settle()
    assert len(harness.channel.submissions) == 2
    retried = harness.channel.submissions[1]
    assert retried.request.session_id == "s1"
    assert retried.payload == shot.data
    harness.channel.last.result(blocks_json())
    await harness.settle()
    assert [c.session_id for c in harness.completed] == ["s1"]


async def test_retry_for_an_older_session_is_ignored(make: Factory, caplog: pytest.LogCaptureFixture) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    harness.channel.last.fail("engine.timeout")
    await harness.settle()
    harness.capture("s2")
    await harness.settle()
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        harness.retry("s1")
        harness.retry("never-seen")
        await harness.settle()
    assert [s.request.session_id for s in harness.channel.submissions] == ["s1", "s2"]
    assert caplog.text.count("不是最近一次截屏") == 2


async def test_retry_while_running_or_after_success_is_ignored(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    harness.retry("s1")  # 识别还在进行
    await harness.settle()
    assert len(harness.channel.submissions) == 1
    harness.channel.last.result(blocks_json())
    await harness.settle()
    harness.retry("s1")  # 已经成功
    await harness.settle()
    assert len(harness.channel.submissions) == 1
    assert len(harness.completed) == 1


async def test_retry_before_any_capture_is_ignored(make: Factory) -> None:
    harness = await make()
    harness.retry("s1")
    await harness.settle()
    assert harness.channel.submissions == []


# ---------------------------------------------------------------- 配置


async def test_configuration_present_at_start_is_the_baseline(make: Factory) -> None:
    harness = await make(start=False)
    harness.configure({"mode": "text", "device": "cpu"})
    await harness.module.start(harness.ctx)
    await harness.settle()
    assert harness.module.config.mode == "text"
    harness.capture("s1")
    await harness.settle()
    assert harness.channel.submissions[0].timeout_s == 30


async def test_feature_switches_apply_to_later_requests_without_reload(make: Factory) -> None:
    harness = await make(start=False)
    harness.configure({"engine": "paddle"})
    await harness.module.start(harness.ctx)
    harness.capture("s1")
    await harness.settle()
    harness.configure(
        {"engine": "paddle", "table": False, "formula": True, "grayscale": True, "resize_max_side": 1600}
    )
    await harness.settle()
    assert harness.channel.submissions[0].request.options["formula"] is False  # 已提交的请求不变
    harness.capture("s2")
    await harness.settle()
    options = harness.channel.submissions[1].request.options
    assert (options["table"], options["formula"], options["grayscale"], options["resize_max_side"]) == (
        False,
        True,
        True,
        1600,
    )
    assert harness.channel.submissions[1].timeout_s == 120


async def test_process_level_changes_update_the_configuration_without_warnings(
    make: Factory, caplog: pytest.LogCaptureFixture
) -> None:
    """引擎、模式、设备、语言变化只更新模块的配置；重载 OCR 进程由组合根负责，模块不报警告。"""
    harness = await make()
    steps: list[dict[str, JsonValue]] = [
        {"mode": "text"},
        {"mode": "text", "device": "gpu", "language": "en"},
        {"mode": "text", "device": "gpu", "language": "en", "engine": "paddle"},
    ]
    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        for step in steps:
            harness.configure(step)
            await harness.settle()
            assert harness.module.config == OcrConfig.model_validate(step)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


async def test_missing_section_means_defaults(make: Factory) -> None:
    harness = await make()
    harness.configure({"engine": "paddle", "mode": "text"})
    await harness.settle()
    harness.configure(None)
    await harness.settle()
    assert harness.module.config == OcrConfig()


async def test_unparsable_section_keeps_the_current_configuration(make: Factory) -> None:
    harness = await make()
    harness.configure({"mode": "text"})
    await harness.settle()
    harness.configure({"mode": "hologram"})
    await harness.settle()
    assert harness.module.config.mode == "text"


# ---------------------------------------------------------------- 进程状态、生命周期


async def test_worker_status_changes_are_recorded(make: Factory, caplog: pytest.LogCaptureFixture) -> None:
    harness = await make()
    with caplog.at_level(logging.DEBUG, logger=MODULE_LOGGER):
        harness.worker(WorkerState.LOADING, "下载模型 30%")
        await harness.settle()
        assert harness.module.worker_state is WorkerState.LOADING
        harness.channel.state = WorkerState.LOADING
        harness.capture("s1")
        await harness.settle()
        harness.worker(WorkerState.READY)
        harness.worker(WorkerState.BUSY)
        await harness.settle()
        harness.bus.publish(WorkerStatuses({"nmt": WorkerStatus(WorkerState.READY)}))
        await harness.settle()
    messages = [(r.levelno, r.getMessage()) for r in caplog.records if r.name == MODULE_LOGGER]
    assert (logging.INFO, "OCR 进程状态：None → loading（下载模型 30%）") in messages
    assert (logging.INFO, "OCR 进程状态：loading → ready") in messages
    assert (logging.DEBUG, "OCR 进程状态：ready → busy") in messages
    assert (logging.INFO, "OCR 进程状态：busy → None") in messages
    assert any("请求排队等待" in m for _, m in messages)
    assert harness.module.worker_state is None


async def test_stop_cancels_the_running_request(make: Factory) -> None:
    harness = await make()
    harness.capture("s1")
    await harness.settle()
    handle = harness.channel.last
    await harness.module.stop()
    assert handle.cancel_calls == 1
    assert handle.closed
    handle.result(blocks_json())
    harness.capture("s2")
    harness.retry("s1")
    await harness.settle()
    assert len(harness.channel.submissions) == 1
    assert harness.completed == []
    await harness.module.stop()  # 重复调用没有效果


async def test_start_twice_is_an_error(make: Factory) -> None:
    harness = await make()
    with pytest.raises(RuntimeError):
        await harness.module.start(harness.ctx)


async def test_timings_use_the_injected_clock(make: Factory, caplog: pytest.LogCaptureFixture) -> None:
    now = [100.0]
    harness = await make(clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger=MODULE_LOGGER):
        harness.capture("s1")
        await harness.settle()
        now[0] = 100.5
        harness.channel.last.push(Started(harness.channel.last.request_id))
        await harness.settle()
        now[0] = 102.0
        harness.channel.last.push(OcrResult(harness.channel.last.request_id, blocks_json()))
        await harness.settle()
    assert "排队 500 毫秒，识别 1500 毫秒" in caplog.text


# ---------------------------------------------------------------- 日志隐私


async def test_logs_never_contain_recognised_text(make: Factory, caplog: pytest.LogCaptureFixture) -> None:
    secret_blocks: list[JsonValue] = [
        {"id": "0", "category": "title", "bbox": [0, 0, 100, 20], "order": 0, "score": 1, "text": SECRET},
        {
            "id": "1",
            "category": "table",
            "bbox": [0, 30, 100, 60],
            "order": 1,
            "score": 1,
            "cells": [{"row": 0, "col": 0, "bbox": [0, 30, 50, 60], "text": SECRET_EN}],
        },
        {
            "id": "2",
            "category": "chart",
            "bbox": [0, 70, 100, 90],
            "order": 2,
            "score": 1,
            "chart_table": [[SECRET]],
        },
        {"id": "3", "category": "text", "bbox": [0, 0, 1, 1], "order": 3, "score": 7, "text": SECRET},
        {"id": "4", "category": "text", "bbox": [0, 95, 100, 99], "order": 4, "score": 1, "text": SECRET_EN},
        {"id": "4", "category": "text", "bbox": [0, 95, 100, 99], "order": 5, "score": 1, "text": SECRET_EN},
    ]
    harness = await make()
    with caplog.at_level(logging.DEBUG):
        harness.capture("s1")
        await harness.settle()
        harness.channel.last.result(secret_blocks)
        await harness.settle()
        harness.configure({"mode": "text"})
        await harness.settle()
        harness.capture("s2")
        await harness.settle()
        lines: list[JsonValue] = [
            {"bbox": [0, 0, 100, 20], "text": SECRET, "score": 1},
            {"bbox": [0, 26, 100, 46], "text": SECRET_EN, "score": 1},
        ]
        harness.channel.last.result(
            [
                {
                    "id": "0",
                    "category": "text",
                    "bbox": [0, 0, 400, 300],
                    "order": 0,
                    "score": 1,
                    "lines": lines,
                }
            ]
        )
        await harness.settle()
    assert len(harness.completed) == 2
    assert harness.completed[1].segments[0].segment.text == f"{SECRET}{SECRET_EN}"
    assert caplog.records, "应当有日志"
    for record in caplog.records:
        rendered = record.getMessage() + (record.exc_text or "")
        assert SECRET not in rendered
        assert SECRET_EN not in rendered


async def test_text_lines_survive_into_segments_unchanged(make: Factory) -> None:
    # 段落合并用的是 contracts 里的 TextLine，解码后的行与 JSON 一一对应。
    harness = await make()
    harness.configure({"mode": "text"})
    await harness.settle()
    harness.capture("s1")
    await harness.settle()
    harness.channel.last.result(text_mode_json())
    await harness.settle()
    # 聚合步骤把文本行合并成段落块，成员行原样进入段落块的 lines。
    blocks = harness.completed[0].blocks
    assert all(block.category is BlockCategory.TEXT for block in blocks)
    lines = [line for block in blocks for line in block.lines]
    assert TextLine(BBox(20, 20, 320, 40), "one", 0.95) in lines
    assert len(lines) == 5

"""竞速模块的附带截图（需求 ENG-07）：CaptureCompleted → 线程池编码 → 截屏会话的任务带图。

沿用 test_module 的假引擎、假检测器和记录器；模块用注入的编码函数创建，便于观察调用线程与时机。
"""

import asyncio
import base64
import dataclasses
import logging
import threading
from collections.abc import AsyncIterator
from typing import Protocol

import pytest

from lst.contracts.events import EngineRoster, TranslateText
from lst.contracts.ids import SegmentId, SessionId
from lst.contracts.ocr import CaptureCompleted, CaptureGeometry, RawImage
from lst.contracts.translation import Engine, JobImage, Segment
from lst.kernel import EventBus, ModuleContext
from lst.services.race import RaceModule
from lst.services.race.screenshot import encode_screenshot
from tests.services.race.test_module import MODULE_LOGGER, FakeDetector, FakeEngine, eventually

GEOMETRY = CaptureGeometry("screen", (0, 0, 1920, 1080), (0, 0, 40, 20), 1.0)
SHOT = SessionId("shot")
SEGMENTS = [Segment(SegmentId("b1"), "Hello world")]


def raw_image(width: int = 40, height: int = 20, value: int = 0x80) -> RawImage:
    return RawImage(width, height, width * 4, bytes([value]) * (width * height * 4))


class VisionEngine(FakeEngine):
    """接收截图的假引擎：EngineInfo.image_max_side 为给定值。"""

    def __init__(self, name: str, priority: int, side: int, *, billing: bool = False) -> None:
        super().__init__(name, priority, billing=billing)
        self._info = dataclasses.replace(self._info, image_max_side=side)


class RecordingEncoder:
    """记录调用线程与参数，再调用真实编码。gate 不为 None 时在线程里等它放行。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[tuple[int, int, int]] = []  # (宽, 高, 最大边长)
        self.threads: set[int] = set()
        self.gate: threading.Event | None = None
        self.entered = threading.Event()
        self.fail = fail

    def __call__(self, image: RawImage, side: int) -> JobImage:
        self.calls.append((image.width, image.height, side))
        self.threads.add(threading.get_ident())
        self.entered.set()
        if self.gate is not None and not self.gate.wait(timeout=5):
            raise TimeoutError("测试没有放行编码")
        if self.fail:
            raise RuntimeError("编码器出错")
        return encode_screenshot(image, side)


class Rig:
    def __init__(
        self, bus: EventBus, module: RaceModule, ctx: ModuleContext, encoder: RecordingEncoder
    ) -> None:
        self.bus = bus
        self.module = module
        self.ctx = ctx
        self.encoder = encoder

    def roster(self, *engines: Engine) -> None:
        self.bus.publish(EngineRoster(tuple(engines)))

    def capture(self, session: SessionId = SHOT, image: RawImage | None = None) -> None:
        self.bus.publish(CaptureCompleted(session, image or raw_image(), GEOMETRY))

    async def settle(self) -> None:
        async with asyncio.timeout(5):
            await self.bus.drain()
            await self.module.drain()
            await self.bus.drain()


class MakeRig(Protocol):
    def __call__(self, *engines: Engine, encoder: RecordingEncoder | None = None) -> Rig: ...


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), raise_errors=True)
    yield instance
    await instance.aclose()


@pytest.fixture
async def make(bus: EventBus) -> AsyncIterator[MakeRig]:
    rigs: list[Rig] = []

    def factory(*engines: Engine, encoder: RecordingEncoder | None = None) -> Rig:
        encoder = encoder or RecordingEncoder()
        module = RaceModule(detector=FakeDetector(), frame_interval=0, image_encoder=encoder)
        rig = Rig(bus, module, ModuleContext(bus, logging.getLogger(MODULE_LOGGER)), encoder)
        if engines:
            rig.roster(*engines)
        rigs.append(rig)
        return rig

    yield factory
    for rig in rigs:
        if rig.encoder.gate is not None:
            rig.encoder.gate.set()
        await rig.module.stop()
        rig.ctx.close()


async def started(rig: Rig) -> Rig:
    await rig.module.start(rig.ctx)
    await rig.settle()
    return rig


def images_of(engine: FakeEngine) -> list[JobImage | None]:
    return [job.image for job in engine.jobs]


async def run_everyone(rig: Rig, top: FakeEngine, *others: FakeEngine) -> None:
    """开始截屏会话，让每个实例都拿到任务：优先级最高的 top 先停在闸门上，其余实例都开始执行后再放行。

    竞速会取消已经不可能改变显示的尝试；这里要检查的是每个实例收到的任务内容，所以先让它们都开始。
    """
    texts = [s.text for s in SEGMENTS]
    top.gate(*texts)
    for engine in others:
        engine.gate(*texts)
    task = asyncio.create_task(rig.module.start_screenshot_session(SHOT, SEGMENTS))
    await eventually(lambda: all(engine.jobs for engine in (top, *others)))
    top.release(*texts)
    for engine in others:
        engine.release(*texts)
    await task
    await rig.settle()


async def test_only_vision_engines_receive_scaled_image(make: MakeRig) -> None:
    vision = VisionEngine("vision", 1, side=10)
    plain = FakeEngine("argos", 2)
    rig = await started(make(vision, plain))
    rig.capture()
    await run_everyone(rig, vision, plain)

    (image,) = images_of(vision)
    assert image is not None
    assert (image.width, image.height, image.media_type) == (10, 5, "image/png")
    assert images_of(plain) == [None]


async def test_each_side_is_encoded_once_at_capture_time(make: MakeRig) -> None:
    small = VisionEngine("small", 1, side=8)
    large = VisionEngine("large", 2, side=20)
    same = VisionEngine("same", 3, side=8)
    rig = await started(make(small, large, same))
    rig.capture()
    # 编码在 CaptureCompleted 到达时就开始，不等识别完成。
    await eventually(lambda: len(rig.encoder.calls) == 2)
    assert sorted(rig.encoder.calls) == [(40, 20, 8), (40, 20, 20)]

    await run_everyone(rig, small, large, same)
    small_image, large_image, same_image = (images_of(e)[0] for e in (small, large, same))
    assert small_image is not None and large_image is not None
    assert (small_image.width, large_image.width) == (8, 20)
    assert same_image is small_image
    assert len(rig.encoder.calls) == 2

    # 同一会话再次开始（重新识别）时复用已编码的截图。
    await rig.module.start_screenshot_session(SHOT, SEGMENTS)
    await rig.settle()
    assert len(rig.encoder.calls) == 2


async def test_encoding_runs_in_thread_without_blocking_loop(make: MakeRig) -> None:
    encoder = RecordingEncoder()
    encoder.gate = threading.Event()
    vision = VisionEngine("vision", 1, side=10)
    rig = await started(make(vision, encoder=encoder))
    rig.capture()
    session = asyncio.create_task(rig.module.start_screenshot_session(SHOT, SEGMENTS))
    await asyncio.to_thread(encoder.entered.wait, 5)

    # 编码函数停在线程里时，事件循环照常运转。
    ticks = 0
    for _ in range(20):
        await asyncio.sleep(0)
        ticks += 1
    assert ticks == 20
    assert not session.done()  # 截屏会话等待截图编码完成后才开始

    encoder.gate.set()
    await asyncio.wait_for(session, 5)
    await rig.settle()
    assert threading.get_ident() not in encoder.threads
    assert images_of(vision)[0] is not None


async def test_no_vision_engine_means_no_encoding(make: MakeRig) -> None:
    plain = FakeEngine("argos", 1)
    rig = await started(make(plain))
    rig.capture()
    await rig.module.start_screenshot_session(SHOT, SEGMENTS)
    await rig.settle()
    assert rig.encoder.calls == []
    assert images_of(plain) == [None]


async def test_other_session_gets_no_image(make: MakeRig) -> None:
    vision = VisionEngine("vision", 1, side=10)
    rig = await started(make(vision))
    rig.capture(SessionId("older"))
    await rig.module.start_screenshot_session(SHOT, SEGMENTS)
    await rig.settle()
    assert images_of(vision) == [None]


async def test_new_capture_replaces_old_image(make: MakeRig) -> None:
    vision = VisionEngine("vision", 1, side=40)
    rig = await started(make(vision))
    rig.capture(SessionId("first"), raw_image(value=0x10))
    rig.capture(SessionId("second"), raw_image(value=0xF0))
    await rig.module.start_screenshot_session(SessionId("first"), SEGMENTS)
    await rig.settle()
    assert images_of(vision) == [None]
    await rig.module.start_screenshot_session(SessionId("second"), SEGMENTS)
    await rig.settle()
    expected = encode_screenshot(raw_image(value=0xF0), 40)
    assert images_of(vision)[-1] == expected


async def test_bilingual_session_has_no_image(make: MakeRig) -> None:
    vision = VisionEngine("vision", 1, side=10)
    rig = await started(make(vision))
    rig.capture()
    await rig.settle()
    rig.bus.send(TranslateText(SHOT, "Hello world", engine_id=vision.info.id))
    await rig.settle()
    assert vision.jobs
    assert images_of(vision) == [None]


async def test_engine_added_after_capture_is_encoded_lazily(make: MakeRig) -> None:
    plain = FakeEngine("argos", 1)
    rig = await started(make(plain))
    rig.capture()
    await rig.settle()
    assert rig.encoder.calls == []

    vision = VisionEngine("vision", 2, side=10)
    rig.roster(plain, vision)
    await rig.settle()
    await run_everyone(rig, plain, vision)
    assert rig.encoder.calls == [(40, 20, 10)]
    assert images_of(vision)[0] is not None


async def test_encoding_failure_translates_without_image(
    make: MakeRig, caplog: pytest.LogCaptureFixture
) -> None:
    vision = VisionEngine("vision", 1, side=10)
    rig = await started(make(vision, encoder=RecordingEncoder(fail=True)))
    rig.capture()
    await rig.module.start_screenshot_session(SHOT, SEGMENTS)
    await rig.settle()
    assert images_of(vision) == [None]
    assert "截图编码失败" in caplog.text


async def test_logs_contain_no_image_data(make: MakeRig, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    vision = VisionEngine("vision", 1, side=40)
    rig = await started(make(vision))
    image = raw_image(value=0x5A)
    rig.capture(image=image)
    await rig.module.start_screenshot_session(SHOT, SEGMENTS)
    await rig.settle()
    encoded = images_of(vision)[0]
    assert encoded is not None
    assert "截图编码完成" in caplog.text
    payload = base64.b64encode(encoded.data).decode()
    for needle in (payload[:40], payload[-40:], repr(encoded.data[:16]), repr(image.data[:16])):
        assert needle not in caplog.text
    assert "data=" not in repr(encoded)


async def test_new_capture_while_waiting_keeps_pending_session(make: MakeRig) -> None:
    """截屏会话等待编码时又截了一张图：旧编码照常完成，等待它的会话带着旧截图开始，不抛出取消。"""
    encoder = RecordingEncoder()
    encoder.gate = threading.Event()
    vision = VisionEngine("vision", 1, side=10)
    rig = await started(make(vision, encoder=encoder))
    rig.capture()
    session = asyncio.create_task(rig.module.start_screenshot_session(SHOT, SEGMENTS))
    await asyncio.to_thread(encoder.entered.wait, 5)

    rig.capture(SessionId("next"))
    await rig.bus.drain()
    encoder.gate.set()
    await asyncio.wait_for(session, 5)
    await rig.settle()

    assert not session.cancelled()
    assert [job.session_id for job in vision.jobs] == [SHOT]
    assert images_of(vision)[0] is not None


async def test_stop_while_waiting_for_encoding_returns_quietly(make: MakeRig) -> None:
    """模块在截屏会话等待编码时停止：调用方正常返回，不开始会话，也不收到 CancelledError。"""
    encoder = RecordingEncoder()
    encoder.gate = threading.Event()
    vision = VisionEngine("vision", 1, side=10)
    rig = await started(make(vision, encoder=encoder))
    rig.capture()
    session = asyncio.create_task(rig.module.start_screenshot_session(SHOT, SEGMENTS))
    await asyncio.to_thread(encoder.entered.wait, 5)

    await rig.module.stop()
    encoder.gate.set()
    await asyncio.wait_for(session, 5)

    assert not session.cancelled()
    assert vision.jobs == []


async def test_rejected_roster_does_not_change_image_sides(
    make: MakeRig, caplog: pytest.LogCaptureFixture
) -> None:
    """调度器拒收的名册（实例号重复）不影响截图编码：仍按原来的名册决定要编码哪些尺寸。"""
    plain = FakeEngine("argos", 1)
    rig = await started(make(plain))
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        rig.roster(VisionEngine("dup", 1, side=10), VisionEngine("dup", 2, side=20))
        await rig.settle()
    assert "引擎名册无效" in caplog.text

    rig.capture()
    await rig.module.start_screenshot_session(SHOT, SEGMENTS)
    await rig.settle()
    assert rig.encoder.calls == []
    assert images_of(plain) == [None]

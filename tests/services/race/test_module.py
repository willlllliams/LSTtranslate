"""竞速模块测试：真实事件总线（raise_errors=True）→ RaceModule → RaceScheduler → 假引擎。

截屏入口既直接调用 start_screenshot_session，也经 OcrCompleted 触发（模块订阅它，用其中的段开始会话）。

语言检测用假的检测器，按文本给出检测结果，必要时在闸门上停住，以控制检测完成的先后。
假引擎按段文本设闸门，测试放行后才产出结果。
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

import pytest

from lst.contracts.config import AppConfig
from lst.contracts.events import (
    AttemptRecorded,
    EngineRoster,
    SegmentsUpdated,
    SessionProgress,
    TranslateText,
)
from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId
from lst.contracts.ocr import (
    BBox,
    BlockCategory,
    CaptureCompleted,
    CaptureGeometry,
    LayoutBlock,
    OcrCompleted,
    OcrSegment,
    RawImage,
)
from lst.contracts.translation import (
    AttemptState,
    Engine,
    EngineInfo,
    EngineKind,
    Job,
    Segment,
    SegmentOutcome,
    SegmentView,
    SkipReason,
)
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, Module, ModuleContext
from lst.services.race import (
    BilingualConfig,
    RaceModule,
    ScreenshotConfig,
    bilingual_langs,
    screenshot_langs,
)

ZH, ZH_HANT, EN, JA, FR = (Lang(x) for x in ("zh-Hans", "zh-Hant", "en", "ja", "fr"))
LF = chr(0x0A)
MODULE_LOGGER = "lst.race"

# 假检测器的默认结果，按文本查表；表里没有的文本检测为英文。
DETECTED: dict[str, str | None] = {
    "你好世界": "zh-Hans",
    "今天天气很好": "zh-Hans",
    "Hello world": "en",
    "Good morning": "en",
    "Bonjour le monde": "fr",
    "こんにちは": "ja",
    "Xyzzy": None,
}


# ---------------------------------------------------------------- 测试替身


class FakeDetector:
    """按文本查表给出检测结果。gates 中的文本要等测试放行后才返回；fail 为真时抛出异常。"""

    def __init__(self, table: Mapping[str, str | None] | None = None) -> None:
        self.table = dict(DETECTED if table is None else table)
        self.calls: list[tuple[str, ...]] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.warm_ups = 0
        self.fail = False
        self.fail_warm_up = False

    async def warm_up(self) -> None:
        self.warm_ups += 1
        if self.fail_warm_up:
            raise RuntimeError("模型文件损坏")

    async def detect_many(self, texts: Sequence[str]) -> list[Lang | None]:
        self.calls.append(tuple(texts))
        for text in texts:
            gate = self.gates.get(text)
            if gate is not None:
                await gate.wait()
        if self.fail:
            raise RuntimeError("检测器崩溃")
        results: list[Lang | None] = []
        for text in texts:
            code = self.table.get(text, "en")
            results.append(None if code is None else Lang(code))
        return results

    def gate(self, text: str) -> asyncio.Event:
        self.gates[text] = asyncio.Event()
        return self.gates[text]


class FakeEngine:
    """支持任何语言对的假引擎。gates 中有闸门的段文本，要等测试放行后才产出结果。"""

    def __init__(
        self, name: str, priority: int, *, billing: bool = False, max_batch_segments: int = 8
    ) -> None:
        self._info = EngineInfo(
            id=EngineId(name),
            name=name,
            kind=EngineKind.ONLINE_LLM if billing else EngineKind.LOCAL_NMT,
            priority=priority,
            billing=billing,
            max_batch_segments=max_batch_segments,
        )
        self.jobs: list[Job] = []
        self.cancelled: list[Job] = []
        self.gates: dict[str, asyncio.Event] = {}

    @property
    def id(self) -> EngineId:
        return self._info.id

    @property
    def info(self) -> EngineInfo:
        return self._info

    def supports(self, source: Lang, target: Lang) -> bool:
        return True

    def gate(self, *texts: str) -> None:
        for text in texts:
            self.gates[text] = asyncio.Event()

    def release(self, *texts: str) -> None:
        for text in texts:
            self.gates[text].set()

    def translated(self, text: str) -> str:
        return f"{self.id}:{text}"

    def sessions(self) -> list[SessionId]:
        return [job.session_id for job in self.jobs]

    def langs_by_text(self) -> dict[str, tuple[Lang, Lang]]:
        """每个收到过的段文本对应的（源语言，目标语言）。"""
        return {s.text: (job.source_lang, job.target_lang) for job in self.jobs for s in job.segments}

    async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        self.jobs.append(job)
        try:
            for segment in job.segments:
                gate = self.gates.get(segment.text)
                if gate is not None:
                    await gate.wait()
                yield SegmentOutcome(segment.id, text=self.translated(segment.text))
        except asyncio.CancelledError:
            self.cancelled.append(job)
            raise


@dataclass
class Recorder:
    """记录总线上竞速模块发布的三类事件。"""

    updates: list[SegmentsUpdated] = field(default_factory=lambda: list[SegmentsUpdated]())
    progress: list[SessionProgress] = field(default_factory=lambda: list[SessionProgress]())
    attempts: list[AttemptRecorded] = field(default_factory=lambda: list[AttemptRecorded]())

    def sessions(self) -> set[SessionId]:
        return {m.session_id for m in (*self.updates, *self.progress, *self.attempts)}

    def count(self, session: str) -> int:
        return sum(m.session_id == session for m in (*self.updates, *self.progress, *self.attempts))

    def find(self, session: str, segment: str) -> SegmentView | None:
        views = [
            v for m in self.updates if m.session_id == session for v in m.views if v.segment_id == segment
        ]
        return views[-1] if views else None

    def view(self, session: str, segment: str) -> SegmentView:
        view = self.find(session, segment)
        assert view is not None, f"会话 {session} 没有段 {segment} 的快照"
        return view

    def shown_engine(self, session: str, segment: str) -> str | None:
        view = self.find(session, segment)
        return None if view is None else view.shown_engine

    def engines(self, session: str, segment: str) -> set[str]:
        """进入过终态的实例。"""
        return {
            m.attempt.engine_id for m in self.attempts if m.session_id == session and m.segment_id == segment
        }

    def last_progress(self, session: str) -> tuple[int, int]:
        items = [(m.finished, m.total) for m in self.progress if m.session_id == session]
        assert items, f"会话 {session} 没有进度事件"
        return items[-1]

    def states(self, session: str, segment: str, engine: FakeEngine) -> list[AttemptState]:
        return [
            m.attempt.state
            for m in self.attempts
            if m.session_id == session and m.segment_id == segment and m.attempt.engine_id == engine.id
        ]


class Harness:
    """事件总线、竞速模块、记录器放在一起，并提供发命令、发配置、等待稳定的便捷方法。

    translate 不指定 engine 时，请求交给创建时给出的第一个引擎（双语窗口里选中的服务）。
    """

    def __init__(
        self,
        bus: EventBus,
        module: RaceModule,
        ctx: ModuleContext,
        recorder: Recorder,
        default_engine: str | None,
    ) -> None:
        self.bus = bus
        self.module = module
        self.ctx = ctx
        self.recorder = recorder
        self.default_engine = default_engine
        self._revision = 0

    def translate(
        self,
        session: str,
        text: str,
        *,
        source: str | None = None,
        target: str | None = None,
        engine: str | None = "",
    ) -> None:
        """engine 为空串时用 default_engine，为 None 时表示没有可用的服务。"""
        chosen = self.default_engine if engine == "" else engine
        self.bus.send(
            TranslateText(
                SessionId(session),
                text,
                source=None if source is None else Lang(source),
                target=None if target is None else Lang(target),
                engine_id=None if chosen is None else EngineId(chosen),
            )
        )

    def configure(self, **sections: JsonValue) -> None:
        self._revision += 1
        self.bus.publish(AppConfig(sections=dict(sections), revision=self._revision))

    def roster(self, *engines: Engine) -> None:
        self.bus.publish(EngineRoster(tuple(engines)))

    async def wait_running(self, session: str, segment: str, engine: FakeEngine) -> None:
        """等到界面看到该实例的尝试进入 RUNNING，并且此前发布的事件都已交给记录器。

        之后这个会话在结果到达之前不会再有新事件，测试据此统计“取消之后是否还有事件”。
        """

        def running() -> bool:
            view = self.recorder.find(session, segment)
            return view is not None and any(
                a.engine_id == engine.id and a.state is AttemptState.RUNNING for a in view.attempts
            )

        await eventually(running)
        await self.bus.drain()

    async def settle(self) -> None:
        """等总线上的处理函数、调度器的任务和合帧刷新都完成，事件都交给记录器。"""
        async with asyncio.timeout(5):
            await self.bus.drain()
            await self.module.drain()
            await self.bus.drain()


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), raise_errors=True)
    yield instance
    await instance.aclose()


class MakeHarness(Protocol):
    def __call__(self, *engines: Engine, detector: FakeDetector | None = None) -> Harness: ...


@pytest.fixture
async def make(bus: EventBus) -> AsyncIterator[MakeHarness]:
    """factory(*engines, detector=None)：创建模块（不启动），并把给出的引擎发布为名册。"""
    harnesses: list[Harness] = []
    recorder = Recorder()
    bus.subscribe(SegmentsUpdated, recorder.updates.append)
    bus.subscribe(SessionProgress, recorder.progress.append)
    bus.subscribe(AttemptRecorded, recorder.attempts.append)

    def factory(*engines: Engine, detector: FakeDetector | None = None) -> Harness:
        module = RaceModule(detector=detector or FakeDetector(), frame_interval=0)
        ctx = ModuleContext(bus, logging.getLogger(MODULE_LOGGER))
        harness = Harness(bus, module, ctx, recorder, engines[0].info.id if engines else None)
        if engines:
            harness.roster(*engines)
        harnesses.append(harness)
        return harness

    yield factory
    for harness in harnesses:
        await harness.module.stop()
        harness.ctx.close()


async def started(harness: Harness) -> Harness:
    await harness.module.start(harness.ctx)
    await harness.settle()
    return harness


async def eventually(predicate: Callable[[], bool], timeout: float = 2.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.001)


def lines(*texts: str) -> str:
    return LF.join(texts)


# ---------------------------------------------------------------- 纯函数：语言对规则


@pytest.mark.parametrize(
    ("detected", "source", "target", "expected"),
    [
        # 互译对的三种情况
        (ZH, None, None, (ZH, EN)),
        (EN, None, None, (EN, ZH)),
        (FR, None, None, (FR, ZH)),
        # 手动指定
        (EN, JA, None, (JA, ZH)),  # 源语言手动，目标按互译对
        (None, EN, None, (EN, ZH)),
        (EN, None, FR, (EN, FR)),  # 目标语言手动
        (EN, ZH, JA, (ZH, JA)),
        # 检测不出：源语言取 pair[0]，目标为自动时取源语言本身（按同语言跳过）
        (None, None, None, (ZH, ZH)),
        (None, None, EN, (ZH, EN)),
    ],
)
def test_bilingual_langs(
    detected: Lang | None, source: Lang | None, target: Lang | None, expected: tuple[Lang, Lang]
) -> None:
    assert bilingual_langs(detected, source, target, (ZH, EN)) == expected


def test_bilingual_langs_uses_configured_pair() -> None:
    pair = (JA, EN)
    assert bilingual_langs(JA, None, None, pair) == (JA, EN)
    assert bilingual_langs(EN, None, None, pair) == (EN, JA)
    assert bilingual_langs(ZH, None, None, pair) == (ZH, JA)
    assert bilingual_langs(None, None, None, pair) == (JA, JA)


def test_screenshot_langs() -> None:
    assert screenshot_langs(EN, ZH) == (EN, ZH)
    assert screenshot_langs(None, ZH) == (ZH, ZH)


# ---------------------------------------------------------------- 生命周期与配置


async def test_satisfies_module_protocol(make: MakeHarness) -> None:
    harness = make()
    module: Module = harness.module
    assert module.name == "race"


async def test_start_applies_existing_config_and_roster(make: MakeHarness) -> None:
    """config、engines 先于 race 启动：总线上已有的配置和名册在 start 返回时就已生效。"""
    engine = FakeEngine("argos", 1)
    harness = make(engine)
    harness.configure(screenshot={"target_lang": "ja"})
    await harness.module.start(harness.ctx)  # 不等总线分发
    assert harness.module.screenshot_config.target_lang == "ja"
    await harness.module.start_screenshot_session(SessionId("shot"), [Segment(SegmentId("b1"), "Hello")])
    await harness.settle()
    assert engine.langs_by_text() == {"Hello": (EN, JA)}


async def test_missing_sections_use_defaults(make: MakeHarness) -> None:
    harness = await started(make())
    harness.configure(bilingual={"pair": ["ja", "en"]})
    await harness.settle()
    assert harness.module.bilingual_config.pair == ("ja", "en")
    harness.configure()  # 两节都缺失
    await harness.settle()
    assert harness.module.screenshot_config == ScreenshotConfig()
    assert harness.module.bilingual_config == BilingualConfig()


async def test_old_sections_and_fields_are_ignored(make: MakeHarness) -> None:
    """旧配置里的 [race] 节与 bilingual.auto_billing 不影响解析，同一节的其他字段照常生效。"""
    harness = await started(make())
    harness.configure(
        race={"mode": "full", "show_source_marker": {"overlay": True}},
        bilingual={"pair": ["ja", "en"], "auto_billing": True},
    )
    await harness.settle()
    assert harness.module.bilingual_config.pair == ("ja", "en")


async def test_unparsable_section_keeps_previous_value(
    make: MakeHarness, caplog: pytest.LogCaptureFixture
) -> None:
    harness = await started(make())
    harness.configure(bilingual={"pair": ["ja", "en"]}, screenshot={"target_lang": "ja"})
    await harness.settle()
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        harness.configure(bilingual={"pair": ["ja", "ja"]}, screenshot={"target_lang": "fr"})
        await harness.settle()
    assert harness.module.bilingual_config.pair == ("ja", "en")
    assert harness.module.screenshot_config.target_lang == "fr"  # 其他节照常更新
    assert "[bilingual]" in caplog.text


async def test_warm_up_runs_once_after_start(make: MakeHarness) -> None:
    detector = FakeDetector()
    harness = make(detector=detector)
    assert detector.warm_ups == 0
    await started(harness)
    await eventually(lambda: detector.warm_ups == 1)


async def test_warm_up_failure_is_logged(make: MakeHarness, caplog: pytest.LogCaptureFixture) -> None:
    detector = FakeDetector()
    detector.fail_warm_up = True
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        await started(make(detector=detector))
        await eventually(lambda: "预热失败" in caplog.text)


async def test_roster_takes_effect(make: MakeHarness) -> None:
    argos, deepl = FakeEngine("argos", 2), FakeEngine("deepl", 1)
    harness = await started(make(argos))
    harness.translate("s1", "Hello world")
    await harness.settle()
    assert argos.sessions() == ["s1"]

    harness.roster(deepl)
    harness.translate("s2", "Good morning", engine="deepl")
    await harness.settle()
    assert argos.sessions() == ["s1"]
    assert deepl.sessions() == ["s2"]
    assert harness.recorder.view("s2", "p1").shown_engine == "deepl"


async def test_invalid_roster_is_rejected(make: MakeHarness, caplog: pytest.LogCaptureFixture) -> None:
    argos = FakeEngine("argos", 1)
    harness = await started(make(argos))
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        harness.roster(FakeEngine("dup", 1), FakeEngine("dup", 2))
        await harness.settle()
    assert "引擎名册无效" in caplog.text
    harness.translate("s1", "Hello world")
    await harness.settle()
    assert argos.sessions() == ["s1"]  # 原来的名册仍然有效


# ---------------------------------------------------------------- 双语窗口：会话与事件


async def test_translate_text_uses_only_the_chosen_engine(make: MakeHarness) -> None:
    """双语窗口的请求只交给选中的服务（BI-04），优先级更高的其他实例不参加。"""
    deepseek, argos = FakeEngine("deepseek", 1), FakeEngine("argos", 2)
    harness = await started(make(deepseek, argos))
    harness.translate("s1", lines("Hello world", "", "  Good morning  "), engine="argos")
    await harness.settle()

    recorder = harness.recorder
    first = next(m for m in recorder.updates if m.session_id == "s1")
    assert [v.segment_id for v in first.views] == ["p1", "p2"]  # 第一帧包含全部段
    for segment, text in (("p1", "Hello world"), ("p2", "Good morning")):
        view = recorder.view("s1", segment)
        assert view.source_text == text
        assert view.shown_engine == "argos"
        assert view.display_text == argos.translated(text)
        assert view.finished
        assert recorder.engines("s1", segment) == {"argos"}
    assert deepseek.jobs == []
    assert recorder.last_progress("s1") == (2, 2)
    assert {job.context for job in argos.jobs} == {lines("Hello world", "Good morning")}
    assert recorder.sessions() == {"s1"}


async def test_pair_rule_three_cases(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.translate("s1", lines("你好世界", "Hello world", "Bonjour le monde"))
    await harness.settle()
    assert engine.langs_by_text() == {
        "你好世界": (ZH, EN),  # 源语言是 pair[0]，译成 pair[1]
        "Hello world": (EN, ZH),  # 源语言是 pair[1]，译成 pair[0]
        "Bonjour le monde": (FR, ZH),  # 都不是，译成 pair[0]
    }


async def test_pair_rule_follows_config(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.configure(bilingual={"pair": ["ja", "en"]})
    await harness.settle()
    harness.translate("s1", lines("こんにちは", "Hello world", "你好世界"))
    await harness.settle()
    assert engine.langs_by_text() == {"こんにちは": (JA, EN), "Hello world": (EN, JA), "你好世界": (ZH, JA)}


async def test_manual_source_skips_detection(make: MakeHarness) -> None:
    detector = FakeDetector()
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine, detector=detector))
    harness.translate("s1", lines("Hello world", "Bonjour le monde"), source="ja")
    await harness.settle()
    assert detector.calls == []
    assert engine.langs_by_text() == {"Hello world": (JA, ZH), "Bonjour le monde": (JA, ZH)}


async def test_manual_target_overrides_pair(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.translate("s1", lines("你好世界", "Hello world", "Bonjour le monde"), target="fr")
    await harness.settle()
    assert engine.langs_by_text() == {"你好世界": (ZH, FR), "Hello world": (EN, FR)}
    assert harness.recorder.view("s1", "p3").skip_reason is SkipReason.SAME_LANGUAGE  # fr → fr


async def test_manual_codes_are_normalized(make: MakeHarness, caplog: pytest.LogCaptureFixture) -> None:
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.translate("s1", "Hello world", source="zh-CN", target="en-US")
    await harness.settle()
    assert engine.langs_by_text() == {"Hello world": (ZH, EN)}

    with caplog.at_level(logging.WARNING, logger=MODULE_LOGGER):
        harness.translate("s2", "Good morning", source="english")  # 非法代码按自动处理
        await harness.settle()
    assert engine.langs_by_text()["Good morning"] == (EN, ZH)
    assert "不是合法的语言代码" in caplog.text


async def test_segment_without_text_is_skipped_as_no_text(make: MakeHarness) -> None:
    """只有数字、符号或单个字母的段不送去翻译，标为“无需翻译”，显示原文（DSP-09）。"""
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.translate("s1", lines("12345", "☆4", "a", "Hello world"), target="ja")
    await harness.settle()
    for segment, text in (("p1", "12345"), ("p2", "☆4"), ("p3", "a")):
        view = harness.recorder.view("s1", segment)
        assert (view.skip_reason, view.display_text, view.finished) == (SkipReason.NO_TEXT, text, True)
    assert set(engine.langs_by_text()) == {"Hello world"}
    assert harness.recorder.last_progress("s1") == (4, 4)


async def test_undetectable_segment_is_skipped_as_same_language(make: MakeHarness) -> None:
    """检测不出时源语言取 pair[0]；目标语言为自动时也取 pair[0]，该段按同语言跳过，显示原文。"""
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.translate("s1", lines("Xyzzy", "Hello world"))
    await harness.settle()
    view = harness.recorder.view("s1", "p1")
    assert view.skip_reason is SkipReason.SAME_LANGUAGE
    assert view.display_text == "Xyzzy"
    assert view.finished
    assert "Xyzzy" not in engine.langs_by_text()
    assert harness.recorder.last_progress("s1") == (2, 2)


async def test_undetectable_segment_with_manual_target_is_translated(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.translate("s1", "Xyzzy", target="en")
    await harness.settle()
    assert engine.langs_by_text() == {"Xyzzy": (ZH, EN)}


async def test_detector_failure_falls_back_to_undetected(
    make: MakeHarness, caplog: pytest.LogCaptureFixture
) -> None:
    detector = FakeDetector()
    detector.fail = True
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine, detector=detector))
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        harness.translate("s1", "Hello world", target="ja")
        await harness.settle()
    assert "语言检测失败" in caplog.text
    assert engine.langs_by_text() == {"Hello world": (ZH, JA)}


# ---------------------------------------------------------------- 选中的服务


async def test_chosen_billing_engine_translates_on_every_request(make: MakeHarness) -> None:
    """选中的是计费服务时，每次请求都交给它，不再等 Ctrl+Enter（BI-04）。"""
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    harness = await started(make(argos, deepseek))
    harness.translate("s1", "Hello world", engine="deepseek")
    await harness.settle()
    assert deepseek.sessions() == ["s1"]
    assert argos.jobs == []
    assert harness.recorder.view("s1", "p1").shown_engine == "deepseek"


async def test_request_without_engine_skips_every_segment(make: MakeHarness) -> None:
    """没有可用的服务（engine_id 为 None）时各段按“没有支持的服务”跳过，显示原文。"""
    argos = FakeEngine("argos", 1)
    harness = await started(make(argos))
    harness.translate("s1", lines("Hello world", "Good morning"), engine=None)
    await harness.settle()
    assert argos.jobs == []
    for segment in ("p1", "p2"):
        view = harness.recorder.view("s1", segment)
        assert (view.skip_reason, view.finished) == (SkipReason.NO_ENGINE, True)


async def test_removed_engine_skips_as_no_engine(make: MakeHarness) -> None:
    """选中的服务已经不在名册里（停用或删除）：段按“没有支持的服务”跳过。"""
    argos = FakeEngine("argos", 1)
    harness = await started(make(argos))
    harness.translate("s1", "Hello world", engine="deepl")
    await harness.settle()
    assert argos.jobs == []
    assert harness.recorder.view("s1", "p1").skip_reason is SkipReason.NO_ENGINE


async def test_resending_same_text_starts_new_session(make: MakeHarness) -> None:
    """立即翻译总是开始新会话：同样的文本以新会话号重新翻译，旧会话取消。"""
    argos = FakeEngine("argos", 1)
    argos.gate("Hello world")
    harness = await started(make(argos))
    harness.translate("s1", "Hello world")
    await harness.wait_running("s1", "p1", argos)
    events_before = harness.recorder.count("s1")

    harness.translate("s2", "Hello world")
    await eventually(lambda: len(argos.cancelled) == 1)
    argos.release("Hello world")
    await harness.settle()
    assert argos.sessions() == ["s1", "s2"]
    assert harness.recorder.count("s1") == events_before
    assert harness.recorder.view("s2", "p1").shown_engine == "argos"


async def test_empty_text_cancels_current_session(make: MakeHarness) -> None:
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    argos.gate("Hello world")
    harness = await started(make(argos, deepseek))
    harness.translate("s1", "Hello world")
    await harness.wait_running("s1", "p1", argos)
    events_before = harness.recorder.count("s1")

    harness.translate("s2", lines("   ", "", "  "))
    await eventually(lambda: len(argos.cancelled) == 1)
    argos.release("Hello world")
    await harness.settle()
    assert harness.recorder.count("s1") == events_before
    assert "s2" not in harness.recorder.sessions()

    # 当前会话已取消：之后的请求照常开始新会话
    harness.translate("s3", "Hello world", engine="deepseek")
    await harness.settle()
    assert deepseek.sessions() == ["s3"]
    assert harness.recorder.view("s3", "p1").shown_engine == "deepseek"


async def test_latest_request_wins_when_detection_finishes_late(make: MakeHarness) -> None:
    """较早的请求检测得慢：它在检测完成后放弃，不会取消较新的会话。"""
    detector = FakeDetector()
    gate = detector.gate("Hello world")
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine, detector=detector))
    harness.translate("s1", "Hello world")
    harness.translate("s2", "Good morning")
    await eventually(lambda: engine.sessions() == ["s2"])
    gate.set()
    await harness.settle()
    assert engine.sessions() == ["s2"]
    assert "s1" not in harness.recorder.sessions()
    assert harness.recorder.view("s2", "p1").finished


async def test_empty_text_supersedes_pending_request(make: MakeHarness) -> None:
    detector = FakeDetector()
    gate = detector.gate("Hello world")
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine, detector=detector))
    harness.translate("s1", "Hello world")
    harness.translate("s2", "")
    await eventually(lambda: len(detector.calls) == 1)
    gate.set()
    await harness.settle()
    assert engine.jobs == []
    assert harness.recorder.sessions() == set()


async def test_duplicate_session_id_keeps_current_session(make: MakeHarness) -> None:
    """会话号与截屏会话重复时调度器拒绝开始，双语窗口的当前会话不受影响。"""
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    harness = await started(make(argos, deepseek))
    await harness.module.start_screenshot_session(SessionId("shared"), [Segment(SegmentId("b1"), "Hi there")])
    harness.translate("s1", "Hello world")
    await harness.settle()

    harness.translate("shared", "Something else")
    with pytest.raises(ExceptionGroup) as info:
        await harness.settle()
    assert info.group_contains(ValueError)

    assert harness.recorder.view("s1", "p1").finished  # 当前会话不受影响
    harness.translate("s2", "Hello world", engine="deepseek")
    await harness.settle()
    assert deepseek.sessions() == ["shared", "s2"]


# ---------------------------------------------------------------- 剪枝


async def test_lower_priority_queued_attempt_is_pruned(make: MakeHarness) -> None:
    """截屏会话里，低优先级实例排队中的尝试在高优先级实例的译文显示后被取消，不调用引擎（DSP-05）。

    argos 每个任务只放一个段，第一个段停在闸门上，第二个段就在 argos 的队列里排队；
    deepseek 很快完成两个段，第二个段由此显示已定。
    """
    deepseek, argos = FakeEngine("deepseek", 1), FakeEngine("argos", 2, max_batch_segments=1)
    argos.gate("Hello world")
    harness = await started(make(deepseek, argos))
    task = asyncio.create_task(
        harness.module.start_screenshot_session(
            SessionId("shot"),
            [Segment(SegmentId("b1"), "Hello world"), Segment(SegmentId("b2"), "Good morning")],
        )
    )
    await eventually(lambda: harness.recorder.shown_engine("shot", "b2") == "deepseek")
    argos.release("Hello world")
    await task
    await harness.settle()
    assert harness.recorder.states("shot", "b2", argos) == [AttemptState.CANCELLED]
    assert harness.recorder.view("shot", "b2").display_text == deepseek.translated("Good morning")


# ---------------------------------------------------------------- 截屏入口


async def test_screenshot_session_uses_screenshot_target(make: MakeHarness) -> None:
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    harness = await started(make(argos, deepseek))
    harness.configure(screenshot={"target_lang": "ja"})
    await harness.settle()
    segments = [Segment(SegmentId("b1"), "Hello world"), Segment(SegmentId("b2"), "12345")]
    await harness.module.start_screenshot_session(SessionId("shot"), segments)
    await harness.settle()

    assert deepseek.langs_by_text() == {"Hello world": (EN, JA)}
    assert deepseek.sessions() == ["shot"]  # 截屏翻译使用全部可用实例，计费实例也在其中
    assert harness.recorder.view("shot", "b1").shown_engine == "deepseek"
    assert harness.recorder.view("shot", "b2").skip_reason is SkipReason.NO_TEXT
    assert harness.recorder.last_progress("shot") == (2, 2)
    assert {job.context for job in deepseek.jobs} == {lines("Hello world", "12345")}


async def test_screenshot_and_bilingual_sessions_coexist(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    engine.gate("Hello world")
    harness = await started(make(engine))
    await harness.module.start_screenshot_session(
        SessionId("shot"), [Segment(SegmentId("b1"), "Hello world")]
    )
    harness.translate("s1", "Good morning")
    await harness.bus.drain()
    engine.release("Hello world")
    await harness.settle()
    assert harness.recorder.view("shot", "b1").shown_engine == "argos"
    assert harness.recorder.view("s1", "p1").shown_engine == "argos"
    assert engine.cancelled == []


async def test_newer_screenshot_wins_when_detection_finishes_late(make: MakeHarness) -> None:
    detector = FakeDetector()
    gate = detector.gate("Hello world")
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine, detector=detector))
    first = asyncio.create_task(
        harness.module.start_screenshot_session(SessionId("old"), [Segment(SegmentId("b1"), "Hello world")])
    )
    await eventually(lambda: len(detector.calls) == 1)
    await harness.module.start_screenshot_session(
        SessionId("new"), [Segment(SegmentId("b1"), "Good morning")]
    )
    gate.set()
    await first
    await harness.settle()
    assert engine.sessions() == ["new"]
    assert "old" not in harness.recorder.sessions()


async def test_screenshot_before_start_raises(make: MakeHarness) -> None:
    harness = make()
    with pytest.raises(RuntimeError, match="尚未启动"):
        await harness.module.start_screenshot_session(SessionId("shot"), [])


# ---------------------------------------------------------------- OcrCompleted 接线（架构文档第 7 章第 6 步）

SHOT_GEOMETRY = CaptureGeometry("screen", (0, 0, 1920, 1080), (10, 10, 4, 3), 1.0)


def _shot(session: str) -> CaptureCompleted:
    return CaptureCompleted(SessionId(session), RawImage(4, 3, 16, bytes(48)), SHOT_GEOMETRY)


def _ocr(session: str, *texts: str) -> OcrCompleted:
    segments = tuple(
        OcrSegment(Segment(SegmentId(f"p{i}"), text), "0", BBox(0, 10 * i, 100, 10 * i + 10))
        for i, text in enumerate(texts, start=1)
    )
    block = LayoutBlock("0", BlockCategory.TEXT, BBox(0, 0, 100, 100), 0, 1.0)
    return OcrCompleted(SessionId(session), (block,), segments)


async def test_ocr_completed_starts_screenshot_session(make: MakeHarness) -> None:
    """OcrCompleted 的段以同一会话号开始翻译，结果经 SegmentsUpdated 与 SessionProgress 发布。"""
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    harness = await started(make(argos, deepseek))
    harness.bus.publish(_shot("shot"))
    harness.bus.publish(_ocr("shot", "Hello world", "Good morning"))
    await harness.settle()

    assert deepseek.sessions() == ["shot"]  # 截屏翻译使用全部可用实例
    assert harness.recorder.view("shot", "p1").shown_engine == "deepseek"
    assert harness.recorder.view("shot", "p2").display_text == "deepseek:Good morning"
    assert harness.recorder.last_progress("shot") == (2, 2)
    assert deepseek.langs_by_text()["Hello world"] == (EN, ZH)


async def test_ocr_completed_without_capture_still_starts(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.bus.publish(_ocr("shot", "Hello world"))
    await harness.settle()
    assert engine.sessions() == ["shot"]


async def test_stale_ocr_result_is_dropped(make: MakeHarness) -> None:
    """新截屏之后才到达的旧会话识别结果直接丢弃，不取消新会话（CAP-06）。"""
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.bus.publish(_shot("old"))
    harness.bus.publish(_shot("new"))
    harness.bus.publish(_ocr("new", "Good morning"))
    harness.bus.publish(_ocr("old", "Hello world"))
    await harness.settle()
    assert engine.sessions() == ["new"]
    assert "old" not in harness.recorder.sessions()


async def test_new_capture_cancels_running_screenshot_session(make: MakeHarness) -> None:
    """新截屏完成时，上一次截屏还在翻译的任务立即取消，迟到的结果不再发布（CAP-06）。"""
    engine = FakeEngine("argos", 1)
    engine.gate("Hello world")
    harness = await started(make(engine))
    harness.bus.publish(_shot("first"))
    harness.bus.publish(_ocr("first", "Hello world"))
    await harness.wait_running("first", "p1", engine)
    before = harness.recorder.count("first")

    harness.bus.publish(_shot("second"))
    await harness.settle()
    assert engine.cancelled == engine.jobs
    engine.release("Hello world")
    await harness.settle()
    assert harness.recorder.count("first") == before


async def test_new_capture_does_not_touch_bilingual_session(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    engine.gate("Good morning")
    harness = await started(make(engine))
    harness.translate("s1", "Good morning")
    await harness.wait_running("s1", "p1", engine)
    harness.bus.publish(_shot("shot"))
    await harness.bus.drain()
    assert engine.cancelled == []
    engine.release("Good morning")
    await harness.settle()
    assert harness.recorder.view("s1", "p1").shown_engine == "argos"


async def test_ocr_completed_without_segments_starts_nothing(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.bus.publish(_shot("shot"))
    harness.bus.publish(OcrCompleted(SessionId("shot"), (), ()))
    await harness.settle()
    assert engine.jobs == []
    assert "shot" not in harness.recorder.sessions()


async def test_ocr_completed_with_duplicate_segment_ids_is_logged(
    make: MakeHarness, caplog: pytest.LogCaptureFixture
) -> None:
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    duplicated = OcrCompleted(
        SessionId("shot"),
        (),
        tuple(OcrSegment(Segment(SegmentId("p1"), t), "0", BBox(0, 0, 1, 1)) for t in ("a", "b")),
    )
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        harness.bus.publish(duplicated)
        await harness.settle()
    assert "无法开始" in caplog.text
    assert engine.jobs == []


async def test_ocr_completed_after_stop_is_ignored(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    await harness.module.stop()
    harness.bus.publish(_ocr("shot", "Hello world"))
    await harness.settle()
    assert engine.jobs == []


# ---------------------------------------------------------------- 停止


async def test_stop_cancels_sessions_and_ignores_later_messages(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    engine.gate("Hello world")
    harness = await started(make(engine))
    harness.translate("s1", "Hello world")
    await harness.wait_running("s1", "p1", engine)
    events_before = harness.recorder.count("s1")

    await harness.module.stop()
    assert engine.cancelled == engine.jobs
    harness.translate("s2", "Good morning")  # 订阅还在（ctx.close 之前），处理函数直接返回
    harness.configure(screenshot={"target_lang": "ja"})
    harness.roster(FakeEngine("deepl", 2))
    await harness.module.start_screenshot_session(SessionId("shot"), [Segment(SegmentId("b1"), "Hi")])
    await harness.settle()
    assert engine.sessions() == ["s1"]
    assert harness.recorder.count("s1") == events_before
    assert harness.recorder.sessions() <= {"s1"}
    assert harness.module.screenshot_config.target_lang == "zh-Hans"

    await harness.module.stop()  # 重复调用没有效果
    harness.ctx.close()


async def test_stop_during_detection_drops_request(make: MakeHarness) -> None:
    detector = FakeDetector()
    gate = detector.gate("Hello world")
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine, detector=detector))
    harness.translate("s1", "Hello world")
    await eventually(lambda: len(detector.calls) == 1)
    await harness.module.stop()
    gate.set()
    await harness.bus.drain()
    assert engine.jobs == []

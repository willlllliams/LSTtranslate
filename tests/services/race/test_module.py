"""竞速模块测试：真实事件总线（raise_errors=True）→ RaceModule → RaceScheduler → 假引擎。

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
    RaceConfig,
    RaceMode,
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
    "12345": None,
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

    def winner(self, session: str, segment: str) -> str | None:
        view = self.find(session, segment)
        return None if view is None else view.winner

    def engines(self, session: str, segment: str) -> set[str]:
        """进入过终态的参赛实例。"""
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
    """事件总线、竞速模块、记录器放在一起，并提供发命令、发配置、等待稳定的便捷方法。"""

    def __init__(self, bus: EventBus, module: RaceModule, ctx: ModuleContext, recorder: Recorder) -> None:
        self.bus = bus
        self.module = module
        self.ctx = ctx
        self.recorder = recorder
        self._revision = 0

    def translate(
        self,
        session: str,
        text: str,
        *,
        source: str | None = None,
        target: str | None = None,
        billing: bool = False,
    ) -> None:
        self.bus.send(
            TranslateText(
                SessionId(session),
                text,
                source=None if source is None else Lang(source),
                target=None if target is None else Lang(target),
                include_billing=billing,
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
        harness = Harness(bus, module, ctx, recorder)
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
    harness.configure(screenshot={"target_lang": "ja"}, race={"mode": "full"})
    await harness.module.start(harness.ctx)  # 不等总线分发
    assert harness.module.mode is RaceMode.FULL
    assert harness.module.screenshot_config.target_lang == "ja"
    await harness.module.start_screenshot_session(SessionId("shot"), [Segment(SegmentId("b1"), "Hello")])
    await harness.settle()
    assert engine.langs_by_text() == {"Hello": (EN, JA)}


async def test_missing_sections_use_defaults(make: MakeHarness) -> None:
    harness = await started(make())
    harness.configure(bilingual={"pair": ["ja", "en"], "auto_billing": True}, race={"mode": "full"})
    await harness.settle()
    assert harness.module.bilingual_config.pair == ("ja", "en")
    harness.configure()  # 三节都缺失
    await harness.settle()
    assert harness.module.race_config == RaceConfig()
    assert harness.module.screenshot_config == ScreenshotConfig()
    assert harness.module.bilingual_config == BilingualConfig()
    assert harness.module.mode is RaceMode.SAVER


async def test_unparsable_section_keeps_previous_value(
    make: MakeHarness, caplog: pytest.LogCaptureFixture
) -> None:
    harness = await started(make())
    harness.configure(race={"mode": "full"}, screenshot={"target_lang": "ja"})
    await harness.settle()
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        harness.configure(race={"mode": "turbo"}, screenshot={"target_lang": "fr"})
        await harness.settle()
    assert harness.module.mode is RaceMode.FULL
    assert harness.module.screenshot_config.target_lang == "fr"  # 其他节照常更新
    assert "[race]" in caplog.text


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
    harness.translate("s2", "Good morning")
    await harness.settle()
    assert argos.sessions() == ["s1"]
    assert deepl.sessions() == ["s2"]
    assert harness.recorder.view("s2", "p1").winner == "deepl"


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


async def test_translate_text_publishes_race_events(make: MakeHarness) -> None:
    deepseek, argos = FakeEngine("deepseek", 1), FakeEngine("argos", 2)
    harness = await started(make(deepseek, argos))
    harness.translate("s1", lines("Hello world", "", "  Good morning  "))
    await harness.settle()

    recorder = harness.recorder
    first = next(m for m in recorder.updates if m.session_id == "s1")
    assert [v.segment_id for v in first.views] == ["p1", "p2"]  # 第一帧包含全部段
    for segment, text in (("p1", "Hello world"), ("p2", "Good morning")):
        view = recorder.view("s1", segment)
        assert view.source_text == text
        assert view.winner == "deepseek"
        assert view.display_text == deepseek.translated(text)
        assert view.finished
        assert recorder.engines("s1", segment) == {"deepseek", "argos"}
    assert recorder.last_progress("s1") == (2, 2)
    assert {job.context for job in deepseek.jobs} == {lines("Hello world", "Good morning")}
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


async def test_undetectable_segment_is_skipped_as_same_language(make: MakeHarness) -> None:
    """检测不出时源语言取 pair[0]；目标语言为自动时也取 pair[0]，该段按同语言跳过，显示原文。"""
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.translate("s1", lines("12345", "Hello world"))
    await harness.settle()
    view = harness.recorder.view("s1", "p1")
    assert view.skip_reason is SkipReason.SAME_LANGUAGE
    assert view.display_text == "12345"
    assert view.finished
    assert "12345" not in engine.langs_by_text()
    assert harness.recorder.last_progress("s1") == (2, 2)


async def test_undetectable_segment_with_manual_target_is_translated(make: MakeHarness) -> None:
    engine = FakeEngine("argos", 1)
    harness = await started(make(engine))
    harness.translate("s1", "12345", target="en")
    await harness.settle()
    assert engine.langs_by_text() == {"12345": (ZH, EN)}


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


# ---------------------------------------------------------------- 计费实例


async def test_billing_engines_wait_for_ctrl_enter_by_default(make: MakeHarness) -> None:
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    harness = await started(make(argos, deepseek))
    harness.translate("s1", "Hello world")
    await harness.settle()
    assert argos.sessions() == ["s1"]
    assert deepseek.jobs == []
    assert harness.recorder.view("s1", "p1").winner == "argos"


async def test_include_billing_with_new_text_starts_session_with_billing(make: MakeHarness) -> None:
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    harness = await started(make(argos, deepseek))
    harness.translate("s1", "Hello world", billing=True)
    await harness.settle()
    assert deepseek.sessions() == ["s1"]
    assert harness.recorder.engines("s1", "p1") == {"argos", "deepseek"}
    assert harness.recorder.view("s1", "p1").winner == "deepseek"


async def test_auto_billing_lets_billing_engines_join_on_pause(make: MakeHarness) -> None:
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    harness = await started(make(argos, deepseek))
    harness.configure(bilingual={"auto_billing": True})
    await harness.settle()
    harness.translate("s1", "Hello world")
    await harness.settle()
    assert deepseek.sessions() == ["s1"]


async def test_ctrl_enter_with_same_session_extends_current_session(make: MakeHarness) -> None:
    """Ctrl+Enter 时窗口沿用会话号：计费实例加入当前会话，免费实例不重新翻译。"""
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    harness = await started(make(argos, deepseek))
    text = lines("Hello world", "Good morning")
    harness.translate("s1", text)
    await harness.settle()
    assert harness.recorder.view("s1", "p1").winner == "argos"

    harness.translate("s1", text, billing=True)
    await harness.settle()
    assert deepseek.sessions() == ["s1"]
    assert argos.sessions() == ["s1"]  # 免费实例没有重新翻译
    for segment in ("p1", "p2"):
        view = harness.recorder.view("s1", segment)
        assert view.winner == "deepseek"
        assert {a.engine_id for a in view.attempts} == {"argos", "deepseek"}
    assert harness.recorder.last_progress("s1") == (2, 2)

    # 再按一次 Ctrl+Enter：计费实例已经参赛过，不会重复请求
    harness.translate("s1", text, billing=True)
    await harness.settle()
    assert len(deepseek.jobs) == 1


async def test_ctrl_enter_with_new_session_id_starts_new_session_even_if_segments_match(
    make: MakeHarness,
) -> None:
    """窗口认为文本变了（例如只多了一个空行）就换会话号；切段结果相同也开始新会话，结果按新会话号发布。"""
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    harness = await started(make(argos, deepseek))
    harness.translate("s1", lines("Hello world", "Good morning"))
    await harness.settle()

    harness.translate("s2", lines("Hello world", "", "Good morning"), billing=True)
    await harness.settle()
    assert deepseek.sessions() == ["s2"]
    for segment in ("p1", "p2"):
        assert harness.recorder.view("s2", segment).winner == "deepseek"


async def test_ctrl_enter_with_different_text_starts_new_session(make: MakeHarness) -> None:
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    argos.gate("Hello world")
    harness = await started(make(argos, deepseek))
    harness.translate("s1", "Hello world")
    await harness.wait_running("s1", "p1", argos)
    events_before = harness.recorder.count("s1")

    harness.translate("s2", "Hello there", billing=True)
    await eventually(lambda: len(argos.cancelled) == 1)  # 旧会话执行中的任务被取消
    argos.release("Hello world")
    await harness.settle()
    assert deepseek.sessions() == ["s2"]
    assert harness.recorder.engines("s2", "p1") == {"argos", "deepseek"}
    assert harness.recorder.count("s1") == events_before  # 旧会话不再产生事件
    assert harness.recorder.view("s2", "p1").winner == "deepseek"


async def test_ctrl_enter_with_same_text_but_other_languages_starts_new_session(make: MakeHarness) -> None:
    """文本相同但手动改了目标语言：当前会话的语言对已经不对，开始新会话。"""
    argos, deepseek = FakeEngine("argos", 2), FakeEngine("deepseek", 1, billing=True)
    harness = await started(make(argos, deepseek))
    harness.translate("s1", "Hello world")
    await harness.settle()
    harness.translate("s2", "Hello world", target="ja", billing=True)
    await harness.settle()
    assert deepseek.sessions() == ["s2"]
    assert deepseek.langs_by_text() == {"Hello world": (EN, JA)}


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

    # 当前会话已取消：同样的文本按 Ctrl+Enter 开始新会话，而不是加入已取消的会话
    harness.translate("s3", "Hello world", billing=True)
    await harness.settle()
    assert deepseek.sessions() == ["s3"]
    assert harness.recorder.view("s3", "p1").winner == "deepseek"


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

    harness.translate("s1", "Hello world", billing=True)  # 沿用当前会话 s1
    await harness.settle()
    assert deepseek.sessions() == ["shared", "s1"]


# ---------------------------------------------------------------- 竞速模式


@pytest.mark.parametrize(
    ("mode", "expected"), [(RaceMode.SAVER, AttemptState.CANCELLED), (RaceMode.FULL, AttemptState.DONE)]
)
async def test_mode_change_takes_effect(make: MakeHarness, mode: RaceMode, expected: AttemptState) -> None:
    """节省模式下，低优先级实例的排队尝试在高优先级实例胜出后被取消；完整模式下照常执行。

    argos 每个任务只放一个段，第一个段停在闸门上，第二个段就在 argos 的队列里排队；
    deepseek 很快完成两个段，第二个段由此显示已定。
    """
    deepseek, argos = FakeEngine("deepseek", 1), FakeEngine("argos", 2, max_batch_segments=1)
    argos.gate("Hello world")
    harness = await started(make(deepseek, argos))
    assert harness.module.mode is RaceMode.SAVER
    harness.configure(race={"mode": mode.value})
    await harness.settle()
    assert harness.module.mode is mode

    harness.translate("s1", lines("Hello world", "Good morning"))
    await eventually(lambda: harness.recorder.winner("s1", "p2") == "deepseek")
    argos.release("Hello world")
    await harness.settle()
    assert harness.recorder.states("s1", "p2", argos) == [expected]
    assert harness.recorder.view("s1", "p2").display_text == deepseek.translated("Good morning")


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
    assert deepseek.sessions() == ["shot"]  # 截屏会话里计费实例直接参赛
    assert harness.recorder.view("shot", "b1").winner == "deepseek"
    assert harness.recorder.view("shot", "b2").skip_reason is SkipReason.SAME_LANGUAGE
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
    assert harness.recorder.view("shot", "b1").winner == "argos"
    assert harness.recorder.view("s1", "p1").winner == "argos"
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
    harness.configure(race={"mode": "full"})
    harness.roster(FakeEngine("deepl", 2))
    await harness.module.start_screenshot_session(SessionId("shot"), [Segment(SegmentId("b1"), "Hi")])
    await harness.settle()
    assert engine.sessions() == ["s1"]
    assert harness.recorder.count("s1") == events_before
    assert harness.recorder.sessions() <= {"s1"}
    assert harness.module.race_config.mode is RaceMode.SAVER

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

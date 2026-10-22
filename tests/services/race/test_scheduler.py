"""竞速调度器测试。

引擎、sink、缓存都用可编程的假实现。假引擎在 asyncio.Event 闸门上等待，由测试逐个放行，
从而精确控制结果到达的顺序；等待某个状态出现时订阅 Signal，每次变化后重新检查条件，不靠轮询。
只有合帧节奏和超时用例用到很短的真实时间。
"""

import asyncio
import logging
import threading
from collections import Counter
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from enum import Enum, auto
from typing import Protocol

import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, Entry, Lang, SegmentId, SessionId
from lst.contracts.ports import CacheKey
from lst.contracts.translation import (
    AttemptState,
    AttemptView,
    EngineInfo,
    EngineKind,
    Job,
    JobImage,
    Segment,
    SegmentOutcome,
    SegmentView,
    SkipReason,
)
from lst.services.race.merger import SessionRace
from lst.services.race.scheduler import LangPair, RaceMode, RaceScheduler, pack_segments

EN = Lang("en")
JA = Lang("ja")
ES = Lang("es")
ZH = Lang("zh-Hans")
S1, S2, S3, S4, S5, S6 = (SegmentId(f"s{i}") for i in range(1, 7))
SESSION = SessionId("sess")
FRAME = 0.001
LOGGER = "lst.services.race.scheduler"

QUEUED = AttemptState.QUEUED
RUNNING = AttemptState.RUNNING
DONE = AttemptState.DONE
FAILED = AttemptState.FAILED
CANCELLED = AttemptState.CANCELLED


def segs(*texts: str, prefix: str = "s") -> tuple[Segment, ...]:
    """按顺序生成段，段号为 prefix1、prefix2……"""
    return tuple(Segment(SegmentId(f"{prefix}{i}"), text) for i, text in enumerate(texts, start=1))


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


class Behavior(Enum):
    """假引擎对一个段的行为。"""

    OK = auto()  # 产出成功结果
    FAIL = auto()  # 产出失败结果
    RAISE = auto()  # 抛出异常
    SKIP = auto()  # 不产出这个段


class FakeEngine:
    """可编程的假引擎。

    gates 中有闸门的段，要等测试放行后才产出结果；behavior 决定每段成功、失败、抛异常还是漏掉；
    extra 在每个任务开头产出额外结果，repeat 让引擎在任务末尾把已产出的段重复一遍。
    jobs 记录收到的全部任务，cancelled 记录生成器收到 CancelledError 的任务。
    """

    def __init__(
        self,
        name: str,
        priority: int,
        *,
        billing: bool = False,
        max_concurrency: int = 1,
        max_batch_segments: int = 1,
        max_batch_chars: int = 4000,
        timeout_s: float | None = None,
        pairs: Iterable[tuple[str, str]] | None = None,
        fingerprint: str = "",
        revision: int = 0,
        image_max_side: int | None = None,
    ) -> None:
        self._info = EngineInfo(
            id=EngineId(name),
            name=name,
            kind=EngineKind.ONLINE_LLM if billing else EngineKind.LOCAL_NMT,
            priority=priority,
            billing=billing,
            max_concurrency=max_concurrency,
            max_batch_segments=max_batch_segments,
            max_batch_chars=max_batch_chars,
            timeout_s=timeout_s,
            fingerprint=fingerprint,
            revision=revision,
            image_max_side=image_max_side,
        )
        self.pairs = None if pairs is None else {(Lang(s), Lang(t)) for s, t in pairs}
        self.behavior: dict[SegmentId, Behavior] = {}
        self.gates: dict[SegmentId, asyncio.Event] = {}
        self.extra: Callable[[Job], list[SegmentOutcome]] | None = None
        self.repeat = False
        self.jobs: list[Job] = []
        self.cancelled: list[Job] = []
        self.active = 0
        self.max_active = 0
        self.signal = Signal()

    @property
    def id(self) -> EngineId:
        return self._info.id

    @property
    def info(self) -> EngineInfo:
        return self._info

    def supports(self, source: Lang, target: Lang) -> bool:
        return self.pairs is None or (source, target) in self.pairs

    def gate(self, *segment_ids: SegmentId) -> None:
        for segment_id in segment_ids:
            self.gates[segment_id] = asyncio.Event()

    def release(self, *segment_ids: SegmentId) -> None:
        for segment_id in segment_ids:
            self.gates[segment_id].set()

    def text(self, segment: Segment) -> str:
        return f"{self.id}:{segment.text}"

    def segments_seen(self) -> list[tuple[SegmentId, ...]]:
        return [tuple(s.id for s in job.segments) for job in self.jobs]

    async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        self.jobs.append(job)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.signal.notify()
        produced: list[SegmentOutcome] = []
        try:
            if self.extra is not None:
                for outcome in self.extra(job):
                    yield outcome
            for segment in job.segments:
                gate = self.gates.get(segment.id)
                if gate is not None:
                    await gate.wait()
                behavior = self.behavior.get(segment.id, Behavior.OK)
                if behavior is Behavior.RAISE:
                    raise RuntimeError("连接中断")
                if behavior is Behavior.SKIP:
                    continue
                if behavior is Behavior.OK:
                    outcome = SegmentOutcome(segment.id, text=self.text(segment))
                else:
                    outcome = SegmentOutcome(segment.id, error="接口返回 500")
                produced.append(outcome)
                yield outcome
            if self.repeat:
                for outcome in produced:
                    yield SegmentOutcome(outcome.segment_id, text="重复的结果")
        except asyncio.CancelledError:
            self.cancelled.append(job)
            raise
        finally:
            self.active -= 1
            self.signal.notify()


class StubbornEngine(FakeEngine):
    """收到取消后不退出、继续产出结果的引擎，模拟违反协议的实现。"""

    def __init__(self, name: str, priority: int) -> None:
        super().__init__(name, priority)
        self.late_yields = 0

    async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        self.jobs.append(job)
        self.signal.notify()
        for segment in job.segments:
            try:
                await self.gates[segment.id].wait()
            except asyncio.CancelledError:
                self.cancelled.append(job)
                self.late_yields += 1
            yield SegmentOutcome(segment.id, text="迟到的译文")


class FakeSink:
    """记录全部 sink 调用。log 按调用顺序记录（方法名，会话号），update_times 记录每次刷新的循环时刻。"""

    def __init__(self) -> None:
        self.updates: list[tuple[SessionId, tuple[SegmentView, ...]]] = []
        self.update_times: list[float] = []
        self.progress: list[tuple[SessionId, int, int]] = []
        self.records: list[tuple[SessionId, SegmentId, AttemptView]] = []
        self.latest: dict[tuple[SessionId, SegmentId], SegmentView] = {}
        self.log: list[tuple[str, SessionId]] = []
        self.threads: set[int] = set()
        self.signal = Signal()

    def segments_updated(self, session_id: SessionId, views: tuple[SegmentView, ...]) -> None:
        self.updates.append((session_id, views))
        self.update_times.append(asyncio.get_running_loop().time())
        for view in views:
            self.latest[(session_id, view.segment_id)] = view
        self._called("segments_updated", session_id)

    def session_progress(self, session_id: SessionId, finished: int, total: int) -> None:
        self.progress.append((session_id, finished, total))
        self._called("session_progress", session_id)

    def attempt_recorded(self, session_id: SessionId, segment_id: SegmentId, attempt: AttemptView) -> None:
        self.records.append((session_id, segment_id, attempt))
        self._called("attempt_recorded", session_id)

    def recorded(self, segment_id: SegmentId, engine: FakeEngine, session: str = "sess") -> list[AttemptView]:
        return [
            a
            for sid, seg, a in self.records
            if sid == session and seg == segment_id and a.engine_id == engine.id
        ]

    def history(self, segment_id: SegmentId, session: str = "sess") -> list[SegmentView]:
        return [
            v for sid, views in self.updates if sid == session for v in views if v.segment_id == segment_id
        ]

    def _called(self, method: str, session_id: SessionId) -> None:
        self.log.append((method, session_id))
        self.threads.add(threading.get_ident())
        self.signal.notify()


class RaisingSink(FakeSink):
    """先记录，再抛出异常的 sink。"""

    def segments_updated(self, session_id: SessionId, views: tuple[SegmentView, ...]) -> None:
        super().segments_updated(session_id, views)
        raise RuntimeError("界面出错")

    def session_progress(self, session_id: SessionId, finished: int, total: int) -> None:
        super().session_progress(session_id, finished, total)
        raise RuntimeError("界面出错")

    def attempt_recorded(self, session_id: SessionId, segment_id: SegmentId, attempt: AttemptView) -> None:
        super().attempt_recorded(session_id, segment_id, attempt)
        raise RuntimeError("统计出错")


class FakeCache:
    """内存缓存。gate 可以让 get_many 停住，fail_get、fail_put 让对应操作抛出异常。"""

    def __init__(self, data: Mapping[CacheKey, str] | None = None) -> None:
        self.data: dict[CacheKey, str] = dict(data or {})
        self.get_calls: list[tuple[CacheKey, ...]] = []
        self.puts: list[tuple[CacheKey, str]] = []
        self.fail_get = False
        self.fail_put = False
        self.gate: asyncio.Event | None = None
        self.signal = Signal()

    async def get_many(self, keys: Sequence[CacheKey]) -> Mapping[CacheKey, str]:
        self.get_calls.append(tuple(keys))
        self.signal.notify()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_get:
            raise OSError("缓存库损坏")
        return {k: self.data[k] for k in keys if k in self.data}

    async def put(self, key: CacheKey, text: str) -> None:
        if self.fail_put:
            raise OSError("磁盘已满")
        self.puts.append((key, text))
        self.data[key] = text


def key(text: str, engine: FakeEngine, source: Lang = EN, target: Lang = ZH) -> CacheKey:
    return CacheKey(text, source, target, engine.id, engine.info.fingerprint)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Harness:
    """把调度器、sink、时钟放在一起，并提供查询最新快照的便捷方法。"""

    def __init__(self, scheduler: RaceScheduler, sink: FakeSink, clock: FakeClock) -> None:
        self.scheduler = scheduler
        self.sink = sink
        self.clock = clock

    async def start(
        self,
        segments: Sequence[Segment],
        *,
        session: str = "sess",
        entry: Entry = Entry.SCREENSHOT,
        sources: Mapping[SegmentId, Lang] | None = None,
        langs: Mapping[SegmentId, LangPair] | None = None,
        include_billing: bool = True,
        context: str = "",
        images: Mapping[int, JobImage] | None = None,
    ) -> None:
        """langs 给出时直接使用；否则每段的语言对是（sources 中的源语言，默认 en）→ zh-Hans。"""
        if langs is None:
            langs = {s.id: ((sources or {}).get(s.id, EN), ZH) for s in segments}
        await self.scheduler.start_session(
            entry,
            SessionId(session),
            segments,
            langs,
            include_billing=include_billing,
            context=context,
            images=images,
        )

    async def extend(self, session: str = "sess") -> None:
        await self.scheduler.extend_session(SessionId(session))

    async def drain(self) -> None:
        async with asyncio.timeout(5):
            await self.scheduler.drain()

    def view(self, segment_id: SegmentId, session: str = "sess") -> SegmentView:
        return self.sink.latest[(SessionId(session), segment_id)]

    def attempt(self, segment_id: SegmentId, engine: FakeEngine, session: str = "sess") -> AttemptView:
        matches = [a for a in self.view(segment_id, session).attempts if a.engine_id == engine.id]
        assert len(matches) == 1
        return matches[0]

    def state(self, segment_id: SegmentId, engine: FakeEngine, session: str = "sess") -> AttemptState | None:
        view = self.sink.latest.get((SessionId(session), segment_id))
        if view is None:
            return None
        return next((a.state for a in view.attempts if a.engine_id == engine.id), None)

    def finished(self, segment_id: SegmentId, session: str = "sess") -> bool:
        view = self.sink.latest.get((SessionId(session), segment_id))
        return view is not None and view.finished

    def displayed(self, segment_id: SegmentId, session: str = "sess") -> list[str]:
        """界面先后显示过的译文，相邻重复的只算一次。"""
        texts: list[str] = []
        for view in self.sink.history(segment_id, session):
            if not texts or texts[-1] != view.display_text:
                texts.append(view.display_text)
        return texts


class MakeHarness(Protocol):
    def __call__(
        self,
        *engines: FakeEngine,
        mode: RaceMode = RaceMode.SAVER,
        cache: FakeCache | None = None,
        frame_interval: float = FRAME,
        sink: FakeSink | None = None,
    ) -> Harness: ...


@pytest.fixture
async def make() -> AsyncIterator[MakeHarness]:
    """创建 Harness 的工厂；测试结束时关闭所有调度器。"""
    harnesses: list[Harness] = []

    def factory(
        *engines: FakeEngine,
        mode: RaceMode = RaceMode.SAVER,
        cache: FakeCache | None = None,
        frame_interval: float = FRAME,
        sink: FakeSink | None = None,
    ) -> Harness:
        sink = sink or FakeSink()
        clock = FakeClock()
        scheduler = RaceScheduler(
            engines, sink, mode=mode, cache=cache, clock=clock, frame_interval=frame_interval
        )
        harness = Harness(scheduler, sink, clock)
        harnesses.append(harness)
        return harness

    yield factory
    for harness in harnesses:
        await harness.scheduler.aclose()


async def wait_jobs(engine: FakeEngine, count: int) -> None:
    await engine.signal.wait_until(lambda: len(engine.jobs) >= count)


# ---------------------------------------------------------------- 需求文档 5.5 节时间线


class TestRequirementTimeline:
    """需求文档 5.5 节：DeepSeek 优先级 1，DeepL 优先级 2，Argos 优先级 3。"""

    @staticmethod
    async def begin(make: MakeHarness) -> tuple[Harness, FakeEngine, FakeEngine, FakeEngine]:
        deepseek = FakeEngine("deepseek", 1, billing=True)
        deepl = FakeEngine("deepl", 2, billing=True)
        argos = FakeEngine("argos", 3)
        engines = (deepseek, deepl, argos)
        for engine in engines:
            engine.gate(S1)
        h = make(*engines)
        await h.start(segs("Hello"))
        for engine in engines:
            await wait_jobs(engine, 1)
        await h.sink.signal.wait_until(lambda: all(h.state(S1, e) is RUNNING for e in engines))
        assert h.view(S1).display_text == "Hello"
        return h, deepseek, deepl, argos

    @staticmethod
    async def arrive(h: Harness, engine: FakeEngine, at: float) -> SegmentView:
        """在时刻 at 放行 engine，返回界面收到的最新快照。"""
        h.clock.now = at
        engine.release(S1)
        await h.sink.signal.wait_until(lambda: h.state(S1, engine) is DONE)
        return h.view(S1)

    async def test_display_upgrades_step_by_step(self, make: MakeHarness) -> None:
        h, deepseek, deepl, argos = await self.begin(make)

        view = await self.arrive(h, argos, 0.3)
        assert (view.display_text, view.winner, view.finished) == ("argos:Hello", argos.id, False)
        view = await self.arrive(h, deepl, 1.2)
        assert (view.display_text, view.winner, view.finished) == ("deepl:Hello", deepl.id, False)
        view = await self.arrive(h, deepseek, 2.1)
        assert (view.display_text, view.winner, view.finished) == ("deepseek:Hello", deepseek.id, True)

        await h.drain()
        assert h.displayed(S1) == ["Hello", "argos:Hello", "deepl:Hello", "deepseek:Hello"]
        # 耗时从派发时刻（时钟 0）算起。
        latencies = {a.engine_id: a.latency_ms for a in h.view(S1).attempts}
        assert latencies == pytest.approx({deepseek.id: 2100.0, deepl.id: 1200.0, argos.id: 300.0})
        assert h.sink.progress[-1] == (SESSION, 1, 1)

    async def test_lower_priority_result_after_best_is_only_recorded(self, make: MakeHarness) -> None:
        h, deepseek, deepl, argos = await self.begin(make)

        await self.arrive(h, argos, 0.3)
        view = await self.arrive(h, deepseek, 0.8)
        assert (view.winner, view.finished) == (deepseek.id, True)
        # DeepL 已在执行，节省模式下照常完成，结果只记录不显示。
        await self.arrive(h, deepl, 1.2)
        await h.drain()

        view = h.view(S1)
        assert view.winner == deepseek.id
        assert [a.engine_id for a in view.attempts] == [deepseek.id, deepl.id, argos.id]
        late = h.attempt(S1, deepl)
        assert (late.state, late.text) == (DONE, "deepl:Hello")
        assert h.displayed(S1) == ["Hello", "argos:Hello", "deepseek:Hello"]


# ---------------------------------------------------------------- 1 参赛与语言对


class TestParticipation:
    async def test_segment_already_in_target_language_is_skipped(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        h = make(a)
        await h.start(segs("Hello", "你好"), sources={S1: EN, S2: ZH})
        await h.drain()

        assert a.segments_seen() == [(S1,)]
        view = h.view(S2)
        assert (view.display_text, view.winner, view.finished, view.attempts) == ("你好", None, True, ())
        assert view.skip_reason is SkipReason.SAME_LANGUAGE
        assert h.view(S1).skip_reason is None
        assert h.sink.progress[-1] == (SESSION, 2, 2)

    async def test_only_engines_supporting_the_pair_take_part(self, make: MakeHarness) -> None:
        en_only = FakeEngine("en_only", 1, pairs=[("en", "zh-Hans")])
        ja_only = FakeEngine("ja_only", 2, pairs=[("ja", "zh-Hans")])
        h = make(en_only, ja_only)
        await h.start(segs("Hello", "こんにちは"), sources={S1: EN, S2: JA})
        await h.drain()

        assert en_only.segments_seen() == [(S1,)]
        assert ja_only.segments_seen() == [(S2,)]
        assert [a.engine_id for a in h.view(S1).attempts] == [en_only.id]
        assert [a.engine_id for a in h.view(S2).attempts] == [ja_only.id]

    async def test_segment_without_any_supporting_engine_is_skipped(self, make: MakeHarness) -> None:
        free = FakeEngine("free", 2, pairs=[("en", "zh-Hans")])
        paid = FakeEngine("paid", 1, billing=True, pairs=[("ja", "zh-Hans")])
        h = make(free, paid)
        await h.start(
            segs("Hello", "こんにちは", "Hola"), sources={S1: EN, S2: JA, S3: ES}, include_billing=False
        )
        await h.drain()

        # s2 只有计费实例支持：暂时没有参赛者，但不标记跳过，等 extend_session。
        assert (h.view(S2).attempts, h.view(S2).skip_reason) == ((), None)
        # s3 的语言对没有任何实例支持，包括计费实例。
        assert (h.view(S3).attempts, h.view(S3).skip_reason) == ((), SkipReason.NO_ENGINE)

        await h.extend()
        await h.drain()
        assert paid.segments_seen() == [(S2,)]
        assert h.view(S2).winner == paid.id
        assert h.view(S3).attempts == ()

    async def test_each_segment_uses_its_own_language_pair(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_batch_segments=10)
        cache = FakeCache()
        h = make(a, cache=cache)
        langs = {S1: (EN, ZH), S2: (ZH, EN), S3: (EN, ZH)}  # 互译对：中英互译
        await h.start(segs("Hello", "你好", "World"), langs=langs)
        await h.drain()

        # 打包按（源语言，目标语言）分组。
        assert a.segments_seen() == [(S1, S3), (S2,)]
        assert [(job.source_lang, job.target_lang) for job in a.jobs] == [(EN, ZH), (ZH, EN)]
        # 缓存键同样使用每段自己的语言对。
        assert cache.get_calls == [(key("Hello", a), key("你好", a, ZH, EN), key("World", a))]
        assert dict(cache.puts)[key("你好", a, ZH, EN)] == "a:你好"

    async def test_billing_engines_stay_out_unless_included(self, make: MakeHarness) -> None:
        free = FakeEngine("free", 2)
        paid = FakeEngine("paid", 1, billing=True)
        h = make(free, paid)
        await h.start(segs("Hello"), include_billing=False)
        await h.drain()

        assert paid.jobs == []
        view = h.view(S1)
        assert [a.engine_id for a in view.attempts] == [free.id]
        assert (view.winner, view.finished) == (free.id, True)

    async def test_dispatch_time_comes_from_clock(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        a.gate(S1)
        h = make(a)
        h.clock.now = 10.0
        await h.start(segs("Hello"))
        await wait_jobs(a, 1)
        h.clock.now = 10.25
        a.release(S1)
        await h.drain()
        assert h.attempt(S1, a).latency_ms == pytest.approx(250.0)


# ---------------------------------------------------------------- 2 缓存


class TestCache:
    async def test_hits_finish_with_zero_latency_and_skip_engine(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        b = FakeEngine("b", 2)
        b.behavior[S2] = Behavior.FAIL
        cache = FakeCache({key("一", b): "缓存译文"})
        h = make(a, b, cache=cache, mode=RaceMode.FULL)
        h.clock.now = 5.0
        await h.start(segs("一", "二"))
        await h.drain()

        # 全部（段，实例）组合在派发前一次查完。
        assert cache.get_calls == [(key("一", a), key("一", b), key("二", a), key("二", b))]
        assert a.segments_seen() == [(S1,), (S2,)]
        assert b.segments_seen() == [(S2,)]
        hit = h.attempt(S1, b)
        assert (hit.state, hit.text, hit.latency_ms, hit.cached) == (DONE, "缓存译文", 0.0, True)
        assert not h.attempt(S1, a).cached
        # 只写入引擎返回的成功结果；缓存命中和失败结果不写。
        assert len(cache.puts) == 2
        assert set(cache.puts) == {(key("一", a), "a:一"), (key("二", a), "a:二")}

    async def test_hit_from_top_engine_leaves_others_hopeless(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        b = FakeEngine("b", 2)
        cache = FakeCache({key("一", a): "缓存译文"})
        h = make(a, b, cache=cache)
        await h.start(segs("一"))
        await h.drain()

        # 缓存命中让 a 成为胜出者，b 的排队尝试被即时剪枝，没有入队，也没有调用引擎。
        assert a.jobs == []
        assert b.jobs == []
        view = h.view(S1)
        assert (view.winner, view.display_text, view.finished) == (a.id, "缓存译文", True)
        assert h.attempt(S1, b).state is CANCELLED

    async def test_billing_cache_hits_join_without_extend(self, make: MakeHarness) -> None:
        free = FakeEngine("free", 3)
        paid = FakeEngine("paid", 1, billing=True)
        cache = FakeCache({key("一", paid): "paid 缓存"})
        h = make(free, paid, cache=cache)
        await h.start(segs("一", "二"), include_billing=False)
        await h.drain()

        # 计费实例的组合同样查缓存。
        assert cache.get_calls == [(key("一", free), key("一", paid), key("二", free), key("二", paid))]
        # 命中的组合参赛并胜出；free 在 s1 上的排队尝试随之被即时剪枝。
        hit = h.attempt(S1, paid)
        assert (hit.state, hit.cached, hit.latency_ms) == (DONE, True, 0.0)
        assert h.view(S1).winner == paid.id
        assert h.attempt(S1, free).state is CANCELLED
        # 没命中的组合不参赛，也不调用计费引擎。
        assert [a.engine_id for a in h.view(S2).attempts] == [free.id]
        assert paid.jobs == []
        assert free.segments_seen() == [(S2,)]

        await h.extend()
        await h.drain()
        # extend_session 之后，没命中的组合正常加入；命中过的组合不重复参赛。
        assert cache.get_calls[-1] == (key("二", paid),)
        assert paid.segments_seen() == [(S2,)]
        assert h.view(S2).winner == paid.id
        assert len(h.sink.recorded(S1, paid)) == 1

    async def test_cache_errors_are_logged_and_race_goes_on(
        self, make: MakeHarness, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)
        a = FakeEngine("a", 1)
        cache = FakeCache({key("一", a): "缓存译文"})
        cache.fail_get = True
        cache.fail_put = True
        h = make(a, cache=cache)
        await h.start(segs("一"))
        await h.drain()

        assert a.segments_seen() == [(S1,)]
        assert h.view(S1).display_text == "a:一"
        messages = [r.getMessage() for r in caplog.records]
        assert any("查询译文缓存失败" in m for m in messages)
        assert any("写入译文缓存失败" in m for m in messages)

    async def test_drain_waits_for_lookup_in_progress(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        cache = FakeCache()
        gate = cache.gate = asyncio.Event()
        h = make(a, cache=cache)
        task = asyncio.create_task(h.start(segs("一")))
        await cache.signal.wait_until(lambda: len(cache.get_calls) == 1)
        asyncio.get_running_loop().call_later(0.01, gate.set)
        await h.drain()

        assert task.done()
        assert h.view(S1).display_text == "a:一"

    async def test_start_cancelled_during_lookup_cancels_its_attempts(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        cache = FakeCache()
        cache.gate = asyncio.Event()
        h = make(a, cache=cache)
        task = asyncio.create_task(h.start(segs("一")))
        await cache.signal.wait_until(lambda: len(cache.get_calls) == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await h.drain()

        assert a.jobs == []
        assert h.attempt(S1, a).state is CANCELLED
        assert h.finished(S1)

    async def test_identical_texts_share_one_cache_entry(self, make: MakeHarness) -> None:
        """同一会话里原文相同的段共用一个缓存键，命中后每个段都以缓存结果完成。"""
        a = FakeEngine("a", 1)
        cache = FakeCache({key("OK", a): "好"})
        h = make(a, cache=cache)
        await h.start(segs("OK", "OK"))
        await h.drain()

        assert cache.get_calls == [(key("OK", a),)]
        assert a.jobs == []
        for segment_id in (S1, S2):
            hit = h.attempt(segment_id, a)
            assert (hit.state, hit.text, hit.cached) == (DONE, "好", True)
            assert h.view(segment_id).finished

    async def test_identical_texts_all_join_on_billing_hit(self, make: MakeHarness) -> None:
        """双语窗口不含计费实例时，原文相同的段都因缓存命中让计费实例参赛。"""
        free = FakeEngine("free", 2)
        paid = FakeEngine("paid", 1, billing=True)
        cache = FakeCache({key("OK", paid): "好"})
        h = make(free, paid, cache=cache)
        await h.start(segs("OK", "OK"), entry=Entry.BILINGUAL, include_billing=False)
        await h.drain()

        assert paid.jobs == []
        for segment_id in (S1, S2):
            assert (h.view(segment_id).winner, h.view(segment_id).display_text) == (paid.id, "好")

    async def test_blank_translation_is_not_written(self, make: MakeHarness) -> None:
        """译文去掉空白后为空时不写缓存，免得以后每次都命中这个空结果。"""

        class BlankEngine(FakeEngine):
            def text(self, segment: Segment) -> str:
                return " 	" if segment.id == S1 else super().text(segment)

        a = BlankEngine("a", 1)
        cache = FakeCache()
        h = make(a, cache=cache)
        await h.start(segs("一", "二"))
        await h.drain()

        assert cache.puts == [(key("二", a), "a:二")]


# ---------------------------------------------------------------- 3 打包


@pytest.mark.parametrize(
    ("texts", "max_segments", "max_chars", "expected"),
    [
        pytest.param(["a"] * 5, 2, 100, [(0, 1), (2, 3), (4,)], id="段数上限"),
        pytest.param(["aaa", "bbb", "cc", "d"], 10, 6, [(0, 1), (2, 3)], id="字符上限"),
        pytest.param(["ab", "x" * 10, "cd"], 10, 5, [(0,), (1,), (2,)], id="超长段单独成任务"),
        pytest.param(["x" * 10, "a", "b"], 10, 5, [(0,), (1, 2)], id="超长段在开头"),
        pytest.param(["a", "b"], 0, 100, [(0,), (1,)], id="段数上限小于1按1处理"),
        pytest.param([], 3, 100, [], id="空输入"),
    ],
)
def test_pack_segments(
    texts: list[str], max_segments: int, max_chars: int, expected: list[tuple[int, ...]]
) -> None:
    segments = segs(*texts)
    batches = pack_segments(segments, max_segments, max_chars)
    assert [tuple(segments.index(s) for s in batch) for batch in batches] == expected


class TestPacking:
    async def test_jobs_grouped_by_language_pair_in_segment_order(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_batch_segments=3, max_batch_chars=100)
        h = make(a)
        sources = {S1: EN, S2: JA, S3: EN, S4: EN, S5: JA, S6: EN}
        await h.start(segs("e1", "j1", "e2", "e3", "j2", "e4"), sources=sources, context="整段原文")
        await h.drain()

        # 每个语言对各自贪心打包，任务按首段顺序入队。
        assert a.segments_seen() == [(S1, S3, S4), (S2, S5), (S6,)]
        assert [job.source_lang for job in a.jobs] == [EN, JA, EN]
        for job in a.jobs:
            assert (job.session_id, job.engine_id, job.target_lang, job.context) == (
                SESSION,
                a.id,
                ZH,
                "整段原文",
            )

    async def test_jobs_respect_char_limit(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_batch_segments=10, max_batch_chars=5)
        h = make(a)
        await h.start(segs("abc", "de", "fghijkl", "m"))
        await h.drain()
        assert a.segments_seen() == [(S1, S2), (S3,), (S4,)]


# ---------------------------------------------------------------- 4 执行与出队剪枝


class TestExecution:
    async def test_segments_are_running_while_engine_works(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_batch_segments=2)
        a.gate(S1)
        h = make(a)
        await h.start(segs("一", "二"))
        await h.sink.signal.wait_until(lambda: h.state(S1, a) is RUNNING and h.state(S2, a) is RUNNING)
        assert len(a.jobs) == 1
        a.release(S1)
        await h.drain()
        assert h.view(S1).display_text == "a:一"

    async def test_job_keeps_only_segments_still_queued(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_concurrency=4)
        a.gate(S1, S2, S3, S4)
        b = FakeEngine("b", 2, max_batch_segments=2)
        b.gate(S1)
        h = make(a, b)
        await h.start(segs("一", "二", "三", "四"))
        # b 的第一个任务 [s1, s2] 停在闸门上，第二个任务 [s3, s4] 在排队。
        await wait_jobs(a, 4)
        await wait_jobs(b, 1)

        a.release(S1, S2, S3)
        await h.sink.signal.wait_until(lambda: bool(h.sink.recorded(S3, b)))
        assert [r.state for r in h.sink.recorded(S3, b)] == [CANCELLED]  # 即时剪枝

        b.release(S1)
        await wait_jobs(b, 2)
        # 出队时 s3 已不在 QUEUED 状态，从任务中移除，只保留 s4。
        assert b.segments_seen() == [(S1, S2), (S4,)]
        a.release(S4)
        await h.drain()

        for segment_id in (S1, S2):
            assert h.view(segment_id).winner == a.id
            assert h.attempt(segment_id, b).state is DONE
        assert len(h.sink.recorded(S3, b)) == 1
        assert h.view(S4).winner == a.id

    async def test_attempt_that_cannot_win_is_pruned_before_race_finishes(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        a.gate(S1, S2)
        b = FakeEngine("b", 2, max_concurrency=2)
        b.gate(S1)
        c = FakeEngine("c", 3)
        c.gate(S1)
        h = make(a, b, c)
        await h.start(segs("一", "二"))
        # c 正在执行 s1、s2 排队；a 在 s2 上也在排队。
        await wait_jobs(c, 1)
        await h.sink.signal.wait_until(lambda: h.state(S2, b) is DONE)

        # b 在 s2 上胜出的同时，优先级更低的 c 被剪掉；a 还可能改变显示，竞速尚未结束。
        assert [r.state for r in h.sink.recorded(S2, c)] == [CANCELLED]
        assert h.state(S2, a) is QUEUED
        assert not h.finished(S2)

        c.release(S1)
        a.release(S1, S2)
        b.release(S1)
        await h.drain()
        assert c.segments_seen() == [(S1,)]
        assert (h.view(S2).winner, h.view(S2).finished) == (a.id, True)


class TestConcurrency:
    async def test_single_slot_runs_jobs_one_after_another(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_concurrency=1)
        a.gate(S1, S2)
        h = make(a)
        await h.start(segs("一", "二"))
        await wait_jobs(a, 1)
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(a.jobs) == 1

        a.release(S1)
        await wait_jobs(a, 2)
        a.release(S2)
        await h.drain()
        assert a.segments_seen() == [(S1,), (S2,)]
        assert a.max_active == 1

    async def test_two_slots_run_jobs_in_parallel(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_concurrency=2)
        a.gate(S1, S2)
        h = make(a)
        await h.start(segs("一", "二"))
        await wait_jobs(a, 2)
        assert a.active == 2

        # 后一个任务先完成，不受前一个阻塞。
        a.release(S2)
        await h.sink.signal.wait_until(lambda: h.finished(S2))
        assert not h.finished(S1)
        a.release(S1)
        await h.drain()
        assert a.max_active == 2


class TestEntryPriority:
    async def test_bilingual_job_runs_before_queued_screenshot_jobs(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)  # 并发 1
        shot = segs("截一", "截二", "截三", prefix="c")
        typed = segs("双语", prefix="b")
        a.gate(shot[0].id)
        h = make(a)
        await h.start(shot, session="shot", entry=Entry.SCREENSHOT)
        await wait_jobs(a, 1)
        # 截屏的 c2、c3 已经在排队，之后入队的双语任务排到它们前面。
        await h.start(typed, session="typed", entry=Entry.BILINGUAL)
        a.release(shot[0].id)
        await h.drain()

        c1, c2, c3 = (s.id for s in shot)
        assert a.segments_seen() == [(c1,), (typed[0].id,), (c2,), (c3,)]


# ---------------------------------------------------------------- 5 引擎故障


class TestEngineFaults:
    async def test_exception_fails_unfinished_segments_of_the_job(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_batch_segments=3)
        a.behavior[S2] = Behavior.RAISE
        b = FakeEngine("b", 2)
        h = make(a, b)
        await h.start(segs("一", "二", "三"))
        await h.drain()

        assert h.attempt(S1, a).state is DONE
        for segment_id in (S2, S3):
            attempt = h.attempt(segment_id, a)
            assert (attempt.state, attempt.error) == (FAILED, "RuntimeError: 连接中断")
            # 一个实例出错不影响其他实例（ENG-06）。
            assert h.view(segment_id).winner == b.id

    async def test_segment_missing_from_stream_fails(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_batch_segments=2)
        a.behavior[S2] = Behavior.SKIP
        h = make(a)
        await h.start(segs("一", "二"))
        await h.drain()

        assert h.attempt(S1, a).state is DONE
        attempt = h.attempt(S2, a)
        assert (attempt.state, attempt.error) == (FAILED, "引擎未返回该段")
        view = h.view(S2)
        assert (view.finished, view.winner, view.display_text) == (True, None, "二")

    async def test_foreign_and_duplicate_outcomes_are_ignored(
        self, make: MakeHarness, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.WARNING, logger=LOGGER)
        a = FakeEngine("a", 1)

        def extra(job: Job) -> list[SegmentOutcome]:
            other = S2 if job.segments[0].id == S1 else S1
            return [
                SegmentOutcome(SegmentId("unknown"), text="无关"),
                SegmentOutcome(other, text="串到别的任务"),  # 同一会话、但不属于本任务的段
            ]

        a.extra = extra
        a.repeat = True
        h = make(a)
        await h.start(segs("一", "二"))
        await h.drain()

        assert h.view(S1).display_text == "a:一"
        assert h.view(S2).display_text == "a:二"
        assert len(h.sink.recorded(S1, a)) == 1
        assert len(h.sink.recorded(S2, a)) == 1
        ignored = [r for r in caplog.records if "已忽略" in r.getMessage()]
        assert len(ignored) == 6  # 两个任务，各有无关段、串段、重复段三次

    async def test_timeout_fails_remaining_segments(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_batch_segments=2, timeout_s=0.02)
        a.gate(S2)
        h = make(a)
        await h.start(segs("一", "二"))
        await h.drain()

        assert h.attempt(S1, a).state is DONE
        attempt = h.attempt(S2, a)
        assert (attempt.state, attempt.error) == (FAILED, "超时（0.02 秒）")
        assert a.cancelled == a.jobs  # 生成器收到了 CancelledError

    async def test_dequeue_error_fails_attempts_of_the_job(
        self, make: MakeHarness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """出队剪枝出错时丢弃任务，任务里的尝试记为内部错误，不会停在 QUEUED 让竞速挂起。"""
        caplog.set_level(logging.ERROR, logger=LOGGER)
        a = FakeEngine("a", 1, max_batch_segments=2)
        h = make(a)

        def broken(self: SessionRace, segment_id: SegmentId, engine_id: EngineId) -> None:
            raise RuntimeError("合并器出错")

        with monkeypatch.context() as patch:
            patch.setattr(SessionRace, "start", broken)
            await h.start(segs("一", "二"))
            await h.drain()

        assert a.jobs == []
        for segment_id in (S1, S2):
            failed = h.attempt(segment_id, a)
            assert (failed.state, failed.error_code) == (FAILED, ErrorCode.INTERNAL)
            assert h.finished(segment_id)
        assert any("出队剪枝出错" in r.getMessage() for r in caplog.records)

        # 工作协程没有退出，之后的任务照常执行。
        await h.start(segs("三", prefix="n"), session="next", entry=Entry.BILINGUAL)
        await h.drain()
        assert a.segments_seen() == [(SegmentId("n1"),)]


# ---------------------------------------------------------------- 6 竞速模式


class TestRaceMode:
    @staticmethod
    async def begin(make: MakeHarness, mode: RaceMode) -> tuple[Harness, FakeEngine, FakeEngine]:
        """a（优先级 1）两段都停在闸门上；b（优先级 2，并发 1）正在执行 s1，s2 在排队。"""
        a = FakeEngine("a", 1, max_concurrency=2)
        a.gate(S1, S2)
        b = FakeEngine("b", 2)
        b.gate(S1)
        h = make(a, b, mode=mode)
        await h.start(segs("一", "二"))
        await wait_jobs(a, 2)
        await wait_jobs(b, 1)
        return h, a, b

    async def test_saver_cancels_queued_attempts_as_soon_as_display_settles(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_concurrency=4)
        a.gate(S1, S2, S3, S4)
        b = FakeEngine("b", 2)  # 并发 1：s1 执行中，s2 至 s4 排在后面
        b.gate(S1)
        h = make(a, b)
        await h.start(segs("一", "二", "三", "四"))
        await wait_jobs(a, 4)
        await wait_jobs(b, 1)

        a.release(S1, S2, S3, S4)
        later = (S2, S3, S4)
        await h.sink.signal.wait_until(lambda: all(h.sink.recorded(s, b) for s in later))
        # b 还停在 s1 上，排在它后面的三个尝试已经是 CANCELLED，不必等到出队。
        assert all([r.state for r in h.sink.recorded(s, b)] == [CANCELLED] for s in later)
        assert b.segments_seen() == [(S1,)]

        b.release(S1)
        await h.drain()
        # 出队时这些任务已经没有排队的段，直接丢弃，不调用引擎。
        assert b.segments_seen() == [(S1,)]
        assert all(len(h.sink.recorded(s, b)) == 1 for s in later)
        # b 在 s1 上已经开始执行，照常完成，结果只记录不显示。
        assert h.view(S1).winner == a.id
        late = h.attempt(S1, b)
        assert (late.state, late.text) == (DONE, "b:一")

    async def test_full_mode_runs_every_attempt(self, make: MakeHarness) -> None:
        h, a, b = await self.begin(make, RaceMode.FULL)
        a.release(S1, S2)
        b.release(S1)
        await h.drain()

        assert b.segments_seen() == [(S1,), (S2,)]
        assert len(h.sink.records) == 4
        assert all(attempt.state is DONE for _, _, attempt in h.sink.records)
        assert h.view(S2).winner == a.id
        assert h.view(S2).complete

    async def test_switching_to_full_stops_later_pruning(self, make: MakeHarness) -> None:
        h, a, b = await self.begin(make, RaceMode.SAVER)
        h.scheduler.set_mode(RaceMode.FULL)
        a.release(S1, S2)
        b.release(S1)
        await h.drain()

        assert b.segments_seen() == [(S1,), (S2,)]
        assert h.attempt(S2, b).state is DONE

    async def test_switching_to_saver_prunes_later_dequeues(self, make: MakeHarness) -> None:
        h, a, b = await self.begin(make, RaceMode.FULL)
        a.release(S1, S2)
        await h.sink.signal.wait_until(lambda: h.finished(S2))

        h.scheduler.set_mode(RaceMode.SAVER)
        b.release(S1)
        await h.drain()
        assert b.segments_seen() == [(S1,)]
        assert h.attempt(S2, b).state is CANCELLED


# ---------------------------------------------------------------- 7 会话


class TestSessions:
    async def test_new_session_on_same_entry_cancels_old_one(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        old = segs("旧一", "旧二", prefix="o")
        new = segs("新", prefix="n")
        a.gate(old[0].id)
        h = make(a)
        await h.start(old, session="old")
        await wait_jobs(a, 1)

        mark = len(h.sink.log)
        await h.start(new, session="new")
        await h.drain()

        # 执行中的旧任务被取消；排队的旧任务出队时丢弃，没有交给引擎。
        assert a.cancelled == [a.jobs[0]]
        assert a.segments_seen() == [(old[0].id,), (new[0].id,)]
        assert {sid for _, sid in h.sink.log[mark:]} == {SessionId("new")}
        assert h.view(new[0].id, "new").display_text == "a:新"

    async def test_cancel_before_first_frame_produces_no_output(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        h = make(a)
        await h.start(segs("一"))
        h.scheduler.cancel_session(Entry.SCREENSHOT)
        await h.drain()

        assert a.jobs == []
        assert h.sink.log == []

    async def test_cancel_session_cancels_running_job(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        a.gate(S1)
        h = make(a)
        await h.start(segs("一"))
        await wait_jobs(a, 1)

        mark = len(h.sink.log)
        h.scheduler.cancel_session(Entry.SCREENSHOT)
        await h.drain()
        assert a.cancelled == a.jobs
        assert h.sink.log[mark:] == []

    async def test_results_after_cancel_are_dropped_even_if_engine_ignores_it(
        self, make: MakeHarness
    ) -> None:
        stubborn = StubbornEngine("stubborn", 1)
        stubborn.gate(S1)
        h = make(stubborn)
        await h.start(segs("一"))
        await wait_jobs(stubborn, 1)

        mark = len(h.sink.log)
        h.scheduler.cancel_session(Entry.SCREENSHOT)
        await h.drain()
        assert stubborn.late_yields == 1
        assert h.sink.log[mark:] == []

    async def test_entries_do_not_cancel_each_other(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_concurrency=2)
        shot = segs("截屏", prefix="c")
        typed = segs("双语", prefix="b")
        a.gate(shot[0].id)
        h = make(a)
        await h.start(shot, session="shot", entry=Entry.SCREENSHOT)
        await h.start(typed, session="typed", entry=Entry.BILINGUAL)
        await h.sink.signal.wait_until(lambda: h.finished(typed[0].id, "typed"))

        a.release(shot[0].id)
        await h.drain()
        assert a.cancelled == []
        assert h.view(shot[0].id, "shot").display_text == "a:截屏"

    async def test_invalid_start_leaves_current_session_running(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        a.gate(S1)
        h = make(a)
        await h.start(segs("一"))

        with pytest.raises(ValueError):  # 缺少语言对
            await h.start(segs("x", prefix="m"), session="no-langs", langs={})
        with pytest.raises(ValueError):  # 段号重复
            await h.start((Segment(S2, "甲"), Segment(S2, "乙")), session="dup")
        with pytest.raises(ValueError):  # 会话号已被另一个入口占用
            await h.start(segs("x", prefix="m"), session="sess", entry=Entry.BILINGUAL)

        a.release(S1)
        await h.drain()
        assert a.cancelled == []
        assert h.view(S1).display_text == "a:一"

    async def test_aclose_cancels_everything_and_rejects_new_sessions(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        a.gate(S1)
        h = make(a)
        await h.start(segs("一"))
        await wait_jobs(a, 1)

        mark = len(h.sink.log)
        await h.scheduler.aclose()
        assert a.cancelled == a.jobs
        assert h.sink.log[mark:] == []
        with pytest.raises(RuntimeError):
            await h.start(segs("二", prefix="n"), session="later")


# ---------------------------------------------------------------- 8 晚加入（BI-04）


class TestLateJoin:
    async def test_billing_engines_join_on_extend_and_reopen_race(self, make: MakeHarness) -> None:
        argos = FakeEngine("argos", 3)
        deepseek = FakeEngine("deepseek", 1, billing=True)
        deepseek.gate(S1)
        deepl = FakeEngine("deepl", 2, billing=True)
        cache = FakeCache()
        h = make(argos, deepseek, deepl, cache=cache)
        await h.start(segs("第一段", "第二段"), include_billing=False)
        await h.drain()
        assert deepseek.jobs == []
        assert deepl.jobs == []
        assert (h.view(S1).winner, h.view(S1).finished) == (argos.id, True)

        cache.data[key("第二段", deepl)] = "deepl 缓存"  # 会话开始之后才出现的缓存条目
        h.clock.now = 5.0
        await h.extend()
        # 更高优先级的实例加入后，已经结束的段重新打开。
        await h.sink.signal.wait_until(lambda: h.state(S1, deepseek) is RUNNING)
        assert not h.view(S1).finished
        h.clock.now = 5.4
        deepseek.release(S1)
        await h.drain()

        # 晚加入的组合同样先查缓存：deepl 在 s2 上命中，只为 s1 调用引擎。
        assert cache.get_calls[-1] == (
            key("第一段", deepseek),
            key("第一段", deepl),
            key("第二段", deepseek),
            key("第二段", deepl),
        )
        assert deepl.segments_seen() == [(S1,)]
        hit = h.attempt(S2, deepl)
        assert (hit.latency_ms, hit.cached) == (0.0, True)
        for segment_id in (S1, S2):
            assert (h.view(segment_id).winner, h.view(segment_id).finished) == (deepseek.id, True)
        # 晚加入实例的耗时从 extend_session 的时刻算起。
        assert h.attempt(S1, deepseek).latency_ms == pytest.approx(400.0)

    async def test_reopened_race_does_not_revive_hopeless_attempt(
        self, make: MakeHarness, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger=LOGGER)
        local = FakeEngine("local", 2, max_concurrency=2)
        local.gate(S1, S2)
        slow = FakeEngine("slow", 3)  # 并发 1：s1 执行中，s2 排队
        slow.gate(S1)
        deepseek = FakeEngine("deepseek", 1, billing=True)
        deepseek.gate(S2)
        h = make(local, slow, deepseek)
        await h.start(segs("一", "二"), include_billing=False)
        await wait_jobs(slow, 1)
        local.release(S1, S2)
        # local 在 s2 上胜出时，排在 slow 队列里的 s2 尝试被即时剪枝。
        await h.sink.signal.wait_until(lambda: bool(h.sink.recorded(S2, slow)))
        assert [r.state for r in h.sink.recorded(S2, slow)] == [CANCELLED]

        await h.extend()  # deepseek 加入，s2 的竞速重新打开
        await h.sink.signal.wait_until(lambda: h.state(S2, deepseek) is RUNNING)
        assert not h.finished(S2)

        # slow 的任务 [s2] 在竞速重开期间出队：已取消的尝试只被移除，不会被重新启动。
        slow.release(S1)
        await slow.signal.wait_until(lambda: slow.active == 0)
        for _ in range(10):  # 让 slow 的工作协程取出下一个任务
            await asyncio.sleep(0)
        assert not h.finished(S2)
        deepseek.release(S2)
        await h.drain()

        assert slow.segments_seen() == [(S1,)]
        assert len(h.sink.recorded(S2, slow)) == 1
        assert (h.view(S2).winner, h.view(S2).finished) == (deepseek.id, True)
        assert caplog.records == []

    async def test_extend_never_rejoins_existing_pairs(self, make: MakeHarness) -> None:
        local = FakeEngine("local", 2)
        deepl = FakeEngine("deepl", 3, billing=True)
        h = make(local, deepl)
        await h.start(segs("一"), include_billing=False)
        await h.drain()

        await h.extend()
        await h.drain()
        # 节省模式：deepl 的优先级低于已经胜出的 local，出队时被剪掉，不调用引擎。
        assert deepl.jobs == []
        assert h.attempt(S1, deepl).state is CANCELLED

        await h.extend()
        await h.drain()
        assert deepl.jobs == []
        assert len(h.sink.recorded(S1, deepl)) == 1

    async def test_extend_follows_each_segment_language_pair(self, make: MakeHarness) -> None:
        free = FakeEngine("free", 3)
        paid = FakeEngine("paid", 1, billing=True, pairs=[("en", "zh-Hans"), ("zh-Hans", "en")])
        h = make(free, paid)
        langs = {S1: (EN, ZH), S2: (ZH, ZH), S3: (JA, ZH), S4: (ZH, EN)}
        await h.start(segs("Hello", "你好", "こんにちは", "世界"), langs=langs, include_billing=False)
        await h.drain()
        await h.extend()
        await h.drain()

        assert paid.segments_seen() == [(S1,), (S4,)]
        assert [(job.source_lang, job.target_lang) for job in paid.jobs] == [(EN, ZH), (ZH, EN)]
        assert h.view(S2).attempts == ()
        assert [a.engine_id for a in h.view(S3).attempts] == [free.id]

    async def test_extend_admits_replaced_billing_instance(self, make: MakeHarness) -> None:
        """计费实例在会话开始后被替换（例如补填了 API key），Ctrl+Enter 时由新实例参赛。"""
        local = FakeEngine("argos", 2)
        old = FakeEngine("deepseek", 1, billing=True, revision=1)
        h = make(local, old)
        await h.start(segs("hello"), entry=Entry.BILINGUAL, include_billing=False)
        await h.drain()

        new = FakeEngine("deepseek", 1, billing=True, revision=2)
        new.gate(S1)
        h.scheduler.set_engines([local, new])
        await h.extend()
        await h.sink.signal.wait_until(lambda: h.state(S1, new) is RUNNING)
        assert not h.finished(S1)
        new.release(S1)
        await h.drain()

        assert old.jobs == []
        assert new.segments_seen() == [(S1,)]
        assert (h.view(S1).winner, h.view(S1).display_text) == (new.id, "deepseek:hello")

    async def test_extend_reruns_attempt_failed_by_replaced_instance(self, make: MakeHarness) -> None:
        """旧实例失败的段由同号新实例重新参赛，旧实例成功的段保持原状。"""
        local = FakeEngine("argos", 2)
        old = FakeEngine("deepseek", 1, billing=True, revision=1)
        old.behavior[S1] = Behavior.FAIL
        h = make(local, old)
        await h.start(segs("一", "二"), entry=Entry.BILINGUAL)
        await h.drain()
        assert h.attempt(S1, old).state is FAILED
        assert h.view(S1).winner == local.id

        new = FakeEngine("deepseek", 1, billing=True, revision=2)
        h.scheduler.set_engines([local, new])
        await h.extend()
        await h.drain()

        assert new.segments_seen() == [(S1,)]
        assert (h.view(S1).winner, h.view(S1).display_text) == (new.id, "deepseek:一")
        assert h.view(S2).display_text == "deepseek:二"  # 旧实例的结果
        assert [r.state for r in h.sink.recorded(S1, new)] == [FAILED, DONE]

        # 新实例自己的结果不再被替换：再按一次 Ctrl+Enter 什么也不发生。
        await h.extend()
        await h.drain()
        assert new.segments_seen() == [(S1,)]

    async def test_replacement_that_joined_is_retired_like_original(self, make: MakeHarness) -> None:
        """加入会话的新实例再被移除时，它在这个会话里的尝试同样按“实例已移除”结束。"""
        local = FakeEngine("argos", 2)
        old = FakeEngine("deepseek", 1, billing=True, revision=1)
        h = make(local, old)
        await h.start(segs("一"), entry=Entry.BILINGUAL, include_billing=False)
        await h.drain()
        new = FakeEngine("deepseek", 1, billing=True, revision=2)
        new.gate(S1)
        h.scheduler.set_engines([local, new])
        await h.extend()
        await wait_jobs(new, 1)

        h.scheduler.set_engines([local])
        await h.drain()
        removed = h.attempt(S1, new)
        assert (removed.state, removed.error) == (FAILED, "实例已移除")
        assert new.cancelled == new.jobs
        assert (h.view(S1).winner, h.view(S1).finished) == (local.id, True)

    async def test_extend_ignores_unknown_or_cancelled_sessions(self, make: MakeHarness) -> None:
        paid = FakeEngine("paid", 1, billing=True)
        h = make(paid)
        await h.extend("unknown")
        await h.start(segs("一"), include_billing=False)
        h.scheduler.cancel_session(Entry.SCREENSHOT)
        await h.extend()
        await h.drain()

        assert paid.jobs == []
        assert h.sink.log == []


# ---------------------------------------------------------------- 实例集合变化（6.5 节）


class TestSetEngines:
    async def test_removing_running_instance_fails_its_attempts_and_settles_display(
        self, make: MakeHarness
    ) -> None:
        a = FakeEngine("a", 1)  # 并发 1：s1 执行中，s2 排队
        a.gate(S1)
        b = FakeEngine("b", 2)
        h = make(a, b)
        await h.start(segs("一", "二"))
        await wait_jobs(a, 1)
        await h.sink.signal.wait_until(lambda: h.state(S1, b) is DONE and h.state(S2, b) is DONE)
        assert not h.finished(S1)  # 优先级最高的 a 仍可能改变显示

        h.scheduler.set_engines([b])
        # set_engines 返回时，a 的执行中和排队中的尝试都已记为失败。
        for segment_id in (S1, S2):
            assert [(r.state, r.error) for r in h.sink.recorded(segment_id, a)] == [(FAILED, "实例已移除")]
        await h.sink.signal.wait_until(lambda: h.finished(S1) and h.finished(S2), timeout=0.5)
        assert h.view(S1).winner == b.id
        await h.drain()

        assert a.cancelled == a.jobs  # 执行中的任务被取消
        assert a.segments_seen() == [(S1,)]  # 排队的任务被丢弃

    async def test_removing_instance_with_only_queued_work(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        b = FakeEngine("b", 2)
        h = make(a, b)
        await h.start(segs("一", "二"))
        # start_session 返回时，任务都在队列里，工作协程还没有运行。
        h.scheduler.set_engines([b])
        await h.drain()

        assert a.jobs == []
        for segment_id in (S1, S2):
            removed = h.attempt(segment_id, a)
            assert (removed.state, removed.error) == (FAILED, "实例已移除")
            assert (h.view(segment_id).winner, h.view(segment_id).finished) == (b.id, True)

    async def test_changed_priority_applies_only_to_later_sessions(self, make: MakeHarness) -> None:
        old_a = FakeEngine("a", 1)
        old_a.gate(S1)
        b = FakeEngine("b", 2)
        h = make(old_a, b)
        await h.start(segs("一"), session="first", entry=Entry.SCREENSHOT)
        await wait_jobs(old_a, 1)

        new_a = FakeEngine("a", 3)  # 同一实例号，优先级从 1 改为 3
        h.scheduler.set_engines([new_a, b])
        # 现存会话里旧实例的尝试按“实例已移除”结束，新对象不加入现存会话。
        assert [r.error for r in h.sink.recorded(S1, old_a, "first")] == ["实例已移除"]
        n1 = SegmentId("n1")
        await h.start(segs("二", prefix="n"), session="second", entry=Entry.BILINGUAL)
        await h.drain()

        assert old_a.cancelled == old_a.jobs
        assert [job.session_id for job in new_a.jobs] == [SessionId("second")]
        assert h.view(S1, "first").winner == b.id
        # 新会话按新优先级合并：b（2）高于 a（3）。
        assert h.view(n1, "second").winner == b.id

    async def test_new_instance_joins_only_later_sessions(self, make: MakeHarness) -> None:
        free = FakeEngine("free", 2)
        free.gate(S1)
        h = make(free, mode=RaceMode.FULL)
        await h.start(segs("一"), session="first", include_billing=False)
        await wait_jobs(free, 1)

        paid = FakeEngine("paid", 1, billing=True)
        added = FakeEngine("added", 3)
        h.scheduler.set_engines([free, paid, added])
        await h.extend("first")  # 现存会话不接纳新实例，extend_session 也一样
        free.release(S1)
        await h.start(segs("二", prefix="n"), session="second", entry=Entry.BILINGUAL)
        await h.drain()

        assert [a.engine_id for a in h.view(S1, "first").attempts] == [free.id]
        second = h.view(SegmentId("n1"), "second")
        assert {a.engine_id for a in second.attempts} == {free.id, paid.id, added.id}
        assert second.winner == paid.id

    async def test_unchanged_instance_is_left_alone(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        a.gate(S1)
        h = make(a)
        await h.start(segs("一"))
        await wait_jobs(a, 1)

        with pytest.raises(ValueError):  # 实例号重复：拒绝，且不做任何修改
            h.scheduler.set_engines([a, a])
        same_info = FakeEngine("a", 1)  # EngineInfo 相等的新对象
        h.scheduler.set_engines([same_info])
        a.release(S1)
        await h.drain()
        assert a.cancelled == []
        assert (h.attempt(S1, a).state, h.view(S1).winner) == (DONE, a.id)

        # 原样保留：之后的会话仍交给原来的对象。
        await h.start(segs("二", prefix="n"), session="next", entry=Entry.BILINGUAL)
        await h.drain()
        assert same_info.jobs == []
        assert len(a.jobs) == 2


# ---------------------------------------------------------------- 9 合帧与进度


class TestFrames:
    async def test_first_frame_contains_every_segment(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, pairs=[("en", "zh-Hans")])
        paid = FakeEngine("paid", 2, billing=True, pairs=[("ja", "zh-Hans")])
        a.gate(S1)
        h = make(a, paid)
        await h.start(
            segs("Hello", "你好", "こんにちは"),
            langs={S1: (EN, ZH), S2: (ZH, ZH), S3: (JA, ZH)},
            include_billing=False,
        )
        await h.sink.signal.wait_until(lambda: bool(h.sink.updates))

        _, first = h.sink.updates[0]
        # s3 只因计费实例未加入而暂无参赛者，合并器不会标记它，第一帧仍要带上它。
        assert [v.segment_id for v in first] == [S1, S2, S3]
        assert (first[1].skip_reason, first[1].finished) == (SkipReason.SAME_LANGUAGE, True)
        assert (first[2].skip_reason, first[2].attempts) == (None, ())
        a.release(S1)
        await h.drain()

    async def test_changes_within_one_frame_are_sent_once(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_batch_segments=3)
        a.gate(S1)
        h = make(a)
        await h.start(segs("一", "二", "三"))
        await h.sink.signal.wait_until(lambda: all(h.state(s, a) is RUNNING for s in (S1, S2, S3)))
        updates_before = len(h.sink.updates)
        progress_before = len(h.sink.progress)

        a.release(S1)  # 三个结果在同一轮事件循环里连续到达
        await h.drain()

        after = h.sink.updates[updates_before:]
        assert len(after) == 1
        _, views = after[0]
        assert [v.segment_id for v in views] == [S1, S2, S3]
        assert all(v.finished for v in views)
        assert h.sink.progress[progress_before:] == [(SESSION, 3, 3)]

    async def test_isolated_change_is_flushed_immediately(self, make: MakeHarness) -> None:
        frame = 0.1
        a = FakeEngine("a", 1)
        a.gate(S1)
        h = make(a, frame_interval=frame)
        await h.start(segs("一"))
        await h.sink.signal.wait_until(lambda: h.state(S1, a) is RUNNING)
        await asyncio.sleep(frame * 1.5)  # 会话空闲超过一帧
        before = len(h.sink.updates)

        a.release(S1)
        # 不等定时器：远不到一帧就收到结果。
        await h.sink.signal.wait_until(lambda: h.state(S1, a) is DONE, timeout=frame / 2)
        assert len(h.sink.updates) == before + 1

    async def test_burst_after_immediate_flush_is_merged_into_one_later_flush(
        self, make: MakeHarness
    ) -> None:
        frame = 0.1
        a = FakeEngine("a", 1, max_concurrency=3)
        a.gate(S1, S2, S3)
        h = make(a, frame_interval=frame)
        await h.start(segs("一", "二", "三"))
        await h.sink.signal.wait_until(lambda: all(h.state(s, a) is RUNNING for s in (S1, S2, S3)))
        await asyncio.sleep(frame * 1.5)
        before = len(h.sink.updates)

        a.release(S1)
        await h.sink.signal.wait_until(lambda: h.state(S1, a) is DONE, timeout=frame / 2)
        # 紧接着的两个结果在不同轮次到达，都落在上次刷新后的一帧之内。
        a.release(S2)
        await a.signal.wait_until(lambda: a.active == 1)
        a.release(S3)
        await h.drain()

        after = h.sink.updates[before:]
        assert [[v.segment_id for v in views] for _, views in after] == [[S1], [S2, S3]]
        gap = h.sink.update_times[before + 1] - h.sink.update_times[before]
        assert gap >= frame * 0.9

    async def test_progress_counts_finished_segments(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1, max_concurrency=2)
        a.gate(S1, S3)
        h = make(a)
        await h.start(segs("Hello", "你好", "World"), sources={S1: EN, S2: ZH, S3: EN})
        await h.sink.signal.wait_until(lambda: bool(h.sink.progress))
        # 不参加竞速的段一开始就算结束。
        assert h.sink.progress[0] == (SESSION, 1, 3)

        a.release(S1)
        await h.sink.signal.wait_until(lambda: h.sink.progress[-1][1] == 2)
        a.release(S3)
        await h.drain()

        finished = [f for _, f, _ in h.sink.progress]
        assert finished == sorted(finished)
        assert finished[-1] == 3
        assert {total for _, _, total in h.sink.progress} == {3}


# ---------------------------------------------------------------- 10 attempt_recorded


async def test_attempt_recorded_once_per_terminal_attempt(make: MakeHarness) -> None:
    a = FakeEngine("a", 1)  # 并发 1：s1 停在闸门上，s2 排队，s3 命中缓存
    a.gate(S1)
    b = FakeEngine("b", 2, max_concurrency=4)
    b.behavior.update({S1: Behavior.FAIL, S2: Behavior.FAIL, S3: Behavior.FAIL})
    c = FakeEngine("c", 3)  # 并发 1：s1 停在闸门上，s2、s3 排队
    c.gate(S1)
    cache = FakeCache({key("三", a): "缓存译文"})
    h = make(a, b, c, cache=cache)
    await h.start(segs("一", "二", "三"))
    await wait_jobs(a, 1)
    await wait_jobs(c, 1)
    await h.sink.signal.wait_until(lambda: all(h.sink.recorded(s, b) for s in (S1, S2, S3)))

    a.release(S1)
    await h.sink.signal.wait_until(lambda: bool(h.sink.recorded(S2, a)))
    c.release(S1)
    await h.drain()

    counts = Counter((seg, attempt.engine_id) for _, seg, attempt in h.sink.records)
    assert set(counts.values()) == {1}
    states = {(seg, attempt.engine_id): attempt.state for _, seg, attempt in h.sink.records}
    assert states == {
        (S1, a.id): DONE,
        (S1, b.id): FAILED,
        (S1, c.id): DONE,
        (S2, a.id): DONE,
        (S2, b.id): FAILED,
        (S2, c.id): CANCELLED,
        (S3, a.id): DONE,  # 缓存命中
        (S3, b.id): CANCELLED,
        (S3, c.id): CANCELLED,
    }
    cached = {(seg, attempt.engine_id) for _, seg, attempt in h.sink.records if attempt.cached}
    assert cached == {(S3, a.id)}
    assert h.sink.recorded(S3, a)[0].latency_ms == 0.0


# ---------------------------------------------------------------- 11 sink


class TestSink:
    async def test_sink_errors_are_logged_and_ignored(
        self, make: MakeHarness, caplog: pytest.LogCaptureFixture
    ) -> None:
        caplog.set_level(logging.ERROR, logger=LOGGER)
        a = FakeEngine("a", 1)
        b = FakeEngine("b", 2)
        sink = RaisingSink()
        h = make(a, b, sink=sink, mode=RaceMode.FULL)
        await h.start(segs("一", "二"))
        await h.drain()

        assert (h.view(S1).winner, h.view(S1).finished) == (a.id, True)
        assert len(sink.records) == 4
        assert sink.progress[-1] == (SESSION, 2, 2)  # segments_updated 抛异常后，同一帧的进度照常发出
        assert any("RaceSink" in r.getMessage() for r in caplog.records)

    async def test_sink_is_called_on_event_loop_thread(self, make: MakeHarness) -> None:
        a = FakeEngine("a", 1)
        h = make(a)
        await h.start(segs("一", "二"))
        await h.drain()
        assert h.sink.log
        assert h.sink.threads == {threading.get_ident()}


class TestConfigFingerprint:
    """EngineInfo.fingerprint 参与缓存键，revision 决定实例是否需要替换（架构文档 6.4、6.5 节）。"""

    async def test_cache_entry_from_other_fingerprint_is_not_used(self, make: MakeHarness) -> None:
        old = FakeEngine("a", 1, fingerprint="model-v1")
        new = FakeEngine("a", 1, fingerprint="model-v2")
        cache = FakeCache({key("一", old): "旧模型的译文"})
        h = make(new, cache=cache)
        await h.start(segs("一"))
        await h.drain()

        assert new.segments_seen() == [(S1,)]
        assert not h.attempt(S1, new).cached
        assert h.view(S1).display_text == "a:一"
        assert cache.puts == [(key("一", new), "a:一")]

    async def test_revision_change_replaces_instance(self, make: MakeHarness) -> None:
        old_a = FakeEngine("a", 1, revision=1)
        old_a.gate(S1)
        h = make(old_a)
        await h.start(segs("一"), session="first", entry=Entry.SCREENSHOT)
        await wait_jobs(old_a, 1)

        # 只换了 API key：译文相关配置不变，fingerprint 相同，revision 不同。
        new_a = FakeEngine("a", 1, revision=2)
        h.scheduler.set_engines([new_a])
        assert [r.error for r in h.sink.recorded(S1, old_a, "first")] == ["实例已移除"]

        await h.start(segs("二", prefix="n"), session="second", entry=Entry.BILINGUAL)
        await h.drain()
        assert [job.session_id for job in new_a.jobs] == [SessionId("second")]


# ---------------------------------------------------------------- 附带截图


def job_image(content: bytes, side: int = 64) -> JobImage:
    return JobImage.from_encoded("image/png", content, side, side // 2)


class TestImages:
    """截屏会话的截图只交给接收截图的实例，带图尝试的缓存键含截图摘要（需求 ENG-07）。"""

    async def test_only_image_lanes_receive_matching_image(self, make: MakeHarness) -> None:
        small, large = job_image(b"small", 512), job_image(b"large", 1568)
        vision = FakeEngine("vision", 1, image_max_side=1568)
        other_side = FakeEngine("other", 2, image_max_side=1024)
        plain = FakeEngine("plain", 3)
        h = make(vision, other_side, plain)
        await h.start(segs("一"), images={512: small, 1568: large})
        await h.drain()

        assert [job.image for job in vision.jobs] == [large]
        # 会话里没有 1024 的截图：该实例照常参赛，只是不带图。
        assert [job.image for job in other_side.jobs] == [None]
        assert [job.image for job in plain.jobs] == [None]

    async def test_session_without_images_sends_none(self, make: MakeHarness) -> None:
        vision = FakeEngine("vision", 1, image_max_side=1568)
        h = make(vision)
        await h.start(segs("一"), entry=Entry.BILINGUAL)
        await h.drain()
        assert [job.image for job in vision.jobs] == [None]

    async def test_cache_key_contains_image_digest(self, make: MakeHarness) -> None:
        image = job_image(b"shot-1")
        vision = FakeEngine("vision", 1, image_max_side=64)
        plain = FakeEngine("plain", 2)
        cache = FakeCache()
        h = make(vision, plain, cache=cache, mode=RaceMode.FULL)
        await h.start(segs("一"), images={64: image})
        await h.drain()

        written = dict(cache.puts)
        with_image = CacheKey("一", EN, ZH, vision.id, "", image.sha256)
        assert written[with_image] == "vision:一"
        assert written[key("一", plain)] == "plain:一"  # 不带图的实例，键与原来相同
        assert key("一", vision) not in written
        assert {k.image_digest for k in cache.get_calls[0]} == {image.sha256, ""}

    async def test_same_text_with_other_image_misses_cache(self, make: MakeHarness) -> None:
        first, second = job_image(b"shot-1"), job_image(b"shot-2")
        vision = FakeEngine("vision", 1, image_max_side=64)
        cache = FakeCache()
        h = make(vision, cache=cache)
        await h.start(segs("一"), session="first", images={64: first})
        await h.drain()
        await h.start(segs("一"), session="second", images={64: second})
        await h.drain()
        assert len(vision.jobs) == 2
        assert not h.attempt(S1, vision, "second").cached

        await h.start(segs("一"), session="third", images={64: job_image(b"shot-1")})
        await h.drain()
        assert len(vision.jobs) == 2  # 同一张截图（摘要相同）命中缓存
        assert h.attempt(S1, vision, "third").cached

    async def test_text_only_cache_entry_is_not_used_for_image_attempt(self, make: MakeHarness) -> None:
        vision = FakeEngine("vision", 1, image_max_side=64)
        cache = FakeCache({key("一", vision): "不带图的译文"})
        h = make(vision, cache=cache)
        await h.start(segs("一"), images={64: job_image(b"shot")})
        await h.drain()
        assert h.view(S1).display_text == "vision:一"
        assert not h.attempt(S1, vision).cached

    async def test_attempt_records_carry_no_image(self, make: MakeHarness) -> None:
        vision = FakeEngine("vision", 1, image_max_side=64)
        h = make(vision)
        await h.start(segs("一"), images={64: job_image(b"PIXELS")})
        await h.drain()
        assert h.sink.records
        for _, _, attempt in h.sink.records:
            assert "PIXELS" not in repr(attempt)

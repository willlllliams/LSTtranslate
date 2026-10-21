"""竞速调度器，实现架构文档 6.4 至 6.6 节。

合并器（merger.py）回答“结果到达后显示什么、竞速是否结束、某个尝试是否还可能改变显示”，
调度器负责其余部分：

- 决定每个段由哪些引擎实例参赛，派发前查译文缓存（RACE-02、RACE-08、RACE-09）；
- 把段按语言对打包成任务，放进各实例的两级队列，按实例的并发上限执行（6.4 节）；
- 节省模式下剪掉不可能改变显示的排队尝试：胜出者改变时即时剪枝，任务出队时兜底（RACE-05）；
- 按入口维护当前会话，新会话开始时取消旧会话；引擎实例集合变化时让受影响的尝试结束（6.5 节）；
- 把合并器的状态变化按帧合并后交给 sink（6.6 节）。

全部代码运行在同一个 asyncio 事件循环线程上，内部状态不加锁（架构文档第 3 章）。
调度器对外只通过注入的 RaceSink 输出，不依赖事件总线，也不依赖 Qt。
"""

import asyncio
import logging
import time
from collections import deque
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, Entry, Lang, SegmentId, SessionId
from lst.contracts.ports import CacheKey, TranslationCache
from lst.contracts.translation import (
    AttemptState,
    AttemptView,
    Engine,
    EngineInfo,
    Job,
    Segment,
    SegmentOutcome,
    SegmentView,
    SkipReason,
)
from lst.services.race.merger import SegmentRace, SessionRace

logger = logging.getLogger(__name__)

ERROR_MISSING = "引擎未返回该段"
ERROR_REMOVED = "实例已移除"

LangPair = tuple[Lang, Lang]
"""（源语言，目标语言）。"""

# 两级队列的取用顺序：双语窗口的任务先于截屏的任务（架构文档 6.4 节）。
_ENTRY_ORDER: tuple[Entry, ...] = (Entry.BILINGUAL, *(e for e in Entry if e is not Entry.BILINGUAL))


class RaceMode(StrEnum):
    """竞速的结束方式（需求 RACE-05）。"""

    SAVER = "saver"
    """节省模式：已经不可能改变显示的排队尝试被剪枝，不调用引擎。"""

    FULL = "full"
    """完整模式：所有参赛实例都跑完，不剪枝。"""


class RaceSink(Protocol):
    """调度器的输出端，由 services.race.module 的 _BusSink 适配成事件总线上的消息（架构文档 5.3 节）。

    三个方法都在事件循环线程上同步调用，实现里不能阻塞。实现抛出的异常由调度器记录日志后忽略。
    会话被取消后，调度器不再为它调用任何方法。
    """

    def segments_updated(self, session_id: SessionId, views: tuple[SegmentView, ...]) -> None:
        """自上次刷新以来有变化的段的快照。每个会话的第一帧包含全部段。"""
        ...

    def session_progress(self, session_id: SessionId, finished: int, total: int) -> None:
        """显示已定的段数和总段数（需求 OV-07），每帧一次。"""
        ...

    def attempt_recorded(self, session_id: SessionId, segment_id: SegmentId, attempt: AttemptView) -> None:
        """一个尝试进入终态（DONE、FAILED、CANCELLED），每个尝试恰好一次，供竞速统计使用（RACE-07）。"""
        ...


def pack_segments(
    segments: Sequence[Segment], max_segments: int, max_chars: int
) -> list[tuple[Segment, ...]]:
    """按段的顺序贪心打包（架构文档 6.4 节）。

    一个任务最多 max_segments 段，文本总长不超过 max_chars。当前任务再放入下一段就会超限时，
    另起一个任务；一个段本身超过 max_chars 时，它单独成为一个任务。max_segments 小于 1 时按 1 处理。
    """
    segment_limit = max(1, max_segments)
    batches: list[tuple[Segment, ...]] = []
    current: list[Segment] = []
    chars = 0
    for segment in segments:
        size = len(segment.text)
        if current and (len(current) >= segment_limit or chars + size > max_chars):
            batches.append(tuple(current))
            current, chars = [], 0
        current.append(segment)
        chars += size
    if current:
        batches.append(tuple(current))
    return batches


# ---------------------------------------------------------------- 内部数据结构


class _Lane:
    """一个引擎实例的执行通道：两级 FIFO 队列，加上 max_concurrency 个工作协程。

    实例被 set_engines 移除或替换后 retired 置位：工作协程和执行中的任务被取消，队列里的任务不再取出。
    """

    __slots__ = (
        "engine",
        "id",
        "idle",
        "info",
        "outstanding",
        "queues",
        "ready",
        "retired",
        "running",
        "workers",
    )

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.info: EngineInfo = engine.info
        self.id: EngineId = self.info.id
        self.queues: dict[Entry, deque[_QueuedJob]] = {entry: deque() for entry in _ENTRY_ORDER}
        self.ready = asyncio.Semaphore(0)  # 计数等于各级队列中的任务总数
        self.workers: list[asyncio.Task[None]] = []
        self.running: set[asyncio.Task[None]] = set()
        # 已入队、尚未处理完（含执行中）的任务数；归零时 idle 置位，供 drain 等待。
        self.outstanding = 0
        self.idle = asyncio.Event()
        self.idle.set()
        self.retired = False

    def put(self, job: "_QueuedJob") -> None:
        self.queues[job.session.entry].append(job)
        self.outstanding += 1
        self.idle.clear()
        self.ready.release()

    async def get(self) -> "_QueuedJob":
        """取下一个任务：先取双语窗口的队列，为空时再取截屏的队列。"""
        await self.ready.acquire()
        for entry in _ENTRY_ORDER:
            queue = self.queues[entry]
            if queue:
                return queue.popleft()
        raise RuntimeError("队列计数与内容不一致")

    def task_done(self) -> None:
        self.outstanding -= 1
        if self.outstanding == 0:
            self.idle.set()


@dataclass(eq=False, slots=True)
class _Session:
    """调度器内部的会话记录。closed 置位后，属于这个会话的结果和刷新全部丢弃。

    lanes 是会话开始时的实例集合：之后新增或替换的实例不加入这个会话，extend_session 也只在其中选。
    last_flush 是上一次刷新的事件循环时刻，用来判断会话是否空闲（架构文档 6.6 节）。
    """

    id: SessionId
    entry: Entry
    race: SessionRace
    langs: dict[SegmentId, LangPair]
    context: str
    order: dict[SegmentId, int]
    lanes: tuple[_Lane, ...]
    closed: bool = False
    last_flush: float | None = None
    flush_handle: asyncio.Handle | None = None
    running: set[asyncio.Task[None]] = field(default_factory=lambda: set[asyncio.Task[None]]())


@dataclass(frozen=True, eq=False, slots=True)
class _QueuedJob:
    """排队中的任务。出队时才决定最终包含哪些段，所以这里不直接存 Job。"""

    session: _Session
    langs: LangPair
    segments: tuple[Segment, ...]


@dataclass(frozen=True, slots=True)
class _Pair:
    """派发中的一个（段，实例）组合。joined 为 False 表示只在缓存命中时才参赛（计费实例未被纳入时）。"""

    segment: Segment
    lane: _Lane
    joined: bool


# ---------------------------------------------------------------- 调度器


class RaceScheduler:
    """竞速调度器。每个入口同一时刻最多一个当前会话，会话之间共享各实例的队列和并发名额。"""

    def __init__(
        self,
        engines: Sequence[Engine],
        sink: RaceSink,
        *,
        mode: RaceMode = RaceMode.SAVER,
        cache: TranslationCache | None = None,
        clock: Callable[[], float] = time.monotonic,
        frame_interval: float = 1 / 60,
    ) -> None:
        if frame_interval < 0:
            raise ValueError("frame_interval 不能为负数")
        self._lanes: dict[EngineId, _Lane] = {}
        for engine in engines:
            lane = _Lane(engine)
            if lane.id in self._lanes:
                raise ValueError(f"引擎实例 {lane.id} 重复")
            self._lanes[lane.id] = lane
        self._sink = sink
        self._mode = mode
        self._cache = cache
        self._clock = clock
        self._frame_interval = frame_interval
        self._sessions: dict[SessionId, _Session] = {}
        self._current: dict[Entry, _Session] = {}
        self._dispatching: set[asyncio.Future[None]] = set()  # 正在查缓存的派发
        self._cache_writes: set[asyncio.Task[None]] = set()
        self._retiring: set[asyncio.Task[None]] = set()  # 被移除实例的工作协程与任务，正在退出
        self._closed = False

    # ---- 公开接口 ----

    @property
    def mode(self) -> RaceMode:
        return self._mode

    def set_mode(self, mode: RaceMode) -> None:
        """切换竞速模式。已经执行的剪枝不会撤回；新模式只影响之后的判断。"""
        self._mode = mode

    def set_engines(self, engines: Sequence[Engine]) -> None:
        """更新引擎实例集合（架构文档 6.5 节活性保证第 4 条），以 EngineInfo 是否相等判断实例有没有变化。

        - 被移除或 EngineInfo 有变化的旧实例：它在所有现存会话中的未结束尝试记为失败，原因为
          “实例已移除”；执行中的任务被取消，队列里的任务丢弃，工作协程退出。
        - 有变化的实例和新增的实例：只参加之后开始的会话。
        - 没有变化的实例：原样保留，包括正在执行的任务和排队的任务。

        实例号重复时抛出 ValueError，不做任何修改。
        """
        self._check_open()
        incoming: dict[EngineId, Engine] = {}
        for engine in engines:
            engine_id = engine.info.id
            if engine_id in incoming:
                raise ValueError(f"引擎实例 {engine_id} 重复")
            incoming[engine_id] = engine
        lanes: dict[EngineId, _Lane] = {}
        for engine_id, engine in incoming.items():
            old = self._lanes.get(engine_id)
            lanes[engine_id] = old if old is not None and old.info == engine.info else _Lane(engine)
        retired = [lane for lane in self._lanes.values() if lanes.get(lane.id) is not lane]
        self._lanes = lanes
        for lane in retired:
            self._retire(lane)

    async def start_session(
        self,
        entry: Entry,
        session_id: SessionId,
        segments: Sequence[Segment],
        langs: Mapping[SegmentId, LangPair],
        *,
        include_billing: bool = True,
        context: str = "",
    ) -> None:
        """开始一个会话：取消同一入口的旧会话，让各段参赛，查缓存后把任务放进队列。

        langs 给出每个段的（源语言，目标语言）。双语窗口的互译规则（RACE-10）会让同一会话里
        不同段的目标语言不同，所以语言对按段指定。下列段不参加竞速，并标出原因：

        - 源语言与目标语言相同（RACE-09），标为 SAME_LANGUAGE；
        - 没有任何实例（包括计费实例）支持该语言对，标为 NO_ENGINE。

        include_billing 为 False 时，计费实例只有缓存命中的组合参赛（命中不产生费用），
        其余组合留给 extend_session（BI-04）；只因此暂时没有参赛者的段不标记跳过。
        方法在任务入队后返回，不等待翻译结果。参数非法时抛出 ValueError，旧会话不受影响。
        """
        self._check_open()
        segments = tuple(segments)
        missing = [s.id for s in segments if s.id not in langs]
        if missing:
            raise ValueError(f"会话 {session_id} 中段 {missing} 缺少语言对")
        race = SessionRace(session_id, segments)  # 段号重复时抛出 ValueError
        old = self._current.get(entry)
        existing = self._sessions.get(session_id)
        if existing is not None and existing is not old:
            raise ValueError(f"会话 {session_id} 已经存在")
        if old is not None:
            self._cancel(old)

        session = _Session(
            id=session_id,
            entry=entry,
            race=race,
            langs={s.id: langs[s.id] for s in segments},
            context=context,
            order={s.id: i for i, s in enumerate(segments)},
            lanes=tuple(self._lanes.values()),
        )
        self._sessions[session_id] = session
        self._current[entry] = session

        now = self._clock()
        memo: dict[LangPair, list[_Lane]] = {}
        pairs: list[_Pair] = []
        for segment in segments:
            pair = session.langs[segment.id]
            if pair[0] == pair[1]:
                race.skip(segment.id, SkipReason.SAME_LANGUAGE)
                continue
            lanes = self._supporting_lanes(session.lanes, pair, memo)
            if not lanes:
                race.skip(segment.id, SkipReason.NO_ENGINE)
                continue
            for lane in lanes:
                if lane.info.billing and not include_billing:
                    if self._cache is not None:
                        pairs.append(_Pair(segment, lane, joined=False))
                    continue
                race.add(segment.id, lane.id, lane.info.priority, now)
                pairs.append(_Pair(segment, lane, joined=True))
        # 即使没有任何参赛实例也安排第一帧，界面由此拿到全部段的初始快照和进度。
        self._touch(session)
        await self._dispatch(session, pairs, now)

    async def extend_session(self, session_id: SessionId) -> None:
        """让计费实例加入一个已经开始的会话（需求 BI-04 的 Ctrl+Enter）。

        对每个没有被标记跳过的段，加入会话开始时就存在、仍未被移除、支持该段语言对、
        但还没参赛的计费实例。已经参赛的组合（包括已被剪枝的）保持原状，不会重新参赛。
        会话不存在或已取消时什么也不做。
        """
        self._check_open()
        session = self._sessions.get(session_id)
        if session is None:
            logger.debug("会话 %s 不存在或已取消，忽略 extend_session", session_id)
            return
        now = self._clock()
        live = tuple(lane for lane in session.lanes if not lane.retired)
        memo: dict[LangPair, list[_Lane]] = {}
        pairs: list[_Pair] = []
        for race in session.race.segments():
            if race.skip_reason is not None:
                continue
            segment = race.segment
            participants = set(race.participants())
            for lane in self._supporting_lanes(live, session.langs[segment.id], memo):
                if not lane.info.billing or lane.id in participants:
                    continue
                session.race.add(segment.id, lane.id, lane.info.priority, now)
                pairs.append(_Pair(segment, lane, joined=True))
        if pairs:
            self._touch(session)
            await self._dispatch(session, pairs, now)

    def cancel_session(self, entry: Entry) -> None:
        """取消该入口的当前会话（RACE-01）。之后这个会话不再产生任何 sink 调用。"""
        session = self._current.get(entry)
        if session is not None:
            self._cancel(session)

    async def drain(self) -> None:
        """等到没有正在派发、排队或执行的任务，并且最后一次合帧刷新已经完成。供测试使用。"""
        while not self._closed:
            busy = [t for t in (*self._dispatching, *self._cache_writes, *self._retiring) if not t.done()]
            if busy:
                await asyncio.wait(busy)
                continue
            lane = next((lane for lane in self._lanes.values() if lane.outstanding), None)
            if lane is not None:
                await lane.idle.wait()
                continue
            handles = [s.flush_handle for s in self._sessions.values() if s.flush_handle is not None]
            if handles:
                now = asyncio.get_running_loop().time()
                delays = [h.when() - now if isinstance(h, asyncio.TimerHandle) else 0.0 for h in handles]
                await asyncio.sleep(max(0.0, min(delays)))
                continue
            return

    async def aclose(self) -> None:
        """取消全部会话，停止工作协程，等待缓存写入完成。之后不能再开始会话。可以重复调用。"""
        if self._closed:
            return
        self._closed = True
        running = [t for s in self._sessions.values() for t in s.running]
        for session in tuple(self._sessions.values()):
            self._cancel(session)
        workers = [w for lane in self._lanes.values() for w in lane.workers]
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*running, *workers, *self._retiring, *self._cache_writes, return_exceptions=True)

    # ---- 派发 ----

    @staticmethod
    def _supporting_lanes(
        lanes: Sequence[_Lane], pair: LangPair, memo: dict[LangPair, list[_Lane]]
    ) -> list[_Lane]:
        """lanes 中支持该语言对的实例（含计费实例），顺序不变。supports 抛出异常时按不支持处理。"""
        cached = memo.get(pair)
        if cached is not None:
            return cached
        supporting: list[_Lane] = []
        for lane in lanes:
            try:
                supported = lane.engine.supports(*pair)
            except Exception:
                logger.exception("实例 %s 的 supports%s 出错，按不支持处理", lane.id, pair)
                continue
            if supported:
                supporting.append(lane)
        memo[pair] = supporting
        return supporting

    async def _dispatch(self, session: _Session, pairs: list[_Pair], now: float) -> None:
        """处理一批（段，实例）组合：先查缓存，再把参赛且仍在排队的组合打包入队。

        pairs 按段的顺序排列。now 是这批组合的派发时刻，缓存命中的结果以同一时刻完成，耗时为 0。
        """
        if not pairs:
            return
        if self._cache is not None:
            # 查缓存期间任务还没入队，用一个 future 标记“派发进行中”，drain 据此等待。
            pending: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._dispatching.add(pending)
            try:
                await self._apply_cache(session, self._cache, pairs, now)
            finally:
                self._dispatching.discard(pending)
                pending.set_result(None)
            if session.closed:
                return
        self._enqueue_pairs(session, [p for p in pairs if p.joined])

    async def _apply_cache(
        self, session: _Session, cache: TranslationCache, pairs: list[_Pair], now: float
    ) -> None:
        """批量查缓存，命中的组合直接以耗时 0 完成（RACE-08）。缓存出错只记日志，按未命中处理。

        未参赛的计费组合命中时先参赛再完成；没命中的保持不参赛。
        """
        keys: dict[CacheKey, _Pair] = {}
        for p in pairs:
            if p.joined and self._state(session, p.segment.id, p.lane.id) is not AttemptState.QUEUED:
                continue
            source, target = session.langs[p.segment.id]
            keys[CacheKey(p.segment.text, source, target, p.lane.id, p.lane.info.fingerprint)] = p
        if not keys:
            return
        try:
            hits = await cache.get_many(tuple(keys))
        except asyncio.CancelledError:
            # 调用方在查缓存时被取消：这批组合不会再入队，把它们标为已取消，免得竞速永远挂起。
            self._abandon(session, [p for p in pairs if p.joined])
            raise
        except Exception:
            logger.exception("会话 %s 查询译文缓存失败，全部按未命中处理", session.id)
            return
        for key, p in keys.items():
            text = hits.get(key)
            if text is None:
                continue
            if session.closed:
                return
            race = session.race.segment(p.segment.id)
            if p.lane.id not in race.participants():
                if p.lane.retired:
                    continue
                session.race.add(p.segment.id, p.lane.id, p.lane.info.priority, now)
            if race.attempt(p.lane.id).state is AttemptState.QUEUED:
                self._resolve(session, p.lane.id, SegmentOutcome(p.segment.id, text=text), now, cached=True)

    def _enqueue_pairs(self, session: _Session, pairs: list[_Pair]) -> None:
        """把仍在排队的组合按实例、语言对分组打包，按首段顺序放进各实例的队列。"""
        grouped: dict[_Lane, dict[LangPair, list[Segment]]] = {}
        for p in pairs:
            if self._state(session, p.segment.id, p.lane.id) is not AttemptState.QUEUED:
                continue
            by_pair = grouped.setdefault(p.lane, {})
            by_pair.setdefault(session.langs[p.segment.id], []).append(p.segment)
        for lane, by_pair in grouped.items():
            if lane.retired:
                # 实例在查缓存期间被移除：它的尝试已由 _retire 记为失败，这里只是兜底。
                self._fail_lane_attempts(session, lane, [s for segs in by_pair.values() for s in segs])
                continue
            jobs = [
                _QueuedJob(session, pair, batch)
                for pair, segments in by_pair.items()
                for batch in pack_segments(segments, lane.info.max_batch_segments, lane.info.max_batch_chars)
            ]
            jobs.sort(key=lambda job: session.order[job.segments[0].id])
            for job in jobs:
                self._enqueue(lane, job)

    def _enqueue(self, lane: _Lane, job: _QueuedJob) -> None:
        if self._closed:
            return
        if not lane.workers:
            lane.workers = [
                asyncio.create_task(self._work(lane), name=f"race-worker-{lane.id}-{i}")
                for i in range(max(1, lane.info.max_concurrency))
            ]
        lane.put(job)

    # ---- 执行 ----

    async def _work(self, lane: _Lane) -> None:
        """实例的一个工作协程：先取双语窗口的任务，再取截屏的任务，剪枝后执行。"""
        while True:
            item = await lane.get()
            try:
                try:
                    job = self._take(lane, item)
                except Exception:
                    # 出错时丢弃这个任务，但不能让工作协程退出，否则该实例永久少一个并发名额。
                    logger.exception("实例 %s 出队剪枝出错（会话 %s）", lane.id, item.session.id)
                    continue
                if job is not None:
                    await self._execute(lane, item.session, job)
            finally:
                lane.task_done()

    def _take(self, lane: _Lane, item: _QueuedJob) -> Job | None:
        """出队兜底剪枝（架构文档 6.4 节）。

        对任务里的每个段检查该实例的尝试：

        - 状态不是 QUEUED（例如已被即时剪枝）：移除；
        - 节省模式下已经不可能改变显示（不属于集合 H）：标为 CANCELLED 并移除；
        - 其余标为 RUNNING 并保留。

        即时剪枝只在胜出者改变时触发，下面两种情况要靠这里补上：晚加入的实例优先级低于已有的胜出者；
        完整模式期间排队的尝试在切换到节省模式之后出队。已取消会话或已移除实例的任务、
        剪枝后为空的任务都返回 None，不调用引擎。
        """
        session = item.session
        if session.closed or lane.retired:
            return None
        saver = self._mode is RaceMode.SAVER
        kept: list[Segment] = []
        for segment in item.segments:
            race = session.race.segment(segment.id)
            if race.attempt(lane.id).state is not AttemptState.QUEUED:
                continue
            if saver and not race.contending(lane.id):
                self._cancel_attempt(session, race, lane.id, self._clock())
                if session.closed:
                    return None
                continue
            kept.append(segment)
        if not kept:
            return None
        for segment in kept:
            session.race.start(segment.id, lane.id)
        self._touch(session)
        source, target = item.langs
        return Job(
            session_id=session.id,
            engine_id=lane.id,
            segments=tuple(kept),
            source_lang=source,
            target_lang=target,
            context=session.context,
        )

    async def _execute(self, lane: _Lane, session: _Session, job: Job) -> None:
        """在独立的 asyncio 任务里执行 job。会话取消或实例移除时只取消这个任务。"""
        task = asyncio.create_task(self._run_job(lane, session, job), name=f"race-job-{lane.id}")
        session.running.add(task)
        lane.running.add(task)
        try:
            await asyncio.wait((task,))
        finally:
            session.running.discard(task)
            lane.running.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            logger.error("实例 %s 的任务执行出错（会话 %s）", lane.id, session.id, exc_info=exc)

    async def _run_job(self, lane: _Lane, session: _Session, job: Job) -> None:
        """调用引擎并逐个合并结果。

        引擎违反协议时由这里兜底：抛出异常、超时、漏掉段，都把还没出结果的段记为失败；
        产出不属于本任务的段号或重复的段号时忽略。会话被取消或实例被移除后到达的结果一律丢弃。
        """
        remaining = {segment.id: segment for segment in job.segments}
        deadline = asyncio.timeout(lane.info.timeout_s)
        try:
            async with deadline:
                stream = lane.engine.translate(job)
                try:
                    async for outcome in stream:
                        if session.closed or lane.retired:
                            return
                        segment = remaining.pop(outcome.segment_id, None)
                        if segment is None:
                            logger.warning(
                                "实例 %s 产出了不属于本任务或重复的段 %s，已忽略（会话 %s）",
                                lane.id,
                                outcome.segment_id,
                                session.id,
                            )
                            continue
                        self._resolve(session, lane.id, outcome, self._clock())
                        if outcome.text is not None:
                            self._write_cache(lane, job, segment, outcome.text)
                finally:
                    await _close_stream(stream)
        except TimeoutError as exc:
            if deadline.expired():
                error, code = _timeout_message(lane.info.timeout_s), ErrorCode.TIMEOUT
            else:
                error, code = _describe(exc), ErrorCode.INTERNAL
        except Exception as exc:
            logger.warning("实例 %s 执行任务时抛出异常（会话 %s）", lane.id, session.id, exc_info=True)
            error, code = _describe(exc), ErrorCode.INTERNAL
        else:
            error, code = ERROR_MISSING, ErrorCode.MISSING_SEGMENT

        if session.closed or lane.retired or not remaining:
            return
        now = self._clock()
        for segment_id in remaining:
            if session.closed:
                return
            self._resolve(session, lane.id, SegmentOutcome(segment_id, error=error, code=code), now)

    def _write_cache(self, lane: _Lane, job: Job, segment: Segment, text: str) -> None:
        """在后台写缓存，不拖慢后续结果的合并。"""
        if self._cache is None:
            return
        key = CacheKey(segment.text, job.source_lang, job.target_lang, job.engine_id, lane.info.fingerprint)
        task = asyncio.create_task(_put_cache(self._cache, key, text))
        self._cache_writes.add(task)
        task.add_done_callback(self._cache_writes.discard)

    # ---- 实例移除 ----

    def _retire(self, lane: _Lane) -> None:
        """移除一个实例：未结束的尝试记为失败，执行中的任务和工作协程被取消，队列不再取出。"""
        lane.retired = True
        now = self._clock()
        for session in tuple(self._sessions.values()):
            if lane not in session.lanes:
                continue  # 会话开始时还没有这个实例，同号的尝试属于别的通道
            for race in session.race.segments():
                if session.closed:
                    break
                if lane.id in race.participants() and race.attempt(lane.id).state.pending:
                    self._resolve(
                        session,
                        lane.id,
                        SegmentOutcome(race.segment.id, error=ERROR_REMOVED, code=ErrorCode.ENGINE_REMOVED),
                        now,
                    )
        for task in (*lane.workers, *lane.running):
            task.cancel()
            self._retiring.add(task)
            task.add_done_callback(self._retiring.discard)

    def _fail_lane_attempts(self, session: _Session, lane: _Lane, segments: Sequence[Segment]) -> None:
        now = self._clock()
        for segment in segments:
            if session.closed:
                return
            self._resolve(
                session,
                lane.id,
                SegmentOutcome(segment.id, error=ERROR_REMOVED, code=ErrorCode.ENGINE_REMOVED),
                now,
            )

    # ---- 合并器状态变更 ----

    @staticmethod
    def _state(session: _Session, segment_id: SegmentId, engine_id: EngineId) -> AttemptState:
        return session.race.segment(segment_id).attempt(engine_id).state

    def _resolve(
        self,
        session: _Session,
        engine_id: EngineId,
        outcome: SegmentOutcome,
        now: float,
        *,
        cached: bool = False,
    ) -> None:
        """把一个结果交给合并器；尝试进入终态时通知 sink。

        节省模式下，结果让胜出者改变时立即剪枝（架构文档 6.4 节）：该段处于 QUEUED、且优先级
        不高于新胜出者的尝试标为 CANCELLED。胜出者不变时没有尝试会离开 H，不必检查。
        """
        race = session.race.segment(outcome.segment_id)
        if not race.attempt(engine_id).state.pending:
            return
        changed = session.race.resolve(engine_id, outcome, now, cached=cached)
        self._touch(session)
        self._record(session, race, engine_id)
        if changed and self._mode is RaceMode.SAVER:
            for other in race.participants():
                if session.closed:
                    return
                if race.attempt(other).state is AttemptState.QUEUED and not race.contending(other):
                    self._cancel_attempt(session, race, other, now)

    def _cancel_attempt(self, session: _Session, race: SegmentRace, engine_id: EngineId, now: float) -> None:
        if session.race.cancel(race.segment.id, engine_id, now):
            self._touch(session)
            self._record(session, race, engine_id)

    def _abandon(self, session: _Session, pairs: list[_Pair]) -> None:
        """把一批未能入队的组合标为已取消。"""
        now = self._clock()
        for p in pairs:
            if session.closed:
                return
            self._cancel_attempt(session, session.race.segment(p.segment.id), p.lane.id, now)

    # ---- 会话取消 ----

    def _cancel(self, session: _Session) -> None:
        """取消会话（架构文档 6.5 节）。

        会话从会话表移除并置 closed：排队中的任务出队时直接丢弃，执行中的任务被取消，
        待执行的刷新被撤销。之后到达的结果在合并前按 closed 丢弃。
        """
        if session.closed:
            return
        session.closed = True
        if self._sessions.get(session.id) is session:
            del self._sessions[session.id]
        if self._current.get(session.entry) is session:
            del self._current[session.entry]
        if session.flush_handle is not None:
            session.flush_handle.cancel()
            session.flush_handle = None
        for task in tuple(session.running):
            task.cancel()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("调度器已关闭")

    # ---- 输出 ----

    def _touch(self, session: _Session) -> None:
        """会话状态有变化，安排一次刷新（架构文档 6.6 节）。

        会话空闲（距上次刷新已满 frame_interval，且没有待执行的刷新）时，第一次变化在事件循环的
        下一轮立即刷新；此后 frame_interval 之内的变化合并到上次刷新满一帧时的那次刷新。
        只用定时刷新的话，Windows 约 15.6 毫秒的定时器粒度会让单个结果的显示延迟超过 30 毫秒。
        """
        if session.closed or session.flush_handle is not None:
            return
        loop = asyncio.get_running_loop()
        if session.last_flush is None or loop.time() >= session.last_flush + self._frame_interval:
            session.flush_handle = loop.call_soon(self._flush, session)
        else:
            session.flush_handle = loop.call_at(
                session.last_flush + self._frame_interval, self._flush, session
            )

    def _flush(self, session: _Session) -> None:
        """把自上次刷新以来变化的段一次性交给 sink，然后报告进度。"""
        session.flush_handle = None
        if session.closed:
            return
        session.last_flush = asyncio.get_running_loop().time()
        views = session.race.take_dirty()
        if views:
            self._notify(lambda: self._sink.segments_updated(session.id, views))
            if session.closed:
                return
        race = session.race
        self._notify(lambda: self._sink.session_progress(session.id, race.finished_count, race.total))

    def _record(self, session: _Session, race: SegmentRace, engine_id: EngineId) -> None:
        if session.closed:
            return
        view = race.attempt(engine_id).view()
        segment_id = race.segment.id
        self._notify(lambda: self._sink.attempt_recorded(session.id, segment_id, view))

    @staticmethod
    def _notify(call: Callable[[], None]) -> None:
        """调用 sink；sink 抛出的异常只记日志，不影响竞速。"""
        try:
            call()
        except Exception:
            logger.exception("RaceSink 处理出错")


# ---------------------------------------------------------------- 辅助函数


async def _close_stream(stream: AsyncIterator[SegmentOutcome]) -> None:
    """关闭引擎的异步生成器，让它的 finally 及时执行。普通异步迭代器没有 aclose，跳过。"""
    if isinstance(stream, AsyncGenerator):
        try:
            await stream.aclose()
        except Exception:
            logger.warning("关闭引擎结果流时出错", exc_info=True)


async def _put_cache(cache: TranslationCache, key: CacheKey, text: str) -> None:
    try:
        await cache.put(key, text)
    except Exception:
        logger.exception("写入译文缓存失败（实例 %s）", key.engine_id)


def _describe(exc: BaseException) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _timeout_message(timeout_s: float | None) -> str:
    return "超时" if timeout_s is None else f"超时（{timeout_s:g} 秒）"

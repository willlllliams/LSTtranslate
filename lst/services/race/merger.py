"""竞速合并器，实现架构文档 6.2、6.3 节的合并规则与结束条件。

本模块是同步的状态机：不依赖事件循环，也不读取时钟，所有时刻都由调用方传入。
调度器负责派发任务和计时，合并器只回答三个问题：结果到达后显示什么，竞速是否结束，
某个仍在排队的尝试是否还可能改变显示。
"""

from collections.abc import Iterable
from dataclasses import dataclass

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, SegmentId, SessionId
from lst.contracts.translation import (
    AttemptState,
    AttemptView,
    Segment,
    SegmentOutcome,
    SegmentView,
    SkipReason,
)


@dataclass(slots=True)
class Attempt:
    """一个引擎实例在一个段上的尝试。"""

    engine_id: EngineId
    priority: int
    state: AttemptState
    dispatched_at: float
    finished_at: float | None = None
    text: str | None = None
    error: str | None = None
    cached: bool = False
    started: bool = False
    error_code: ErrorCode | None = None

    def view(self) -> AttemptView:
        latency = None if self.finished_at is None else (self.finished_at - self.dispatched_at) * 1000.0
        return AttemptView(
            engine_id=self.engine_id,
            state=self.state,
            text=self.text,
            error=self.error,
            latency_ms=latency,
            cached=self.cached,
            started=self.started,
            error_code=self.error_code,
        )


class SegmentRace:
    """一个段的竞速状态。

    记参赛实例集合为 E，当前胜出实例（当前显示的是它的译文）为 w，尚无成功结果时为 None。
    成功结果按“w 为空，或新结果优先级严格更高”替换 w；失败结果不改变 w。
    竞速在集合 H 为空时结束，H 是仍处于 QUEUED/RUNNING、且 w 为空或优先级严格高于 w 的实例。
    每个实例号在 E 中最多一个尝试，E 只在会话开始派发时确定，之后不再加入成员。
    """

    __slots__ = ("_attempts", "_shown", "segment", "skip_reason")

    def __init__(self, segment: Segment) -> None:
        self.segment = segment
        self._attempts: dict[EngineId, Attempt] = {}
        self._shown: EngineId | None = None
        self.skip_reason: SkipReason | None = None

    # ---- 查询 ----

    @property
    def shown_engine(self) -> EngineId | None:
        """当前显示的译文来自哪个实例（当前胜出者）；还没有成功结果时为 None。"""
        return self._shown

    @property
    def display_text(self) -> str:
        if self._shown is None:
            return self.segment.text
        text = self._attempts[self._shown].text
        assert text is not None
        return text

    @property
    def finished(self) -> bool:
        """显示已定：H 为空。没有任何参赛实例时也视为结束，界面显示原文。"""
        return not any(self._can_change_display(a) for a in self._attempts.values())

    def contending(self, engine_id: EngineId) -> bool:
        """该实例的尝试是否属于 H，即仍可能改变显示。

        引理：当前胜出者的优先级只升不降，所以一个尝试一旦不属于 H，之后再也不会属于 H。
        证明：w 只在新结果的优先级严格高于 w 时改变，因此 w 的优先级 p(w) 单调不增，而尝试 a 属于 H 的
        条件 p(a) < p(w) 一旦不成立就保持不成立；尝试离开 QUEUED/RUNNING 后也不会回到这两个状态。
        调度器据此取消不属于 H 的尝试（架构文档 6.4 节）。
        """
        return self._can_change_display(self._attempts[engine_id])

    def attempt(self, engine_id: EngineId) -> Attempt:
        return self._attempts[engine_id]

    def participants(self) -> tuple[EngineId, ...]:
        return tuple(self._attempts)

    def view(self) -> SegmentView:
        attempts = sorted(self._attempts.values(), key=self._view_order)
        return SegmentView(
            segment_id=self.segment.id,
            source_text=self.segment.text,
            display_text=self.display_text,
            shown_engine=self._shown,
            finished=self.finished,
            attempts=tuple(a.view() for a in attempts),
            skip_reason=self.skip_reason,
        )

    # ---- 状态变更 ----

    def add(self, engine_id: EngineId, priority: int, now: float) -> None:
        """让一个实例参赛，初始状态为 QUEUED。同一实例不能重复参赛。"""
        if engine_id in self._attempts:
            raise ValueError(f"实例 {engine_id} 已经参加段 {self.segment.id} 的竞速")
        self._attempts[engine_id] = Attempt(
            engine_id=engine_id, priority=priority, state=AttemptState.QUEUED, dispatched_at=now
        )

    def start(self, engine_id: EngineId) -> None:
        """实例开始处理这个段：QUEUED → RUNNING。"""
        attempt = self._attempts[engine_id]
        if attempt.state is not AttemptState.QUEUED:
            raise ValueError(
                f"实例 {engine_id} 在段 {self.segment.id} 上的状态是 {attempt.state.name}，不能开始"
            )
        attempt.state = AttemptState.RUNNING
        attempt.started = True

    def resolve(
        self, engine_id: EngineId, outcome: SegmentOutcome, now: float, *, cached: bool = False
    ) -> bool:
        """记录一个结果，返回显示的译文是否改变。

        结果可以来自 QUEUED（缓存命中时直接完成，cached=True）或 RUNNING 状态。已经结束的尝试再收到结果时
        忽略，返回 False：这只会在引擎违反“每段至多产出一次”的协议时发生。
        """
        if outcome.segment_id != self.segment.id:
            raise ValueError(f"结果属于段 {outcome.segment_id}，不属于段 {self.segment.id}")
        attempt = self._attempts[engine_id]
        if not attempt.state.pending:
            return False

        attempt.finished_at = now
        attempt.cached = cached
        if not outcome.ok:
            attempt.state = AttemptState.FAILED
            attempt.error = outcome.error
            attempt.error_code = outcome.code or ErrorCode.INTERNAL
            return False

        attempt.state = AttemptState.DONE
        attempt.text = outcome.text

        if self._shown is None or attempt.priority < self._attempts[self._shown].priority:
            self._shown = engine_id
            return True
        return False

    def cancel(self, engine_id: EngineId, now: float, *, running: bool = False) -> bool:
        """取消一个尝试，返回是否真的取消了。

        默认只取消仍在排队的尝试；running 为真时，正在执行的尝试也可以取消（调度器取消了执行它的任务）。
        已经结束的尝试不受影响。取消的尝试记为 CANCELLED，不是失败。
        """
        attempt = self._attempts[engine_id]
        if attempt.state is not AttemptState.QUEUED and not (
            running and attempt.state is AttemptState.RUNNING
        ):
            return False
        attempt.state = AttemptState.CANCELLED
        attempt.finished_at = now
        return True

    # ---- 内部 ----

    def _can_change_display(self, attempt: Attempt) -> bool:
        """attempt 是否属于集合 H。"""
        if not attempt.state.pending:
            return False
        if self._shown is None:
            return True
        return attempt.priority < self._attempts[self._shown].priority

    def _view_order(self, attempt: Attempt) -> tuple[int, int, str]:
        """界面列表的排序：胜出者第一，其余按优先级，再按实例名。"""
        return (0 if attempt.engine_id == self._shown else 1, attempt.priority, attempt.engine_id)


class SessionRace:
    """一个会话内所有段的竞速，并记录哪些段需要刷新界面（架构文档 6.6 节）。"""

    __slots__ = ("_dirty", "_races", "session_id")

    def __init__(self, session_id: SessionId, segments: Iterable[Segment]) -> None:
        self.session_id = session_id
        self._races: dict[SegmentId, SegmentRace] = {}
        for segment in segments:
            if segment.id in self._races:
                raise ValueError(f"会话 {session_id} 中段号 {segment.id} 重复")
            self._races[segment.id] = SegmentRace(segment)
        # 用 dict 保持插入顺序。构造时全部段记为待刷新，界面的第一帧因此包含会话里的每个段，
        # 包括没有参赛者的段（标记了跳过原因）。
        self._dirty: dict[SegmentId, None] = dict.fromkeys(self._races)

    def segment(self, segment_id: SegmentId) -> SegmentRace:
        return self._races[segment_id]

    def segments(self) -> tuple[SegmentRace, ...]:
        return tuple(self._races.values())

    @property
    def total(self) -> int:
        return len(self._races)

    @property
    def finished_count(self) -> int:
        return sum(1 for r in self._races.values() if r.finished)

    def add(self, segment_id: SegmentId, engine_id: EngineId, priority: int, now: float) -> None:
        self._races[segment_id].add(engine_id, priority, now)
        self._mark(segment_id)

    def start(self, segment_id: SegmentId, engine_id: EngineId) -> None:
        self._races[segment_id].start(engine_id)
        self._mark(segment_id)

    def skip(self, segment_id: SegmentId, reason: SkipReason) -> None:
        """标记一个段不参加竞速及其原因。只能在该段还没有参赛实例时调用。"""
        race = self._races[segment_id]
        if race.participants():
            raise ValueError(f"段 {segment_id} 已有参赛实例，不能标记为跳过")
        race.skip_reason = reason
        self._mark(segment_id)

    def resolve(
        self, engine_id: EngineId, outcome: SegmentOutcome, now: float, *, cached: bool = False
    ) -> bool:
        race = self._races[outcome.segment_id]
        before = race.attempt(engine_id).state
        changed = race.resolve(engine_id, outcome, now, cached=cached)
        if race.attempt(engine_id).state is not before:
            self._mark(outcome.segment_id)
        return changed

    def cancel(
        self, segment_id: SegmentId, engine_id: EngineId, now: float, *, running: bool = False
    ) -> bool:
        cancelled = self._races[segment_id].cancel(engine_id, now, running=running)
        if cancelled:
            self._mark(segment_id)
        return cancelled

    def take_dirty(self) -> tuple[SegmentView, ...]:
        """取出自上次调用以来有变化的段的快照，并清空待刷新集合。"""
        views = tuple(self._races[sid].view() for sid in self._dirty)
        self._dirty.clear()
        return views

    def _mark(self, segment_id: SegmentId) -> None:
        self._dirty[segment_id] = None

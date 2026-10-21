"""竞速合并器测试。

用例测试覆盖需求文档 5.5 节的时间线和各边界情况；
性质测试用 hypothesis 随机生成参赛组合与到达顺序，逐条验证架构文档 6.2、6.3 节证明的性质。
"""

from dataclasses import dataclass
from itertools import pairwise, permutations, product

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, SegmentId, SessionId
from lst.contracts.translation import AttemptState, Segment, SegmentOutcome, SkipReason
from lst.services.race.merger import SegmentRace, SessionRace

SEG = SegmentId("s1")
ARGOS = EngineId("argos")
DEEPL = EngineId("deepl")
DEEPSEEK = EngineId("deepseek")


def ok(text: str, segment_id: SegmentId = SEG) -> SegmentOutcome:
    return SegmentOutcome(segment_id=segment_id, text=text)


def fail(error: str = "boom", segment_id: SegmentId = SEG) -> SegmentOutcome:
    return SegmentOutcome(segment_id=segment_id, error=error)


def make_race(*participants: tuple[EngineId, int]) -> SegmentRace:
    race = SegmentRace(Segment(SEG, "原文"))
    for engine_id, priority in participants:
        race.add(engine_id, priority, now=0.0)
    return race


# ---------------------------------------------------------------- 用例测试


class TestRequirementTimeline:
    """需求文档 5.5 节的两条时间线。"""

    def test_display_upgrades_step_by_step(self) -> None:
        race = make_race((DEEPSEEK, 1), (DEEPL, 2), (ARGOS, 3))
        assert race.display_text == "原文"

        assert race.resolve(ARGOS, ok("argos 译文"), now=0.3)
        assert (race.winner, race.finished) == (ARGOS, False)

        assert race.resolve(DEEPL, ok("deepl 译文"), now=1.2)
        assert (race.winner, race.finished) == (DEEPL, False)

        assert race.resolve(DEEPSEEK, ok("deepseek 译文"), now=2.1)
        assert (race.winner, race.finished) == (DEEPSEEK, True)
        assert race.display_text == "deepseek 译文"

    def test_lower_priority_result_after_best_is_only_recorded(self) -> None:
        race = make_race((DEEPSEEK, 1), (DEEPL, 2), (ARGOS, 3))
        race.resolve(ARGOS, ok("argos 译文"), now=0.3)
        race.resolve(DEEPSEEK, ok("deepseek 译文"), now=0.8)
        assert race.finished

        assert not race.resolve(DEEPL, ok("deepl 译文"), now=1.2)
        assert race.winner == DEEPSEEK
        assert race.attempt(DEEPL).state is AttemptState.DONE
        assert race.attempt(DEEPL).text == "deepl 译文"


def test_failure_never_overrides_and_top_failure_finishes_race() -> None:
    race = make_race((DEEPSEEK, 1), (ARGOS, 3))
    race.resolve(ARGOS, ok("argos 译文"), now=0.3)
    assert not race.finished

    assert not race.resolve(DEEPSEEK, fail("401"), now=0.5)
    assert race.winner == ARGOS
    assert race.finished
    assert race.attempt(DEEPSEEK).error == "401"


def test_all_failed_shows_source_text() -> None:
    race = make_race((DEEPSEEK, 1), (ARGOS, 3))
    race.resolve(ARGOS, fail("模型缺失"), now=0.1)
    assert not race.finished
    race.resolve(DEEPSEEK, fail("超时"), now=0.2)
    assert race.finished
    assert race.winner is None
    assert race.display_text == "原文"


def test_equal_priority_keeps_first_arrival() -> None:
    a, b = EngineId("a"), EngineId("b")
    race = make_race((a, 2), (b, 2))
    assert race.resolve(b, ok("b"), now=0.1)
    assert not race.resolve(a, ok("a"), now=0.2)
    assert race.winner == b


def test_no_participants_is_finished_with_source_text() -> None:
    race = make_race()
    assert race.finished
    assert race.winner is None
    assert race.display_text == "原文"


def test_cache_hit_resolves_directly_from_queued() -> None:
    race = make_race((DEEPL, 2))
    assert race.resolve(DEEPL, ok("缓存译文"), now=0.0)
    view = race.view()
    assert view.attempts[0].latency_ms == 0.0
    assert view.finished


def test_duplicate_result_is_ignored() -> None:
    race = make_race((DEEPL, 2), (DEEPSEEK, 1))
    race.resolve(DEEPL, ok("第一次"), now=0.1)
    assert not race.resolve(DEEPL, ok("第二次"), now=0.2)
    assert race.attempt(DEEPL).text == "第一次"


def test_result_for_other_segment_is_rejected() -> None:
    race = make_race((DEEPL, 2))
    with pytest.raises(ValueError):
        race.resolve(DEEPL, ok("x", segment_id=SegmentId("other")), now=0.1)


def test_duplicate_participant_is_rejected() -> None:
    race = make_race((DEEPL, 2))
    with pytest.raises(ValueError):
        race.add(DEEPL, 2, now=0.0)


class TestCancel:
    def test_only_queued_attempts_can_be_cancelled(self) -> None:
        race = make_race((DEEPL, 2), (ARGOS, 3))
        race.start(ARGOS)
        assert race.cancel(DEEPL, now=0.1)
        assert not race.cancel(ARGOS, now=0.1)
        assert race.attempt(DEEPL).state is AttemptState.CANCELLED
        assert race.attempt(ARGOS).state is AttemptState.RUNNING

    def test_cancelled_attempt_cannot_start(self) -> None:
        race = make_race((DEEPL, 2))
        race.cancel(DEEPL, now=0.1)
        with pytest.raises(ValueError):
            race.start(DEEPL)

    def test_nothing_contends_after_best_result(self) -> None:
        race = make_race((DEEPSEEK, 1), (DEEPL, 2), (ARGOS, 3))
        race.start(DEEPL)
        assert race.contending(DEEPL) and race.contending(ARGOS)

        race.resolve(DEEPSEEK, ok("deepseek 译文"), now=0.5)
        assert race.finished
        # DEEPL 已在执行，只能等它跑完；ARGOS 仍在排队，出队时会被剪枝。两者都不再竞争。
        assert not race.contending(DEEPL)
        assert not race.contending(ARGOS)

    def test_cancelling_a_higher_priority_attempt_can_finish_race(self) -> None:
        race = make_race((DEEPSEEK, 1), (ARGOS, 3))
        race.resolve(ARGOS, ok("argos 译文"), now=0.3)
        assert not race.finished
        race.cancel(DEEPSEEK, now=0.4)
        assert race.finished


class TestLateJoin:
    """双语窗口 Ctrl+Enter 时计费引擎加入已经开始的会话（需求 BI-04）。"""

    def test_higher_priority_join_reopens_race(self) -> None:
        race = make_race((ARGOS, 3))
        race.resolve(ARGOS, ok("argos 译文"), now=0.3)
        assert race.finished

        race.add(DEEPSEEK, 1, now=1.0)
        assert not race.finished
        race.resolve(DEEPSEEK, ok("deepseek 译文"), now=2.0)
        assert (race.winner, race.finished) == (DEEPSEEK, True)

    def test_lower_priority_join_keeps_race_finished(self) -> None:
        race = make_race((DEEPL, 2))
        race.resolve(DEEPL, ok("deepl 译文"), now=0.3)
        race.add(ARGOS, 3, now=1.0)
        assert race.finished
        assert not race.contending(ARGOS)


def test_view_orders_winner_first_and_reports_latency() -> None:
    race = make_race((DEEPSEEK, 1), (DEEPL, 2), (ARGOS, 3))
    race.resolve(DEEPL, ok("deepl 译文"), now=1.2)
    view = race.view()
    assert [a.engine_id for a in view.attempts] == [DEEPL, DEEPSEEK, ARGOS]
    assert view.attempts[0].latency_ms == pytest.approx(1200.0)
    assert view.attempts[1].latency_ms is None
    assert (view.display_text, view.winner, view.finished) == ("deepl 译文", DEEPL, False)


class TestSessionRace:
    def make_session(self) -> SessionRace:
        segments = [Segment(SegmentId("s1"), "一"), Segment(SegmentId("s2"), "二")]
        session = SessionRace(SessionId("sess"), segments)
        for seg in segments:
            session.add(seg.id, ARGOS, 3, now=0.0)
            session.add(seg.id, DEEPSEEK, 1, now=0.0)
        session.take_dirty()
        return session

    def test_duplicate_segment_ids_rejected(self) -> None:
        with pytest.raises(ValueError):
            SessionRace(SessionId("x"), [Segment(SEG, "a"), Segment(SEG, "b")])

    def test_dirty_tracks_changed_segments_once(self) -> None:
        session = self.make_session()
        session.resolve(ARGOS, ok("一译", SegmentId("s1")), now=0.1)
        session.resolve(DEEPSEEK, ok("一译+", SegmentId("s1")), now=0.2)
        views = session.take_dirty()
        assert [v.segment_id for v in views] == [SegmentId("s1")]
        assert views[0].display_text == "一译+"
        assert session.take_dirty() == ()

    def test_state_change_without_display_change_is_dirty(self) -> None:
        session = self.make_session()
        session.start(SegmentId("s2"), DEEPSEEK)
        assert [v.segment_id for v in session.take_dirty()] == [SegmentId("s2")]

    def test_ignored_duplicate_is_not_dirty(self) -> None:
        session = self.make_session()
        session.resolve(ARGOS, ok("一译", SegmentId("s1")), now=0.1)
        session.take_dirty()
        session.resolve(ARGOS, ok("又一次", SegmentId("s1")), now=0.2)
        assert session.take_dirty() == ()

    def test_progress(self) -> None:
        session = self.make_session()
        assert (session.finished_count, session.total) == (0, 2)
        session.resolve(DEEPSEEK, ok("一译", SegmentId("s1")), now=0.2)
        assert session.finished_count == 1


# ---------------------------------------------------------------- 性质测试


@dataclass(frozen=True)
class Step:
    """随机场景里的一步：实例 index 产出结果，success 决定成败；cancel=True 表示改为取消它。"""

    index: int
    success: bool
    cancel: bool


@st.composite
def scenarios(draw: st.DrawFn) -> tuple[list[int], list[Step]]:
    priorities = draw(st.lists(st.integers(min_value=1, max_value=4), min_size=1, max_size=6))
    order = draw(st.permutations(range(len(priorities))))
    steps = [Step(i, draw(st.booleans()), draw(st.integers(0, 4)) == 0) for i in order]
    return priorities, steps


def engine(i: int) -> EngineId:
    return EngineId(f"e{i}")


def reference_h_empty(race: SegmentRace) -> bool:
    """独立按定义计算 H 是否为空。"""
    w = race.winner
    for e in race.participants():
        a = race.attempt(e)
        if a.state in (AttemptState.QUEUED, AttemptState.RUNNING) and (
            w is None or a.priority < race.attempt(w).priority
        ):
            return False
    return True


@given(scenarios())
def test_properties_hold_for_any_arrival_order(scenario: tuple[list[int], list[Step]]) -> None:
    priorities, steps = scenario
    race = make_race(*((engine(i), p) for i, p in enumerate(priorities)))

    changes = 0
    shown: list[int] = []  # 每次显示变化后的胜出者优先级
    finished_winner: EngineId | None = None
    finished_seen = False

    for t, step in enumerate(steps, start=1):
        e = engine(step.index)
        if step.cancel:
            race.cancel(e, now=float(t))
        else:
            race.start(e)
            if race.resolve(e, ok(f"t{step.index}") if step.success else fail(), now=float(t)):
                changes += 1
                w = race.winner
                assert w is not None
                shown.append(race.attempt(w).priority)

        # 结束条件与按定义计算的结果一致。
        assert race.finished == reference_h_empty(race)
        # 参赛集合不变时，结束后胜出者不再变化。
        if finished_seen:
            assert race.winner == finished_winner
        if race.finished and not finished_seen:
            finished_seen, finished_winner = True, race.winner
        # contending 与 H 的定义一致；竞速结束时没有任何尝试仍在竞争。
        w = race.winner
        for x in race.participants():
            a = race.attempt(x)
            in_h = a.state.pending and (w is None or a.priority < race.attempt(w).priority)
            assert race.contending(x) == in_h
        if race.finished:
            assert not any(race.contending(x) for x in race.participants())

    # 性质 1：显示优先级严格递减，变化次数不超过参赛实例数。
    assert all(a > b for a, b in pairwise(shown))
    assert changes <= len(priorities)

    # 所有步骤执行完，每个实例都已结束，竞速必然结束。
    assert race.finished

    # 性质 2：最终胜出者是成功实例中优先级最高者，并列时取最先到达者。
    successes = [
        (race.attempt(engine(s.index)).priority, t, engine(s.index))
        for t, s in enumerate(steps)
        if not s.cancel and s.success
    ]
    expected_winner = min(successes)[2] if successes else None
    assert race.winner == expected_winner


# ---------------------------------------------------------------- 新增接口


class TestContendingAndComplete:
    def test_contending_follows_definition_of_h(self) -> None:
        race = make_race((DEEPSEEK, 1), (DEEPL, 2), (ARGOS, 3))
        assert all(race.contending(e) for e in (DEEPSEEK, DEEPL, ARGOS))

        race.resolve(DEEPL, ok("deepl 译文"), now=0.5)
        assert race.contending(DEEPSEEK)
        assert not race.contending(DEEPL)  # 已结束
        assert not race.contending(ARGOS)  # 优先级低于胜出者，可剪枝

    def test_complete_requires_every_attempt_finished(self) -> None:
        race = make_race((DEEPSEEK, 1), (ARGOS, 3))
        race.resolve(DEEPSEEK, ok("deepseek 译文"), now=0.5)
        assert race.finished
        assert not race.complete
        race.resolve(ARGOS, ok("argos 译文"), now=0.6)
        assert race.complete
        assert race.view().complete

    def test_cached_flag_reaches_view(self) -> None:
        race = make_race((DEEPL, 2), (ARGOS, 3))
        race.resolve(DEEPL, ok("缓存译文"), now=0.0, cached=True)
        race.resolve(ARGOS, ok("argos 译文"), now=0.3)
        views = {a.engine_id: a for a in race.view().attempts}
        assert views[DEEPL].cached
        assert not views[ARGOS].cached


class TestSkip:
    def test_skip_reason_reaches_view_and_marks_dirty(self) -> None:
        session = SessionRace(SessionId("sess"), [Segment(SEG, "same")])
        session.skip(SEG, SkipReason.SAME_LANGUAGE)
        (view,) = session.take_dirty()
        assert view.skip_reason is SkipReason.SAME_LANGUAGE
        assert view.finished and view.complete
        assert view.display_text == "same"

    def test_skip_rejected_when_participants_exist(self) -> None:
        session = SessionRace(SessionId("sess"), [Segment(SEG, "x")])
        session.add(SEG, DEEPL, 2, now=0.0)
        with pytest.raises(ValueError):
            session.skip(SEG, SkipReason.NO_ENGINE)


# ---------------------------------------------------------------- 穷举测试


def _final_winner(
    order: tuple[int, ...], outcomes: tuple[bool, ...], *, saver: bool
) -> tuple[EngineId | None, list[EngineId | None]]:
    """按给定到达顺序与成败执行一次竞速，返回最终胜出者与每步之后的胜出者。

    saver=True 时模拟节省模式的出队剪枝：轮到某个实例执行时，若它已不在 H 中就取消它。
    参赛实例 i 的优先级为 i + 1，满足前提 1（优先级互异）。
    """
    race = make_race(*((engine(i), i + 1) for i in range(len(order))))
    history: list[EngineId | None] = []
    for t, i in enumerate(order, start=1):
        e = engine(i)
        if saver and not race.contending(e):
            assert race.cancel(e, now=float(t))
        else:
            race.start(e)
            race.resolve(e, ok(f"t{i}") if outcomes[i] else fail(), now=float(t))
        history.append(race.winner)
    return race.winner, history


@pytest.mark.parametrize("n", [1, 2, 3, 4])
def test_exhaustive_orders_and_outcomes(n: int) -> None:
    """参赛实例不超过 4 个时，遍历所有到达顺序与成败组合。

    逐一检查：
    - 性质 2：完整模式的终态是成功实例中优先级最高者，与到达顺序无关；
    - 引理 2：节省模式的终态与完整模式相同；
    - 定理 1：显示已定之后胜出者不再变化。
    """
    for order in permutations(range(n)):
        for outcomes in product((True, False), repeat=n):
            successes = [i for i in range(n) if outcomes[i]]
            expected = engine(min(successes)) if successes else None

            full, _ = _final_winner(order, outcomes, saver=False)
            saver, _ = _final_winner(order, outcomes, saver=True)
            assert full == expected, (order, outcomes)
            assert saver == expected, (order, outcomes)

            # 定理 1：重放一遍，找到第一次显示已定的时刻，之后胜出者不变。
            race = make_race(*((engine(i), i + 1) for i in range(n)))
            settled: EngineId | None = None
            settled_seen = False
            for t, i in enumerate(order, start=1):
                race.start(engine(i))
                race.resolve(engine(i), ok("x") if outcomes[i] else fail(), now=float(t))
                if settled_seen:
                    assert race.winner == settled, (order, outcomes)
                elif race.finished:
                    settled_seen, settled = True, race.winner
            assert settled_seen


def test_started_flag_marks_attempts_that_actually_ran() -> None:
    race = make_race((DEEPSEEK, 1), (DEEPL, 2), (ARGOS, 3))
    race.start(DEEPSEEK)
    race.resolve(DEEPSEEK, ok("deepseek 译文"), now=0.5)
    race.resolve(DEEPL, ok("缓存译文"), now=0.0, cached=True)
    race.cancel(ARGOS, now=0.6)
    views = {a.engine_id: a for a in race.view().attempts}
    assert views[DEEPSEEK].started
    assert not views[DEEPL].started  # 缓存命中，没有执行
    assert not views[ARGOS].started  # 执行前被取消


def test_error_code_reaches_view_and_defaults_to_internal() -> None:
    race = make_race((DEEPSEEK, 1), (ARGOS, 3))
    race.resolve(DEEPSEEK, SegmentOutcome(SEG, error="HTTP 401", code=ErrorCode.AUTH), now=0.5)
    race.resolve(ARGOS, fail("没有错误码"), now=0.6)
    views = {a.engine_id: a for a in race.view().attempts}
    assert views[DEEPSEEK].error_code is ErrorCode.AUTH
    assert views[ARGOS].error_code is ErrorCode.INTERNAL


def test_successful_outcome_rejects_error_code() -> None:
    with pytest.raises(ValueError):
        SegmentOutcome(SEG, text="译文", code=ErrorCode.AUTH)

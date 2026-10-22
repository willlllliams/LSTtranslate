"""竞速统计模块的端到端测试：真实事件总线 → StatsModule → SQLite 统计库。"""

import asyncio
import logging
import sqlite3
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lst.contracts.errors import ErrorCode
from lst.contracts.events import AttemptRecorded, SegmentsUpdated
from lst.contracts.ids import EngineId, SegmentId, SessionId
from lst.contracts.translation import AttemptState, AttemptView, SegmentView
from lst.kernel import EventBus, Module, ModuleContext
from lst.services.stats import EngineStats, SqliteStatsStore, StatsModule

DEEPSEEK = EngineId("deepseek")  # 优先级 1
ARGOS = EngineId("argos")  # 优先级 2
SESSION = SessionId("session-1")
S1, S2 = SegmentId("s1"), SegmentId("s2")
MODULE_LOGGER = "lst.stats"


def view(
    segment_id: SegmentId, shown_engine: EngineId | None, attempts: tuple[AttemptView, ...], *, finished: bool
) -> SegmentView:
    return SegmentView(
        segment_id=segment_id,
        source_text="原文",
        display_text="译文" if shown_engine else "原文",
        shown_engine=shown_engine,
        finished=finished,
        attempts=attempts,
    )


def done(engine_id: EngineId, latency_ms: float, *, cached: bool = False) -> AttemptView:
    return AttemptView(
        engine_id, AttemptState.DONE, "译文", None, latency_ms, cached=cached, started=not cached
    )


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), raise_errors=True)
    yield instance
    await instance.aclose()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "stats.sqlite3"


async def start_module(bus: EventBus, path: Path) -> tuple[StatsModule, ModuleContext]:
    module = StatsModule(SqliteStatsStore(path))
    ctx = ModuleContext(bus, logging.getLogger(MODULE_LOGGER))
    await module.start(ctx)
    return module, ctx


async def summary_of(path: Path) -> list[EngineStats]:
    store = SqliteStatsStore(path)
    await store.open()
    try:
        return await store.summary()
    finally:
        await store.close()


async def test_end_to_end_with_event_bus(
    bus: EventBus, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """一个两段的会话：

    - s1：Argos 先返回（120 ms），DeepSeek 后返回（300 ms）并替换显示，胜出者是 DeepSeek。
    - s2：DeepSeek 执行后超时；Argos 命中缓存并胜出，缓存命中的胜出不计入。
    """
    module = StatsModule(SqliteStatsStore(db_path))
    static_check: Module = module  # 静态检查：满足 Module 协议
    ctx = ModuleContext(bus, logging.getLogger(f"lst.{static_check.name}"))
    await module.start(ctx)

    argos_s1 = done(ARGOS, 120.0)
    deepseek_s1 = done(DEEPSEEK, 300.0)
    deepseek_s2 = AttemptView(
        DEEPSEEK, AttemptState.FAILED, None, "超时", 900.0, started=True, error_code=ErrorCode.TIMEOUT
    )
    argos_s2 = done(ARGOS, 0.0, cached=True)

    with caplog.at_level(logging.WARNING):
        bus.publish(AttemptRecorded(SESSION, S2, argos_s2))
        bus.publish(AttemptRecorded(SESSION, S1, argos_s1))
        # 第一次刷新：s1 暂由 Argos 显示但未定，s2 还在等 DeepSeek；未定的段不记录。
        bus.publish(
            SegmentsUpdated(
                SESSION,
                (
                    view(S1, ARGOS, (argos_s1,), finished=False),
                    view(S2, ARGOS, (argos_s2,), finished=False),
                ),
            )
        )
        bus.publish(AttemptRecorded(SESSION, S1, deepseek_s1))
        bus.publish(AttemptRecorded(SESSION, S2, deepseek_s2))
        bus.publish(
            SegmentsUpdated(
                SESSION,
                (
                    view(S1, DEEPSEEK, (deepseek_s1, argos_s1), finished=True),
                    view(S2, ARGOS, (deepseek_s2, argos_s2), finished=True),
                ),
            )
        )
        await bus.drain()

        expected = [
            EngineStats(ARGOS, executed=1, wins=0, p50_ms=120.0, p95_ms=120.0, failure_rate=0.0),
            EngineStats(DEEPSEEK, executed=2, wins=1, p50_ms=300.0, p95_ms=300.0, failure_rate=0.5),
        ]
        assert await module._store.summary() == expected

        await module.stop()
        ctx.close()
    assert caplog.records == []

    # 数据在库文件里：重新打开仍能读到。
    assert await summary_of(db_path) == expected


async def test_events_between_stop_and_close_are_dropped(
    bus: EventBus, db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    module, ctx = await start_module(bus, db_path)
    await module.stop()
    # 组合根先 stop 再取消订阅，中间到达的事件不应报错。
    with caplog.at_level(logging.DEBUG, logger=MODULE_LOGGER):
        bus.publish(AttemptRecorded(SESSION, S1, done(ARGOS, 10.0)))
        bus.publish(SegmentsUpdated(SESSION, (view(S1, ARGOS, (done(ARGOS, 10.0),), finished=True),)))
        await bus.drain()
    ctx.close()
    assert caplog.records == []
    assert await summary_of(db_path) == []


async def test_store_failure_is_only_logged(
    bus: EventBus, db_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, ctx = await start_module(bus, db_path)
    store = module._store

    async def broken(*_args: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "record_attempt", broken)
    with caplog.at_level(logging.ERROR, logger=MODULE_LOGGER):
        bus.publish(AttemptRecorded(SESSION, S1, done(ARGOS, 10.0)))
        # 总线开了 raise_errors，处理函数抛出的异常会让 drain 抛出 ExceptionGroup。
        # drain 正常返回，说明异常在模块内部被记录后吞下。
        await bus.drain()
    errors = [r for r in caplog.records if r.name == MODULE_LOGGER and r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert errors[0].exc_info is not None

    # 写入失败不影响之后的事件。
    monkeypatch.undo()
    a = done(ARGOS, 10.0)
    bus.publish(AttemptRecorded(SESSION, S2, a))
    bus.publish(SegmentsUpdated(SESSION, (view(S2, ARGOS, (a,), finished=True),)))
    await bus.drain()
    rows = await store.summary()
    assert [(r.engine_id, r.executed, r.wins) for r in rows] == [(ARGOS, 1, 1)]

    await module.stop()
    ctx.close()


async def test_stop_closes_store(bus: EventBus, db_path: Path) -> None:
    module, ctx = await start_module(bus, db_path)
    await module.stop()
    ctx.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        await module._store.summary()

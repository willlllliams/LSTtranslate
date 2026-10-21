"""统计快照的测试：StatsModule 发布 StatsSnapshot、处理 ResetStats（需求 RACE-07）。

真实的事件总线与真实的 SQLite 统计库；快照间隔缩短到几十毫秒，测试用 wait_for 等快照出现。
"""

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from lst.contracts.events import AttemptRecorded, EngineStatsRow, ResetStats, SegmentsUpdated, StatsSnapshot
from lst.contracts.ids import EngineId, SegmentId, SessionId
from lst.contracts.translation import AttemptState, AttemptView, SegmentView
from lst.kernel import EventBus, ModuleContext
from lst.services.stats import SqliteStatsStore, StatsModule

ARGOS = EngineId("argos")
DEEPSEEK = EngineId("deepseek")
SESSION = SessionId("session-1")
S1 = SegmentId("s1")
INTERVAL = 0.03


def done(engine_id: EngineId, latency_ms: float) -> AttemptView:
    return AttemptView(engine_id, AttemptState.DONE, "译文", None, latency_ms, started=True)


def finished_view(winner: EngineId, *attempts: AttemptView) -> SegmentView:
    return SegmentView(S1, "原文", "译文", winner, True, attempts)


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), raise_errors=True)
    yield instance
    await instance.aclose()


class Snapshots:
    """收集总线上的 StatsSnapshot。"""

    def __init__(self, bus: EventBus) -> None:
        self.items: list[StatsSnapshot] = []
        bus.subscribe(StatsSnapshot, self.items.append)

    async def wait_for(
        self, predicate: Callable[[StatsSnapshot], bool], timeout: float = 5.0
    ) -> StatsSnapshot:
        async def poll() -> StatsSnapshot:
            while True:
                if self.items and predicate(self.items[-1]):
                    return self.items[-1]
                await asyncio.sleep(0.005)

        return await asyncio.wait_for(poll(), timeout)


@pytest.fixture
async def snapshots(bus: EventBus) -> Snapshots:
    # 总线只允许在事件循环线程订阅，所以这个夹具是协程。
    return Snapshots(bus)


async def start(
    bus: EventBus, tmp_path: Path, interval: float = INTERVAL
) -> tuple[StatsModule, ModuleContext]:
    module = StatsModule(SqliteStatsStore(tmp_path / "stats.sqlite3"), snapshot_interval_s=interval)
    ctx = ModuleContext(bus, logging.getLogger("lst.stats"))
    await module.start(ctx)
    return module, ctx


async def test_start_publishes_empty_snapshot(bus: EventBus, snapshots: Snapshots, tmp_path: Path) -> None:
    module, ctx = await start(bus, tmp_path)
    await bus.drain()
    assert snapshots.items == [StatsSnapshot(())]
    # State 有最新值回放：晚订阅的界面马上拿到当前统计。
    assert bus.latest(StatsSnapshot) == StatsSnapshot(())
    await module.stop()
    ctx.close()


async def test_records_become_a_snapshot(bus: EventBus, snapshots: Snapshots, tmp_path: Path) -> None:
    module, ctx = await start(bus, tmp_path)
    argos, deepseek = done(ARGOS, 120.0), done(DEEPSEEK, 300.0)
    failed = AttemptView(DEEPSEEK, AttemptState.FAILED, None, "超时", 900.0, started=True)
    bus.publish(AttemptRecorded(SESSION, S1, argos))
    bus.publish(AttemptRecorded(SESSION, S1, deepseek))
    bus.publish(AttemptRecorded(SESSION, SegmentId("s2"), failed))
    bus.publish(SegmentsUpdated(SESSION, (finished_view(DEEPSEEK, deepseek, argos),)))

    snapshot = await snapshots.wait_for(lambda s: len(s.engines) == 2 and s.engines[1].wins == 1)
    assert snapshot.engines == (
        EngineStatsRow(ARGOS, executed=1, wins=0, p50_ms=120.0, p95_ms=120.0, failure_rate=0.0),
        EngineStatsRow(DEEPSEEK, executed=2, wins=1, p50_ms=300.0, p95_ms=300.0, failure_rate=0.5),
    )
    # 行里只有数字，没有任何文本字段（需求 PRV-02）。
    for row in snapshot.engines:
        assert {f.name for f in dataclasses.fields(row)} == {
            "engine_id",
            "executed",
            "wins",
            "p50_ms",
            "p95_ms",
            "failure_rate",
        }
    await module.stop()
    ctx.close()


async def test_changes_inside_the_interval_share_one_snapshot(
    bus: EventBus, snapshots: Snapshots, tmp_path: Path
) -> None:
    module, ctx = await start(bus, tmp_path)
    await bus.drain()
    before = len(snapshots.items)
    for index in range(20):
        bus.publish(AttemptRecorded(SESSION, SegmentId(f"s{index}"), done(ARGOS, 10.0 + index)))
    await snapshots.wait_for(lambda s: bool(s.engines) and s.engines[0].executed == 20)
    # 20 次写入合并成很少几次发布（计时器在第一次写入完成后启动，之后的写入并入同一次）。
    assert len(snapshots.items) - before <= 3
    await module.stop()
    ctx.close()


async def test_reset_clears_statistics_and_publishes_immediately(
    bus: EventBus, snapshots: Snapshots, tmp_path: Path
) -> None:
    module, ctx = await start(bus, tmp_path)
    bus.publish(AttemptRecorded(SESSION, S1, done(ARGOS, 120.0)))
    await snapshots.wait_for(lambda s: len(s.engines) == 1)

    bus.send(ResetStats())
    await snapshots.wait_for(lambda s: s.engines == ())
    assert await module._store.summary() == []
    await module.stop()
    ctx.close()


async def test_reset_cancels_a_pending_snapshot(bus: EventBus, snapshots: Snapshots, tmp_path: Path) -> None:
    """重置时若还有一次没发布的快照，它不能在重置之后把旧数据发出去。"""
    module, ctx = await start(bus, tmp_path, interval=0.2)
    bus.publish(AttemptRecorded(SESSION, S1, done(ARGOS, 120.0)))
    await bus.drain()
    assert module._timer is not None
    bus.send(ResetStats())
    await bus.drain()
    await asyncio.sleep(0.4)
    assert all(s.engines == () for s in snapshots.items)
    await module.stop()
    ctx.close()


async def test_stop_cancels_pending_snapshot(bus: EventBus, snapshots: Snapshots, tmp_path: Path) -> None:
    module, ctx = await start(bus, tmp_path, interval=0.1)
    bus.publish(AttemptRecorded(SESSION, S1, done(ARGOS, 120.0)))
    await bus.drain()
    count = len(snapshots.items)
    await module.stop()
    await asyncio.sleep(0.25)
    assert len(snapshots.items) == count
    ctx.close()


async def test_negative_interval_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="间隔"):
        StatsModule(SqliteStatsStore(tmp_path / "stats.sqlite3"), snapshot_interval_s=-1)

"""竞速统计模块：把总线上的竞速事件写入统计库，并发布统计快照（需求 RACE-07，架构文档 4.3、5.3 节）。"""

import asyncio
import logging
from typing import Final

from lst.contracts.events import (
    AttemptRecorded,
    EngineStatsRow,
    ResetStats,
    SegmentsUpdated,
    StatsSnapshot,
)
from lst.kernel import EventBus, ModuleContext
from lst.services.stats.store import SqliteStatsStore

DEFAULT_SNAPSHOT_INTERVAL_S: Final = 1.0
"""统计有变化后，隔多久发布一次快照。竞速进行中尝试结束得很密集，快照每次都要查一遍统计库，所以合并发布。"""


class StatsModule:
    """订阅 ``AttemptRecorded`` 与 ``SegmentsUpdated`` 写入统计库，并发布 ``StatsSnapshot``。

    ``start`` 打开统计库并发布一次快照，``stop`` 关闭它。写入失败只记日志，不影响竞速和其他模块。
    组合根先调用 ``stop`` 再取消订阅，这段时间里到达的事件直接丢弃。

    快照：每次写入统计库之后，安排一次延后 ``snapshot_interval_s`` 秒的发布，期间的其他写入合并进同一次，
    所以界面上的数字最多落后这么久。``ResetStats`` 命令清空统计库，并立即发布空快照。
    """

    name = "stats"

    def __init__(
        self, store: SqliteStatsStore, *, snapshot_interval_s: float = DEFAULT_SNAPSHOT_INTERVAL_S
    ) -> None:
        if snapshot_interval_s < 0:
            raise ValueError(f"快照间隔不能为负数，收到 {snapshot_interval_s}")
        self._store = store
        self._interval = snapshot_interval_s
        self._logger = logging.getLogger(__name__)
        self._bus: EventBus | None = None
        self._running = False
        self._timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    async def start(self, ctx: ModuleContext) -> None:
        self._logger = ctx.logger
        self._bus = ctx.bus
        await self._store.open()
        self._running = True
        ctx.subscribe(AttemptRecorded, self._on_attempt)
        ctx.subscribe(SegmentsUpdated, self._on_segments)
        ctx.subscribe(ResetStats, self._on_reset)
        await self._publish_snapshot()

    async def stop(self) -> None:
        self._running = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._store.close()

    # ------------------------------------------------------------------ 写入

    async def _on_attempt(self, message: AttemptRecorded) -> None:
        if not self._running:
            return
        try:
            await self._store.record_attempt(message.session_id, message.segment_id, message.attempt)
        except Exception:
            self._logger.exception("写入竞速统计失败（实例 %s）", message.attempt.engine_id)
            return
        self._schedule_snapshot()

    async def _on_segments(self, message: SegmentsUpdated) -> None:
        if not self._running:
            return
        # 只有显示已定的段写入统计库，由 record_segments 筛选；没有这样的段时它不访问数据库线程。
        try:
            written = await self._store.record_segments(message.session_id, message.views)
        except Exception:
            self._logger.exception("写入竞速统计失败（会话 %s 的胜出记录）", message.session_id)
            return
        if written:
            self._schedule_snapshot()

    # ------------------------------------------------------------------ 快照

    async def _on_reset(self, _command: ResetStats) -> None:
        if not self._running:
            return
        try:
            await self._store.reset()
        except Exception:
            self._logger.exception("重置竞速统计失败")
            return
        self._logger.info("竞速统计已重置")
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        await self._publish_snapshot()

    def _schedule_snapshot(self) -> None:
        """安排一次延后的快照发布；已经安排过时，这次变化并入那一次。"""
        if not self._running or self._timer is not None:
            return
        self._timer = asyncio.get_running_loop().call_later(self._interval, self._fire_snapshot)

    def _fire_snapshot(self) -> None:
        self._timer = None
        if not self._running:
            return
        task = asyncio.get_running_loop().create_task(self._publish_snapshot())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _publish_snapshot(self) -> None:
        bus = self._bus
        if bus is None:
            return
        try:
            stats = await self._store.summary()
        except Exception:
            if self._running:
                self._logger.exception("读取竞速统计失败，没有发布统计快照")
            return
        rows = tuple(
            EngineStatsRow(
                engine_id=item.engine_id,
                executed=item.executed,
                wins=item.wins,
                p50_ms=item.p50_ms,
                p95_ms=item.p95_ms,
                failure_rate=item.failure_rate,
            )
            for item in stats
        )
        if self._running:
            bus.publish(StatsSnapshot(rows))

"""竞速统计的 SQLite 存储（需求 RACE-07、PRV-02，架构文档 2.9.3、10.2 节）。

库里只有实例号、状态、耗时、标记、错误码、时间戳和段的字符数，不保存任何原文、译文或错误详情。

统计口径：

- 执行过的尝试（executed）：started 为真、cached 为假、以 DONE 或 FAILED 结束，并且不是因实例被移除而
  结束的（错误码 race.engine_removed）。命中缓存的尝试和被取消的尝试都不计入：调度器在更高优先级的实例
  胜出后取消低优先级实例排队中和执行中的尝试（RACE-05），这些尝试没有给出结果，计入会压低它们的胜出率，
  把执行中被取消的记成失败还会抬高失败率。实例被移除是用户改了配置，与引擎本身的可靠性无关，
  计入会抬高失败率。
- 胜出次数：以该实例为最终胜出者的段数，并且胜出的那个尝试本身是执行过的；以缓存结果胜出的段不计。
- 首个结果耗时 P50、P95：每个实例最近 window 个执行过且成功（DONE）的尝试的 latency_ms，
  按 ``percentile`` 的线性插值计算。
- 失败率：执行过的尝试中 FAILED 所占比例。

库的版本历史：版本 2 把 segments 表的 winner、winner_executed 两列改名为 shown_engine、shown_executed，
与契约里 SegmentView.shown_engine 的叫法一致；版本 1 的库打开时就地改名，数据不变。
"""

import logging
import math
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, SegmentId, SessionId
from lst.contracts.translation import AttemptState, AttemptView, SegmentView
from lst.services._shared.sqlite import SqliteThread, ensure_schema, transaction

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 2
# attempts 以 (会话, 段, 实例) 唯一：同一尝试重复记录时以最后一次为准，id 随之更新为最新。
# segments 以 (会话, 段) 唯一：同一段重复记录时以最后一次为准。
_SCHEMA = """
CREATE TABLE attempts (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    engine_id TEXT NOT NULL,
    state TEXT NOT NULL,
    latency_ms REAL,
    cached INTEGER NOT NULL,
    started INTEGER NOT NULL,
    error_code TEXT,
    recorded_at REAL NOT NULL,
    UNIQUE (session_id, segment_id, engine_id)
);
CREATE INDEX attempts_by_engine ON attempts (engine_id, id);
CREATE TABLE segments (
    session_id TEXT NOT NULL,
    segment_id TEXT NOT NULL,
    shown_engine TEXT,
    shown_executed INTEGER NOT NULL,
    chars INTEGER NOT NULL,
    recorded_at REAL NOT NULL,
    PRIMARY KEY (session_id, segment_id)
) WITHOUT ROWID;
"""

_MIGRATIONS: dict[int, str] = {
    1: """
ALTER TABLE segments RENAME COLUMN winner TO shown_engine;
ALTER TABLE segments RENAME COLUMN winner_executed TO shown_executed;
""",
}
"""旧版本号 → 升级到下一版本的 SQL（见模块说明的版本历史）。"""

_TERMINAL = (AttemptState.DONE.name, AttemptState.FAILED.name)
_EXECUTED = (
    f"started = 1 AND cached = 0 AND state IN ({', '.join(repr(s) for s in _TERMINAL)})"
    f" AND error_code IS NOT '{ErrorCode.ENGINE_REMOVED.value}'"
)


@dataclass(frozen=True, slots=True)
class EngineStats:
    """一个引擎实例的累计统计。没有样本时 p50_ms、p95_ms 为 None，executed 为 0 时 failure_rate 为 None。"""

    engine_id: EngineId
    executed: int
    wins: int
    p50_ms: float | None
    p95_ms: float | None
    failure_rate: float | None


def percentile(values: Iterable[float], q: float) -> float:
    """线性插值的分位数，q 取 [0, 1]。

    把样本升序排成 x_0 ≤ x_1 ≤ … ≤ x_{n-1}，令 h = (n - 1)q、k = ⌊h⌋，结果为
    x_k + (h - k)(x_{k+1} - x_k)；k = n - 1 时结果就是 x_{n-1}。这是 Hyndman–Fan 第 7 类定义，
    与 numpy 的默认方法和 ``statistics.quantiles(method="inclusive")`` 一致。
    选它而不选最近秩法，是因为 q = 0.5 时它给出通常意义的中位数：样本数为偶数时取中间两个值的平均。
    """
    xs = sorted(values)
    if not xs:
        raise ValueError("percentile 需要至少一个样本")
    if not 0.0 <= q <= 1.0:
        raise ValueError(f"分位点必须在 [0, 1] 内，收到 {q}")
    h = (len(xs) - 1) * q
    k = math.floor(h)
    if k >= len(xs) - 1:
        return xs[-1]
    return xs[k] + (h - k) * (xs[k + 1] - xs[k])


def _shown_executed(view: SegmentView) -> bool:
    """胜出（当前显示）的尝试是否执行过：started 为真且不是缓存命中。"""
    if view.shown_engine is None:
        return False
    for attempt in view.attempts:
        if attempt.engine_id == view.shown_engine:
            return attempt.started and not attempt.cached
    return False


class SqliteStatsStore:
    """竞速统计存储。所有数据库操作都在一个专用后台线程上执行。

    window 是计算耗时百分位时每个实例取的最近成功样本数。
    """

    def __init__(self, path: Path, *, window: int = 1000) -> None:
        if window < 1:
            raise ValueError(f"window 必须是正整数，收到 {window}")
        self._window = window
        self._db = SqliteThread(path, name="lst-stats", prepare=self._prepare, logger=logger)

    @property
    def window(self) -> int:
        return self._window

    async def open(self) -> None:
        """打开数据库。文件损坏或无法打开时记录错误并改用内存数据库。"""
        await self._db.open()

    async def close(self) -> None:
        """关闭数据库。重复调用没有效果；关闭后再调用其他方法抛出 RuntimeError。"""
        await self._db.close()

    async def record_attempt(
        self, session_id: SessionId, segment_id: SegmentId, attempt: AttemptView
    ) -> None:
        """记录一个已结束的尝试。只保存实例号、状态、耗时、两个标记、错误码和记录时刻。"""
        if attempt.state.pending:
            raise ValueError(f"只记录已结束的尝试，收到状态 {attempt.state.name}")
        row = (
            session_id,
            segment_id,
            attempt.engine_id,
            attempt.state.name,
            attempt.latency_ms,
            int(attempt.cached),
            int(attempt.started),
            None if attempt.error_code is None else attempt.error_code.value,
            time.time(),
        )
        await self._db.run(lambda conn: self._insert_attempt(conn, row))

    async def record_segments(self, session_id: SessionId, views: Sequence[SegmentView]) -> int:
        """记录显示已定的段的胜出者，在一个事务里写入，返回写入的段数。未定的段跳过。

        按 (会话, 段) 覆盖写入，同一段以最后一次为准；shown_engine 为 None 记为无胜出者。
        只保存胜出实例号、胜出尝试是否执行过和原文的字符数。
        """
        now = time.time()
        rows = [
            (
                session_id,
                view.segment_id,
                view.shown_engine,
                int(_shown_executed(view)),
                len(view.source_text),
                now,
            )
            for view in views
            if view.finished
        ]
        if not rows:
            self._db.ensure_open()
            return 0
        await self._db.run(lambda conn: self._upsert_segments(conn, rows))
        return len(rows)

    async def summary(self) -> list[EngineStats]:
        """每个出现过的引擎实例一行，按实例号排序。"""
        return await self._db.run(self._summary_sync)

    async def reset(self) -> None:
        """清空全部统计。"""
        await self._db.run(self._reset_sync)

    # ---- 以下方法在后台线程上执行 ----

    @staticmethod
    def _prepare(conn: sqlite3.Connection) -> None:
        ensure_schema(conn, _SCHEMA_VERSION, _SCHEMA, _MIGRATIONS)

    @staticmethod
    def _insert_attempt(conn: sqlite3.Connection, row: tuple[object, ...]) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO attempts (session_id, segment_id, engine_id, state, latency_ms,"
            " cached, started, error_code, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )

    @staticmethod
    def _upsert_segments(conn: sqlite3.Connection, rows: Sequence[tuple[object, ...]]) -> None:
        with transaction(conn):
            conn.executemany(
                "INSERT INTO segments"
                " (session_id, segment_id, shown_engine, shown_executed, chars, recorded_at)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (session_id, segment_id) DO UPDATE SET"
                " shown_engine = excluded.shown_engine, shown_executed = excluded.shown_executed,"
                " chars = excluded.chars, recorded_at = excluded.recorded_at",
                rows,
            )

    def _summary_sync(self, conn: sqlite3.Connection) -> list[EngineStats]:
        totals: dict[str, tuple[int, int]] = {}
        for engine_id, executed, failed in conn.execute(
            f"SELECT engine_id, SUM({_EXECUTED}), SUM({_EXECUTED} AND state = 'FAILED')"
            " FROM attempts GROUP BY engine_id"
        ):
            totals[str(engine_id)] = (int(executed), int(failed))
        wins: dict[str, int] = {
            str(engine_id): int(n)
            for engine_id, n in conn.execute(
                "SELECT shown_engine, COUNT(*) FROM segments"
                " WHERE shown_engine IS NOT NULL AND shown_executed = 1 GROUP BY shown_engine"
            )
        }
        result: list[EngineStats] = []
        for engine_id in sorted(totals.keys() | wins.keys()):
            executed, failed = totals.get(engine_id, (0, 0))
            latencies = [
                float(latency)
                for (latency,) in conn.execute(
                    f"SELECT latency_ms FROM attempts WHERE engine_id = ? AND {_EXECUTED}"
                    " AND state = 'DONE' AND latency_ms IS NOT NULL ORDER BY id DESC LIMIT ?",
                    (engine_id, self._window),
                )
            ]
            result.append(
                EngineStats(
                    engine_id=EngineId(engine_id),
                    executed=executed,
                    wins=wins.get(engine_id, 0),
                    p50_ms=percentile(latencies, 0.5) if latencies else None,
                    p95_ms=percentile(latencies, 0.95) if latencies else None,
                    failure_rate=failed / executed if executed else None,
                )
            )
        return result

    @staticmethod
    def _reset_sync(conn: sqlite3.Connection) -> None:
        with transaction(conn):
            conn.execute("DELETE FROM attempts")
            conn.execute("DELETE FROM segments")

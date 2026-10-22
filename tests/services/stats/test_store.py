"""竞速统计存储测试（需求 RACE-07、PRV-02，架构文档 10.2 节）。"""

import itertools
import logging
import sqlite3
import statistics
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, SegmentId, SessionId
from lst.contracts.translation import AttemptState, AttemptView, SegmentView
from lst.services.stats import EngineStats, SqliteStatsStore, percentile

DEEPSEEK = EngineId("deepseek")
ARGOS = EngineId("argos")
GOOGLE = EngineId("google")
SESSION = SessionId("session-1")
LOGGER = "lst.services.stats"

DONE = AttemptState.DONE
FAILED = AttemptState.FAILED
CANCELLED = AttemptState.CANCELLED

_segment_ids = itertools.count(1)


def next_segment() -> SegmentId:
    """每次返回一个新段号，保证 (会话, 段, 实例) 不重复。"""
    return SegmentId(f"s{next(_segment_ids)}")


def attempt(
    engine_id: EngineId,
    state: AttemptState,
    latency_ms: float | None = 100.0,
    *,
    cached: bool = False,
    started: bool = True,
    error_code: ErrorCode | None = None,
    text: str | None = None,
    error: str | None = None,
) -> AttemptView:
    return AttemptView(
        engine_id=engine_id,
        state=state,
        text=text,
        error=error,
        latency_ms=latency_ms,
        cached=cached,
        started=started,
        error_code=error_code,
    )


def done(engine_id: EngineId, latency_ms: float) -> AttemptView:
    return attempt(engine_id, DONE, latency_ms, text="译文")


def dones(engine_id: EngineId, *latencies: float) -> list[AttemptView]:
    return [done(engine_id, latency) for latency in latencies]


def cache_hit(engine_id: EngineId) -> AttemptView:
    return attempt(engine_id, DONE, 0.0, cached=True, started=False, text="缓存译文")


def segment(
    segment_id: SegmentId,
    winner: EngineId | None,
    attempts: tuple[AttemptView, ...],
    *,
    finished: bool = True,
    source_text: str = "原文",
) -> SegmentView:
    display = source_text
    for a in attempts:
        if a.engine_id == winner and a.text is not None:
            display = a.text
    return SegmentView(
        segment_id=segment_id,
        source_text=source_text,
        display_text=display,
        winner=winner,
        finished=finished,
        attempts=attempts,
        complete=finished,
    )


async def record(store: SqliteStatsStore, *attempts: AttemptView, session: SessionId = SESSION) -> None:
    """每个尝试记在一个新段上。"""
    for a in attempts:
        await store.record_attempt(session, next_segment(), a)


def by_engine(rows: list[EngineStats]) -> dict[EngineId, EngineStats]:
    return {row.engine_id: row for row in rows}


def db_bytes(path: Path) -> bytes:
    files = (path, path.with_name(path.name + "-wal"))
    return b"".join(p.read_bytes() for p in files if p.exists())


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "stats.sqlite3"


@pytest.fixture
async def store(db_path: Path) -> AsyncIterator[SqliteStatsStore]:
    instance = SqliteStatsStore(db_path)
    await instance.open()
    yield instance
    await instance.close()


async def open_store(path: Path, *, window: int = 1000) -> SqliteStatsStore:
    instance = SqliteStatsStore(path, window=window)
    await instance.open()
    return instance


# ---------------------------------------------------------------- 统计口径


async def test_only_executed_attempts_count(store: SqliteStatsStore) -> None:
    """缓存命中、执行前取消、执行前因实例移除而失败的尝试都不计入；执行后失败的计入（RACE-07）。"""
    await record(
        store,
        cache_hit(DEEPSEEK),
        # 口径按两个标记定义：cached 为真就不计入，即使 started 也为真。
        attempt(DEEPSEEK, DONE, 1.0, cached=True, started=True),
        attempt(DEEPSEEK, CANCELLED, 5.0, started=False),
        attempt(DEEPSEEK, FAILED, 8.0, started=False, error_code=ErrorCode.ENGINE_REMOVED),
        attempt(DEEPSEEK, FAILED, 900.0, error_code=ErrorCode.TIMEOUT),
        done(DEEPSEEK, 120.0),
    )
    stats = by_engine(await store.summary())[DEEPSEEK]
    assert stats.executed == 2
    assert stats.failure_rate == 0.5
    # 百分位只看执行过且成功的尝试：缓存命中的 0 毫秒和失败的 900 毫秒都不在样本里。
    assert stats.p50_ms == 120.0
    assert stats.p95_ms == 120.0


async def test_running_attempt_ended_by_engine_removal_does_not_count(store: SqliteStatsStore) -> None:
    """执行中的尝试因实例被移除或替换而结束：这是配置变化，不计入执行数和失败率。"""
    await record(
        store,
        attempt(DEEPSEEK, FAILED, 300.0, started=True, error_code=ErrorCode.ENGINE_REMOVED),
        done(DEEPSEEK, 120.0),
    )
    stats = by_engine(await store.summary())[DEEPSEEK]
    assert (stats.executed, stats.failure_rate) == (1, 0.0)


async def test_engine_with_only_unexecuted_attempts(store: SqliteStatsStore) -> None:
    await record(store, cache_hit(ARGOS), attempt(ARGOS, CANCELLED, 1.0, started=False))
    assert await store.summary() == [
        EngineStats(engine_id=ARGOS, executed=0, wins=0, p50_ms=None, p95_ms=None, failure_rate=None)
    ]


@pytest.mark.parametrize("state", [AttemptState.QUEUED, AttemptState.RUNNING])
async def test_pending_attempt_is_rejected(store: SqliteStatsStore, state: AttemptState) -> None:
    with pytest.raises(ValueError, match="已结束"):
        await store.record_attempt(SESSION, next_segment(), attempt(DEEPSEEK, state, None))
    assert await store.summary() == []


async def test_rerecorded_attempt_counts_once(store: SqliteStatsStore) -> None:
    seg = next_segment()
    await store.record_attempt(SESSION, seg, attempt(DEEPSEEK, FAILED, 50.0, error_code=ErrorCode.NETWORK))
    await store.record_attempt(SESSION, seg, done(DEEPSEEK, 70.0))
    stats = by_engine(await store.summary())[DEEPSEEK]
    assert (stats.executed, stats.failure_rate, stats.p50_ms) == (1, 0.0, 70.0)


async def test_failure_rate(store: SqliteStatsStore) -> None:
    await record(
        store,
        done(GOOGLE, 100.0),
        done(GOOGLE, 200.0),
        done(GOOGLE, 300.0),
        attempt(GOOGLE, FAILED, 400.0, error_code=ErrorCode.QUOTA),
        cache_hit(GOOGLE),  # 不计入分母
    )
    stats = by_engine(await store.summary())[GOOGLE]
    assert stats.executed == 4
    assert stats.failure_rate == 0.25


async def test_all_executed_failed(store: SqliteStatsStore) -> None:
    await record(store, attempt(GOOGLE, FAILED, 30.0, error_code=ErrorCode.AUTH))
    stats = by_engine(await store.summary())[GOOGLE]
    assert (stats.executed, stats.failure_rate, stats.p50_ms, stats.p95_ms) == (1, 1.0, None, None)


# ---------------------------------------------------------------- 胜出次数


async def test_winner_change_keeps_last_record(store: SqliteStatsStore) -> None:
    """双语窗口晚加入让胜出者从 Argos 变成 DeepSeek，以最后一次记录为准。"""
    seg = next_segment()
    argos, deepseek = done(ARGOS, 80.0), done(DEEPSEEK, 600.0)
    await store.record_attempt(SESSION, seg, argos)
    await store.record_segments(SESSION, [segment(seg, ARGOS, (argos,))])
    assert by_engine(await store.summary())[ARGOS].wins == 1

    await store.record_attempt(SESSION, seg, deepseek)
    await store.record_segments(SESSION, [segment(seg, DEEPSEEK, (deepseek, argos))])
    rows = by_engine(await store.summary())
    assert (rows[ARGOS].wins, rows[DEEPSEEK].wins) == (0, 1)


async def test_unfinished_segment_is_not_recorded(store: SqliteStatsStore) -> None:
    seg = next_segment()
    argos, deepseek = done(ARGOS, 80.0), done(DEEPSEEK, 600.0)
    await record(store, argos, deepseek)
    await store.record_segments(SESSION, [segment(seg, DEEPSEEK, (deepseek, argos))])
    # 晚加入后段变回未定，此时的快照不覆盖已有记录。
    await store.record_segments(SESSION, [segment(seg, ARGOS, (argos,), finished=False)])
    assert by_engine(await store.summary())[DEEPSEEK].wins == 1


async def test_segment_without_winner(store: SqliteStatsStore) -> None:
    seg = next_segment()
    failed = attempt(ARGOS, FAILED, 50.0, error_code=ErrorCode.SERVER)
    await store.record_attempt(SESSION, seg, done(ARGOS, 40.0))
    await store.record_segments(SESSION, [segment(seg, ARGOS, (done(ARGOS, 40.0),))])
    # 同一段最后一次记录没有胜出者，之前的胜出被覆盖。
    await store.record_segments(SESSION, [segment(seg, None, (failed,))])
    assert by_engine(await store.summary())[ARGOS].wins == 0


async def test_cached_winner_does_not_count(store: SqliteStatsStore) -> None:
    hit = cache_hit(DEEPSEEK)
    executed = done(DEEPSEEK, 300.0)
    cached_seg, executed_seg = next_segment(), next_segment()
    await store.record_attempt(SESSION, cached_seg, hit)
    await store.record_attempt(SESSION, executed_seg, executed)
    await store.record_segments(
        SESSION,
        [segment(cached_seg, DEEPSEEK, (hit,)), segment(executed_seg, DEEPSEEK, (executed,))],
    )
    stats = by_engine(await store.summary())[DEEPSEEK]
    assert (stats.executed, stats.wins) == (1, 1)


async def test_same_segment_id_in_different_sessions(store: SqliteStatsStore) -> None:
    seg = SegmentId("s1")
    for session in (SessionId("a"), SessionId("b")):
        a = done(ARGOS, 50.0)
        await store.record_attempt(session, seg, a)
        await store.record_segments(session, [segment(seg, ARGOS, (a,))])
    stats = by_engine(await store.summary())[ARGOS]
    assert (stats.executed, stats.wins) == (2, 2)


# ---------------------------------------------------------------- 耗时百分位


async def test_percentiles_hand_computed(store: SqliteStatsStore) -> None:
    """样本升序为 10, 20, 30, 40，n = 4。

    P50：h = 3 × 0.5 = 1.5，取 x_1 + 0.5 × (x_2 - x_1) = 20 + 5 = 25。
    P95：h = 3 × 0.95 = 2.85，取 x_2 + 0.85 × (x_3 - x_2) = 30 + 8.5 = 38.5。
    """
    await record(store, *dones(DEEPSEEK, 40.0, 10.0, 30.0, 20.0))
    stats = by_engine(await store.summary())[DEEPSEEK]
    assert stats.p50_ms == pytest.approx(25.0)
    assert stats.p95_ms == pytest.approx(38.5)


async def test_window_uses_most_recent_samples(db_path: Path) -> None:
    store = await open_store(db_path, window=4)
    try:
        # 先记 3 个 1000 毫秒的旧样本，再记 4 个新样本；窗口只看最近 4 个。
        await record(store, *dones(DEEPSEEK, 1000.0, 1000.0, 1000.0))
        await record(store, *dones(DEEPSEEK, 10.0, 20.0, 30.0, 40.0))
        # 窗口只数成功样本：最近的失败尝试不占名额。
        await record(store, attempt(DEEPSEEK, FAILED, 5000.0, error_code=ErrorCode.TIMEOUT))
        stats = by_engine(await store.summary())[DEEPSEEK]
        assert stats.p50_ms == pytest.approx(25.0)
        assert stats.p95_ms == pytest.approx(38.5)
        assert stats.executed == 8  # 窗口只影响百分位，计数仍然累计全部
    finally:
        await store.close()


async def test_window_is_per_engine(db_path: Path) -> None:
    store = await open_store(db_path, window=2)
    try:
        await record(store, *dones(ARGOS, 10.0, 20.0))
        # 之后记录的别的实例不挤占 Argos 的窗口。
        await record(store, *dones(DEEPSEEK, 500.0, 500.0, 500.0, 500.0, 500.0))
        rows = by_engine(await store.summary())
        assert rows[ARGOS].p50_ms == pytest.approx(15.0)
        assert rows[DEEPSEEK].p50_ms == pytest.approx(500.0)
    finally:
        await store.close()


@pytest.mark.parametrize("window", [0, -5])
def test_window_must_be_positive(db_path: Path, window: int) -> None:
    with pytest.raises(ValueError, match="正整数"):
        SqliteStatsStore(db_path, window=window)


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([42.0], 0.5, 42.0),
        ([42.0], 0.95, 42.0),
        ([100.0, 200.0], 0.5, 150.0),  # h = 0.5
        ([100.0, 200.0], 0.95, 195.0),  # h = 0.95
        ([40.0, 10.0, 30.0, 20.0], 0.5, 25.0),
        ([40.0, 10.0, 30.0, 20.0], 0.95, 38.5),
        ([float(v) for v in range(1, 101)], 0.5, 50.5),  # h = 49.5，x_49 = 50，x_50 = 51
        ([float(v) for v in range(1, 101)], 0.95, 95.05),  # h = 94.05，x_94 = 95，x_95 = 96
        ([3.0, 1.0, 2.0], 0.0, 1.0),
        ([3.0, 1.0, 2.0], 1.0, 3.0),
    ],
)
def test_percentile_values(values: list[float], q: float, expected: float) -> None:
    assert percentile(values, q) == pytest.approx(expected)


def test_percentile_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="至少一个样本"):
        percentile([], 0.5)
    with pytest.raises(ValueError, match="分位点"):
        percentile([1.0], 1.5)


latencies = st.lists(st.floats(min_value=0.0, max_value=1e6, allow_nan=False), min_size=1, max_size=60)


@given(latencies)
def test_percentile_matches_statistics_inclusive(values: list[float]) -> None:
    """与 statistics.quantiles(method="inclusive") 的第 50、95 个百分位切点一致。"""
    cuts = statistics.quantiles(values, n=100, method="inclusive")
    assert percentile(values, 0.5) == pytest.approx(cuts[49], rel=1e-9, abs=1e-6)
    assert percentile(values, 0.95) == pytest.approx(cuts[94], rel=1e-9, abs=1e-6)


@given(latencies, st.floats(0.0, 1.0), st.floats(0.0, 1.0))
def test_percentile_is_monotone_and_bounded(values: list[float], q1: float, q2: float) -> None:
    lo, hi = sorted((q1, q2))
    assert min(values) <= percentile(values, lo) <= percentile(values, hi) <= max(values)


# ---------------------------------------------------------------- 持久化与隐私


async def test_persists_across_restart(db_path: Path) -> None:
    first = await open_store(db_path)
    seg = next_segment()
    a = done(ARGOS, 60.0)
    await first.record_attempt(SESSION, seg, a)
    await first.record_segments(SESSION, [segment(seg, ARGOS, (a,))])
    before = await first.summary()
    await first.close()

    second = await open_store(db_path)
    try:
        assert await second.summary() == before
        assert before == [EngineStats(ARGOS, executed=1, wins=1, p50_ms=60.0, p95_ms=60.0, failure_rate=0.0)]
    finally:
        await second.close()


async def test_reset(store: SqliteStatsStore) -> None:
    seg = next_segment()
    a = done(ARGOS, 60.0)
    await store.record_attempt(SESSION, seg, a)
    await store.record_segments(SESSION, [segment(seg, ARGOS, (a,))])
    await store.reset()
    assert await store.summary() == []


async def test_no_text_is_stored(db_path: Path) -> None:
    """原文、译文和错误详情都不写入统计库（PRV-02，架构文档 10.2 节）。"""
    source = "SOURCE-私密原文-4d2a"
    translation = "TRANSLATION-私密译文-8c1e"
    error_detail = "ERROR-DETAIL-回显原文-77aa"
    engine = EngineId("engine-marker-3f9b")
    store = await open_store(db_path)
    seg = next_segment()
    won = attempt(engine, DONE, 210.0, text=translation)
    lost = attempt(ARGOS, FAILED, 90.0, error=error_detail, error_code=ErrorCode.BAD_RESPONSE)
    await store.record_attempt(SESSION, seg, won)
    await store.record_attempt(SESSION, seg, lost)
    await store.record_segments(SESSION, [segment(seg, engine, (won, lost), source_text=source)])

    # 逐列核对库里实际保存的内容。
    attempts, segments = await store._db.run(
        lambda conn: (
            conn.execute(
                "SELECT engine_id, state, latency_ms, cached, started, error_code FROM attempts ORDER BY id"
            ).fetchall(),
            conn.execute("SELECT segment_id, winner, winner_executed, chars FROM segments").fetchall(),
        )
    )
    assert attempts == [
        (engine, "DONE", 210.0, 0, 1, None),
        (ARGOS, "FAILED", 90.0, 0, 1, "engine.bad_response"),
    ]
    assert segments == [(seg, engine, 1, len(source))]

    while_open = db_bytes(db_path)
    await store.close()
    after_close = db_bytes(db_path)
    for data in (while_open, after_close):
        for secret in (source, translation, error_detail, "私密原文", "私密译文"):
            assert secret.encode() not in data
        assert engine.encode() in data  # 对照：读到的字节里确实有这几条记录


# ---------------------------------------------------------------- 线程与健壮性


async def test_database_work_runs_on_background_thread(store: SqliteStatsStore) -> None:
    seen: set[int] = set()

    def install(conn: sqlite3.Connection) -> None:
        conn.set_trace_callback(lambda _sql: seen.add(threading.get_ident()))

    await store._db.run(install)
    seg = next_segment()
    a = done(ARGOS, 10.0)
    await store.record_attempt(SESSION, seg, a)
    await store.record_segments(SESSION, [segment(seg, ARGOS, (a,))])
    await store.summary()
    await store.reset()

    worker = store._db.thread_id
    assert worker is not None
    assert worker != threading.get_ident()
    assert seen == {worker}


async def test_corrupted_file_is_quarantined_and_replaced(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    garbage = b"\x00garbage, not sqlite\xff" * 100
    db_path.write_bytes(garbage)
    store = SqliteStatsStore(db_path)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await store.open()
    try:
        assert not store._db.in_memory
        (quarantined,) = db_path.parent.glob(f"{db_path.name}.corrupt-*")
        assert quarantined.read_bytes() == garbage
        assert any(r.levelno == logging.ERROR and r.name.startswith(LOGGER) for r in caplog.records)
        await record(store, done(ARGOS, 10.0))
        assert by_engine(await store.summary())[ARGOS].executed == 1
    finally:
        await store.close()
    # 原路径上现在是新建的库，重新打开后数据仍在。
    reopened = SqliteStatsStore(db_path)
    await reopened.open()
    try:
        assert by_engine(await reopened.summary())[ARGOS].executed == 1
    finally:
        await reopened.close()


async def test_calls_after_close_raise(db_path: Path) -> None:
    store = await open_store(db_path)
    await store.close()
    await store.close()
    with pytest.raises(RuntimeError, match="已关闭"):
        await store.summary()
    with pytest.raises(RuntimeError, match="已关闭"):
        await store.record_attempt(SESSION, next_segment(), done(ARGOS, 1.0))
    with pytest.raises(RuntimeError, match="已关闭"):
        await store.record_segments(SESSION, [])
    with pytest.raises(RuntimeError, match="已关闭"):
        await store.reset()

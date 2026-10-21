"""SQLite 译文缓存测试（需求 RACE-08，架构文档 2.9.3、6.4 节）。

全部用例使用 pytest 的临时目录里的真实数据库文件。检查隐私时直接读取数据库文件和 WAL 文件的字节。
"""

import asyncio
import dataclasses
import logging
import sqlite3
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import closing
from pathlib import Path

import pytest

from lst.contracts.ids import EngineId, Lang
from lst.contracts.ports import CacheKey, TranslationCache
from lst.services.cache import SqliteTranslationCache, cache_digest

EN = Lang("en")
ZH = Lang("zh-Hans")
JA = Lang("ja")
DEEPSEEK = EngineId("deepseek")
ARGOS = EngineId("argos")
LOGGER = "lst.services.cache"


def key(
    text: str = "Hello, world.",
    *,
    source: Lang = EN,
    target: Lang = ZH,
    engine_id: EngineId = DEEPSEEK,
    fingerprint: str = "fp-1",
) -> CacheKey:
    return CacheKey(text, source, target, engine_id, fingerprint)


def db_bytes(path: Path) -> bytes:
    """数据库文件与 WAL 文件的全部字节。连接打开期间，刚提交的数据在 WAL 文件里。"""
    files = (path, path.with_name(path.name + "-wal"))
    return b"".join(p.read_bytes() for p in files if p.exists())


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "cache.sqlite3"


@pytest.fixture
async def cache(db_path: Path) -> AsyncIterator[SqliteTranslationCache]:
    instance = SqliteTranslationCache(db_path)
    await instance.open()
    yield instance
    await instance.close()


async def open_cache(path: Path, *, capacity: int = 100_000) -> SqliteTranslationCache:
    instance = SqliteTranslationCache(path, capacity=capacity)
    await instance.open()
    return instance


# ---------------------------------------------------------------- 查询与写入


async def test_hit_and_miss(cache: SqliteTranslationCache) -> None:
    port: TranslationCache = cache  # 静态检查：满足 TranslationCache 端口
    hit, miss = key("Good morning."), key("Good night.")
    await port.put(hit, "早上好。")

    assert await port.get_many([hit, miss]) == {hit: "早上好。"}
    assert await port.get_many([miss]) == {}


async def test_get_many_with_empty_and_duplicate_keys(cache: SqliteTranslationCache) -> None:
    k = key()
    await cache.put(k, "你好，世界。")
    assert await cache.get_many([]) == {}
    assert await cache.get_many([k, k, k]) == {k: "你好，世界。"}


async def test_put_overwrites_existing_entry(cache: SqliteTranslationCache) -> None:
    k = key()
    await cache.put(k, "旧译文")
    await cache.put(k, "新译文")
    assert await cache.get_many([k]) == {k: "新译文"}
    assert await cache.count() == 1


async def test_different_fingerprint_misses(cache: SqliteTranslationCache) -> None:
    """换模型或改提示词后指纹改变，旧译文不会被误用（RACE-08）。"""
    await cache.put(key(fingerprint="model-a"), "模型 A 的译文")
    assert await cache.get_many([key(fingerprint="model-b")]) == {}
    assert await cache.get_many([key(fingerprint="model-a")]) == {key(fingerprint="model-a"): "模型 A 的译文"}


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "Hello, world!"},
        {"source": JA},
        {"target": JA},
        {"engine_id": ARGOS},
        {"fingerprint": "fp-2"},
    ],
    ids=["text", "source", "target", "engine_id", "fingerprint"],
)
async def test_any_key_field_change_misses(cache: SqliteTranslationCache, changes: dict[str, str]) -> None:
    base = key()
    await cache.put(base, "你好，世界。")
    other = dataclasses.replace(base, **changes)
    assert await cache.get_many([other]) == {}
    assert await cache.get_many([base]) == {base: "你好，世界。"}


async def test_digest_is_unambiguous_across_field_boundaries(cache: SqliteTranslationCache) -> None:
    """直接拼接时 "ab"+"c" 与 "a"+"bc" 相同；长度前缀让两者成为不同的键。"""
    left = key("ab", source=Lang("c"))
    right = key("a", source=Lang("bc"))
    assert cache_digest(left) != cache_digest(right)

    await cache.put(left, "左")
    await cache.put(right, "右")
    assert await cache.get_many([left, right]) == {left: "左", right: "右"}


async def test_clear_and_count(cache: SqliteTranslationCache) -> None:
    for i in range(5):
        await cache.put(key(f"line {i}"), f"第 {i} 行")
    assert await cache.count() == 5

    await cache.clear()
    assert await cache.count() == 0
    assert await cache.get_many([key("line 0")]) == {}

    await cache.put(key("after clear"), "清空之后")
    assert await cache.count() == 1


# ---------------------------------------------------------------- 持久化与容量


async def test_persists_across_restart(db_path: Path) -> None:
    first = await open_cache(db_path)
    await first.put(key(), "你好，世界。")
    await first.close()

    second = await open_cache(db_path)
    try:
        assert await second.get_many([key()]) == {key(): "你好，世界。"}
        assert await second.count() == 1
    finally:
        await second.close()


async def test_eviction_keeps_recently_used_entries(db_path: Path) -> None:
    cache = await open_cache(db_path, capacity=10)
    try:
        keys = [key(f"line {i}") for i in range(11)]
        for i in range(10):
            await cache.put(keys[i], f"t{i}")
        assert await cache.count() == 10  # 恰好等于容量，不淘汰

        # 命中刷新使用序号：keys[0] 从最旧变成最新。
        assert await cache.get_many([keys[0]]) == {keys[0]: "t0"}

        # 第 11 条超过容量，淘汰到 90%（9 条）：删去最久未用的 keys[1]、keys[2]。
        await cache.put(keys[10], "t10")
        assert await cache.count() == 9
        hits = await cache.get_many(keys)
        assert set(hits) == {keys[0], *keys[3:]}

        # 淘汰一次留出余量，下一次写入不再触发淘汰。
        await cache.put(key("extra"), "extra")
        assert await cache.count() == 10
    finally:
        await cache.close()


async def test_overwrite_does_not_trigger_eviction(db_path: Path) -> None:
    cache = await open_cache(db_path, capacity=3)
    try:
        keys = [key(f"line {i}") for i in range(3)]
        for k in keys:
            await cache.put(k, "v1")
        await cache.put(keys[0], "v2")
        assert await cache.count() == 3
        assert await cache.get_many(keys) == {keys[0]: "v2", keys[1]: "v1", keys[2]: "v1"}
    finally:
        await cache.close()


async def test_capacity_reduced_between_runs_evicts_on_open(db_path: Path) -> None:
    first = await open_cache(db_path, capacity=10)
    keys = [key(f"line {i}") for i in range(10)]
    for i, k in enumerate(keys):
        await first.put(k, f"t{i}")
    await first.close()

    # 容量调成 5：打开时淘汰到 5 的 90%，向下取整为 4 条，保留最近写入的 4 条。
    second = await open_cache(db_path, capacity=5)
    try:
        assert await second.count() == 4
        assert set(await second.get_many(keys)) == set(keys[6:])
    finally:
        await second.close()


async def test_capacity_one_keeps_latest_entry(db_path: Path) -> None:
    cache = await open_cache(db_path, capacity=1)
    try:
        await cache.put(key("a"), "A")
        await cache.put(key("b"), "B")
        assert await cache.count() == 1
        assert await cache.get_many([key("a"), key("b")]) == {key("b"): "B"}
    finally:
        await cache.close()


@pytest.mark.parametrize("capacity", [0, -1])
def test_capacity_must_be_positive(db_path: Path, capacity: int) -> None:
    with pytest.raises(ValueError, match="正整数"):
        SqliteTranslationCache(db_path, capacity=capacity)


# ---------------------------------------------------------------- 存储格式与隐私


async def test_wal_mode_and_synchronous_normal(db_path: Path) -> None:
    cache = await open_cache(db_path)
    synchronous = await cache._db.run(lambda conn: conn.execute("PRAGMA synchronous").fetchone()[0])
    await cache.close()

    assert synchronous == 1  # NORMAL
    # WAL 模式记录在文件头里，另开一个连接也能读到。
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


async def test_source_text_is_not_stored_in_plaintext(db_path: Path) -> None:
    source = "SOURCE-私密原文-5f1c"
    translation = "TRANSLATION-译文-9b2e"
    k = key(source)
    cache = await open_cache(db_path)
    await cache.put(k, translation)
    while_open = db_bytes(db_path)
    await cache.close()
    after_close = db_bytes(db_path)

    for data in (while_open, after_close):
        assert source.encode() not in data
        assert "私密原文".encode() not in data
        # 对照：译文按设计明文保存，键以哈希保存，说明读到的字节确实包含这条记录。
        assert translation.encode() in data
        assert cache_digest(k) in data


# ---------------------------------------------------------------- 线程与并发


async def test_database_work_runs_on_background_thread(cache: SqliteTranslationCache) -> None:
    seen: set[int] = set()

    def install(conn: sqlite3.Connection) -> None:
        # 每执行一条 SQL 记一次当前线程。
        conn.set_trace_callback(lambda _sql: seen.add(threading.get_ident()))

    await cache._db.run(install)
    await cache.put(key(), "你好")
    await cache.get_many([key(), key("missing")])
    await cache.count()
    await cache.clear()

    worker = cache._db.thread_id
    assert worker is not None
    assert worker != threading.get_ident()
    assert seen == {worker}
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    assert names[worker].startswith("lst-cache")


async def test_event_loop_keeps_running_while_worker_is_busy(cache: SqliteTranslationCache) -> None:
    gate = threading.Event()
    busy = asyncio.create_task(cache._db.run(lambda _conn: gate.wait(5)))
    # 后台线程卡在闸门上时，排在后面的写入要等它结束。
    queued = asyncio.create_task(cache.put(key(), "排队中的写入"))
    for _ in range(20):
        await asyncio.sleep(0)
    # 事件循环一直在运行；如果数据库操作在循环线程上执行，这里会先阻塞 5 秒再看到 busy 已完成。
    assert not busy.done()
    assert not queued.done()

    gate.set()
    assert await busy is True
    await queued
    assert await cache.get_many([key()]) == {key(): "排队中的写入"}


async def test_concurrent_get_and_put(cache: SqliteTranslationCache) -> None:
    keys = [key(f"line {i}") for i in range(100)]
    expected = {k: f"第 {i} 行" for i, k in enumerate(keys)}

    async def reader() -> int:
        hits = await cache.get_many(keys)
        # 并发读到的每一条都必须是完整写入的那条译文。
        assert all(expected[k] == v for k, v in hits.items())
        return len(hits)

    counts = await asyncio.gather(
        *(cache.put(k, v) for k, v in expected.items()),
        *(reader() for _ in range(20)),
    )
    assert all(0 <= n <= 100 for n in counts if n is not None)
    assert await cache.get_many(keys) == expected
    assert await cache.count() == 100


async def test_concurrent_puts_to_same_key_apply_in_call_order(cache: SqliteTranslationCache) -> None:
    k = key()
    await asyncio.gather(*(cache.put(k, f"版本 {i}") for i in range(10)))
    assert await cache.get_many([k]) == {k: "版本 9"}


async def test_hit_refreshes_last_used(cache: SqliteTranslationCache) -> None:
    """查询先返回，刷新使用序号作为下一个后台任务执行，排在之后提交的操作前面。"""
    k = key()
    await cache.put(k, "你好")
    assert await cache.get_many([k, key("missing")]) == {k: "你好"}
    stamps = await cache._db.run(lambda conn: conn.execute("SELECT last_used FROM translations").fetchall())
    assert stamps == [(2,)]  # put 用了序号 1，命中刷新用了序号 2


async def test_background_task_failure_is_logged(
    cache: SqliteTranslationCache, caplog: pytest.LogCaptureFixture
) -> None:
    def boom(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cache._db.submit(boom)
        await cache.count()  # 后台线程按提交顺序执行，count 返回时 boom 已经执行过
    assert any(r.levelno == logging.WARNING and r.exc_info for r in caplog.records)

    await cache.close()
    cache._db.submit(boom)  # 关闭之后不再提交，也不报错


# ---------------------------------------------------------------- 健壮性与生命周期


async def test_corrupted_file_is_quarantined_and_replaced(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    garbage = b"this is not a sqlite database\n" * 64
    db_path.write_bytes(garbage)
    cache = SqliteTranslationCache(db_path)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await cache.open()
    try:
        # 坏文件改名保留，原路径上新建一个库，缓存照常持久化。
        assert not cache._db.in_memory
        assert [r for r in caplog.records if r.levelno == logging.ERROR and r.name.startswith(LOGGER)]
        await cache.put(key(), "你好")
    finally:
        await cache.close()
    (quarantined,) = db_path.parent.glob(f"{db_path.name}.corrupt-*")
    assert quarantined.read_bytes() == garbage  # 损坏的文件原样保留，只改了名字
    reopened = await open_cache(db_path)
    try:
        assert await reopened.get_many([key()]) == {key(): "你好"}
    finally:
        await reopened.close()


async def test_corrupted_file_falls_back_to_memory_when_rename_fails(
    db_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    garbage = b"this is not a sqlite database\n" * 64
    db_path.write_bytes(garbage)

    def refuse(self: Path, target: Path) -> Path:
        raise PermissionError("locked")

    monkeypatch.setattr(Path, "rename", refuse)
    cache = SqliteTranslationCache(db_path)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await cache.open()
    try:
        assert cache._db.in_memory
        await cache.put(key(), "你好")
        assert await cache.get_many([key()]) == {key(): "你好"}
    finally:
        await cache.close()
    assert db_path.read_bytes() == garbage  # 改名失败时原文件保持不动


async def test_clear_vacuums_deleted_translations(db_path: Path) -> None:
    cache = await open_cache(db_path)
    try:
        await cache.put(key(), "一段不应残留的译文")
        await cache.clear()
    finally:
        await cache.close()
    data = b"".join(p.read_bytes() for p in db_path.parent.glob(f"{db_path.name}*"))
    assert "一段不应残留的译文".encode() not in data


async def test_unopenable_path_falls_back_to_memory(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "is-a-directory"
    path.mkdir()
    cache = SqliteTranslationCache(path)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await cache.open()
    try:
        assert cache._db.in_memory
        assert any(r.levelno == logging.ERROR for r in caplog.records)
        await cache.put(key(), "你好")
        assert await cache.count() == 1
    finally:
        await cache.close()


async def test_unknown_schema_version_is_quarantined(db_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("PRAGMA user_version = 99")
    cache = SqliteTranslationCache(db_path)
    with caplog.at_level(logging.ERROR, logger=LOGGER):
        await cache.open()
    try:
        assert not cache._db.in_memory
        errors = [r.exc_info[1] for r in caplog.records if r.exc_info is not None]
        assert any(isinstance(e, sqlite3.DatabaseError) and "版本为 99" in str(e) for e in errors)
    finally:
        await cache.close()
    assert len(list(db_path.parent.glob(f"{db_path.name}.corrupt-*"))) == 1


async def test_missing_parent_directory_is_created(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "dir" / "cache.sqlite3"
    cache = await open_cache(path)
    try:
        assert not cache._db.in_memory
        await cache.put(key(), "你好")
    finally:
        await cache.close()
    assert path.is_file()


async def test_lifecycle_errors(db_path: Path) -> None:
    cache = SqliteTranslationCache(db_path)
    with pytest.raises(RuntimeError, match="尚未打开"):
        await cache.get_many([key()])

    await cache.open()
    with pytest.raises(RuntimeError, match="已经打开过"):
        await cache.open()
    worker = cache._db.thread_id

    await cache.close()
    await cache.close()  # 重复关闭没有效果
    calls: list[Callable[[], Awaitable[object]]] = [
        lambda: cache.get_many([key()]),
        lambda: cache.get_many([]),
        lambda: cache.put(key(), "x"),
        cache.count,
        cache.clear,
    ]
    for call in calls:
        with pytest.raises(RuntimeError, match="已关闭"):
            await call()

    # 后台线程在关闭后退出。
    for _ in range(200):
        if all(thread.ident != worker for thread in threading.enumerate()):
            break
        await asyncio.sleep(0.01)
    assert all(thread.ident != worker for thread in threading.enumerate())


async def test_operations_submitted_before_close_complete(db_path: Path) -> None:
    cache = await open_cache(db_path)
    pending = [asyncio.create_task(cache.put(key(f"line {i}"), f"第 {i} 行")) for i in range(20)]
    await asyncio.sleep(0)  # 让写入任务先提交到后台线程
    await cache.close()
    await asyncio.gather(*pending)

    reopened = await open_cache(db_path)
    try:
        assert await reopened.count() == 20
    finally:
        await reopened.close()

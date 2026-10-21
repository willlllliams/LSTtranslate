"""CacheModule 的测试：ClearCache 命令清空译文缓存并发布 CacheCleared。"""

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lst.contracts.events import CacheCleared, ClearCache
from lst.contracts.ids import EngineId, Lang
from lst.contracts.ports import CacheKey
from lst.kernel import EventBus, Module, ModuleContext
from lst.services.cache import CacheModule, SqliteTranslationCache


def key(text: str) -> CacheKey:
    return CacheKey(text, Lang("en"), Lang("zh-Hans"), EngineId("deepseek"), "fp")


@pytest.fixture
async def bus() -> AsyncIterator[EventBus]:
    instance = EventBus(asyncio.get_running_loop(), raise_errors=True)
    yield instance
    await instance.aclose()


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncIterator[SqliteTranslationCache]:
    instance = SqliteTranslationCache(tmp_path / "cache.sqlite3")
    await instance.open()
    yield instance
    with contextlib.suppress(RuntimeError):
        await instance.close()


async def test_clear_cache_empties_the_cache_and_reports_the_count(
    bus: EventBus, cache: SqliteTranslationCache, caplog: pytest.LogCaptureFixture
) -> None:
    for text in ("one", "two", "three"):
        await cache.put(key(text), f"译文-{text}")
    cleared: list[CacheCleared] = []
    bus.subscribe(CacheCleared, cleared.append)
    module = CacheModule(cache)
    static_check: Module = module  # 静态检查：满足 Module 协议
    ctx = ModuleContext(bus, logging.getLogger(f"lst.{static_check.name}"))
    await module.start(ctx)

    with caplog.at_level(logging.DEBUG):
        bus.send(ClearCache())
        await bus.drain()

    assert cleared == [CacheCleared(removed=3)]
    assert await cache.count() == 0
    assert await cache.get_many([key("one")]) == {}
    # 日志里只有条目数，没有译文。
    assert all("译文-" not in record.getMessage() for record in caplog.records)
    await module.stop()
    ctx.close()


async def test_clear_cache_on_empty_cache(bus: EventBus, cache: SqliteTranslationCache) -> None:
    cleared: list[CacheCleared] = []
    bus.subscribe(CacheCleared, cleared.append)
    module = CacheModule(cache)
    ctx = ModuleContext(bus, logging.getLogger("lst.cache"))
    await module.start(ctx)
    bus.send(ClearCache())
    await bus.drain()
    assert cleared == [CacheCleared(removed=0)]
    await module.stop()
    ctx.close()


async def test_clear_failure_is_logged_without_event(
    bus: EventBus, cache: SqliteTranslationCache, caplog: pytest.LogCaptureFixture
) -> None:
    cleared: list[CacheCleared] = []
    bus.subscribe(CacheCleared, cleared.append)
    module = CacheModule(cache)
    ctx = ModuleContext(bus, logging.getLogger("lst.cache"))
    await module.start(ctx)
    await cache.close()  # 缓存已关闭：count 抛出 RuntimeError

    with caplog.at_level(logging.ERROR, logger="lst.cache"):
        bus.send(ClearCache())
        await bus.drain()

    assert cleared == []
    assert [r.levelno for r in caplog.records if r.name == "lst.cache"] == [logging.ERROR]
    await module.stop()
    ctx.close()


async def test_commands_after_stop_are_ignored(bus: EventBus, cache: SqliteTranslationCache) -> None:
    await cache.put(key("one"), "译文")
    cleared: list[CacheCleared] = []
    bus.subscribe(CacheCleared, cleared.append)
    module = CacheModule(cache)
    ctx = ModuleContext(bus, logging.getLogger("lst.cache"))
    await module.start(ctx)
    await module.stop()
    bus.send(ClearCache())  # 订阅还在（组合根先 stop 再取消订阅）
    await bus.drain()
    assert cleared == []
    assert await cache.count() == 1
    ctx.close()

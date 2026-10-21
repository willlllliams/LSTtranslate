"""译文缓存模块：处理清空缓存的命令（需求 RACE-08，架构文档 5.3 节）。"""

import logging

from lst.contracts.events import CacheCleared, ClearCache
from lst.kernel import EventBus, ModuleContext
from lst.services.cache.store import SqliteTranslationCache


class CacheModule:
    """订阅 ``ClearCache``，清空译文缓存并发布 ``CacheCleared``。

    缓存对象由组合根创建、打开和关闭，同一个对象还注入给竞速模块读写译文，所以这里不管它的生命周期。
    清空期间到达的新译文写入会排在清空之后，不会丢失；清空失败只记日志，不发布事件。
    日志里只有条目数，不含任何译文。
    """

    name = "cache"

    def __init__(self, cache: SqliteTranslationCache) -> None:
        self._cache = cache
        self._logger = logging.getLogger(__name__)
        self._bus: EventBus | None = None
        self._running = False

    async def start(self, ctx: ModuleContext) -> None:
        self._logger = ctx.logger
        self._bus = ctx.bus
        self._running = True
        ctx.subscribe(ClearCache, self._on_clear)

    async def stop(self) -> None:
        self._running = False

    async def _on_clear(self, _command: ClearCache) -> None:
        if not self._running:
            return
        try:
            removed = await self._cache.count()
            await self._cache.clear()
        except Exception:
            self._logger.exception("清空译文缓存失败")
            return
        self._logger.info("译文缓存已清空（%d 条）", removed)
        if self._bus is not None and self._running:
            self._bus.publish(CacheCleared(removed))

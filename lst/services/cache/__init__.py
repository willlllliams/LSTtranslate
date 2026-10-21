"""译文缓存：主进程独占的 SQLite 数据库（需求 RACE-08，架构文档 2.9.3 节）。"""

from lst.services.cache.module import CacheModule
from lst.services.cache.store import SqliteTranslationCache, cache_digest

__all__ = ["CacheModule", "SqliteTranslationCache", "cache_digest"]

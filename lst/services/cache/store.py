"""SQLite 译文缓存（需求 RACE-08，架构文档 2.9.3、6.4 节）。

表的主键是缓存键五个字段的 SHA-256，原文只以哈希形式存在；语言、实例号、配置指纹和译文明文保存。
last_used 是使用序号：每次写入或命中时取一个递增的整数，淘汰时删除序号最小的条目。
"""

import hashlib
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from lst.contracts.ports import CacheKey
from lst.services.cache._sqlite import SqliteThread, ensure_schema, transaction

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1
# 一条 IN 查询携带的哈希个数上限，远低于 SQLite 的绑定参数上限 32766。
_LOOKUP_CHUNK = 500
_SCHEMA = """
CREATE TABLE translations (
    key BLOB PRIMARY KEY,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    engine_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    translation TEXT NOT NULL,
    last_used INTEGER NOT NULL
) WITHOUT ROWID;
CREATE INDEX translations_last_used ON translations (last_used);
"""


def cache_digest(key: CacheKey) -> bytes:
    """计算缓存键的 SHA-256。

    五个字段依次写成“UTF-8 字节长度（8 字节大端整数）+ UTF-8 字节”再拼接。
    长度前缀让拼接结果与字段一一对应：直接拼接时，原文 "ab" 加源语言 "c" 与原文 "a" 加源语言 "bc"
    会得到同一个串。
    """
    digest = hashlib.sha256()
    for field in (key.text, key.source, key.target, key.engine_id, key.fingerprint):
        data = field.encode("utf-8", "surrogatepass")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.digest()


class SqliteTranslationCache:
    """实现 ``TranslationCache`` 端口的 SQLite 译文缓存。

    用法：``await open()`` 之后调用 ``get_many``、``put`` 等方法，退出时 ``await close()``。
    所有数据库操作都在一个专用后台线程上执行。

    条目数超过 capacity 时，按使用序号淘汰最久未用的条目，一次淘汰到 capacity 的 90%，
    之后的若干次写入不再触发淘汰。打开时若条目数已超过 capacity（例如用户调小了上限），立即淘汰一次。
    """

    def __init__(self, path: Path, *, capacity: int = 100_000) -> None:
        if capacity < 1:
            raise ValueError(f"缓存容量必须是正整数，收到 {capacity}")
        self._capacity = capacity
        self._low_water = max(1, capacity * 9 // 10)
        self._db = SqliteThread(path, name="lst-cache", prepare=self._prepare, logger=logger)
        # 以下两个字段只在后台线程上读写：当前条目数，最近一次分配的使用序号。
        self._count = 0
        self._clock = 0

    @property
    def capacity(self) -> int:
        """条目数上限。"""
        return self._capacity

    async def open(self) -> None:
        """打开数据库。文件损坏或无法打开时记录错误并改用内存数据库。"""
        await self._db.open()

    async def close(self) -> None:
        """关闭数据库。重复调用没有效果；关闭后再调用其他方法抛出 RuntimeError。"""
        await self._db.close()

    async def get_many(self, keys: Sequence[CacheKey]) -> dict[CacheKey, str]:
        """批量查询，只返回命中的键。命中的条目刷新使用序号。

        刷新使用序号要改写命中条目所在的页，几百条命中需要几十毫秒，而调度器要等查询返回才派发任务。
        所以查询先返回，刷新作为下一个后台任务执行；之后提交的写入排在它后面，淘汰顺序不受影响。
        """
        if not keys:
            self._db.ensure_open()
            return {}
        snapshot = tuple(keys)
        found, hits = await self._db.run(lambda conn: self._lookup_sync(conn, snapshot))
        if hits:
            self._db.submit(lambda conn: self._touch_sync(conn, hits))
        return found

    async def put(self, key: CacheKey, text: str) -> None:
        """写入一条译文；键已存在时覆盖译文并刷新使用序号。"""
        await self._db.run(lambda conn: self._put_sync(conn, key, text))

    async def clear(self) -> None:
        """删除全部条目，并执行 VACUUM 重写文件，让已删除的译文不再残留在空闲页里。"""
        await self._db.run(self._clear_sync)

    async def count(self) -> int:
        """当前条目数。"""
        return await self._db.run(self._count_sync)

    # ---- 以下方法在后台线程上执行 ----

    def _prepare(self, conn: sqlite3.Connection) -> None:
        ensure_schema(conn, _SCHEMA_VERSION, _SCHEMA)
        count, clock = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(last_used), 0) FROM translations"
        ).fetchone()
        self._count = int(count)
        self._clock = int(clock)
        if self._count > self._capacity:
            with transaction(conn):
                deleted = self._evict(conn, self._count)
            self._count -= deleted

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    def _lookup_sync(
        self, conn: sqlite3.Connection, keys: tuple[CacheKey, ...]
    ) -> tuple[dict[CacheKey, str], list[bytes]]:
        """查询译文，返回命中的键与它们的哈希。"""
        by_digest = {cache_digest(key): key for key in keys}
        digests = list(by_digest)
        found: dict[CacheKey, str] = {}
        hits: list[bytes] = []
        for start in range(0, len(digests), _LOOKUP_CHUNK):
            chunk = digests[start : start + _LOOKUP_CHUNK]
            marks = ", ".join("?" * len(chunk))
            for digest, translation in conn.execute(
                f"SELECT key, translation FROM translations WHERE key IN ({marks})", chunk
            ):
                hits.append(bytes(digest))
                found[by_digest[bytes(digest)]] = str(translation)
        return found, hits

    def _touch_sync(self, conn: sqlite3.Connection, digests: list[bytes]) -> None:
        """把命中条目的使用序号刷新为最新。"""
        stamp = self._tick()
        with transaction(conn):
            conn.executemany(
                "UPDATE translations SET last_used = ? WHERE key = ?",
                [(stamp, digest) for digest in digests],
            )

    def _put_sync(self, conn: sqlite3.Connection, key: CacheKey, text: str) -> None:
        digest = cache_digest(key)
        stamp = self._tick()
        with transaction(conn):
            cursor = conn.execute(
                "INSERT OR IGNORE INTO translations"
                " (key, source, target, engine_id, fingerprint, translation, last_used)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest, key.source, key.target, key.engine_id, key.fingerprint, text, stamp),
            )
            count = self._count
            if cursor.rowcount == 1:
                count += 1
            else:
                conn.execute(
                    "UPDATE translations SET translation = ?, last_used = ? WHERE key = ?",
                    (text, stamp, digest),
                )
            if count > self._capacity:
                count -= self._evict(conn, count)
        # 事务提交之后才更新计数，回滚时计数保持不变。
        self._count = count

    def _evict(self, conn: sqlite3.Connection, count: int) -> int:
        """在事务内调用：删除使用序号最小的条目，让条目数从 count 降到低水位。返回删除的条数。"""
        return conn.execute(
            "DELETE FROM translations WHERE key IN (SELECT key FROM translations ORDER BY last_used LIMIT ?)",
            (count - self._low_water,),
        ).rowcount

    @staticmethod
    def _count_sync(conn: sqlite3.Connection) -> int:
        return int(conn.execute("SELECT COUNT(*) FROM translations").fetchone()[0])

    def _clear_sync(self, conn: sqlite3.Connection) -> None:
        with transaction(conn):
            conn.execute("DELETE FROM translations")
        conn.execute("VACUUM")
        self._count = 0

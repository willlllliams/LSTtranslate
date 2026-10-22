"""独占一个 sqlite3 连接的后台线程（架构文档 2.9.3 节）。

译文缓存（lst.services.cache）与竞速统计（lst.services.stats）共用这一份实现。
"""

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Callable, Generator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import Path

type Prepare = Callable[[sqlite3.Connection], None]


class _Phase(Enum):
    NEW = auto()
    OPEN = auto()
    CLOSED = auto()


class SqliteThread:
    """一个后台线程，以及只在这个线程上创建、使用、关闭的 sqlite3 连接。

    数据库操作写成以连接为参数的函数，经 ``run`` 提交。线程按提交顺序逐个执行，
    提交方的协程在等待结果期间不占用事件循环（架构文档第 3 章）。sqlite3 的 ``check_same_thread``
    保持默认开启，连接一旦在别的线程上被使用就会报错。

    ``prepare`` 在连接建立、设置好 WAL 之后于后台线程执行，负责建表和读取初始状态。
    打开文件或 ``prepare`` 失败（文件损坏、不是 SQLite 数据库、路径不可写等）时记录 ERROR 日志，
    改用内存数据库再执行一次 ``prepare``：缓存与统计只是辅助功能，不能让程序因此无法启动。
    """

    def __init__(self, path: Path, *, name: str, prepare: Prepare, logger: logging.Logger) -> None:
        self._path = path
        self._name = name
        self._prepare = prepare
        self._logger = logger
        self._phase = _Phase.NEW
        self._executor: ThreadPoolExecutor | None = None
        # 以下字段只在后台线程上写入。
        self._conn: sqlite3.Connection | None = None
        self._thread_id: int | None = None
        self._in_memory = False

    @property
    def thread_id(self) -> int | None:
        """后台线程的 ``threading.get_ident()``，打开之前为 None。"""
        return self._thread_id

    @property
    def in_memory(self) -> bool:
        """文件无法使用、已改用内存数据库时为 True。"""
        return self._in_memory

    async def open(self) -> None:
        """启动后台线程并建立连接。每个实例只能打开一次。"""
        if self._phase is not _Phase.NEW:
            raise RuntimeError(f"数据库 {self._path.name} 已经打开过")
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self._name)
        self._executor = executor
        self._phase = _Phase.OPEN
        try:
            await asyncio.get_running_loop().run_in_executor(executor, self._open_sync)
        except BaseException:
            self._phase = _Phase.CLOSED
            executor.shutdown(wait=False)
            raise

    def ensure_open(self) -> None:
        """未打开或已关闭时抛出 RuntimeError。"""
        if self._phase is _Phase.NEW:
            raise RuntimeError(f"数据库 {self._path.name} 尚未打开")
        if self._phase is _Phase.CLOSED:
            raise RuntimeError(f"数据库 {self._path.name} 已关闭")

    async def run[T](self, fn: Callable[[sqlite3.Connection], T]) -> T:
        """把 ``fn(conn)`` 交给后台线程执行并等待结果。"""
        self.ensure_open()
        executor = self._executor
        assert executor is not None

        def job() -> T:
            conn = self._conn
            if conn is None:
                raise RuntimeError(f"数据库 {self._path.name} 的连接不可用")
            return fn(conn)

        return await asyncio.get_running_loop().run_in_executor(executor, job)

    def submit(self, fn: Callable[[sqlite3.Connection], object]) -> None:
        """把 ``fn(conn)`` 交给后台线程执行，不等待结果。

        用于不影响返回值的后续工作；fn 抛出的异常记 WARNING 日志。已关闭时不再提交。
        """
        if self._phase is not _Phase.OPEN or self._executor is None:
            return

        def job() -> None:
            conn = self._conn
            if conn is None:
                return
            try:
                fn(conn)
            except Exception:
                self._logger.warning("数据库 %s 的后台任务失败", self._path.name, exc_info=True)

        self._executor.submit(job)

    async def close(self) -> None:
        """关闭连接并让后台线程退出。重复调用没有效果。

        关闭之前已经提交的操作照常执行完；关闭之后再调用 ``run`` 抛出 RuntimeError。
        """
        if self._phase is _Phase.CLOSED:
            return
        self._phase = _Phase.CLOSED
        executor = self._executor
        if executor is None:
            return
        try:
            # shield：等待方被取消时，关闭连接的任务仍然留在队列里执行。
            await asyncio.shield(asyncio.get_running_loop().run_in_executor(executor, self._close_sync))
        finally:
            # 队列里只剩已提交的任务，wait=False 不阻塞事件循环，线程执行完它们后退出。
            executor.shutdown(wait=False)

    # ---- 以下方法在后台线程上执行 ----

    def _open_sync(self) -> None:
        self._thread_id = threading.get_ident()
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = self._connect(self._path)
            return
        except (OSError, sqlite3.Error):
            self._logger.exception("无法使用数据库文件 %s", self._path)

        # 文件损坏或版本不符：把坏文件改名保留，再新建一个库。只改名、不删除，用户可以自行检查。
        # 路径不是普通文件（例如是目录）时不动它，直接改用内存数据库。
        if self._path.is_file() and self._quarantine():
            try:
                self._conn = self._connect(self._path)
                return
            except (OSError, sqlite3.Error):
                self._logger.exception("新建数据库文件 %s 失败", self._path)
        self._logger.error("本次运行改用内存数据库，数据不会保留")
        self._conn = self._connect(None)
        self._in_memory = True

    def _quarantine(self) -> bool:
        """把损坏的数据库文件及其 -wal、-shm 文件改名为带 .corrupt-时间戳 后缀的文件，返回是否成功。"""
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        try:
            for suffix in ("", "-wal", "-shm"):
                source = self._path.with_name(self._path.name + suffix)
                if source.exists():
                    source.rename(self._path.with_name(f"{self._path.name}.corrupt-{stamp}{suffix}"))
        except OSError:
            self._logger.exception("无法改名损坏的数据库文件 %s", self._path)
            return False
        self._logger.error(
            "数据库文件 %s 已损坏，原文件改名为 %s.corrupt-%s", self._path, self._path.name, stamp
        )
        return True

    def _connect(self, path: Path | None) -> sqlite3.Connection:
        """建立连接；path 为 None 时使用内存数据库。"""
        conn = sqlite3.connect(":memory:" if path is None else path, autocommit=True)
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if path is not None and mode != "wal":
                self._logger.warning("数据库 %s 无法开启 WAL 模式，当前模式为 %s", path.name, mode)
            conn.execute("PRAGMA synchronous=NORMAL")
            self._prepare(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _close_sync(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Generator[None]:
    """显式事务：正常退出时提交，出现异常时回滚。连接须以 ``autocommit=True`` 打开。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        # 某些错误（如磁盘已满）会让 SQLite 自动回滚，这时不能再执行 ROLLBACK。
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def ensure_schema(
    conn: sqlite3.Connection, version: int, script: str, migrations: Mapping[int, str] | None = None
) -> None:
    """新库按 script 建表并把 ``user_version`` 设为 version；已有的库按 migrations 逐级升级到 version。

    migrations 的键是旧版本号 v，值是把版本 v 的库改成版本 v + 1 的 SQL。每一级在一个事务里执行并写入
    新版本号，中途出错时这一级整体回滚。版本号高于 version，或缺少某一级迁移时抛出
    ``sqlite3.DatabaseError``，由 ``SqliteThread`` 按文件不可用处理。
    """
    found = conn.execute("PRAGMA user_version").fetchone()[0]
    if found == version:
        return
    if found == 0:
        conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        return
    steps = migrations or {}
    if found > version or any(v not in steps for v in range(found, version)):
        raise sqlite3.DatabaseError(f"数据库版本为 {found}，本程序只认识版本 {version}")
    for current in range(found, version):
        try:
            conn.executescript(f"BEGIN;\n{steps[current]}\nPRAGMA user_version = {current + 1};\nCOMMIT;")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

"""存储位置模块：发布 StorageLocations，处理 ChangeDataDir、ChangeModelsDir、RestartApp
（需求 CFG-06、CFG-07、MOD-04，架构文档 2.9.5、8.5 节）。

更改数据目录：

1. 数据目录由命令行参数或环境变量指定时拒绝（FIXED）：位置文件的优先级更低，改了也不生效；
2. 检查新目录：绝对路径、不在当前数据目录里面、实际写得进文件（``storage.probe_writable``）；
3. 要复制现有数据时，检查新目录里没有同名条目、空间足够，在线程里复制并报告进度；失败时删除复制了一半的
   内容，位置文件不动；
4. 写位置文件（选的是默认位置时删除位置文件），记下 pending_data_dir，界面提示重启程序。

更改模型目录：

1. 检查新目录：绝对路径、不在当前模型目录里面、写得进文件；要移动时检查同名子目录与空间；
2. 请组合根停下用到模型目录的一切（``StorageHost.suspend_models``）：NMT 与 OCR 进程、模型管理、OCR 引擎、
   显卡运行库三个模块，进行中的下载随之取消。进程不停的话，它们加载着目录里的 DLL 与模型文件，
   Windows 上改名和删除都会失败；
3. 移动（``storage.ModelsMove``），失败时移回原处；
4. 把新目录写进配置的 ``[storage].models_dir``（新目录是默认位置时写空串）。写不进去时同样移回原处；
5. 请组合根按新目录（失败时按原目录）恢复：重建三个模块、字典引擎换词典目录、重新扫描并启动进程。
   不需要重启程序。

同一时间只进行一次更改，进行中再收到更改命令时报告 BUSY。
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Final, Protocol

from lst.app.paths import AppPaths, DataDirChoice, default_data_dir, write_location
from lst.app.storage import (
    ModelsMove,
    StorageError,
    copy_data,
    describe_os_error,
    is_inside,
    plan_data_copy,
    plan_models_move,
    probe_writable,
    remove_created,
    remove_paths,
    same_path,
)
from lst.contracts.storage import (
    ChangeDataDir,
    ChangeModelsDir,
    RestartApp,
    StorageErrorCode,
    StorageFailure,
    StorageKind,
    StorageLocations,
    StorageProgress,
)
from lst.kernel import ModuleContext

PROGRESS_INTERVAL_S: Final = 0.2


class StorageHost(Protocol):
    """组合根为存储位置模块提供的操作。"""

    async def suspend_models(self) -> None:
        """停下用到模型目录的进程与模块。"""
        ...

    async def resume_models(self, models_dir: Path | None) -> None:
        """按 models_dir（None 为默认位置）重建模块、重新启动进程。"""
        ...

    async def save_models_dir(self, value: str) -> str | None:
        """把 [storage].models_dir 写进配置文件。成功返回 None，失败返回原因。"""
        ...

    def request_restart(self) -> None:
        """退出并在退出后重新启动程序。"""
        ...


class StorageModule:
    """存储位置模块，实现 kernel 的 Module 协议。

    构造参数：
        paths：返回组合根当前路径的函数（模型目录会在运行中变化）；
        choice：本次运行的数据目录、来源与位置文件；
        host：组合根，见 StorageHost；
        clock：单调时钟，测试用来控制进度节流。
    """

    name = "storage"

    def __init__(
        self,
        paths: Callable[[], AppPaths],
        choice: DataDirChoice,
        host: StorageHost,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._paths = paths
        self._choice = choice
        self._host = host
        self._clock = clock
        self._ctx: ModuleContext | None = None
        self._logger = logging.getLogger(__name__)
        self._pending: Path | None = None
        self._task: StorageProgress | None = None
        self._failure: StorageFailure | None = None
        self._last_progress = 0.0
        self._busy = False

    # ------------------------------------------------------------------ 生命周期

    async def start(self, ctx: ModuleContext) -> None:
        if self._ctx is not None:
            raise RuntimeError("存储位置模块已经启动")
        self._ctx = ctx
        self._logger = ctx.logger
        ctx.subscribe(ChangeDataDir, self._on_change_data_dir)
        ctx.subscribe(ChangeModelsDir, self._on_change_models_dir)
        ctx.subscribe(RestartApp, lambda _: self._host.request_restart())
        self.publish()

    async def stop(self) -> None:
        self._ctx = None

    # ------------------------------------------------------------------ 状态

    def locations(self) -> StorageLocations:
        paths = self._paths()
        return StorageLocations(
            data_dir=str(paths.data_dir),
            data_source=self._choice.source,
            models_dir=str(paths.models_root),
            models_default=paths.models_dir is None,
            pending_data_dir=str(self._pending) if self._pending is not None else None,
            task=self._task,
            failure=self._failure,
        )

    def publish(self) -> None:
        if self._ctx is not None:
            self._ctx.bus.publish(self.locations())

    def _progress_callback(self, kind: StorageKind, target: Path, total: int) -> Callable[[int], None]:
        """给线程用的进度回调：累加字节数，每 200 毫秒最多回到事件循环发布一次。"""
        loop = asyncio.get_running_loop()
        done = 0

        def report(count: int) -> None:
            nonlocal done
            done += count
            value = done
            loop.call_soon_threadsafe(self._update_progress, kind, target, value, total)

        return report

    def _update_progress(self, kind: StorageKind, target: Path, done: int, total: int) -> None:
        if not self._busy:
            return
        now = self._clock()
        if done < total and now - self._last_progress < PROGRESS_INTERVAL_S:
            return
        self._last_progress = now
        self._task = StorageProgress(kind, str(target), min(done, total), total)
        self.publish()

    def _begin(self, kind: StorageKind, target: Path, total: int = 0) -> None:
        self._task = StorageProgress(kind, str(target), 0, total)
        self._last_progress = self._clock()
        self.publish()

    def _fail(self, kind: StorageKind, code: StorageErrorCode, target: str, detail: str = "") -> None:
        self._logger.warning("更改%s目录到 %s 失败（%s）：%s", _NAMES[kind], target, code.value, detail)
        self._failure = StorageFailure(kind, code, target, detail)
        self._task = None
        self.publish()

    async def _guarded(self, kind: StorageKind, target: str, work: Callable[[], Awaitable[None]]) -> None:
        if self._busy:
            self._failure = StorageFailure(kind, StorageErrorCode.BUSY, target)
            self.publish()
            return
        self._busy = True
        self._failure = None
        try:
            await work()
        except StorageError as exc:
            self._fail(kind, exc.code, target, exc.detail)
        finally:
            self._busy = False
            if self._task is not None:
                self._task = None
                self.publish()

    # ------------------------------------------------------------------ 数据目录

    async def _on_change_data_dir(self, command: ChangeDataDir) -> None:
        await self._guarded(StorageKind.DATA, command.target, lambda: self._change_data_dir(command))

    async def _change_data_dir(self, command: ChangeDataDir) -> None:
        if self._choice.source.fixed:
            raise StorageError(StorageErrorCode.FIXED, self._choice.source.value)
        target = Path(command.target)
        if not target.is_absolute():
            raise StorageError(StorageErrorCode.RELATIVE)
        paths = self._paths()
        current = paths.data_dir
        if same_path(target, current):
            # 选回当前目录：撤销还没生效的更改。
            await self._write_pointer(current)
            self._pending = None
            self._logger.info("数据目录保持为 %s", current)
            self.publish()
            return
        if is_inside(target, current):
            raise StorageError(StorageErrorCode.NESTED)
        probe = await asyncio.to_thread(probe_writable, target)
        if probe.error is not None:
            raise StorageError(StorageErrorCode.NOT_WRITABLE, probe.error)
        copied: list[Path] = []
        if command.copy:
            exclude = [paths.models_root] if paths.models_dir is not None else []
            try:
                plan = await asyncio.to_thread(plan_data_copy, current, target, exclude=exclude)
            except BaseException:
                await asyncio.to_thread(remove_created, probe.created)
                raise
            self._begin(StorageKind.DATA, target, plan.total_bytes)
            report = self._progress_callback(StorageKind.DATA, target, plan.total_bytes)
            try:
                await asyncio.to_thread(copy_data, plan, report)
            except OSError as exc:
                await asyncio.to_thread(remove_created, probe.created)
                raise StorageError(StorageErrorCode.COPY_FAILED, describe_os_error(exc)) from exc
            copied = [target / entry.name for entry in plan.entries]
            self._logger.info("已把数据目录 %s 复制到 %s（%d 字节）", current, target, plan.total_bytes)
        try:
            await self._write_pointer(target)
        except StorageError:
            await asyncio.to_thread(remove_paths, copied)
            await asyncio.to_thread(remove_created, probe.created)
            raise
        self._pending = target
        self._logger.info("数据目录改为 %s，重启程序后生效", target)
        self.publish()

    async def _write_pointer(self, target: Path) -> None:
        """写位置文件；目标是默认位置时删除位置文件。"""
        value = None if same_path(target, default_data_dir()) else target
        try:
            await asyncio.to_thread(write_location, self._choice.location, value)
        except OSError as exc:
            raise StorageError(StorageErrorCode.SAVE_FAILED, describe_os_error(exc)) from exc

    # ------------------------------------------------------------------ 模型目录

    async def _on_change_models_dir(self, command: ChangeModelsDir) -> None:
        await self._guarded(StorageKind.MODELS, command.target, lambda: self._change_models_dir(command))

    async def _change_models_dir(self, command: ChangeModelsDir) -> None:
        paths = self._paths()
        target = Path(command.target) if command.target else paths.default_models_dir
        if not target.is_absolute():
            raise StorageError(StorageErrorCode.RELATIVE)
        current = paths.models_root
        if same_path(target, current):
            return
        if is_inside(target, current):
            raise StorageError(StorageErrorCode.NESTED)
        probe = await asyncio.to_thread(probe_writable, target)
        if probe.error is not None:
            raise StorageError(StorageErrorCode.NOT_WRITABLE, probe.error)
        move: ModelsMove | None = None
        if command.move:
            try:
                move = await asyncio.to_thread(plan_models_move, current, target)
            except BaseException:
                await asyncio.to_thread(remove_created, probe.created)
                raise
        value = "" if same_path(target, paths.default_models_dir) else str(target)
        self._begin(StorageKind.MODELS, target, move.total_bytes if move is not None else 0)
        await self._host.suspend_models()
        resumed: Path | None = paths.models_dir
        done = False
        try:
            if move is not None:
                report = self._progress_callback(StorageKind.MODELS, target, move.total_bytes)
                try:
                    await asyncio.to_thread(move.run, report)
                except OSError as exc:
                    raise StorageError(StorageErrorCode.MOVE_FAILED, describe_os_error(exc)) from exc
            error = await self._host.save_models_dir(value)
            if error is not None:
                if move is not None:
                    await asyncio.to_thread(move.rollback)
                raise StorageError(StorageErrorCode.SAVE_FAILED, error)
            if move is not None:
                left = await asyncio.to_thread(move.commit)
                for path in left:
                    self._logger.warning("旧模型目录里的 %s 没能删除，可以手动删除", path)
            resumed = Path(value) if value else None
            done = True
            self._logger.info("模型目录改为 %s", target)
        finally:
            if not done:
                await asyncio.to_thread(remove_created, probe.created)
            await self._host.resume_models(resumed)
            self.publish()


_NAMES: Final = {StorageKind.DATA: "数据", StorageKind.MODELS: "模型"}

__all__ = ["StorageHost", "StorageModule"]

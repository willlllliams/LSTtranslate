"""本地 NMT 模型管理模块（需求 MOD-01、MOD-04、INS-03、INS-07，架构文档 8.5 节）。

模块维护三份数据，每次变化都发布一个完整的 ``ModelCatalog``：

1. 已安装：模型目录下可用的 .argosmodel，启动时扫描，安装或删除后重新扫描；
2. 可下载：最近一次成功获取的 Argos 模型索引。只在收到 ``RefreshModelCatalog`` 时联网获取，
   程序启动时不发请求，只用本地模型时程序没有任何网络请求（需求 PRV-01）；
3. 下载进度：同一时间只下载一个模型，其余排队。

下载流程：流式写入模型目录下的 ``<名>.part``（不用 tempfile，架构文档 2.9.5 节）→ 校验 → 用 ``os.replace``
原子改名为 ``<名>.argosmodel`` → 删除同一语言对的旧版本 → 发布 ``ModelsInstalled``。失败或取消时删除临时文件。

线程：模块的状态只在事件循环线程上读写。扫描、校验、写文件、改名、删除这些磁盘操作都放进
线程池（_shared.aio.blocking），网络用 httpx 的异步流式接口，下载大文件时事件循环照常运转（需求 REL-04）。
"""

import asyncio
import itertools
import json
import logging
import time
from collections import deque
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Final

import httpx

from lst.contracts.models import (
    ACTIVE_DOWNLOAD_STATES,
    CancelModelDownload,
    DeleteModel,
    DownloadModels,
    DownloadProgress,
    DownloadState,
    ModelCatalog,
    ModelErrorCode,
    ModelInfo,
    ModelKind,
    ModelsInstalled,
    RefreshModelCatalog,
    nmt_model_id,
    version_key,
)
from lst.kernel import ModuleContext
from lst.services._shared.aio import blocking
from lst.services._shared.download import content_length, open_for_write, stream_to_file
from lst.services.models.argos import (
    PACKAGE_SUFFIX,
    PART_SUFFIX,
    InvalidPackage,
    PackageMeta,
    Pair,
    best_package,
    delete_files,
    install_package,
    package_stem,
    remove_partial_files,
    scan_installed,
    verify_download,
)
from lst.services.models.index import DEFAULT_INDEX_URL, IndexEntry, IndexFormatError, parse_index
from lst.services.models.net import (
    DOWNLOAD_TIMEOUT,
    INDEX_TIMEOUT,
    classify_transport_error,
    describe_error,
    safe_url,
)

MAX_INDEX_BYTES: Final = 8 * 1024 * 1024
"""模型索引的大小上限。真实索引只有几十 KB，超过上限说明地址指向了别的东西。"""

PROGRESS_INTERVAL_S: Final = 0.2
"""下载进度的发布间隔。每收到一块数据就发布会让界面每秒刷新上千次。"""

WRITE_CHUNK_BYTES: Final = 1 << 20
"""攒够这么多数据再交给线程写盘，减少线程切换。"""

_HTTP_OK: Final = 200


class _Failure(Exception):
    """获取索引或下载失败。code 是原因分类，detail 是给用户看的技术细节（不含地址的查询参数）。"""

    def __init__(self, code: ModelErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class ModelsModule:
    """本地 NMT 模型的目录、下载与删除，实现 kernel 的 Module 协议。

    构造参数：
        models_dir：模型目录（需求 MOD-04），与 NMT 进程扫描的目录相同；
        client：共用的 httpx 客户端，测试注入 MockTransport；
        index_url：Argos 模型索引的地址；
        progress_interval：下载进度的最短发布间隔，单位秒；
        clock：单调时钟，测试注入假时钟检查进度节流；
        write_chunk_bytes：攒够多少字节交给线程写盘一次。
    """

    name = "models"

    def __init__(
        self,
        models_dir: Path,
        client: httpx.AsyncClient,
        index_url: str = DEFAULT_INDEX_URL,
        *,
        progress_interval: float = PROGRESS_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        write_chunk_bytes: int = WRITE_CHUNK_BYTES,
    ) -> None:
        self._models_dir = Path(models_dir)
        self._client = client
        self._index_url = index_url
        self._interval = progress_interval
        self._clock = clock
        self._write_chunk = max(1, write_chunk_bytes)
        self._ctx: ModuleContext | None = None
        self._logger = logging.getLogger(__name__)
        self._installed: dict[Pair, list[PackageMeta]] = {}
        self._index: dict[Pair, IndexEntry] = {}
        self._index_loaded = False
        self._refreshing = False
        self._index_error: str | None = None
        self._index_error_code: ModelErrorCode | None = None
        self._downloads: dict[str, DownloadProgress] = {}
        self._queue: deque[str] = deque()
        self._current: str | None = None
        self._current_task: asyncio.Task[None] | None = None
        self._installing = False
        self._worker: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def models_dir(self) -> Path:
        return self._models_dir

    # ------------------------------------------------------------------ 生命周期

    async def start(self, ctx: ModuleContext) -> None:
        """清理上次留下的临时文件，扫描已安装的模型，发布只含已安装模型的 ModelCatalog。"""
        if self._ctx is not None:
            raise RuntimeError("模型管理模块已经启动")
        self._ctx = ctx
        self._logger = ctx.logger
        removed = await blocking(remove_partial_files, self._models_dir, self._logger)
        if removed:
            self._logger.info("已删除 %d 个上次下载中断留下的临时文件", removed)
        self._installed = await blocking(scan_installed, self._models_dir, self._logger)
        self._logger.info("模型目录 %s 中有 %d 个可用的本地翻译模型", self._models_dir, len(self._installed))
        ctx.subscribe(RefreshModelCatalog, self._on_refresh)
        ctx.subscribe(DownloadModels, self._on_download)
        ctx.subscribe(CancelModelDownload, self._on_cancel)
        ctx.subscribe(DeleteModel, self._on_delete)
        self._publish()

    async def stop(self) -> None:
        """取消排队与进行中的下载（删除临时文件）和正在进行的索引获取。重复调用没有效果。"""
        self._closed = True
        self._queue.clear()
        tasks = [
            task
            for task in (self._worker, self._current_task, self._refresh_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ 模型目录

    def catalog(self) -> ModelCatalog:
        """当前的模型目录快照。"""
        return ModelCatalog(
            models=self._models(),
            downloads=dict(self._downloads),
            index_loaded=self._index_loaded,
            refreshing=self._refreshing,
            index_error=self._index_error,
            index_error_code=self._index_error_code,
        )

    def _models(self) -> tuple[ModelInfo, ...]:
        """合并已安装的包与索引：每个语言对一项，按 (source, target) 排序。"""
        infos: list[ModelInfo] = []
        for pair in sorted(self._installed.keys() | self._index.keys()):
            entry = self._index.get(pair)
            packages = self._installed.get(pair)
            best = best_package(packages) if packages else None
            versions = [item.version for item in (entry, best) if item is not None]
            size = entry.size_bytes if entry is not None else None
            if size is None and best is not None:
                size = best.size
            infos.append(
                ModelInfo(
                    id=nmt_model_id(*pair),
                    kind=ModelKind.NMT,
                    source=pair[0],
                    target=pair[1],
                    version=max(versions, key=version_key),
                    size_bytes=size,
                    installed=best is not None,
                    url=entry.url if entry is not None else "",
                    installed_version=best.version if best is not None else None,
                )
            )
        return tuple(infos)

    def _pair_of(self, model_id: str) -> Pair | None:
        for pair in itertools.chain(self._installed, self._index):
            if nmt_model_id(*pair) == model_id:
                return pair
        return None

    def _covered(self, entry: IndexEntry) -> bool:
        """同一语言对已经装有相同或更高版本（需求 INS-07）。"""
        packages = self._installed.get(entry.pair)
        if not packages:
            return False
        return version_key(best_package(packages).version) >= version_key(entry.version)

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        """创建模块自己持有的任务。任务意外抛出的异常记一条日志，不会变成“异常没有被取回”的警告。"""
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._log_task_failure)
        return task

    def _log_task_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.error("后台任务 %s 意外结束", task.get_name(), exc_info=error)

    def _publish(self) -> None:
        if self._ctx is None or self._closed:
            return
        self._ctx.bus.publish(self.catalog())

    def _set(self, model_id: str, progress: DownloadProgress) -> None:
        self._downloads[model_id] = progress
        self._publish()

    # ------------------------------------------------------------------ 模型索引

    async def _on_refresh(self, _command: RefreshModelCatalog) -> None:
        """获取索引。已经在获取时不重复请求，等那一次结束。"""
        if self._closed:
            return
        task = self._refresh_task
        if task is None or task.done():
            task = self._refresh_task = self._spawn(self._refresh(), "models:refresh")
        else:
            self._logger.debug("模型索引正在获取，忽略重复的刷新命令")
        await asyncio.wait({task})

    async def _refresh(self) -> None:
        self._refreshing = True
        self._publish()
        try:
            entries = await self._fetch_index()
        except _Failure as failure:
            self._index_error, self._index_error_code = failure.detail, failure.code
            self._logger.warning("获取模型索引失败（%s）：%s", safe_url(self._index_url), failure.detail)
        except Exception as exc:
            self._index_error, self._index_error_code = describe_error(exc), ModelErrorCode.INTERNAL
            self._logger.exception("获取模型索引时出现未预期的错误")
        else:
            self._index = {entry.pair: entry for entry in entries}
            self._index_loaded = True
            self._index_error = self._index_error_code = None
            self._logger.info("模型索引中有 %d 个可下载的本地翻译模型", len(entries))
        finally:
            self._refreshing = False
        self._publish()

    async def _fetch_index(self) -> list[IndexEntry]:
        body = bytearray()
        try:
            async with self._client.stream(
                "GET", self._index_url, timeout=INDEX_TIMEOUT, follow_redirects=True
            ) as response:
                if response.status_code != _HTTP_OK:
                    raise _Failure(ModelErrorCode.HTTP, f"HTTP {response.status_code}")
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_INDEX_BYTES:
                        raise _Failure(ModelErrorCode.BAD_INDEX, "模型索引大小超出上限")
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise _Failure(classify_transport_error(exc), describe_error(exc)) from exc
        try:
            data: object = json.loads(bytes(body))
        except ValueError as exc:
            raise _Failure(ModelErrorCode.BAD_INDEX, "模型索引不是合法的 JSON") from exc
        try:
            return parse_index(data, self._logger)
        except IndexFormatError as exc:
            raise _Failure(ModelErrorCode.BAD_INDEX, str(exc)) from exc

    # ------------------------------------------------------------------ 下载

    async def _on_download(self, command: DownloadModels) -> None:
        """把模型加入队列，需要时启动下载任务，并等队列清空后再返回。"""
        if self._closed:
            return
        changed = [self._enqueue(model_id) for model_id in dict.fromkeys(command.ids)]
        if any(changed):
            self._publish()
        worker = self._worker
        if self._queue and (worker is None or worker.done()):
            worker = self._worker = self._spawn(self._run_queue(), "models:downloads")
        if worker is not None and not worker.done():
            await asyncio.wait({worker})

    def _enqueue(self, model_id: str) -> bool:
        """把一个模型放进队列，返回下载状态是否有变化。"""
        progress = self._downloads.get(model_id)
        if progress is not None and progress.state in ACTIVE_DOWNLOAD_STATES:
            self._logger.debug("模型 %s 已在下载队列中", model_id)
            return False
        pair = self._pair_of(model_id)
        entry = self._index.get(pair) if pair is not None else None
        if entry is None:
            detail = "模型索引里没有这个模型" if self._index_loaded else "还没有获取模型索引"
            self._logger.warning("无法下载模型 %s：%s", model_id, detail)
            self._downloads[model_id] = DownloadProgress(
                DownloadState.FAILED, error=detail, error_code=ModelErrorCode.UNAVAILABLE
            )
            return True
        if self._covered(entry):
            self._logger.info(
                "已安装 %s→%s 的相同或更高版本，不重复下载 %s", entry.source, entry.target, entry.version
            )
            return False
        self._downloads[model_id] = DownloadProgress(DownloadState.QUEUED, total_bytes=entry.size_bytes)
        self._queue.append(model_id)
        return True

    async def _run_queue(self) -> None:
        """逐个下载队列里的模型。每个模型在自己的任务里下载，取消一个不影响后面的。"""
        while self._queue and not self._closed:
            model_id = self._queue.popleft()
            pair = self._pair_of(model_id)
            entry = self._index.get(pair) if pair is not None else None
            if entry is None:
                # 排队期间重新获取了索引，新索引里没有这个模型。
                self._set(
                    model_id,
                    DownloadProgress(
                        DownloadState.FAILED,
                        error="模型索引里没有这个模型",
                        error_code=ModelErrorCode.UNAVAILABLE,
                    ),
                )
                continue
            if self._covered(entry):
                self._set(model_id, DownloadProgress(DownloadState.DONE))
                continue
            task = self._spawn(self._download(model_id, entry), f"models:download:{model_id}")
            self._current, self._current_task = model_id, task
            try:
                await asyncio.wait({task})
            finally:
                self._current = None
                self._current_task = None
            progress = self._downloads.get(model_id)
            if task.cancelled() and progress is not None and progress.state in ACTIVE_DOWNLOAD_STATES:
                # 任务在第一次运行之前就被取消，_download 一行也没有执行，状态还停在“排队”。
                self._set(model_id, DownloadProgress(DownloadState.CANCELLED))

    def _on_cancel(self, command: CancelModelDownload) -> None:
        model_id = command.id
        if model_id in self._queue:
            self._queue.remove(model_id)
            self._logger.info("已取消排队中的模型 %s", model_id)
            self._set(model_id, DownloadProgress(DownloadState.CANCELLED))
            return
        task = self._current_task
        if model_id == self._current and task is not None and not task.done():
            if self._installing:
                # 校验已经通过、正在改名：此时取消只会留下状态不一致的模型目录。
                self._logger.info("模型 %s 正在安装，不能再取消", model_id)
                return
            task.cancel()  # 下载任务自己删除临时文件并发布“已取消”
            return
        self._logger.debug("模型 %s 没有在下载，忽略取消命令", model_id)

    async def _download(self, model_id: str, entry: IndexEntry) -> None:
        """下载、校验并安装一个模型。失败只记录在下载状态里，不抛出；取消时清理后把取消传出去。"""
        stem = package_stem(entry.from_code, entry.to_code, entry.version)
        part = self._models_dir / f"{stem}{PART_SUFFIX}"
        final = self._models_dir / f"{stem}{PACKAGE_SUFFIX}"
        self._logger.info(
            "开始下载模型 %s→%s %s：%s", entry.source, entry.target, entry.version, safe_url(entry.url)
        )
        self._set(model_id, DownloadProgress(DownloadState.DOWNLOADING, 0, entry.size_bytes))
        done, total = 0, entry.size_bytes
        try:
            done, total = await self._fetch_package(model_id, entry, part)
            self._set(model_id, DownloadProgress(DownloadState.VERIFYING, done, total))
            meta = await blocking(verify_download, part, entry.pair)
            if meta.version != entry.version:
                self._logger.warning(
                    "模型包 %s 的版本是 %r，模型索引中是 %r", final.name, meta.version, entry.version
                )
            obsolete = [package.path for package in self._installed.get(entry.pair, [])]
            # 从改名开始到重新扫描结束都不接受取消：模型包一旦就位，这次下载就必须以“完成”收尾，
            # 否则目录不更新、ModelsInstalled 不发布，而磁盘上新包已经装好、旧版本已被删除。
            self._installing = True
            try:
                await blocking(install_package, part, final, obsolete, self._logger)
                self._installed = await blocking(scan_installed, self._models_dir, self._logger)
            finally:
                self._installing = False
        except asyncio.CancelledError:
            await self._discard(part)
            self._logger.info("已取消模型 %s 的下载", model_id)
            self._set(model_id, DownloadProgress(DownloadState.CANCELLED))
            raise
        except (_Failure, InvalidPackage) as failure:
            await self._fail(model_id, part, failure.code, failure.detail, done, total)
            return
        except OSError as exc:
            await self._fail(model_id, part, ModelErrorCode.DISK, describe_error(exc), done, total)
            return
        except Exception as exc:
            self._logger.exception("下载模型 %s 时出现未预期的错误", model_id)
            await self._fail(model_id, part, ModelErrorCode.INTERNAL, describe_error(exc), done, total)
            return
        self._logger.info("已安装模型 %s→%s %s", entry.source, entry.target, entry.version)
        self._set(model_id, DownloadProgress(DownloadState.DONE, done, total))
        if not self._closed and self._ctx is not None:
            self._ctx.bus.publish(ModelsInstalled((model_id,)))

    async def _fetch_package(self, model_id: str, entry: IndexEntry, part: Path) -> tuple[int, int | None]:
        """把模型包流式写入临时文件，返回（收到的字节数，总字节数）。"""
        await blocking(self._models_dir.mkdir, parents=True, exist_ok=True)
        sink = await blocking(open_for_write, part)
        try:
            async with self._client.stream(
                "GET", entry.url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as response:
                if response.status_code != _HTTP_OK:
                    raise _Failure(ModelErrorCode.HTTP, f"HTTP {response.status_code}")
                total = content_length(response) or entry.size_bytes
                self._set(model_id, DownloadProgress(DownloadState.DOWNLOADING, 0, total))
                last = self._clock()

                def report(received: int) -> None:
                    nonlocal last
                    now = self._clock()
                    if now - last >= self._interval:
                        last = now
                        self._set(model_id, DownloadProgress(DownloadState.DOWNLOADING, received, total))

                done = await stream_to_file(response, sink, chunk_bytes=self._write_chunk, on_progress=report)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise _Failure(classify_transport_error(exc), describe_error(exc)) from exc
        finally:
            await blocking(sink.close)
        return done, total if total is not None else done

    async def _fail(
        self, model_id: str, part: Path, code: ModelErrorCode, detail: str, done: int, total: int | None
    ) -> None:
        await self._discard(part)
        self._logger.warning("下载模型 %s 失败（%s）：%s", model_id, code.value, detail)
        self._set(
            model_id,
            DownloadProgress(DownloadState.FAILED, done, total, error=detail, error_code=code),
        )

    async def _discard(self, part: Path) -> None:
        try:
            await blocking(part.unlink, missing_ok=True)
        except OSError:
            self._logger.warning("无法删除临时文件 %s", part.name, exc_info=True)

    # ------------------------------------------------------------------ 删除

    async def _on_delete(self, command: DeleteModel) -> None:
        """删除该语言对的全部模型包，让 NMT 进程不再看到任何一个版本。"""
        if self._closed:
            return
        pair = self._pair_of(command.id)
        packages = self._installed.get(pair, []) if pair is not None else []
        if not packages:
            self._logger.warning("模型 %s 没有安装，忽略删除命令", command.id)
            return
        deleted = await blocking(delete_files, [package.path for package in packages], self._logger)
        self._installed = await blocking(scan_installed, self._models_dir, self._logger)
        progress = self._downloads.get(command.id)
        if progress is not None and progress.state not in ACTIVE_DOWNLOAD_STATES:
            del self._downloads[command.id]
        self._logger.info("已删除模型 %s 的 %d 个模型包", command.id, deleted)
        self._publish()
        if deleted and not self._closed and self._ctx is not None:
            self._ctx.bus.publish(ModelsInstalled((), removed=(command.id,)))

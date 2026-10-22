"""本地翻译显卡运行库模块：cuBLAS 12 的下载、安装与删除（需求 MOD-06，架构文档 8.3.1 节）。

模块启动时清理上次中断留下的临时文件，扫描已装的版本，检测本机的 NVIDIA 显卡，发布 ``NmtRuntimeInfo``；
之后处理三个命令：

- ``InstallNmtRuntime``：流式下载 wheel 到 ``<root>/<文件名>.part``，边下边算 SHA-256 → 核对大小与摘要 →
  解压两个 DLL 与许可证（见 package.py）→ 发布 ``NmtRuntimeChanged(installed=True)``，
  组合根据此重启 NMT 进程。
  下载期间可以取消；进入解压阶段后不再接受取消，解压大约要几秒。
- ``CancelNmtRuntimeInstall``：取消下载，删除临时文件，回到之前的状态。
- ``DeleteNmtRuntime``：先调用组合根给的 ``before_delete``（停止 NMT 进程，DLL 被加载时删不掉），再删除全部
  版本目录，发布 ``NmtRuntimeChanged(installed=False)``；组合根随后按配置重新启动 NMT 进程。

程序启动时不联网，只有用户点“下载”才发请求（需求 PRV-01）。模块状态只在事件循环线程上读写，磁盘操作放进
线程池，下载用 httpx 的异步流式接口。
"""

import asyncio
import hashlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Final

import httpx

from lst.contracts.nmt_runtime import (
    CancelNmtRuntimeInstall,
    DeleteNmtRuntime,
    InstallNmtRuntime,
    NmtRuntimeChanged,
    NmtRuntimeErrorCode,
    NmtRuntimeInfo,
    NmtRuntimeState,
)
from lst.kernel import ModuleContext
from lst.services._shared.aio import blocking
from lst.services._shared.download import content_length, open_for_write, stream_to_file
from lst.services._shared.gpu import GpuProbe, probe_adapters, safe_probe
from lst.services._shared.net import DOWNLOAD_TIMEOUT, classify_transport_error, describe_error, safe_url
from lst.services.nmt_runtime.package import (
    CUBLAS_WHEEL,
    PART_SUFFIX,
    InstalledRuntime,
    InvalidRuntime,
    WheelSpec,
    delete_all,
    install_wheel,
    remove_leftovers,
    scan_installed,
    verify_wheel,
)

PROGRESS_INTERVAL_S: Final = 0.2
"""下载进度的发布间隔。"""

WRITE_CHUNK_BYTES: Final = 1 << 20
_HTTP_OK: Final = 200


class _Failure(Exception):
    def __init__(self, code: NmtRuntimeErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


async def _nothing() -> None:
    return None


class NmtRuntimeModule:
    """显卡运行库的下载、安装与删除，实现 kernel 的 Module 协议。

    构造参数：
        root：运行库目录（模型目录下的 runtimes/cublas12）；
        client：共用的 httpx 客户端，测试注入 MockTransport；
        spec：要下载的 wheel，测试注入小文件；
        before_delete：删除前调用，组合根在这里停止 NMT 进程；
        probe：显卡检测，测试注入假的显卡列表；
        progress_interval、clock：下载进度的发布节流。
    """

    name = "nmt_runtime"

    def __init__(
        self,
        root: Path,
        client: httpx.AsyncClient,
        *,
        spec: WheelSpec = CUBLAS_WHEEL,
        before_delete: Callable[[], Awaitable[None]] = _nothing,
        probe: GpuProbe = probe_adapters,
        progress_interval: float = PROGRESS_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._root = Path(root)
        self._client = client
        self._spec = spec
        self._before_delete = before_delete
        self._probe = probe
        self._interval = progress_interval
        self._clock = clock
        self._ctx: ModuleContext | None = None
        self._logger = logging.getLogger(__name__)
        self._installed: InstalledRuntime | None = None
        self._info = NmtRuntimeInfo(version=spec.version, download_bytes=spec.size)
        self._task: asyncio.Task[None] | None = None
        self._unpacking = False
        self._closed = False

    @property
    def root(self) -> Path:
        return self._root

    def info(self) -> NmtRuntimeInfo:
        return self._info

    # ------------------------------------------------------------------ 生命周期

    async def start(self, ctx: ModuleContext) -> None:
        if self._ctx is not None:
            raise RuntimeError("显卡运行库模块已经启动")
        self._ctx = ctx
        self._logger = ctx.logger
        await blocking(self._root.mkdir, parents=True, exist_ok=True)
        removed = await blocking(remove_leftovers, self._root)
        if removed:
            self._logger.info("已删除 %d 个上次下载显卡运行库中断留下的临时文件", removed)
        self._installed = await blocking(scan_installed, self._root)
        adapters = await blocking(safe_probe, self._probe, self._logger)
        gpus = tuple(adapter.name for adapter in adapters if adapter.nvidia)
        self._info = replace(self._info, gpus=gpus)
        if self._installed is not None:
            self._logger.info(
                "已装显卡运行库 cuBLAS %s：%s", self._installed.version, self._installed.bin_dir
            )
        self._info = self._settled()
        ctx.subscribe(InstallNmtRuntime, self._on_install)
        ctx.subscribe(CancelNmtRuntimeInstall, self._on_cancel)
        ctx.subscribe(DeleteNmtRuntime, self._on_delete)
        self._publish()

    async def stop(self) -> None:
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    # ------------------------------------------------------------------ 状态

    def _settled(self) -> NmtRuntimeInfo:
        """不在下载中的状态：按是否已装给出 INSTALLED 或 NOT_INSTALLED，清掉进度与错误。"""
        installed = self._installed
        return replace(
            self._info,
            state=NmtRuntimeState.INSTALLED if installed is not None else NmtRuntimeState.NOT_INSTALLED,
            installed_version=installed.version if installed is not None else None,
            installed_bytes=installed.size if installed is not None else 0,
            progress=None,
            error_code=None,
            detail="",
        )

    def _set(self, info: NmtRuntimeInfo) -> None:
        self._info = info
        self._publish()

    def _publish(self) -> None:
        if self._ctx is not None and not self._closed:
            self._ctx.bus.publish(self._info)

    def _busy(self) -> bool:
        return self._task is not None and not self._task.done()

    # ------------------------------------------------------------------ 安装

    def _on_install(self, _command: InstallNmtRuntime) -> None:
        if self._closed or self._busy():
            return
        if self._installed is not None and self._installed.version == self._spec.version:
            return
        self._task = asyncio.create_task(self._install(), name="nmt-runtime-install")

    def _on_cancel(self, _command: CancelNmtRuntimeInstall) -> None:
        if self._busy() and not self._unpacking and self._task is not None:
            self._task.cancel()

    async def _install(self) -> None:
        spec = self._spec
        part = self._root / f"{spec.file_name}{PART_SUFFIX}"
        self._logger.info("开始下载显卡运行库：%s", safe_url(spec.url))
        self._set(replace(self._info, state=NmtRuntimeState.DOWNLOADING, progress=0.0, error_code=None))
        try:
            digest = await self._fetch(part)
            self._set(replace(self._info, state=NmtRuntimeState.INSTALLING, progress=None))
            await blocking(verify_wheel, part, spec, digest)
            self._unpacking = True
            try:
                self._installed = await blocking(install_wheel, part, self._root, spec)
            finally:
                self._unpacking = False
        except asyncio.CancelledError:
            await self._discard(part)
            self._logger.info("已取消显卡运行库的下载")
            if not self._closed:
                self._set(self._settled())
            raise
        except _Failure as failure:
            await self._fail(part, failure.code, failure.detail)
            return
        except InvalidRuntime as exc:
            await self._fail(part, NmtRuntimeErrorCode.INVALID_PACKAGE, str(exc))
            return
        except OSError as exc:
            await self._fail(part, NmtRuntimeErrorCode.DISK, describe_error(exc))
            return
        except Exception as exc:
            self._logger.exception("安装显卡运行库时出现未预期的错误")
            await self._fail(part, NmtRuntimeErrorCode.INTERNAL, describe_error(exc))
            return
        installed = self._installed
        assert installed is not None
        self._logger.info("已安装显卡运行库 cuBLAS %s：%s", installed.version, installed.bin_dir)
        self._set(self._settled())
        if not self._closed and self._ctx is not None:
            self._ctx.bus.publish(NmtRuntimeChanged(installed=True, bin_dir=str(installed.bin_dir)))

    async def _fetch(self, part: Path) -> str:
        """把 wheel 流式写入临时文件，返回 SHA-256。"""
        spec = self._spec
        digest = hashlib.sha256()
        sink = await blocking(open_for_write, part)
        try:
            async with self._client.stream(
                "GET", spec.url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as response:
                if response.status_code != _HTTP_OK:
                    raise _Failure(NmtRuntimeErrorCode.HTTP, f"HTTP {response.status_code}")
                total = content_length(response) or spec.size
                last = self._clock()

                def report(received: int) -> None:
                    nonlocal last
                    now = self._clock()
                    if now - last >= self._interval:
                        last = now
                        self._set(replace(self._info, progress=min(1.0, received / total)))

                await stream_to_file(
                    response, sink, chunk_bytes=WRITE_CHUNK_BYTES, on_progress=report, digest=digest
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            code = classify_transport_error(
                exc, timeout=NmtRuntimeErrorCode.TIMEOUT, network=NmtRuntimeErrorCode.NETWORK
            )
            raise _Failure(code, describe_error(exc)) from exc
        finally:
            await blocking(sink.close)
        return digest.hexdigest()

    async def _fail(self, part: Path, code: NmtRuntimeErrorCode, detail: str) -> None:
        await self._discard(part)
        self._logger.warning("安装显卡运行库失败（%s）：%s", code.value, detail)
        if not self._closed:
            self._set(replace(self._settled(), state=NmtRuntimeState.ERROR, error_code=code, detail=detail))

    async def _discard(self, part: Path) -> None:
        try:
            await blocking(part.unlink, missing_ok=True)
        except OSError:
            self._logger.warning("无法删除临时文件 %s", part.name, exc_info=True)

    # ------------------------------------------------------------------ 删除

    async def _on_delete(self, _command: DeleteNmtRuntime) -> None:
        if self._closed or self._busy() or self._installed is None:
            return
        self._task = asyncio.create_task(self._delete(), name="nmt-runtime-delete")

    async def _delete(self) -> None:
        await self._before_delete()
        try:
            await blocking(delete_all, self._root)
        except OSError as exc:
            self._logger.warning("删除显卡运行库失败：%s", describe_error(exc))
            self._installed = await blocking(scan_installed, self._root)
            failed = replace(
                self._settled(),
                state=NmtRuntimeState.ERROR,
                error_code=NmtRuntimeErrorCode.DISK,
                detail=describe_error(exc),
            )
            self._set(failed)
        else:
            self._installed = None
            self._logger.info("已删除显卡运行库")
            self._set(self._settled())
        if not self._closed and self._ctx is not None:
            installed = self._installed
            self._ctx.bus.publish(
                NmtRuntimeChanged(
                    installed=installed is not None, bin_dir=str(installed.bin_dir) if installed else ""
                )
            )

r"""OCR 引擎的安装管理模块（需求 OCRE-01 至 OCRE-09，lst/contracts/ocr_engines.py）。

模块发布 ``OcrEngineCatalog``，处理 ``InstallOcrEngine``、``CancelOcrEngineInstall``、``UninstallOcrEngine``、
``DownloadOcrModel``、``CancelOcrModelDownload``、``DeleteOcrModel``，需要下载的引擎装好或卸载后发布
``OcrEnginesChanged``。

三个引擎，在目录里按 OcrEngineKind 的定义顺序排列：

- windows：不需要下载。启动时读取系统 OCR 的可识别语言，有语言为 INSTALLED，没有为 UNAVAILABLE
  （NO_LANGUAGE）。用户装了语言包之后，对 windows 发 InstallOcrEngine 会重新读取一次（windows.py）；
- rapid：RapidOCR，安装版按需下载运行环境（默认模型在 wheel 里）与其余模型，
  源码版只检查 .venv-ocr（rapid.py）；
  组合根没有传入 rapid 的路径时，目录里没有这一项，对它的命令被忽略；
- paddle：PaddleOCR，安装版按需下载运行环境与模型，源码版只检查开发环境（paddle.py）。

两个需要下载的引擎共用 installer.py 的流程（状态、版本、进度、uv、取消、失败清理、卸载、模型下载），
彼此独立：各有自己的引擎目录、安装任务与忙碌标志，可以同时安装。

运行环境版本（OCRE-07）：启动时在线程里用 DXGI 检测一次显卡（gpu.py，可以注入假的检测函数），据此给出每个
引擎的 recommended_variant。InstallOcrEngine 的 variant 为 None 时安装推荐版本。

模型（OCRE-08）：每个引擎的 models 列出各环节的可选模型；当前选用哪个由配置节 ``[ocr_rapid]``、
``[ocr_paddle]``
的 ``<环节>_model`` 字段决定（字段约定见 lst/services/ocr/config.py），模块订阅 AppConfig 读取这些字段，
标出 selected，并拒绝删除选用中的模型。

进程相关的状态（LOADING、READY、进程出错的 ERROR）不在这里计算。组合根按 OCR 进程的 WorkerStatus
算出状态后调用 ``set_process_state``，模块把它合并到当前引擎的条目里：只有当前引擎、并且安装状态是
INSTALLED 时才合并，安装状态本身优先。

线程：模块状态只在事件循环线程上读写；扫描、解压、改名、删除、写文件都放进线程（_shared.aio.blocking）。
"""

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Mapping
from dataclasses import dataclass, replace
from typing import Any

import httpx

from lst.contracts.config import AppConfig
from lst.contracts.ocr_engines import (
    CancelOcrEngineInstall,
    CancelOcrModelDownload,
    DeleteOcrModel,
    DownloadOcrModel,
    InstallOcrEngine,
    OcrEngineCatalog,
    OcrEngineErrorCode,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEnginesChanged,
    OcrEngineState,
    OcrStage,
    UninstallOcrEngine,
)
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import ModuleContext
from lst.services._shared.aio import blocking
from lst.services._shared.gpu import GpuAdapter, GpuProbe, probe_adapters, safe_probe
from lst.services.ocr_engines.catalog import (
    PADDLE_AUXILIARY,
    PADDLE_LANGUAGES,
    PADDLE_MODELS,
    RAPID_MODELS,
    V6_LANGUAGES,
    ModelSpec,
)
from lst.services.ocr_engines.installer import (
    POLL_INTERVAL_S,
    PROGRESS_INTERVAL_S,
    WRITE_CHUNK_BYTES,
    BeforeRuntimeUpdate,
    BeforeUninstall,
    InstallOptions,
    RuntimeEngine,
)
from lst.services.ocr_engines.layout import (
    DEFAULT_MODEL_BASE_URL,
    DOWNLOAD_BYTES,
    RUNTIME_BYTES,
    DevPaddleEnvironment,
    DevRapidEnvironment,
    PaddleInstallPaths,
    RapidInstallPaths,
)
from lst.services.ocr_engines.paddle import PaddleEngine
from lst.services.ocr_engines.rapid import RapidEngine
from lst.services.ocr_engines.runtime import CommandRunner, run_command
from lst.services.ocr_engines.windows import LanguageProbe, probe_windows_languages, windows_engine_info

__all__ = ["BeforeRuntimeUpdate", "BeforeUninstall", "OcrEnginesModule"]

PIPELINE_SECTION: dict[OcrEngineKind, str] = {
    OcrEngineKind.RAPID: "ocr_rapid",
    OcrEngineKind.PADDLE: "ocr_paddle",
}
"""引擎 → 流水线配置节名（与 lst/services/ocr/config.py 的 PIPELINE_SECTIONS 相同）。"""

RAPID_STAGE_KEYS: dict[OcrStage, str] = {
    OcrStage.DETECTION: "det_model_path",
    OcrStage.RECOGNITION: "rec_model_path",
    OcrStage.TEXTLINE_ORIENTATION: "cls_model_path",
}
"""RapidOCR 后端读取的模型路径键（lst/workers/ocr_worker/rapid_backend.py）。"""


def _variant_bytes(kind: OcrEngineKind, table: Mapping[tuple[OcrEngineKind, Any], int]) -> dict[Any, int]:
    return {variant: size for (engine, variant), size in table.items() if engine is kind}


@dataclass(frozen=True, slots=True)
class _Overlay:
    kind: OcrEngineKind
    state: OcrEngineState
    error_code: OcrEngineErrorCode | None
    detail: str


class _Host:
    """引擎对象看到的宿主（installer.EngineHost），把调用转给模块。"""

    def __init__(self, module: "OcrEnginesModule") -> None:
        self._module = module

    @property
    def logger(self) -> logging.Logger:
        return self._module._logger

    @property
    def closed(self) -> bool:
        return self._module._closed

    def publish(self) -> None:
        self._module._publish()

    def announce(self, event: OcrEnginesChanged) -> None:
        self._module._announce(event)

    def spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        return self._module._spawn(coro, name)


class OcrEnginesModule:
    """OCR 引擎的目录、安装、版本、模型、取消与卸载，实现 kernel 的 Module 协议。

    构造参数：
        paddle：安装版传 PaddleInstallPaths（引擎目录、模型缓存、uv、Python、版本清单目录），
            源码版传 DevPaddleEnvironment（.venv-ocr 与候选模型缓存）；
        client：共用的 httpx 客户端，测试注入 MockTransport；
        rapid：安装版传 RapidInstallPaths（引擎目录、模型目录、uv、Python、版本清单目录），源码版传
            DevRapidEnvironment（.venv-ocr）；None 表示不提供 RapidOCR，目录里没有这一项；
        paddle_models、rapid_models：模型目录（catalog.py），测试注入小目录；
        model_base_url：PaddleX 模型下载地址前缀；
        index_url：uv 使用的包索引（镜像），None 用 uv 的默认值 PyPI；
        runner：外部命令运行器，测试注入假运行器；
        windows_probe：读取 Windows OCR 语言的函数，测试注入假函数；
        gpu_probe：枚举显卡的函数（gpu.probe_adapters），测试注入假适配器列表；
        environ：uv 子进程的基础环境变量，None 时取 os.environ；
        before_uninstall：卸载前由组合根执行的操作（停 OCR 进程）；
        before_runtime_update：替换已有运行环境之前由组合根执行的操作（停 OCR 进程），见 BeforeRuntimeUpdate；
        runtime_bytes、download_bytes：各引擎各版本运行环境装好后的大小（进度权重）与下载大小（界面显示），
            键是 (引擎, 版本)，缺省用 layout.RUNTIME_BYTES、layout.DOWNLOAD_BYTES；
        progress_interval、poll_interval、clock、write_chunk_bytes：进度节流、uv 缓存统计间隔、
            单调时钟与写盘块大小，测试用来控制时间。
    """

    name = "ocr_engines"

    def __init__(
        self,
        paddle: PaddleInstallPaths | DevPaddleEnvironment,
        client: httpx.AsyncClient,
        *,
        rapid: RapidInstallPaths | DevRapidEnvironment | None = None,
        paddle_models: tuple[ModelSpec, ...] = PADDLE_MODELS + PADDLE_AUXILIARY,
        rapid_models: tuple[ModelSpec, ...] = RAPID_MODELS,
        model_base_url: str = DEFAULT_MODEL_BASE_URL,
        index_url: str | None = None,
        runner: CommandRunner = run_command,
        windows_probe: LanguageProbe = probe_windows_languages,
        gpu_probe: GpuProbe = probe_adapters,
        environ: Mapping[str, str] | None = None,
        before_uninstall: BeforeUninstall | None = None,
        before_runtime_update: BeforeRuntimeUpdate | None = None,
        runtime_bytes: Mapping[tuple[OcrEngineKind, Any], int] = RUNTIME_BYTES,
        download_bytes: Mapping[tuple[OcrEngineKind, Any], int] = DOWNLOAD_BYTES,
        progress_interval: float = PROGRESS_INTERVAL_S,
        poll_interval: float = POLL_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        write_chunk_bytes: int = WRITE_CHUNK_BYTES,
    ) -> None:
        options = InstallOptions(
            runner=runner,
            environ=environ,
            index_url=index_url,
            before_uninstall=before_uninstall,
            before_runtime_update=before_runtime_update,
            progress_interval=progress_interval,
            poll_interval=poll_interval,
            clock=clock,
            write_chunk_bytes=max(1, write_chunk_bytes),
        )
        self._probe = windows_probe
        self._gpu_probe = gpu_probe
        self._adapters: tuple[GpuAdapter, ...] = ()
        self._ctx: ModuleContext | None = None
        self._logger = logging.getLogger(__name__)
        self._closed = False
        self._windows = OcrEngineInfo(OcrEngineKind.WINDOWS, OcrEngineState.UNAVAILABLE, needs_download=False)
        self._active: OcrEngineKind | None = None
        self._running: OcrEngineKind | None = None
        self._overlay: _Overlay | None = None
        host = _Host(self)
        engines: list[RuntimeEngine] = []
        if rapid is not None:
            engines.append(
                RapidEngine(
                    rapid,
                    host,
                    options,
                    client=client,
                    models=rapid_models,
                    runtime_bytes=_variant_bytes(OcrEngineKind.RAPID, runtime_bytes),
                    download_bytes=_variant_bytes(OcrEngineKind.RAPID, download_bytes),
                    languages=V6_LANGUAGES,
                )
            )
        engines.append(
            PaddleEngine(
                paddle,
                host,
                options,
                client=client,
                models=paddle_models,
                model_base_url=model_base_url,
                runtime_bytes=_variant_bytes(OcrEngineKind.PADDLE, runtime_bytes),
                download_bytes=_variant_bytes(OcrEngineKind.PADDLE, download_bytes),
                languages=PADDLE_LANGUAGES,
            )
        )
        self._engines: dict[OcrEngineKind, RuntimeEngine] = {engine.kind: engine for engine in engines}

    # ------------------------------------------------------------------ 发布与后台任务

    def _publish(self) -> None:
        if self._ctx is None or self._closed:
            return
        self._ctx.bus.publish(self.catalog())

    def _announce(self, event: OcrEnginesChanged) -> None:
        if self._ctx is not None and not self._closed:
            self._ctx.bus.publish(event)

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(self._log_task_failure)
        return task

    def _log_task_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.error("后台任务 %s 意外结束", task.get_name(), exc_info=error)

    # ------------------------------------------------------------------ 生命周期

    async def start(self, ctx: ModuleContext) -> None:
        """读取 Windows OCR 语言、检测显卡、清理半成品、检查各引擎是否已装，然后发布 OcrEngineCatalog。"""
        if self._ctx is not None:
            raise RuntimeError("OCR 引擎模块已经启动")
        self._ctx = ctx
        self._logger = ctx.logger
        self._windows = await blocking(windows_engine_info, self._probe, self._logger)
        self._adapters = await blocking(safe_probe, self._gpu_probe, self._logger)
        for engine in self._engines.values():
            engine.set_adapters(self._adapters)
            await engine.refresh(cleanup=True)
        ctx.subscribe(InstallOcrEngine, self._on_install)
        ctx.subscribe(CancelOcrEngineInstall, self._on_cancel)
        ctx.subscribe(UninstallOcrEngine, self._on_uninstall)
        ctx.subscribe(DownloadOcrModel, self._on_download_model)
        ctx.subscribe(CancelOcrModelDownload, self._on_cancel_model)
        ctx.subscribe(DeleteOcrModel, self._on_delete_model)
        ctx.subscribe(AppConfig, self._on_config)
        self._publish()

    async def stop(self) -> None:
        """取消进行中的安装与模型下载（删除半成品）。重复调用没有效果。"""
        self._closed = True
        for engine in self._engines.values():
            await engine.stop()

    # ------------------------------------------------------------------ 目录

    @property
    def adapters(self) -> tuple[GpuAdapter, ...]:
        """启动时检测到的显卡。"""
        return self._adapters

    @property
    def download_bytes(self) -> int:
        """PaddleOCR 全部下载内容（运行环境与默认组合的模型）的估计大小。"""
        return self._engines[OcrEngineKind.PADDLE].download_bytes

    def catalog(self) -> OcrEngineCatalog:
        """当前的引擎目录快照，已合并组合根给出的进程状态。"""
        infos = [self._windows, *(engine.info() for engine in self._engines.values())]
        order = list(OcrEngineKind)
        infos.sort(key=lambda info: order.index(info.kind))
        engines = tuple(self._apply_overlay(info) for info in infos)
        return OcrEngineCatalog(engines=engines, active=self._active, running=self._running)

    def _apply_overlay(self, info: OcrEngineInfo) -> OcrEngineInfo:
        overlay = self._overlay
        if (
            overlay is None
            or overlay.kind is not info.kind
            or info.kind is not (self._running or self._active)
            or info.state is not OcrEngineState.INSTALLED
        ):
            return info
        return replace(
            info,
            state=overlay.state,
            progress=None,
            error_code=overlay.error_code if overlay.state is OcrEngineState.ERROR else None,
            detail=overlay.detail or info.detail,
        )

    def set_running(self, kind: OcrEngineKind | None) -> None:
        """组合根决定 OCR 进程实际运行的引擎后调用，None 表示没有进程。只在事件循环线程上调用。"""
        if kind is self._running:
            return
        self._running = kind
        self._publish()

    def can_run(self, kind: OcrEngineKind) -> bool:
        """引擎是否已经可以启动 OCR 进程：Windows 有 OCR 语言、RapidOCR 或 PaddleOCR 已装好。

        只看安装状态，不看组合根合并进来的进程状态（进程出错不代表引擎没装好）。目录里没有的引擎为 False。
        """
        if kind is OcrEngineKind.WINDOWS:
            return self._windows.state is OcrEngineState.INSTALLED
        engine = self._engines.get(kind)
        return engine is not None and engine.can_run()

    def installed_variant(self, kind: OcrEngineKind) -> str:
        """已装的运行环境版本（取值同 OcrRuntimeVariant），没有安装或不需要下载的引擎为空串。"""
        engine = self._engines.get(kind)
        variant = engine.installed_variant if engine is not None else None
        return variant.value if variant is not None else ""

    def selected_models_ready(self, kind: OcrEngineKind) -> tuple[tuple[str, bool], ...]:
        """各环节选用的模型及其是否就位（最近一次扫描的结果），组合根把它算进进程决策的依据。"""
        engine = self._engines.get(kind)
        if engine is None:
            return ()
        return tuple(
            (model_id, engine.model_ready_now(model_id))
            for _stage, model_id in sorted(engine.selection.items(), key=lambda item: item[0].value)
        )

    def worker_model_settings(self, kind: OcrEngineKind) -> dict[str, JsonValue]:
        """OCR 进程需要、配置里没有的模型信息：RapidOCR 是各环节选用的模型文件的路径与识别模型覆盖的语言。

        PaddleOCR 按模型名在模型缓存里找模型，这里返回空字典。
        """
        engine = self._engines.get(kind)
        if not isinstance(engine, RapidEngine):
            return {}
        specs = {spec.id: spec for spec in engine.model_specs()}
        settings: dict[str, JsonValue] = {}
        for stage, key in RAPID_STAGE_KEYS.items():
            spec = specs.get(engine.selection.get(stage, ""))
            if spec is not None:
                settings[key] = str(engine.model_path(spec))
        languages: list[JsonValue] = list(engine.languages())
        settings["recognition_languages"] = languages
        return settings

    def set_process_state(
        self,
        kind: OcrEngineKind,
        state: OcrEngineState | None,
        error_code: OcrEngineErrorCode | None = None,
        detail: str = "",
    ) -> None:
        """组合根按 OCR 进程的状态调用：state 取 LOADING、READY、ERROR，None 表示清除（进程已停止）。

        只在事件循环线程上调用。只对当前引擎、且安装状态为 INSTALLED 时生效。
        """
        overlay = None if state is None else _Overlay(kind, state, error_code, detail)
        if overlay == self._overlay:
            return
        self._overlay = overlay
        self._publish()

    def _on_config(self, config: AppConfig) -> None:
        """从 [ocr] 节取当前引擎（enabled 为假时为 None），从各引擎的流水线配置节取选用的模型。

        字段缺失时按配置模型的默认值（启用、windows、各环节的默认模型）。
        """
        raw = config.section("ocr")
        section: Mapping[str, object] = raw if isinstance(raw, dict) else {}
        active: OcrEngineKind | None
        if section.get("enabled", True) is False:
            active = None
        else:
            try:
                active = OcrEngineKind(str(section.get("engine", OcrEngineKind.WINDOWS.value)))
            except ValueError:
                active = None
        changed = active is not self._active
        self._active = active
        for kind, engine in self._engines.items():
            pipeline = config.section(PIPELINE_SECTION[kind])
            values: Mapping[str, object] = pipeline if isinstance(pipeline, dict) else {}
            selection = {
                stage: str(values[f"{stage.value}_model"])
                for stage in engine.stage_defaults
                if isinstance(values.get(f"{stage.value}_model"), str)
            }
            changed = engine.set_selection(selection) or changed
        if changed:
            self._publish()

    # ------------------------------------------------------------------ 命令

    async def _on_install(self, command: InstallOcrEngine) -> None:
        if self._closed:
            return
        if command.kind is OcrEngineKind.WINDOWS:
            # Windows OCR 不需要下载；重新读取一次语言，用户刚装了语言包时状态随之更新。
            self._windows = await blocking(windows_engine_info, self._probe, self._logger)
            self._publish()
            return
        engine = self._engines.get(command.kind)
        if engine is None:
            self._logger.info("没有配置 OCR 引擎 %s，忽略安装命令", command.kind.value)
            return
        await engine.install(command.variant)

    def _on_cancel(self, command: CancelOcrEngineInstall) -> None:
        engine = self._engines.get(command.kind)
        if engine is None:
            self._logger.debug("引擎 %s 没有在安装，忽略取消命令", command.kind.value)
            return
        engine.cancel()

    async def _on_uninstall(self, command: UninstallOcrEngine) -> None:
        if self._closed:
            return
        if command.kind is OcrEngineKind.WINDOWS:
            self._logger.debug("Windows OCR 是系统组件，忽略卸载命令")
            return
        engine = self._engines.get(command.kind)
        if engine is None:
            self._logger.info("没有配置 OCR 引擎 %s，忽略卸载命令", command.kind.value)
            return
        await engine.uninstall()

    async def _on_download_model(self, command: DownloadOcrModel) -> None:
        engine = self._engines.get(command.kind)
        if self._closed or engine is None:
            self._logger.info("引擎 %s 没有可下载的模型，忽略下载命令", command.kind.value)
            return
        await engine.download_model(command.model_id)

    def _on_cancel_model(self, command: CancelOcrModelDownload) -> None:
        engine = self._engines.get(command.kind)
        if engine is not None:
            engine.cancel_model(command.model_id)

    async def _on_delete_model(self, command: DeleteOcrModel) -> None:
        engine = self._engines.get(command.kind)
        if self._closed or engine is None:
            self._logger.info("引擎 %s 没有可删除的模型，忽略删除命令", command.kind.value)
            return
        await engine.delete_model(command.model_id)

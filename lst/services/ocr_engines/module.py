r"""OCR 引擎的安装管理模块（需求 OCRE-01 至 OCRE-06，lst/contracts/ocr_engines.py）。

模块发布 ``OcrEngineCatalog``，处理 ``InstallOcrEngine``、``CancelOcrEngineInstall``、``UninstallOcrEngine``，
PaddleOCR 装好或卸载后发布 ``OcrEnginesChanged``。

两个引擎：

- windows：不需要下载。启动时读取系统 OCR 的可识别语言，有语言为 INSTALLED，没有为 UNAVAILABLE
  （NO_LANGUAGE）。用户装了语言包之后，对 windows 发 InstallOcrEngine 会重新读取一次。
- paddle：安装版按需下载，源码版只检查开发环境。

PaddleOCR 的安装分两步，目录约定见 layout.py：

1. 运行环境：``uv pip install --target site.part``，成功后写入运行环境标记，
   用 ``os.replace`` 原子改名为 site，删除 uv 的缓存。
   运行环境标记记录版本清单的 SHA-256，清单没变时重试安装直接复用；
2. 模型：逐个流式下载 ``<模型名>_infer.tar`` 到 ``.<模型名>_infer.tar.part``，解压到 ``.<模型名>.extract``，
   校验后原子改名为 ``official_models/<模型名>``。已经可用的模型跳过。

两步都在 DOWNLOADING 状态下进行，可以取消；全部完成后进入 INSTALLING，复查运行环境与全部模型，
最后写安装标记 installed.json，状态变为 INSTALLED 并发布 ``OcrEnginesChanged(paddle, True)``。
安装标记与运行环境标记都记录版本清单的 SHA-256。启动时两者都要等于安装目录里当前清单的 SHA-256 才算
INSTALLED；程序升级带来新清单时报 NOT_INSTALLED，detail 说明需要更新运行环境，用户点“安装”后只重装
运行环境、复用已下载的模型。替换已有的 site 目录之前调用组合根注入的 before_runtime_update，让它先停掉
正在使用旧运行环境的 OCR 进程。卸载期间（含 before_uninstall 回调）忽略安装命令与重复的卸载命令。
INSTALLING 阶段的取消命令被忽略。取消或失败时删除半成品（site.part、uv 缓存、.part 文件、解压目录），
已经完整装好的运行环境与模型保留，重试时复用；卸载删除全部。

进度覆盖两步，权重按各自的字节数：运行环境取 ``runtime_bytes``（装好后约 975 MB），每个模型取清单里的
大小；已经就位的部分不计入。运行环境阶段 uv 不输出机器可读的进度，模块每隔 ``poll_interval`` 秒统计
uv 缓存目录的大小作为估计，封顶为这一段的 98%，uv 成功后补满。进度只增不减，最短发布间隔为
``progress_interval``；状态变化立即发布。

进程相关的状态（LOADING、READY、进程出错的 ERROR）不在这里计算。组合根按 OCR 进程的 WorkerStatus
算出状态后调用 ``set_process_state``，模块把它合并到当前引擎的条目里：只有当前引擎、并且安装状态是
INSTALLED 时才合并，安装状态本身优先。

源码运行时（传入 DevPaddleEnvironment）：.venv-ocr 存在且某个模型缓存的 official_models 下有模型，
paddle 报告 INSTALLED；安装与卸载命令一律忽略并记 INFO，模块不写开发环境的任何文件。

线程：模块状态只在事件循环线程上读写；扫描、解压、改名、删除、写文件都放进线程（_shared.aio.blocking）。
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx

from lst.contracts.config import AppConfig
from lst.contracts.ocr_engines import (
    CancelOcrEngineInstall,
    InstallOcrEngine,
    OcrEngineCatalog,
    OcrEngineErrorCode,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEnginesChanged,
    OcrEngineState,
    UninstallOcrEngine,
)
from lst.kernel import ModuleContext
from lst.services._shared.aio import blocking
from lst.services._shared.download import content_length, open_for_write, stream_to_file
from lst.services.ocr_engines.archive import (
    InvalidArchive,
    dir_size,
    extract_model,
    remove_leftovers,
    remove_tree,
)
from lst.services.ocr_engines.layout import (
    DEFAULT_MODEL_BASE_URL,
    DEFAULT_PADDLE_MODELS,
    OFFICIAL_MODELS,
    PADDLE_LANGUAGES,
    RUNTIME_BYTES,
    RUNTIME_STAMP,
    DevPaddleEnvironment,
    PaddleInstallPaths,
    PaddleModel,
    features_of,
    model_ready,
    model_url,
    read_marker,
)
from lst.services.ocr_engines.net import (
    DOWNLOAD_TIMEOUT,
    classify_transport_error,
    describe_error,
    safe_url,
)
from lst.services.ocr_engines.runtime import (
    CommandRunner,
    classify_uv_failure,
    environment_from_os,
    failure_detail,
    file_sha256,
    requires_hashes,
    run_command,
    uv_environment,
    uv_install_args,
)
from lst.services.ocr_engines.windows import LanguageProbe, probe_windows_languages, windows_engine_info

PROGRESS_INTERVAL_S: Final = 0.2
"""进度的最短发布间隔。"""

POLL_INTERVAL_S: Final = 1.0
"""运行环境阶段统计 uv 缓存大小的间隔。uv 缓存里有几万个文件，统计一次要几十毫秒。"""

WRITE_CHUNK_BYTES: Final = 1 << 20
"""攒够这么多数据再交给线程写盘，减少线程切换。"""

RUNTIME_PROGRESS_CAP: Final = 0.98
"""按 uv 缓存大小估计的运行环境进度的上限；uv 成功退出后才补满。"""

_HTTP_OK: Final = 200
_BUSY: Final = frozenset({OcrEngineState.QUEUED, OcrEngineState.DOWNLOADING, OcrEngineState.INSTALLING})

type BeforeUninstall = Callable[[OcrEngineKind], Awaitable[None]]
"""卸载前由组合根执行的操作，例如该引擎是当前引擎时先停 OCR 进程，让运行环境的 DLL 不再被占用。"""

type BeforeRuntimeUpdate = Callable[[OcrEngineKind], Awaitable[None]]
"""替换已有的运行环境之前由组合根执行的操作：停掉正在使用旧运行环境的 OCR 进程。

只在 site 目录已经存在、新的运行环境已经装进 site.part、即将删除旧 site 时调用一次，参数是 PADDLE。
返回之后模块删除旧 site 并把 site.part 改名为 site；回调抛出的异常按安装失败处理（旧 site 保留）。
"""

RUNTIME_OUTDATED_DETAIL: Final = "版本清单已随程序更新，需要重新安装 PaddleOCR 运行环境；已下载的模型保留"


class _Failure(Exception):
    """安装失败。code 是原因分类，detail 是技术细节（地址已脱敏）。"""

    def __init__(self, code: OcrEngineErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class _Overlay:
    kind: OcrEngineKind
    state: OcrEngineState
    error_code: OcrEngineErrorCode | None
    detail: str


def _runtime_stamp(site: Path) -> str | None:
    """运行环境标记里记录的版本清单 SHA-256；没有标记时返回 None。"""
    data = read_marker(site / RUNTIME_STAMP)
    value = data.get("requirements_sha256") if data is not None else None
    return value if isinstance(value, str) else None


def _commit_runtime(paths: PaddleInstallPaths, requirements_sha256: str) -> None:
    """uv 成功后：写运行环境标记，原子改名 site.part → site，删除 uv 缓存。"""
    part = paths.site_part
    if not part.is_dir() or not any(part.iterdir()):
        raise _Failure(OcrEngineErrorCode.INSTALL_FAILED, "uv 成功退出，但目标目录是空的")
    (part / RUNTIME_STAMP).write_text(
        json.dumps({"requirements_sha256": requirements_sha256}), encoding="utf-8"
    )
    remove_tree(paths.site_dir)
    os.replace(part, paths.site_dir)
    remove_tree(paths.uv_cache)


def _write_marker(paths: PaddleInstallPaths, models: tuple[PaddleModel, ...]) -> None:
    missing = [model.name for model in models if not model_ready(paths.model_dir(model.name))]
    if missing or not paths.site_dir.is_dir():
        raise _Failure(
            OcrEngineErrorCode.INVALID_PACKAGE,
            "安装完成后复查失败：" + ("缺少运行环境" if not missing else "缺少模型 " + ", ".join(missing)),
        )
    paths.engine_dir.mkdir(parents=True, exist_ok=True)
    temporary = paths.marker.with_name(paths.marker.name + ".part")
    temporary.write_text(
        json.dumps(
            {
                "runtime_requirements_sha256": _runtime_stamp(paths.site_dir),
                "models": [model.name for model in models],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, paths.marker)


def _install_status(paths: PaddleInstallPaths, models: tuple[PaddleModel, ...]) -> tuple[bool, str]:
    """PaddleOCR 是否已装好，以及没装好时给用户看的说明。

    已装好要求：安装标记、运行环境与全部模型都在，并且运行环境标记与安装标记里记录的版本清单 SHA-256
    都等于安装目录里当前的版本清单。程序升级带来新清单时两者对不上，返回（False，需要更新的说明），
    重新安装只重装运行环境、复用模型。版本清单文件不存在（安装目录不完整）时不比较哈希。
    """
    marker = read_marker(paths.marker)
    if marker is None or not paths.site_dir.is_dir():
        return False, ""
    if not all(model_ready(paths.model_dir(model.name)) for model in models):
        return False, ""
    if not paths.requirements.is_file():
        return True, ""
    expected = file_sha256(paths.requirements)
    recorded = marker.get("runtime_requirements_sha256")
    if _runtime_stamp(paths.site_dir) != expected or recorded != expected:
        return False, RUNTIME_OUTDATED_DETAIL
    return True, ""


def _installed(paths: PaddleInstallPaths, models: tuple[PaddleModel, ...]) -> bool:
    return _install_status(paths, models)[0]


def _dev_status(dev: DevPaddleEnvironment) -> tuple[bool, str]:
    """开发环境是否可用，以及说明。"""
    if not dev.python.is_file():
        return False, f"找不到 {dev.python}，按 ocr-requirements.txt 的说明创建 .venv-ocr"
    for cache in dev.model_caches:
        models = cache / OFFICIAL_MODELS
        if models.is_dir() and any(entry.is_dir() for entry in models.iterdir()):
            return True, f"源码运行：使用 {dev.venv} 与 {cache}"
    caches = "、".join(str(cache / OFFICIAL_MODELS) for cache in dev.model_caches)
    return False, f"源码运行：{caches} 下都没有模型"


def _uninstall_files(paths: PaddleInstallPaths) -> None:
    """先删安装标记，再删运行环境、uv 缓存与全部 PaddleOCR 模型。中途失败时引擎不会被误判为已安装。"""
    for target in (
        paths.marker,
        paths.marker.with_name(paths.marker.name + ".part"),
        paths.site_part,
        paths.uv_cache,
        paths.site_dir,
        paths.models_dir,
    ):
        remove_tree(target)


class OcrEnginesModule:
    """OCR 引擎的目录、安装、取消与卸载，实现 kernel 的 Module 协议。

    构造参数：
        paddle：安装版传 PaddleInstallPaths（引擎目录、模型缓存、uv、Python、版本清单），
            源码版传 DevPaddleEnvironment（.venv-ocr 与候选模型缓存）；
        client：共用的 httpx 客户端，测试注入 MockTransport；
        models：要下载的 PaddleX 模型清单；
        model_base_url：模型下载地址前缀；
        index_url：uv 使用的包索引（镜像），None 用 uv 的默认值 PyPI；
        runner：外部命令运行器，测试注入假运行器；
        windows_probe：读取 Windows OCR 语言的函数，测试注入假函数；
        environ：uv 子进程的基础环境变量，None 时取 os.environ；
        before_uninstall：卸载前由组合根执行的操作（停 OCR 进程）；
        before_runtime_update：替换已有运行环境之前由组合根执行的操作（停 OCR 进程），见 BeforeRuntimeUpdate；
        runtime_bytes：运行环境的估计大小，作为进度权重；
        paddle_languages：PaddleOCR 默认识别模型覆盖的语言；
        progress_interval、poll_interval、clock、write_chunk_bytes：进度节流、uv 缓存统计间隔、
            单调时钟与写盘块大小，测试用来控制时间。
    """

    name = "ocr_engines"

    def __init__(
        self,
        paddle: PaddleInstallPaths | DevPaddleEnvironment,
        client: httpx.AsyncClient,
        *,
        models: tuple[PaddleModel, ...] = DEFAULT_PADDLE_MODELS,
        model_base_url: str = DEFAULT_MODEL_BASE_URL,
        index_url: str | None = None,
        runner: CommandRunner = run_command,
        windows_probe: LanguageProbe = probe_windows_languages,
        environ: Mapping[str, str] | None = None,
        before_uninstall: BeforeUninstall | None = None,
        before_runtime_update: BeforeRuntimeUpdate | None = None,
        runtime_bytes: int = RUNTIME_BYTES,
        paddle_languages: tuple[str, ...] = PADDLE_LANGUAGES,
        progress_interval: float = PROGRESS_INTERVAL_S,
        poll_interval: float = POLL_INTERVAL_S,
        clock: Callable[[], float] = time.monotonic,
        write_chunk_bytes: int = WRITE_CHUNK_BYTES,
    ) -> None:
        self._paths = paddle if isinstance(paddle, PaddleInstallPaths) else None
        self._dev = paddle if isinstance(paddle, DevPaddleEnvironment) else None
        self._client = client
        self._models = models
        self._base_url = model_base_url
        self._index_url = index_url
        self._runner = runner
        self._probe = windows_probe
        self._environ = environ
        self._before_uninstall = before_uninstall
        self._before_runtime_update = before_runtime_update
        self._runtime_bytes = max(1, runtime_bytes)
        self._paddle_languages = paddle_languages
        self._interval = progress_interval
        self._poll = poll_interval
        self._clock = clock
        self._write_chunk = max(1, write_chunk_bytes)
        self._ctx: ModuleContext | None = None
        self._logger = logging.getLogger(__name__)
        self._closed = False
        self._windows = OcrEngineInfo(OcrEngineKind.WINDOWS, OcrEngineState.UNAVAILABLE, needs_download=False)
        self._state = OcrEngineState.NOT_INSTALLED
        self._progress: float | None = None
        self._error: OcrEngineErrorCode | None = None
        self._detail = ""
        self._active: OcrEngineKind | None = None
        self._overlay: _Overlay | None = None
        self._task: asyncio.Task[None] | None = None
        self._uninstalling = False
        # 进度：每一段的权重（字节）与已完成的比例。
        self._weights: dict[str, float] = {}
        self._fractions: dict[str, float] = {}
        self._last_report = 0.0
        self._current_model: PaddleModel | None = None

    # ------------------------------------------------------------------ 生命周期

    async def start(self, ctx: ModuleContext) -> None:
        """读取 Windows OCR 语言、清理半成品、检查 PaddleOCR 是否已装，然后发布 OcrEngineCatalog。"""
        if self._ctx is not None:
            raise RuntimeError("OCR 引擎模块已经启动")
        self._ctx = ctx
        self._logger = ctx.logger
        self._windows = await blocking(windows_engine_info, self._probe, self._logger)
        await self._refresh_paddle(cleanup=True)
        ctx.subscribe(InstallOcrEngine, self._on_install)
        ctx.subscribe(CancelOcrEngineInstall, self._on_cancel)
        ctx.subscribe(UninstallOcrEngine, self._on_uninstall)
        ctx.subscribe(AppConfig, self._on_config)
        self._publish()

    async def stop(self) -> None:
        """取消进行中的安装（删除半成品）。重复调用没有效果。"""
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _refresh_paddle(self, *, cleanup: bool) -> None:
        if self._dev is not None:
            ok, detail = await blocking(_dev_status, self._dev)
            self._state = OcrEngineState.INSTALLED if ok else OcrEngineState.NOT_INSTALLED
            self._detail = detail
            self._logger.info("PaddleOCR（%s）：%s", "可用" if ok else "不可用", detail)
            return
        paths = self._require_paths()
        if cleanup:
            removed = await blocking(
                remove_leftovers, [paths.site_part, paths.uv_cache], paths.models_dir, self._logger
            )
            if removed:
                self._logger.info("已删除 %d 项上次安装中断留下的文件", removed)
        installed, detail = await blocking(_install_status, paths, self._models)
        self._state = OcrEngineState.INSTALLED if installed else OcrEngineState.NOT_INSTALLED
        self._detail = detail
        if detail:
            self._logger.info("PaddleOCR 需要更新：%s", detail)
        else:
            self._logger.info("PaddleOCR %s", "已安装" if installed else "未安装")

    def _require_paths(self) -> PaddleInstallPaths:
        assert self._paths is not None
        return self._paths

    # ------------------------------------------------------------------ 目录

    @property
    def download_bytes(self) -> int:
        return self._runtime_bytes + sum(model.size_bytes for model in self._models)

    def catalog(self) -> OcrEngineCatalog:
        """当前的引擎目录快照，已合并组合根给出的进程状态。"""
        paddle = OcrEngineInfo(
            OcrEngineKind.PADDLE,
            self._state,
            needs_download=self._dev is None,
            download_bytes=self.download_bytes if self._dev is None else None,
            progress=self._progress if self._state in _BUSY else None,
            languages=self._paddle_languages,
            features=features_of(self._models),
            error_code=self._error if self._state is OcrEngineState.ERROR else None,
            detail=self._detail,
        )
        engines = tuple(self._apply_overlay(info) for info in (self._windows, paddle))
        return OcrEngineCatalog(engines=engines, active=self._active)

    def _apply_overlay(self, info: OcrEngineInfo) -> OcrEngineInfo:
        overlay = self._overlay
        if (
            overlay is None
            or overlay.kind is not info.kind
            or info.kind is not self._active
            or info.state is not OcrEngineState.INSTALLED
        ):
            return info
        return OcrEngineInfo(
            info.kind,
            overlay.state,
            needs_download=info.needs_download,
            download_bytes=info.download_bytes,
            languages=info.languages,
            features=info.features,
            error_code=overlay.error_code if overlay.state is OcrEngineState.ERROR else None,
            detail=overlay.detail or info.detail,
        )

    def can_run(self, kind: OcrEngineKind) -> bool:
        """引擎是否已经可以启动 OCR 进程：Windows 有 OCR 语言、PaddleOCR 已装好。

        只看安装状态，不看组合根合并进来的进程状态（进程出错不代表引擎没装好）。
        """
        if kind is OcrEngineKind.WINDOWS:
            return self._windows.state is OcrEngineState.INSTALLED
        return self._state is OcrEngineState.INSTALLED

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

    def _publish(self) -> None:
        if self._ctx is None or self._closed:
            return
        self._ctx.bus.publish(self.catalog())

    def _set_state(
        self,
        state: OcrEngineState,
        *,
        error: OcrEngineErrorCode | None = None,
        detail: str = "",
    ) -> None:
        self._state = state
        self._error = error
        self._detail = detail
        if state not in _BUSY:
            self._progress = None
        self._publish()

    def _on_config(self, config: AppConfig) -> None:
        """从 [ocr] 节取当前引擎：enabled 为假时为 None。字段缺失时按 OcrConfig 的默认值（启用、windows）。"""
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
        if active is not self._active:
            self._active = active
            self._publish()

    # ------------------------------------------------------------------ 命令

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

    async def _on_install(self, command: InstallOcrEngine) -> None:
        if self._closed:
            return
        if command.kind is OcrEngineKind.WINDOWS:
            # Windows OCR 不需要下载；重新读取一次语言，用户刚装了语言包时状态随之更新。
            self._windows = await blocking(windows_engine_info, self._probe, self._logger)
            self._publish()
            return
        if self._dev is not None:
            self._logger.info("源码运行，不安装 PaddleOCR，以免改动开发环境 %s", self._dev.venv)
            return
        if self._state in _BUSY:
            self._logger.debug("PaddleOCR 已在安装中，忽略重复的安装命令")
            return
        if self._uninstalling:
            self._logger.info("PaddleOCR 正在卸载，忽略安装命令")
            return
        if self._state is OcrEngineState.INSTALLED:
            self._logger.debug("PaddleOCR 已安装，忽略安装命令")
            return
        self._progress = 0.0
        self._set_state(OcrEngineState.QUEUED)
        task = self._task = self._spawn(self._install(), "ocr_engines:install:paddle")
        await asyncio.wait({task})
        if task.cancelled() and self._state in _BUSY:
            # 任务在第一次运行之前就被取消，_install 一行也没有执行。
            self._set_state(OcrEngineState.NOT_INSTALLED)

    def _on_cancel(self, command: CancelOcrEngineInstall) -> None:
        if command.kind is not OcrEngineKind.PADDLE or self._dev is not None:
            self._logger.debug("引擎 %s 没有在安装，忽略取消命令", command.kind.value)
            return
        task = self._task
        if task is None or task.done() or self._state not in _BUSY:
            self._logger.debug("PaddleOCR 没有在安装，忽略取消命令")
            return
        if self._state is OcrEngineState.INSTALLING:
            self._logger.info("PaddleOCR 正在完成安装，不能再取消")
            return
        self._logger.info("取消 PaddleOCR 的安装")
        task.cancel()

    async def _on_uninstall(self, command: UninstallOcrEngine) -> None:
        if self._closed:
            return
        if command.kind is OcrEngineKind.WINDOWS:
            self._logger.debug("Windows OCR 是系统组件，忽略卸载命令")
            return
        if self._dev is not None:
            self._logger.info("源码运行，不卸载 PaddleOCR，以免改动开发环境 %s", self._dev.venv)
            return
        if self._state in _BUSY:
            self._logger.info("PaddleOCR 正在安装，忽略卸载命令；先取消安装")
            return
        if self._uninstalling:
            self._logger.debug("PaddleOCR 已在卸载中，忽略重复的卸载命令")
            return
        # 在第一个 await 之前同步设标志：卸载期间到达的安装命令被忽略，不会一边删文件一边装。
        self._uninstalling = True
        try:
            await self._uninstall()
        finally:
            self._uninstalling = False

    async def _uninstall(self) -> None:
        was_installed = self._state is OcrEngineState.INSTALLED
        if self._before_uninstall is not None:
            await self._before_uninstall(OcrEngineKind.PADDLE)
        paths = self._require_paths()
        try:
            await blocking(_uninstall_files, paths)
        except OSError as exc:
            self._logger.warning("卸载 PaddleOCR 失败：%s", exc)
            installed = await blocking(_installed, paths, self._models)
            if installed:
                self._set_state(OcrEngineState.INSTALLED)
            else:
                self._set_state(
                    OcrEngineState.ERROR, error=OcrEngineErrorCode.DISK, detail=describe_error(exc)
                )
            if was_installed and not installed and self._ctx is not None and not self._closed:
                self._ctx.bus.publish(OcrEnginesChanged(OcrEngineKind.PADDLE, False))
            return
        self._logger.info("已卸载 PaddleOCR：删除了运行环境与模型目录 %s", paths.models_dir)
        self._set_state(OcrEngineState.NOT_INSTALLED)
        if was_installed and self._ctx is not None and not self._closed:
            self._ctx.bus.publish(OcrEnginesChanged(OcrEngineKind.PADDLE, False))

    # ------------------------------------------------------------------ 安装

    async def _install(self) -> None:
        """安装 PaddleOCR。失败只记录在状态里，不抛出；取消时清理半成品后把取消传出去。"""
        paths = self._require_paths()
        try:
            await self._install_steps(paths)
        except asyncio.CancelledError:
            await self._cleanup(paths)
            self._logger.info("已取消 PaddleOCR 的安装")
            self._set_state(OcrEngineState.NOT_INSTALLED)
            raise
        except _Failure as failure:
            await self._fail(paths, failure.code, failure.detail)
            return
        except InvalidArchive as exc:
            await self._fail(paths, OcrEngineErrorCode.INVALID_PACKAGE, str(exc))
            return
        except OSError as exc:
            await self._fail(paths, OcrEngineErrorCode.DISK, describe_error(exc))
            return
        except Exception as exc:
            self._logger.exception("安装 PaddleOCR 时出现未预期的错误")
            await self._fail(paths, OcrEngineErrorCode.INTERNAL, describe_error(exc))
            return
        finally:
            self._current_model = None
        self._logger.info("PaddleOCR 安装完成")
        self._set_state(OcrEngineState.INSTALLED)
        if not self._closed and self._ctx is not None:
            self._ctx.bus.publish(OcrEnginesChanged(OcrEngineKind.PADDLE, True))

    async def _install_steps(self, paths: PaddleInstallPaths) -> None:
        for required, label in ((paths.uv, "uv"), (paths.python, "Python"), (paths.requirements, "版本清单")):
            if not await blocking(required.is_file):
                raise _Failure(OcrEngineErrorCode.INTERNAL, f"安装目录不完整，找不到{label}：{required}")
        requirements_sha256 = await blocking(file_sha256, paths.requirements)
        stamp = await blocking(_runtime_stamp, paths.site_dir)
        runtime_needed = stamp != requirements_sha256
        pending = [
            model for model in self._models if not await blocking(model_ready, paths.model_dir(model.name))
        ]
        self._weights = {"runtime": float(self._runtime_bytes)} if runtime_needed else {}
        self._weights.update({model.name: float(model.size_bytes) for model in pending})
        self._fractions = dict.fromkeys(self._weights, 0.0)
        self._last_report = self._clock()
        self._set_state(OcrEngineState.DOWNLOADING)
        if runtime_needed:
            await self._install_runtime(paths, requirements_sha256)
        else:
            self._logger.info("PaddleOCR 运行环境已就位，跳过")
        for model in pending:
            await self._download_model(paths, model)
        self._set_state(OcrEngineState.INSTALLING)
        self._progress = 1.0
        await blocking(_write_marker, paths, self._models)

    def _advance(self, part: str, fraction: float, *, force: bool = False) -> None:
        """更新一段的完成比例，按节流间隔发布进度。进度只增不减。"""
        if part not in self._fractions:
            return
        self._fractions[part] = max(self._fractions[part], min(1.0, fraction))
        total = sum(self._weights.values())
        value = (
            sum(self._weights[key] * self._fractions[key] for key in self._weights) / total if total else 1.0
        )
        self._progress = max(self._progress or 0.0, min(1.0, value))
        now = self._clock()
        if force or now - self._last_report >= self._interval:
            self._last_report = now
            self._publish()

    async def _install_runtime(self, paths: PaddleInstallPaths, requirements_sha256: str) -> None:
        await blocking(remove_tree, paths.site_part)
        await blocking(remove_tree, paths.uv_cache)
        await blocking(paths.engine_dir.mkdir, parents=True, exist_ok=True)
        text = await blocking(paths.requirements.read_text, encoding="utf-8")
        args = uv_install_args(
            paths.uv,
            paths.python,
            paths.site_part,
            paths.requirements,
            hashed=requires_hashes(text),
            index_url=self._index_url,
        )
        base = self._environ if self._environ is not None else environment_from_os()
        env = uv_environment(base, paths.uv_cache)
        self._logger.info("开始安装 PaddleOCR 运行环境：%s", " ".join(args[1:4]))
        monitor = self._spawn(self._watch_runtime(paths.uv_cache), "ocr_engines:runtime-progress")
        try:
            result = await self._runner(args, env)
        except OSError as exc:
            raise _Failure(OcrEngineErrorCode.INSTALL_FAILED, f"无法运行 uv：{describe_error(exc)}") from exc
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if result.returncode != 0:
            raise _Failure(classify_uv_failure(result.output), failure_detail(result))
        if self._before_runtime_update is not None and await blocking(paths.site_dir.is_dir):
            # 旧的运行环境可能正被 OCR 进程使用（DLL 被占用时删不掉），先请组合根停掉进程。
            await self._before_runtime_update(OcrEngineKind.PADDLE)
        await blocking(_commit_runtime, paths, requirements_sha256)
        self._logger.info("PaddleOCR 运行环境已安装到 %s", paths.site_dir)
        self._advance("runtime", 1.0, force=True)

    async def _watch_runtime(self, cache: Path) -> None:
        """每隔 poll_interval 秒按 uv 缓存目录的大小估计运行环境阶段的进度。"""
        while True:
            await asyncio.sleep(self._poll)
            size = await blocking(dir_size, cache)
            self._advance("runtime", min(RUNTIME_PROGRESS_CAP, size / self._runtime_bytes))

    async def _download_model(self, paths: PaddleInstallPaths, model: PaddleModel) -> None:
        self._current_model = model
        url = model_url(self._base_url, model.name)
        part = paths.download_part(model.name)
        self._logger.info("开始下载 OCR 模型 %s：%s", model.name, safe_url(url))
        sink = await blocking(open_for_write, part)
        length: int | None = None
        received = 0
        try:
            async with self._client.stream(
                "GET", url, timeout=DOWNLOAD_TIMEOUT, follow_redirects=True
            ) as response:
                if response.status_code != _HTTP_OK:
                    raise _Failure(OcrEngineErrorCode.HTTP, f"{model.name}：HTTP {response.status_code}")
                length = content_length(response)
                expected = length or model.size_bytes

                def report(done: int) -> None:
                    # 下载占这一段的 95%，解压与改名占 5%。
                    self._advance(model.name, 0.95 * done / expected)

                received = await stream_to_file(
                    response, sink, chunk_bytes=self._write_chunk, on_progress=report
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise _Failure(classify_transport_error(exc), f"{model.name}：{describe_error(exc)}") from exc
        finally:
            await blocking(sink.close)
        if length is not None and received != length:
            raise _Failure(
                OcrEngineErrorCode.INVALID_PACKAGE, f"{model.name}：收到 {received} 字节，应为 {length} 字节"
            )
        try:
            await blocking(
                extract_model, part, paths.extract_dir(model.name), paths.model_dir(model.name), model.name
            )
        except InvalidArchive as exc:
            raise _Failure(OcrEngineErrorCode.INVALID_PACKAGE, f"{model.name}：{exc}") from exc
        self._current_model = None
        self._logger.info("OCR 模型 %s 已就位（%d 字节）", model.name, received)
        self._advance(model.name, 1.0, force=True)

    async def _cleanup(self, paths: PaddleInstallPaths) -> None:
        """删除半成品：site.part、uv 缓存、当前模型的 .part 与解压目录，以及其余以点开头的临时项。"""
        targets = [paths.site_part, paths.uv_cache, paths.marker.with_name(paths.marker.name + ".part")]
        model = self._current_model
        if model is not None:
            targets += [paths.download_part(model.name), paths.extract_dir(model.name)]
        try:
            await blocking(remove_leftovers, targets, paths.models_dir, self._logger)
        except OSError:
            self._logger.warning("清理安装半成品失败", exc_info=True)

    async def _fail(self, paths: PaddleInstallPaths, code: OcrEngineErrorCode, detail: str) -> None:
        await self._cleanup(paths)
        self._logger.warning("安装 PaddleOCR 失败（%s）：%s", code.value, detail)
        self._set_state(OcrEngineState.ERROR, error=code, detail=detail)

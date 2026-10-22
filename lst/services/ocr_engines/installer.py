r"""需要下载的 OCR 引擎的通用流程：安装状态、运行环境版本、进度、uv 安装、取消、失败清理、卸载、安装标记，
以及模型的下载、取消与删除。

PaddleOCR（paddle.py）与 RapidOCR（rapid.py）各是 ``RuntimeEngine`` 的一个子类，共用这里的流程，
子类只补充自己的部分：开发环境的检查、模型目录与模型文件的下载方式、运行环境的完整性检查、卸载时多删的
目录。目录约定见 layout.py，模型目录见 catalog.py。

安装（安装版）
--------------

``install(variant)`` 安装指定版本的运行环境（variant 为 None 时用推荐版本，推荐版本不可装时用 CPU 版）：

1. 运行环境：``uv pip install --target site.part``（命令行见 runtime.py），版本清单是
   ``app\ocr-<引擎>-<版本>-requirements.txt``；成功后写入运行环境标记，用 ``os.replace`` 原子改名为 site，
   删除 uv 的缓存。运行环境标记记录版本清单的 SHA-256，清单没变时重试安装直接复用；
2. 默认组合里还没有就位的模型（catalog.py 里 default 为真、不是随运行环境自带的模型），逐个下载。

两步都在 DOWNLOADING 状态下进行，可以取消；全部完成后进入 INSTALLING，复查运行环境与默认组合，最后写
安装标记 installed.json（版本清单的 SHA-256、已装的版本、默认组合的模型），状态变为 INSTALLED 并发布
``OcrEnginesChanged(<引擎>, True)``。

换版本：已经装好时对另一个版本发安装命令，按上面的流程重装运行环境；不同版本的清单不同，运行环境标记
对不上，必然重装；模型在 site 之外，已经就位，不再下载。替换已有的 site 目录之前调用组合根注入的
before_runtime_update，让它先停掉正在使用旧运行环境的 OCR 进程。换版本途中取消时旧运行环境原样保留，
状态回到 INSTALLED（旧版本）；失败时状态为 ERROR 并给出原因，旧运行环境仍然可用（can_run 为真），
OCR 照常使用旧版本。

启动时安装标记与运行环境标记都要等于已装版本当前清单的 SHA-256 才算 INSTALLED；程序升级带来新清单时报
NOT_INSTALLED，detail 说明需要更新运行环境，用户点“安装”后只重装运行环境，已下载的模型保留。
卸载期间（含 before_uninstall 回调）忽略安装命令与重复的卸载命令。INSTALLING 阶段的取消命令被忽略。
取消或失败时删除半成品（site.part、uv 缓存、子类的临时文件），已经完整装好的运行环境与模型保留，重试时
复用；卸载删除运行环境与全部模型。

进度覆盖全部步骤，权重按各自的字节数：运行环境取该版本装好后的大小，每个模型取它的大小（未知时按
UNKNOWN_MODEL_WEIGHT）；已经就位的部分不计入。运行环境阶段 uv 不输出机器可读的进度，每隔 poll_interval 秒
统计 uv 缓存目录的大小作为估计，封顶为这一段的 98%，uv 成功后补满。进度只增不减，最短发布间隔为
progress_interval；状态变化立即发布。

模型（OCRE-08）
---------------

- 模型状态按“下载任务 → 最近一次错误 → 是否就位”给出：DOWNLOADING、QUEUED 带进度，ERROR 带错误码，
  就位为 DOWNLOADED，否则 NOT_DOWNLOADED；随运行环境自带的模型在运行环境装好后为 DOWNLOADED；
- ``download_model``：引擎已安装（或源码运行时子类允许下载）时，在后台下载一个模型，下载中的文件以点开头、
  带 .part 后缀，校验（长度、SHA-256 或模型包结构）通过后原子改名到最终位置。已就位、随运行环境自带或
  已在下载时忽略。不同模型可以同时下载，与引擎安装互斥（引擎在安装中时忽略模型下载命令）；
- ``cancel_model``：取消排队或下载中的模型，删除半成品；
- ``delete_model``：删除一个已下载的模型。配置正在选用（selected）、随运行环境自带、附属模型或正在下载的
  模型拒绝删除，记 INFO。

当前选用的模型（selected）由模块按配置节 ``[ocr_<引擎>]`` 的 ``<环节>_model`` 字段告诉引擎（set_selection）。

源码运行
--------

传入开发环境（DevPaddleEnvironment、DevRapidEnvironment）时，子类检查 .venv-ocr 是否可用，可用即 INSTALLED，
已装版本按 .venv-ocr 里的推理库判断；安装与卸载命令一律忽略并记 INFO，模块不写开发环境的任何文件。

线程：引擎状态只在事件循环线程上读写；扫描、改名、删除、写文件都放进线程（_shared.aio.blocking）。
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Final, Protocol

from lst.contracts.ocr_engines import (
    OcrEngineErrorCode,
    OcrEngineInfo,
    OcrEngineKind,
    OcrEnginesChanged,
    OcrEngineState,
    OcrModelInfo,
    OcrModelState,
    OcrRuntimeVariant,
    OcrStage,
)
from lst.services._shared.aio import blocking
from lst.services._shared.gpu import GpuAdapter
from lst.services.ocr_engines.archive import InvalidArchive, dir_size, remove_leftovers, remove_tree
from lst.services.ocr_engines.catalog import ModelSpec
from lst.services.ocr_engines.gpu import recommend_variant
from lst.services.ocr_engines.layout import (
    ENGINE_VARIANTS,
    RUNTIME_STAMP,
    RuntimeInstallPaths,
    marker_variant,
    read_marker,
)
from lst.services.ocr_engines.net import describe_error
from lst.services.ocr_engines.runtime import (
    CommandRunner,
    classify_uv_failure,
    environment_from_os,
    failure_detail,
    file_sha256,
    requires_hashes,
    requires_resolution,
    run_command,
    uv_environment,
    uv_install_args,
)

PROGRESS_INTERVAL_S: Final = 0.2
"""进度的最短发布间隔。"""

POLL_INTERVAL_S: Final = 1.0
"""运行环境阶段统计 uv 缓存大小的间隔。uv 缓存里有几万个文件，统计一次要几十毫秒。"""

WRITE_CHUNK_BYTES: Final = 1 << 20
"""下载时攒够这么多数据再交给线程写盘，减少线程切换。"""

RUNTIME_PROGRESS_CAP: Final = 0.98
"""按 uv 缓存大小估计的运行环境进度的上限；uv 成功退出后才补满。"""

RUNTIME_PART: Final = "runtime"
"""进度里运行环境这一段的名字。模型各是一段，名字是模型 id。"""

UNKNOWN_MODEL_WEIGHT: Final = 20 * 1024 * 1024
"""大小未知的模型在进度里的权重。"""

BUSY_STATES: Final = frozenset({OcrEngineState.QUEUED, OcrEngineState.DOWNLOADING, OcrEngineState.INSTALLING})
MODEL_BUSY: Final = frozenset({OcrModelState.QUEUED, OcrModelState.DOWNLOADING})

type BeforeUninstall = Callable[[OcrEngineKind], Awaitable[None]]
"""卸载前由组合根执行的操作，例如该引擎是当前引擎时先停 OCR 进程，让运行环境的 DLL 不再被占用。"""

type BeforeRuntimeUpdate = Callable[[OcrEngineKind], Awaitable[None]]
"""替换已有的运行环境之前由组合根执行的操作：停掉正在使用旧运行环境的 OCR 进程。

只在 site 目录已经存在、新的运行环境已经装进 site.part、即将删除旧 site 时调用一次，参数是引擎。
返回之后删除旧 site 并把 site.part 改名为 site；回调抛出的异常按安装失败处理（旧 site 保留）。
"""

type ProgressCallback = Callable[[float], None]
"""下载进度回调，参数是 0 到 1 的完成比例。"""


class InstallFailure(Exception):
    """安装或下载失败。code 是原因分类，detail 是技术细节（地址已脱敏）。"""

    def __init__(self, code: OcrEngineErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class EngineHost(Protocol):
    """引擎对象依赖的宿主（OcrEnginesModule）：日志、发布目录、发布引擎变化与后台任务。"""

    @property
    def logger(self) -> logging.Logger: ...

    @property
    def closed(self) -> bool: ...

    def publish(self) -> None:
        """引擎状态或进度变了，重新发布 OcrEngineCatalog。"""
        ...

    def announce(self, event: OcrEnginesChanged) -> None:
        """发布 OcrEnginesChanged。"""
        ...

    def spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        """创建后台任务，意外结束时记日志。"""
        ...


@dataclass(frozen=True, slots=True)
class InstallOptions:
    """各引擎共用的安装参数，见 OcrEnginesModule 的构造参数。"""

    runner: CommandRunner = run_command
    environ: Mapping[str, str] | None = None
    index_url: str | None = None
    before_uninstall: BeforeUninstall | None = None
    before_runtime_update: BeforeRuntimeUpdate | None = None
    progress_interval: float = PROGRESS_INTERVAL_S
    poll_interval: float = POLL_INTERVAL_S
    clock: Callable[[], float] = field(default=time.monotonic)
    write_chunk_bytes: int = WRITE_CHUNK_BYTES


def runtime_stamp(site: Path) -> str | None:
    """运行环境标记里记录的版本清单 SHA-256；没有标记时返回 None。"""
    data = read_marker(site / RUNTIME_STAMP)
    value = data.get("requirements_sha256") if data is not None else None
    return value if isinstance(value, str) else None


def commit_runtime(paths: RuntimeInstallPaths, requirements_sha256: str) -> None:
    """uv 成功后：写运行环境标记，原子改名 site.part → site，删除 uv 缓存。"""
    part = paths.site_part
    if not part.is_dir() or not any(part.iterdir()):
        raise InstallFailure(OcrEngineErrorCode.INSTALL_FAILED, "uv 成功退出，但目标目录是空的")
    (part / RUNTIME_STAMP).write_text(
        json.dumps({"requirements_sha256": requirements_sha256}), encoding="utf-8"
    )
    remove_tree(paths.site_dir)
    os.replace(part, paths.site_dir)
    remove_tree(paths.uv_cache)


@dataclass(slots=True)
class _ModelJob:
    """一个模型的下载状态。task 为 None 时只记录最近一次的错误。"""

    state: OcrModelState
    progress: float | None = None
    error: OcrEngineErrorCode | None = None
    task: asyncio.Task[None] | None = None


class RuntimeEngine:
    """一个需要下载的 OCR 引擎：安装状态、版本、进度、模型与各命令的处理。见模块说明。

    子类设置类属性 kind、title，并实现或覆盖下列方法：

    - check_dev()：开发环境是否可用及说明（只在源码运行时调用，在线程里执行）；
    - dev_site()：开发环境的 site-packages，用来判断已装版本；
    - model_specs()：全部模型（可选模型与附属模型）；stage_defaults：各环节的默认模型；
    - model_is_ready(spec)：模型是否已就位（在线程里执行）；
    - fetch_model(spec, report)：下载一个模型并原子放到最终位置（协程，可以取消）；
    - model_leftovers(spec)：下载一个模型时可能留下的临时文件；
    - delete_model_files(spec)：删除一个已下载的模型（在线程里执行）；
    - dev_downloads()：源码运行时是否允许下载模型；
    - missing_parts(paths)：已装好的引擎必须有、却缺少的部分，空列表表示齐全（在线程里执行）；
    - runtime_complete(paths)：已有的运行环境是否完整，不完整时重装（在线程里执行）；
    - uninstall_targets(paths)：卸载时在运行环境之外还要删除的路径；
    - leftover_dir(paths)：启动时清理半成品要看的模型目录；
    - features、outdated_detail、variant_detail：目录里显示的信息。
    """

    kind: OcrEngineKind
    title: str
    outdated_detail: str = "版本清单已随程序更新，需要重新安装运行环境"
    stage_defaults: ClassVar[Mapping[OcrStage, str]] = {}

    def __init__(
        self,
        paths: RuntimeInstallPaths | None,
        host: EngineHost,
        options: InstallOptions,
        *,
        runtime_bytes: Mapping[OcrRuntimeVariant, int],
        download_bytes: Mapping[OcrRuntimeVariant, int],
        languages: tuple[str, ...],
    ) -> None:
        self._paths = paths
        self._host = host
        self._options = options
        self._runtime_bytes = dict(runtime_bytes)
        self._download_bytes = dict(download_bytes)
        self._languages = languages
        self.state = OcrEngineState.NOT_INSTALLED
        self._progress: float | None = None
        self._error: OcrEngineErrorCode | None = None
        self._detail = ""
        self._task: asyncio.Task[None] | None = None
        self._uninstalling = False
        self._variants: tuple[OcrRuntimeVariant, ...] = ENGINE_VARIANTS[self.kind]
        self._installed_variant: OcrRuntimeVariant | None = None
        self._target_variant: OcrRuntimeVariant | None = None
        self._recommended: OcrRuntimeVariant | None = None
        self._adapters: tuple[GpuAdapter, ...] = ()
        self._runtime_intact = False
        # 进度：每一段的权重（字节）与已完成的比例。
        self._weights: dict[str, float] = {}
        self._fractions: dict[str, float] = {}
        self._last_report = 0.0
        # 模型
        self._ready: dict[str, bool] = {}
        self._jobs: dict[str, _ModelJob] = {}
        self._selection: dict[OcrStage, str] = dict(self.stage_defaults)
        self._current_spec: ModelSpec | None = None

    # ------------------------------------------------------------------ 子类的部分

    @property
    def dev_mode(self) -> bool:
        """源码运行（传入的是开发环境而不是安装路径）。"""
        return self._paths is None

    def dev_venv(self) -> Path:
        """开发环境的 venv 目录，用于日志。"""
        raise NotImplementedError

    def dev_site(self) -> Path:
        raise NotImplementedError

    def check_dev(self) -> tuple[bool, str]:
        raise NotImplementedError

    def model_specs(self) -> tuple[ModelSpec, ...]:
        return ()

    def dev_downloads(self) -> bool:
        """源码运行时是否允许下载、删除模型。"""
        return False

    def model_is_ready(self, spec: ModelSpec) -> bool:
        del spec
        return False

    async def fetch_model(self, spec: ModelSpec, report: ProgressCallback) -> None:
        del spec, report
        raise NotImplementedError

    def model_leftovers(self, spec: ModelSpec) -> list[Path]:
        del spec
        return []

    def delete_model_files(self, spec: ModelSpec) -> None:
        del spec
        raise NotImplementedError

    def missing_parts(self, paths: RuntimeInstallPaths) -> list[str]:
        del paths
        return []

    def runtime_complete(self, paths: RuntimeInstallPaths) -> bool:
        """已有的运行环境是否完整（在线程里执行）。不完整时即使版本清单没变也重装运行环境。"""
        del paths
        return True

    def uninstall_targets(self, paths: RuntimeInstallPaths) -> list[Path]:
        del paths
        return []

    def leftover_dir(self, paths: RuntimeInstallPaths) -> Path | None:
        del paths
        return None

    def variant_detail(self, variant: OcrRuntimeVariant | None) -> str:
        """已装版本的说明（写进 detail，只进日志）。"""
        del variant
        return ""

    @property
    def features(self) -> tuple[str, ...]:
        return ("text",)

    # ------------------------------------------------------------------ 状态

    @property
    def logger(self) -> logging.Logger:
        return self._host.logger

    @property
    def installed_variant(self) -> OcrRuntimeVariant | None:
        return self._installed_variant

    @property
    def variants(self) -> tuple[OcrRuntimeVariant, ...]:
        return self._variants

    @property
    def recommended_variant(self) -> OcrRuntimeVariant | None:
        return self._recommended

    def require_paths(self) -> RuntimeInstallPaths:
        assert self._paths is not None
        return self._paths

    def download_bytes_of(self, variant: OcrRuntimeVariant | None) -> int:
        """一个版本的全部下载内容（运行环境与默认组合里需要下载的模型）的估计大小。"""
        chosen = variant or OcrRuntimeVariant.CPU
        runtime = self._download_bytes.get(chosen, self._download_bytes.get(OcrRuntimeVariant.CPU, 0))
        models = sum(spec.size_bytes or 0 for spec in self.model_specs() if spec.default and not spec.bundled)
        return runtime + models

    @property
    def download_bytes(self) -> int:
        return self.download_bytes_of(self._installed_variant or self._recommended)

    def info(self) -> OcrEngineInfo:
        """目录里这个引擎的条目（不含组合根合并的进程状态）。"""
        detail = self._detail
        extra = self.variant_detail(self._installed_variant)
        if extra and not detail:
            detail = extra
        return OcrEngineInfo(
            self.kind,
            self.state,
            needs_download=not self.dev_mode,
            download_bytes=None if self.dev_mode else self.download_bytes,
            progress=self._progress if self.state in BUSY_STATES else None,
            languages=self.languages(),
            features=self.features,
            error_code=self._error if self.state is OcrEngineState.ERROR else None,
            detail=detail,
            variants=self._variants,
            installed_variant=self._installed_variant,
            recommended_variant=self._recommended,
            models=self.models_info(),
        )

    def languages(self) -> tuple[str, ...]:
        return self._languages

    def can_run(self) -> bool:
        """可以启动 OCR 进程：已安装；或者正在换版本、换版本失败，但旧运行环境还完整。

        换版本的下载阶段 OCR 照常使用旧运行环境，直到替换 site 之前由 before_runtime_update 停掉进程。
        """
        if self.state is OcrEngineState.INSTALLED:
            return True
        return self._runtime_intact and (self.state in BUSY_STATES or self.state is OcrEngineState.ERROR)

    def set_state(
        self,
        state: OcrEngineState,
        *,
        error: OcrEngineErrorCode | None = None,
        detail: str = "",
    ) -> None:
        self.state = state
        self._error = error
        self._detail = detail
        if state not in BUSY_STATES:
            self._progress = None
        self._host.publish()

    def set_adapters(self, adapters: tuple[GpuAdapter, ...]) -> None:
        """模块检测到的显卡，用来给出推荐版本。"""
        self._adapters = adapters
        self._recommended = recommend_variant(adapters, self._variants)

    def set_selection(self, selection: Mapping[OcrStage, str]) -> bool:
        """配置里各环节当前选用的模型；环节缺失时用默认模型。返回是否有变化。"""
        merged = {**self.stage_defaults, **selection}
        if merged == self._selection:
            return False
        self._selection = merged
        return True

    @property
    def selection(self) -> dict[OcrStage, str]:
        return dict(self._selection)

    def install_status(self, paths: RuntimeInstallPaths) -> tuple[bool, str, OcrRuntimeVariant | None]:
        """是否已装好、没装好时给用户看的说明，以及已装的版本（在线程里执行）。

        已装好要求：安装标记、运行环境与 missing_parts 列出的部分都在，并且运行环境标记与安装标记里记录的
        版本清单 SHA-256 都等于已装版本当前的版本清单。程序升级带来新清单时两者对不上，返回
        （False，outdated_detail，已装版本），重新安装只重装运行环境。该版本的清单文件不存在时不比较哈希。
        """
        marker = read_marker(paths.marker)
        if marker is None or not paths.site_dir.is_dir():
            return False, "", None
        variant = marker_variant(marker)
        if self.missing_parts(paths):
            return False, "", variant
        requirements = paths.requirements(variant)
        if not requirements.is_file():
            return True, "", variant
        expected = file_sha256(requirements)
        recorded = marker.get("runtime_requirements_sha256")
        if runtime_stamp(paths.site_dir) != expected or recorded != expected:
            return False, self.outdated_detail, variant
        return True, "", variant

    def _installed(self, paths: RuntimeInstallPaths) -> bool:
        return self.install_status(paths)[0]

    def _available_variants(self, paths: RuntimeInstallPaths) -> tuple[OcrRuntimeVariant, ...]:
        declared = ENGINE_VARIANTS[self.kind]
        return tuple(variant for variant in declared if paths.requirements(variant).is_file()) or declared[:1]

    async def refresh(self, *, cleanup: bool) -> None:
        """读取安装状态与模型状态；cleanup 为真时先删除上次安装中断留下的半成品。"""
        if self._paths is None:
            ok, detail = await blocking(self.check_dev)
            self.state = OcrEngineState.INSTALLED if ok else OcrEngineState.NOT_INSTALLED
            self._detail = detail
            self._installed_variant = await blocking(self._detect_dev_variant) if ok else None
            self.logger.info("%s（%s）：%s", self.title, "可用" if ok else "不可用", detail)
            await self.scan_models()
            return
        paths = self._paths
        if cleanup:
            removed = await blocking(
                remove_leftovers, [paths.site_part, paths.uv_cache], self.leftover_dir(paths), self.logger
            )
            if removed:
                self.logger.info("已删除 %d 项 %s 上次安装中断留下的文件", removed, self.title)
        self._variants = await blocking(self._available_variants, paths)
        self._recommended = recommend_variant(self._adapters, self._variants)
        installed, detail, variant = await blocking(self.install_status, paths)
        self.state = OcrEngineState.INSTALLED if installed else OcrEngineState.NOT_INSTALLED
        self._installed_variant = variant if installed else None
        self._runtime_intact = installed
        self._detail = detail
        if detail:
            self.logger.info("%s 需要更新：%s", self.title, detail)
        else:
            self.logger.info(
                "%s %s",
                self.title,
                f"已安装（{variant.value if variant else '?'} 版）" if installed else "未安装",
            )
        await self.scan_models()

    def _detect_dev_variant(self) -> OcrRuntimeVariant:
        from lst.services.ocr_engines.layout import detect_variant

        return detect_variant(self.kind, self.dev_site())

    async def scan_models(self) -> None:
        """重新检查每个模型是否就位（在线程里），不发布。"""
        specs = self.model_specs()
        self._ready = await blocking(lambda: {spec.id: self.model_is_ready(spec) for spec in specs})

    # ------------------------------------------------------------------ 模型目录

    def _spec(self, model_id: str) -> ModelSpec | None:
        return next((spec for spec in self.model_specs() if spec.id == model_id), None)

    def models_info(self) -> tuple[OcrModelInfo, ...]:
        """可选模型（不含附属模型）的展示信息，按 model_specs 的顺序。"""
        infos: list[OcrModelInfo] = []
        for spec in self.model_specs():
            if spec.stage is None:
                continue
            job = self._jobs.get(spec.id)
            if job is not None and job.state in MODEL_BUSY:
                state, progress, error = job.state, job.progress, None
            elif self._ready.get(spec.id, False):
                state, progress, error = OcrModelState.DOWNLOADED, None, None
            elif job is not None and job.state is OcrModelState.ERROR:
                state, progress, error = OcrModelState.ERROR, None, job.error
            else:
                state, progress, error = OcrModelState.NOT_DOWNLOADED, None, None
            infos.append(
                OcrModelInfo(
                    id=spec.id,
                    stage=spec.stage,
                    name=spec.display_name,
                    state=state,
                    size_bytes=spec.size_bytes,
                    languages=spec.languages,
                    default=spec.default,
                    bundled=spec.bundled,
                    selected=self._selection.get(spec.stage) == spec.id,
                    progress=progress,
                    error_code=error,
                )
            )
        return tuple(infos)

    def model_ready_now(self, model_id: str) -> bool:
        """最近一次扫描时模型是否就位（不访问磁盘）。"""
        return self._ready.get(model_id, False)

    def _models_allowed(self) -> bool:
        if self.dev_mode:
            return self.dev_downloads() and self.state is OcrEngineState.INSTALLED
        return self.state is OcrEngineState.INSTALLED or (
            self.state is OcrEngineState.ERROR and self._runtime_intact
        )

    async def download_model(self, model_id: str) -> None:
        """处理模型下载命令：在后台下载，见模块说明。等下载结束才返回。"""
        spec = self._spec(model_id)
        if spec is None or spec.stage is None:
            self.logger.info("%s 没有可下载的模型 %s，忽略下载命令", self.title, model_id)
            return
        if not self._models_allowed() or self._uninstalling:
            self.logger.info("%s 未安装或正在安装、卸载，忽略模型 %s 的下载命令", self.title, model_id)
            return
        job = self._jobs.get(model_id)
        if (job is not None and job.state in MODEL_BUSY) or spec.bundled or self._ready.get(model_id):
            self.logger.debug("%s 的模型 %s 已就位或正在下载，忽略下载命令", self.title, model_id)
            return
        job = self._jobs[model_id] = _ModelJob(OcrModelState.QUEUED, 0.0)
        self._host.publish()
        task = job.task = self._host.spawn(self._download_job(spec, job), f"ocr_engines:model:{model_id}")
        await asyncio.wait({task})
        if task.cancelled() and job.state in MODEL_BUSY:
            self._finish_job(spec, None)

    def _finish_job(self, spec: ModelSpec, error: InstallFailure | None) -> None:
        if error is None:
            self._jobs.pop(spec.id, None)
        else:
            self._jobs[spec.id] = _ModelJob(OcrModelState.ERROR, error=error.code)
        self._host.publish()

    async def _download_job(self, spec: ModelSpec, job: _ModelJob) -> None:
        last = [0.0]

        def report(fraction: float) -> None:
            job.progress = max(job.progress or 0.0, min(1.0, fraction))
            now = self._options.clock()
            if now - last[0] >= self._options.progress_interval:
                last[0] = now
                self._host.publish()

        job.state = OcrModelState.DOWNLOADING
        self._host.publish()
        try:
            await self.fetch_model(spec, report)
        except asyncio.CancelledError:
            await self._cleanup_model(spec)
            self.logger.info("已取消 %s 模型 %s 的下载", self.title, spec.id)
            self._finish_job(spec, None)
            raise
        except InstallFailure as failure:
            await self._cleanup_model(spec)
            self.logger.warning("下载 %s 模型 %s 失败（%s）：%s", self.title, spec.id, failure.code.value,
                                failure.detail)  # fmt: skip
            self._finish_job(spec, failure)
            return
        except OSError as exc:
            await self._cleanup_model(spec)
            self.logger.warning("下载 %s 模型 %s 时写盘失败：%s", self.title, spec.id, exc)
            self._finish_job(spec, InstallFailure(OcrEngineErrorCode.DISK, describe_error(exc)))
            return
        except Exception as exc:
            await self._cleanup_model(spec)
            self.logger.exception("下载 %s 模型 %s 时出现未预期的错误", self.title, spec.id)
            self._finish_job(spec, InstallFailure(OcrEngineErrorCode.INTERNAL, describe_error(exc)))
            return
        self._ready[spec.id] = await blocking(self.model_is_ready, spec)
        self.logger.info("%s 模型 %s 已下载", self.title, spec.id)
        self._finish_job(spec, None)

    async def _cleanup_model(self, spec: ModelSpec) -> None:
        targets = self.model_leftovers(spec)
        try:
            await blocking(remove_leftovers, targets, None, self.logger)
        except OSError:
            self.logger.warning("清理 %s 模型 %s 的半成品失败", self.title, spec.id, exc_info=True)

    def cancel_model(self, model_id: str) -> None:
        job = self._jobs.get(model_id)
        if job is None or job.task is None or job.task.done() or job.state not in MODEL_BUSY:
            self.logger.debug("%s 的模型 %s 没有在下载，忽略取消命令", self.title, model_id)
            return
        self.logger.info("取消 %s 模型 %s 的下载", self.title, model_id)
        job.task.cancel()

    async def delete_model(self, model_id: str) -> None:
        """处理删除命令，拒绝的情况见模块说明。"""
        spec = self._spec(model_id)
        if spec is None or spec.stage is None:
            self.logger.info("%s 没有可删除的模型 %s，忽略删除命令", self.title, model_id)
            return
        reason = ""
        if spec.bundled:
            reason = "随运行环境自带"
        elif self._selection.get(spec.stage) == model_id:
            reason = "配置正在选用，先把该环节改为其他模型"
        elif (job := self._jobs.get(model_id)) is not None and job.state in MODEL_BUSY:
            reason = "正在下载，先取消下载"
        elif self.dev_mode and not self.dev_downloads():
            reason = "源码运行时模型属于开发环境"
        elif not self._ready.get(model_id, False):
            reason = "没有下载"
        if reason:
            self.logger.info("拒绝删除 %s 的模型 %s：%s", self.title, model_id, reason)
            return
        try:
            await blocking(self.delete_model_files, spec)
        except OSError as exc:
            self.logger.warning("删除 %s 的模型 %s 失败：%s", self.title, model_id, exc)
        self._ready[model_id] = await blocking(self.model_is_ready, spec)
        self._jobs.pop(model_id, None)
        if not self._ready[model_id]:
            self.logger.info("已删除 %s 的模型 %s", self.title, model_id)
        self._host.publish()

    # ------------------------------------------------------------------ 命令

    def resolve_variant(self, requested: OcrRuntimeVariant | None) -> OcrRuntimeVariant | None:
        """安装命令的目标版本：给出的版本、推荐版本或 CPU 版；给出的版本不可装时返回 None。"""
        if requested is not None:
            return requested if requested in self._variants else None
        if self._recommended is not None and self._recommended in self._variants:
            return self._recommended
        return self._variants[0] if self._variants else None

    async def install(self, variant: OcrRuntimeVariant | None = None) -> None:
        """处理安装命令：排队并等安装任务结束。已安装同一版本、安装中、卸载中或源码运行时忽略。"""
        if self.dev_mode:
            self.logger.info("源码运行，不安装 %s，以免改动开发环境 %s", self.title, self.dev_venv())
            return
        if self.state in BUSY_STATES:
            self.logger.debug("%s 已在安装中，忽略重复的安装命令", self.title)
            return
        if self._uninstalling:
            self.logger.info("%s 正在卸载，忽略安装命令", self.title)
            return
        target = self.resolve_variant(variant)
        if target is None:
            available = ", ".join(v.value for v in self._variants)
            self.logger.warning(
                "%s 没有 %s 版的版本清单（可装：%s），忽略安装命令",
                self.title,
                variant.value if variant else "?",
                available,
            )
            return
        if self.state is OcrEngineState.INSTALLED and target is self._installed_variant:
            self.logger.debug("%s 已安装 %s 版，忽略安装命令", self.title, target.value)
            return
        if any(job.state in MODEL_BUSY for job in self._jobs.values()):
            self.logger.info("%s 有模型正在下载，忽略安装命令；先等下载结束或取消", self.title)
            return
        self._target_variant = target
        self._progress = 0.0
        self.set_state(OcrEngineState.QUEUED)
        task = self._task = self._host.spawn(self._install(target), f"ocr_engines:install:{self.kind.value}")
        await asyncio.wait({task})
        if task.cancelled() and self.state in BUSY_STATES:
            # 任务在第一次运行之前就被取消，_install 一行也没有执行。
            await self._settle_after_abort()

    async def _settle_after_abort(self) -> None:
        """取消或任务没跑起来之后：旧运行环境还在就回到 INSTALLED（旧版本），否则 NOT_INSTALLED。"""
        paths = self.require_paths()
        installed, detail, variant = await blocking(self.install_status, paths)
        self._installed_variant = variant if installed else None
        self._runtime_intact = installed
        self.set_state(OcrEngineState.INSTALLED if installed else OcrEngineState.NOT_INSTALLED, detail=detail)

    def cancel(self) -> None:
        """处理取消命令：只取消排队或下载中的安装。"""
        task = self._task
        if self.dev_mode or task is None or task.done() or self.state not in BUSY_STATES:
            self.logger.debug("%s 没有在安装，忽略取消命令", self.title)
            return
        if self.state is OcrEngineState.INSTALLING:
            self.logger.info("%s 正在完成安装，不能再取消", self.title)
            return
        self.logger.info("取消 %s 的安装", self.title)
        task.cancel()

    async def uninstall(self) -> None:
        """处理卸载命令：删除运行环境与子类的模型。安装中、卸载中或源码运行时忽略。"""
        if self.dev_mode:
            self.logger.info("源码运行，不卸载 %s，以免改动开发环境 %s", self.title, self.dev_venv())
            return
        if self.state in BUSY_STATES:
            self.logger.info("%s 正在安装，忽略卸载命令；先取消安装", self.title)
            return
        if self._uninstalling:
            self.logger.debug("%s 已在卸载中，忽略重复的卸载命令", self.title)
            return
        # 在第一个 await 之前同步设标志：卸载期间到达的安装命令被忽略，不会一边删文件一边装。
        self._uninstalling = True
        try:
            for job in list(self._jobs.values()):
                if job.task is not None and not job.task.done():
                    job.task.cancel()
                    await asyncio.gather(job.task, return_exceptions=True)
            await self._uninstall()
        finally:
            self._uninstalling = False

    async def stop(self) -> None:
        """取消进行中的安装与模型下载（删除半成品）。"""
        tasks = [self._task] + [job.task for job in self._jobs.values()]
        pending = [task for task in tasks if task is not None and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _announce(self, installed: bool) -> None:
        if not self._host.closed:
            self._host.announce(OcrEnginesChanged(self.kind, installed))

    def _uninstall_files(self, paths: RuntimeInstallPaths) -> None:
        """先删安装标记，再删运行环境、uv 缓存与子类的模型。中途失败时引擎不会被误判为已安装。"""
        for target in (
            paths.marker,
            paths.marker_part,
            paths.site_part,
            paths.uv_cache,
            paths.site_dir,
            *self.uninstall_targets(paths),
        ):
            remove_tree(target)

    async def _uninstall(self) -> None:
        was_installed = self.can_run()
        before = self._options.before_uninstall
        if before is not None:
            await before(self.kind)
        paths = self.require_paths()
        try:
            await blocking(self._uninstall_files, paths)
        except OSError as exc:
            self.logger.warning("卸载 %s 失败：%s", self.title, exc)
            installed = await blocking(self._installed, paths)
            self._runtime_intact = installed
            await self.scan_models()
            if installed:
                self.set_state(OcrEngineState.INSTALLED)
            else:
                self._installed_variant = None
                self.set_state(
                    OcrEngineState.ERROR, error=OcrEngineErrorCode.DISK, detail=describe_error(exc)
                )
            if was_installed and not installed:
                self._announce(False)
            return
        self.logger.info("已卸载 %s：删除了 %s", self.title, paths.engine_dir)
        self._installed_variant = None
        self._runtime_intact = False
        self._jobs.clear()
        await self.scan_models()
        self.set_state(OcrEngineState.NOT_INSTALLED)
        if was_installed:
            self._announce(False)

    # ------------------------------------------------------------------ 安装

    async def _install(self, variant: OcrRuntimeVariant) -> None:
        """安装。失败只记录在状态里，不抛出；取消时清理半成品后把取消传出去。"""
        paths = self.require_paths()
        try:
            await self._install_steps(paths, variant)
        except asyncio.CancelledError:
            await self._cleanup(paths)
            self.logger.info("已取消 %s 的安装", self.title)
            await self.scan_models()
            await self._settle_after_abort()
            raise
        except InstallFailure as failure:
            await self._fail(paths, failure.code, failure.detail)
            return
        except InvalidArchive as exc:
            await self._fail(paths, OcrEngineErrorCode.INVALID_PACKAGE, str(exc))
            return
        except OSError as exc:
            await self._fail(paths, OcrEngineErrorCode.DISK, describe_error(exc))
            return
        except Exception as exc:
            self.logger.exception("安装 %s 时出现未预期的错误", self.title)
            await self._fail(paths, OcrEngineErrorCode.INTERNAL, describe_error(exc))
            return
        finally:
            self._current_spec = None
            self._target_variant = None
        self.logger.info("%s %s 版安装完成", self.title, variant.value)
        self._installed_variant = variant
        self._runtime_intact = True
        await self.scan_models()
        self.set_state(OcrEngineState.INSTALLED)
        self._announce(True)

    def _pending_models(self) -> list[ModelSpec]:
        """默认组合里需要下载、还没就位的模型（在线程里执行）。"""
        return [
            spec
            for spec in self.model_specs()
            if spec.default and not spec.bundled and not self.model_is_ready(spec)
        ]

    async def _install_steps(self, paths: RuntimeInstallPaths, variant: OcrRuntimeVariant) -> None:
        requirements = paths.requirements(variant)
        for required, label in ((paths.uv, "uv"), (paths.python, "Python"), (requirements, "版本清单")):
            if not await blocking(required.is_file):
                raise InstallFailure(
                    OcrEngineErrorCode.INTERNAL, f"安装目录不完整，找不到{label}：{required}"
                )
        requirements_sha256 = await blocking(file_sha256, requirements)
        stamp = await blocking(runtime_stamp, paths.site_dir)
        runtime_needed = stamp != requirements_sha256 or not await blocking(self.runtime_complete, paths)
        pending = await blocking(self._pending_models)
        runtime_weight = float(self._runtime_bytes.get(variant, 1) or 1)
        self._weights = {RUNTIME_PART: runtime_weight} if runtime_needed else {}
        self._weights.update({spec.id: float(spec.size_bytes or UNKNOWN_MODEL_WEIGHT) for spec in pending})
        self._fractions = dict.fromkeys(self._weights, 0.0)
        self._last_report = self._options.clock()
        self.set_state(OcrEngineState.DOWNLOADING)
        if runtime_needed:
            await self._install_runtime(paths, requirements, requirements_sha256, runtime_weight)
        else:
            self.logger.info("%s 运行环境（%s 版）已就位，跳过", self.title, variant.value)
        for spec in pending:
            self._current_spec = spec
            await self.fetch_model(spec, lambda fraction, part=spec.id: self.advance(part, fraction))
            self._current_spec = None
            self.advance(spec.id, 1.0, force=True)
        self.set_state(OcrEngineState.INSTALLING)
        self._progress = 1.0
        await blocking(self._write_marker, paths, variant)

    def advance(self, part: str, fraction: float, *, force: bool = False) -> None:
        """更新一段的完成比例，按节流间隔发布进度。进度只增不减。"""
        if part not in self._fractions:
            return
        self._fractions[part] = max(self._fractions[part], min(1.0, fraction))
        total = sum(self._weights.values())
        value = (
            sum(self._weights[key] * self._fractions[key] for key in self._weights) / total if total else 1.0
        )
        self._progress = max(self._progress or 0.0, min(1.0, value))
        now = self._options.clock()
        if force or now - self._last_report >= self._options.progress_interval:
            self._last_report = now
            self._host.publish()

    async def _install_runtime(
        self, paths: RuntimeInstallPaths, requirements: Path, requirements_sha256: str, weight: float
    ) -> None:
        options = self._options
        await blocking(remove_tree, paths.site_part)
        await blocking(remove_tree, paths.uv_cache)
        await blocking(paths.engine_dir.mkdir, parents=True, exist_ok=True)
        text = await blocking(requirements.read_text, encoding="utf-8")
        args = uv_install_args(
            paths.uv,
            paths.python,
            paths.site_part,
            requirements,
            hashed=requires_hashes(text),
            index_url=options.index_url,
            resolve=requires_resolution(text),
        )
        base = options.environ if options.environ is not None else environment_from_os()
        env = uv_environment(base, paths.uv_cache)
        self.logger.info("开始安装 %s 运行环境：%s", self.title, " ".join(args[1:4]))
        monitor = self._host.spawn(
            self._watch_runtime(paths.uv_cache, weight), f"ocr_engines:runtime-progress:{self.kind.value}"
        )
        try:
            result = await options.runner(args, env)
        except OSError as exc:
            raise InstallFailure(
                OcrEngineErrorCode.INSTALL_FAILED, f"无法运行 uv：{describe_error(exc)}"
            ) from exc
        finally:
            monitor.cancel()
            await asyncio.gather(monitor, return_exceptions=True)
        if result.returncode != 0:
            raise InstallFailure(classify_uv_failure(result.output), failure_detail(result))
        if options.before_runtime_update is not None and await blocking(paths.site_dir.is_dir):
            # 旧的运行环境可能正被 OCR 进程使用（DLL 被占用时删不掉），先请组合根停掉进程。
            await options.before_runtime_update(self.kind)
        # 从这里起旧运行环境被替换，失败时不能再按旧版本运行。
        if self._runtime_intact:
            self._runtime_intact = False
            self._host.publish()
        await blocking(commit_runtime, paths, requirements_sha256)
        self.logger.info("%s 运行环境已安装到 %s", self.title, paths.site_dir)
        self.advance(RUNTIME_PART, 1.0, force=True)

    async def _watch_runtime(self, cache: Path, weight: float) -> None:
        """每隔 poll_interval 秒按 uv 缓存目录的大小估计运行环境阶段的进度。"""
        while True:
            await asyncio.sleep(self._options.poll_interval)
            size = await blocking(dir_size, cache)
            self.advance(RUNTIME_PART, min(RUNTIME_PROGRESS_CAP, size / weight))

    def _write_marker(self, paths: RuntimeInstallPaths, variant: OcrRuntimeVariant) -> None:
        missing = self.missing_parts(paths) + [f"模型 {spec.id}" for spec in self._pending_models()]
        if missing or not paths.site_dir.is_dir():
            raise InstallFailure(
                OcrEngineErrorCode.INVALID_PACKAGE,
                "安装完成后复查失败："
                + ("缺少运行环境" if not paths.site_dir.is_dir() else "缺少 " + ", ".join(missing)),
            )
        paths.engine_dir.mkdir(parents=True, exist_ok=True)
        temporary = paths.marker_part
        defaults = [spec.id for spec in self.model_specs() if spec.default]
        temporary.write_text(
            json.dumps(
                {
                    "runtime_requirements_sha256": runtime_stamp(paths.site_dir),
                    "variant": variant.value,
                    "models": defaults,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, paths.marker)

    async def _cleanup(self, paths: RuntimeInstallPaths) -> None:
        """删除半成品：site.part、uv 缓存、安装标记的临时文件，以及正在下载的模型的临时文件。"""
        targets = [paths.site_part, paths.uv_cache, paths.marker_part]
        if self._current_spec is not None:
            targets += self.model_leftovers(self._current_spec)
        try:
            await blocking(remove_leftovers, targets, self.leftover_dir(paths), self.logger)
        except OSError:
            self.logger.warning("清理 %s 的安装半成品失败", self.title, exc_info=True)

    async def _fail(self, paths: RuntimeInstallPaths, code: OcrEngineErrorCode, detail: str) -> None:
        await self._cleanup(paths)
        await self.scan_models()
        self.logger.warning("安装 %s 失败（%s）：%s", self.title, code.value, detail)
        if not self._runtime_intact:
            self._installed_variant = None
        self.set_state(OcrEngineState.ERROR, error=code, detail=detail)

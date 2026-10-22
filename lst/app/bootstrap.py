"""组合根：创建全部模块、按固定顺序启动与停止，并把它们需要的端口注入进去（架构文档 4.3、4.4 节）。

启动顺序：配置 → 统计 → 缓存 → 模型 → 引擎 → 竞速 → 显卡运行库 → OCR 引擎 → OCR → 应用更新 → 存储位置
→ 截屏 → 覆盖窗口 → 托盘 → 热键 → 双语窗口 → 设置窗口 → 自动化接口 →（AppOptions.extra_modules）
→ 首次运行引导。
- 托盘先于热键启动：热键在收到配置时注册，注册失败的通知要有托盘接收；
- 配置服务最先启动，它在启动时发现的配置问题（ConfigRejected，是 Event，不回放）早于其他模块订阅，
  全部模块启动之后由这里补发一次，托盘与设置窗口都能收到；
- NMT 进程在配置就绪之后按 [nmt] 节启动，[nmt] 节变化时重载；
- OCR 进程按 [ocr] 节与当前引擎的流水线配置节（[ocr_rapid]、[ocr_paddle]）启动（需求 OCR-07 的后台预加载），
  启动命令随当前 OCR 引擎而定（OCRE-01）：Windows 自带 OCR 用主环境，
  RapidOCR 与 PaddleOCR 各用自己的运行环境。
  当前引擎不可用（Windows 无 OCR 语言、引擎未安装）时不启动。决定进程加载内容的字段（process_key：引擎、
  识别模式、设备、识别语言、流水线配置里带 reload 标记的字段）、已装的运行环境版本、选用模型是否就位，
  或引擎目录变化（OcrEngineCatalog）时重新决定；子能力开关与请求级参数随每个请求发送，不重载
  （架构文档 8.4 节）。OCR 进程的状态合并进 OcrEngineCatalog；
- 推理进程进入 FAILED 后，托盘或设置窗口发出 RestartWorker，这里按当前配置重新启动该进程；OCR 进程进入
  FAILED 后，决定进程的依据（去重键）变化时也会重新启动它，例如改了识别语言或推理设备、装好了运行环境的
  另一个版本、装了 Windows OCR 语言；与 OCR 无关的配置变化不重启，否则每次保存任何设置都会把一个确定性
  故障的进程重新拉起，设置窗口一直显示“正在加载”；
- 配置、引擎目录、RestartWorker、模型变化引起的进程决策都在同一把锁里串行执行，每次读取总线上最新的
  AppConfig：总线把每个协程处理函数包成独立的任务，不串行化时先到的旧配置可能在后到的新配置之后才应用；
- 应用更新服务检查 GitHub Releases（UPD-01 至 UPD-06）；
- 模型目录（[storage].models_dir，默认是数据目录下的 models）在配置服务启动之后才知道，所以用到它的模块
  （模型管理、OCR 引擎、显卡运行库、引擎模块的词典目录）都在配置服务之后创建。启动时先把旧版本放在数据目录里
  的 engines、runtimes 改名到模型目录下（storage.migrate_legacy_layout）；
- 模型目录在运行中改变时（设置里更改，或手工改了配置文件），组合根先停下 NMT 与 OCR 进程和上面三个模块，
  再按新目录重建模块、换词典目录、重新启动进程（suspend_models、resume_models，由存储位置模块调用，
  见 storage_module.py）。停下期间配置变化只更新界面语言与主题，进程决策等恢复后按最新配置一次做完；
- 模型管理模块的 NMT 模型目录由 [nmt] 节解析（model_dir 单独指定，留空时用模型目录下的 nmt），与 NMT 进程
  用同一个目录；它在运行中改变时换一个新的模型管理模块。模型装好或删除后（ModelsInstalled）重载 NMT 进程；
- 存储位置模块的 RestartApp 由这里记下“退出后重新启动”，入口在释放单实例锁之后启动新进程（__main__.py）；
- 自动化接口（lst.app.automation，架构文档第 16 章）直接操作截屏、覆盖窗口与设置窗口，所以在它们之后启动，
  停止时先于它们关掉管道。开关的默认值随构建类型（AppOptions.flavor）；
- 首次运行引导最后启动：它在 start 里可能立即弹出窗口，这时其他模块都已就绪。
退出时按相反顺序停止，再停推理进程、关缓存、关 HTTP 客户端。
"""

import asyncio
import hashlib
import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from lst.app.automation import CONFIG_SECTIONS as AUTOMATION_SECTIONS
from lst.app.automation import AutomationDeps, AutomationModule
from lst.app.paths import (
    INSTALL_MARKER,
    PROGRAM_DIR,
    AppPaths,
    DataDirChoice,
    install_root,
    is_installed,
)
from lst.app.storage import migrate_legacy_layout
from lst.app.storage_module import StorageModule
from lst.app.supervisor import WorkerSupervisor
from lst.contracts.automation import BuildFlavor
from lst.contracts.config import AppConfig
from lst.contracts.events import (
    NoticeLevel,
    NotificationRequested,
    QuitRequested,
    RestartWorker,
    WorkerStatus,
    WorkerStatuses,
)
from lst.contracts.models import ModelCatalog, ModelKind, ModelsInstalled, RefreshModelCatalog
from lst.contracts.nmt_runtime import NmtRuntimeChanged
from lst.contracts.ocr_engines import OcrEngineCatalog, OcrEngineErrorCode, OcrEngineKind, OcrEngineState
from lst.contracts.ports import WorkerState
from lst.contracts.storage import DataDirSource
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, Module, ModuleContext
from lst.services.cache import CacheModule, SqliteTranslationCache
from lst.services.config import ConfigService, KeyringSecretStore, RedactingFilter, SecretStore
from lst.services.engines import ENGINE_KINDS, EngineDeps, EngineListSchema, EnginesModule
from lst.services.models import DEFAULT_DICT_INDEX_URL, DEFAULT_INDEX_URL, ModelsModule
from lst.services.nmt_runtime import NmtRuntimeModule, installed_bin_dir
from lst.services.ocr import CONFIG_SECTIONS as OCR_SECTIONS
from lst.services.ocr import (
    PIPELINE_SECTIONS,
    OcrConfig,
    OcrModule,
    PipelineConfig,
    pipeline_or_default,
    process_key,
    worker_settings,
)
from lst.services.ocr_engines import (
    DevPaddleEnvironment,
    DevRapidEnvironment,
    OcrEnginesModule,
    PaddleInstallPaths,
    RapidInstallPaths,
)
from lst.services.race import CONFIG_SECTIONS as RACE_SECTIONS
from lst.services.race import RaceModule
from lst.services.stats import SqliteStatsStore, StatsModule
from lst.services.updates import CONFIG_SECTIONS as UPDATE_SECTIONS
from lst.services.updates import UpdatesModule, running_version
from lst.ui import i18n
from lst.ui.bilingual import BilingualModule
from lst.ui.capture import CaptureModule
from lst.ui.common.bindings import CONFIG_SECTIONS as BINDING_SECTIONS
from lst.ui.hotkeys import CONFIG_SECTIONS as HOTKEY_SECTIONS
from lst.ui.hotkeys import HotkeysModule
from lst.ui.onboarding import OnboardingModule
from lst.ui.overlay import OverlayModule
from lst.ui.settings import SettingsModule
from lst.ui.theme import CONFIG_SECTIONS as UI_SECTIONS
from lst.ui.theme import UiConfig, apply_app_style
from lst.ui.tray import TrayModule

logger = logging.getLogger(__name__)

APP_NAME = "LSTtranslate"
LEGACY_NMT_MODELS = PROGRAM_DIR / "assets" / "argosmodel"
LEGACY_OCR_MODELS = PROGRAM_DIR / "assets" / "PaddleOCR_and_PP-Structure"


class NmtConfig(BaseModel):
    """配置文件的 [nmt] 节：NMT 进程的设置（架构文档 2.5、8.3 节）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = Field(default=True, description="是否启动本地 NMT 进程")
    model_dir: str = Field(
        default="",
        description="单独指定存放 .argosmodel 的目录；留空时用模型目录下的 nmt（[storage].models_dir）",
    )
    device: Literal["auto", "cpu", "cuda"] = Field(default="auto", description="推理设备")
    compute_type: str = Field(default="auto", description="CTranslate2 计算类型")
    beam_size: int = Field(default=2, ge=1, le=8, description="束搜索宽度")


class StorageConfig(BaseModel):
    """配置文件的 [storage] 节：模型目录（需求 MOD-04、CFG-07，架构文档 2.9.5 节）。

    数据目录不在这里：配置文件本身在数据目录里，数据目录记在数据目录之外的位置文件里（paths.py）。
    这一节由存储位置模块在移动模型之后写入，设置窗口不直接修改它。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    models_dir: str = Field(
        default="",
        description="模型目录：本地翻译模型、OCR 模型与引擎运行环境、词典包、显卡运行库都放在这里；"
        "留空时用数据目录下的 models",
    )

    @field_validator("models_dir")
    @classmethod
    def _absolute(cls, value: str) -> str:
        if value and not Path(value).is_absolute():
            raise ValueError("模型目录必须是绝对路径")
        return value


APP_SECTIONS: dict[str, type[BaseModel]] = {
    "nmt": NmtConfig,
    "storage": StorageConfig,
    **AUTOMATION_SECTIONS,
}

i18n.register_catalog(
    {
        "app.worker_crashed.title": {"zh-Hans": "推理进程已重启", "en": "Inference process restarted"},
        "app.worker_crashed.message": {
            "zh-Hans": "{worker}意外退出，正在自动重启",
            "en": "{worker} exited unexpectedly and is restarting",
        },
        "app.worker_failed.title": {"zh-Hans": "推理进程已停用", "en": "Inference process disabled"},
        "app.worker_failed.message": {
            "zh-Hans": "{worker}反复出错，已停止重启。请检查设置或导出诊断信息",
            "en": "{worker} keeps failing and was stopped. Check the settings or export diagnostics",
        },
        "app.second_instance.title": {
            "zh-Hans": "LSTtranslate 已在运行",
            "en": "LSTtranslate is already running",
        },
        "app.second_instance.message": {
            "zh-Hans": "程序已在系统托盘中运行",
            "en": "The program is already running in the system tray",
        },
        "app.data_dir_fallback.title": {"zh-Hans": "数据目录不可用", "en": "Data folder unavailable"},
        "app.data_dir_fallback.message": {
            "zh-Hans": "选定的数据目录 {path} 无法使用（例如所在的磁盘没有接上），本次改用默认数据目录",
            "en": "The chosen data folder {path} cannot be used (for example, its drive is not connected). "
            "The default data folder is used this time",
        },
    }
)


def all_config_sections() -> dict[str, type[BaseModel]]:
    """全部普通配置节。节名重复时抛 ValueError（配置服务也会检查）。"""
    sections: dict[str, type[BaseModel]] = {}
    for group in (
        APP_SECTIONS,
        UI_SECTIONS,
        HOTKEY_SECTIONS,
        BINDING_SECTIONS,
        RACE_SECTIONS,
        OCR_SECTIONS,
        UPDATE_SECTIONS,
    ):
        overlap = sections.keys() & group.keys()
        if overlap:
            raise ValueError(f"配置节名重复：{sorted(overlap)}")
        sections.update(group)
    return sections


def nmt_worker_settings(
    config: NmtConfig, paths: AppPaths, cuda_dll_dir: Path | None = None
) -> dict[str, JsonValue]:
    """由 [nmt] 节生成 NMT 进程的 Configure 设置。

    model_dir 留空时用模型目录下的 nmt；那里没有模型而旧项目的 assets/argosmodel 里有时，用旧目录，
    开发时不必复制模型就能直接运行。解压目录与 CUDA 编译缓存始终放在模型目录下的 nmt 里。
    cuda_dll_dir 是设置里下载的显卡运行库的 DLL 目录（需求 MOD-06），没有装时不传，NMT 进程只在开发环境的
    nvidia 包里找 cuBLAS，找不到就用 CPU。
    """
    if config.model_dir:
        model_dir = Path(config.model_dir)
    elif not any(paths.nmt_models_dir.glob("*.argosmodel")) and any(LEGACY_NMT_MODELS.glob("*.argosmodel")):
        model_dir = LEGACY_NMT_MODELS
    else:
        model_dir = paths.nmt_models_dir
    return {
        "model_dir": str(model_dir),
        "extract_dir": str(paths.nmt_models_dir / ".extracted"),
        "cuda_cache_dir": str(paths.nmt_models_dir / ".cuda-cache"),
        **({"cuda_dll_dir": str(cuda_dll_dir)} if cuda_dll_dir is not None else {}),
        "device": config.device,
        "compute_type": config.compute_type,
        "beam_size": config.beam_size,
    }


def ocr_model_cache(paths: AppPaths) -> Path:
    """OCR 模型缓存目录（PaddleX 的 PADDLE_PDX_CACHE_HOME，模型在其中的 official_models/ 下）。

    模型目录下的 ocr 里没有模型而旧项目的 assets/PaddleOCR_and_PP-Structure 里有时用旧目录，
    与 NMT 模型的回退相同。“有模型”指 official_models 下至少有一个子目录。
    """
    if not _has_models(paths.ocr_models_dir) and _has_models(LEGACY_OCR_MODELS):
        return LEGACY_OCR_MODELS
    return paths.ocr_models_dir


def _has_models(cache: Path) -> bool:
    """official_models 下至少有一个模型子目录，与 OCR 引擎模块判断源码版环境可用的条件相同。"""
    models = cache / "official_models"
    return models.is_dir() and any(entry.is_dir() for entry in models.iterdir())


def ocr_process_settings(
    config: OcrConfig,
    cache: Path,
    pipeline: PipelineConfig | None = None,
    models: Mapping[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    """OCR 进程的 Configure 内容：services.ocr 给出的字段（含流水线配置），
    加上模型缓存目录与 services.ocr_engines
    给出的模型信息（RapidOCR 的模型文件路径与识别语言）。"""
    return {**worker_settings(config, pipeline), **(models or {}), "model_cache_dir": str(cache)}


WINOCR_BACKEND = "lst.workers.ocr_worker.winocr_backend:WinOcrBackend"
RAPID_BACKEND = "lst.workers.ocr_worker.rapid_backend:RapidBackend"


def paddle_layout(paths: AppPaths) -> PaddleInstallPaths | DevPaddleEnvironment:
    """PaddleOCR 引擎的目录：安装版按需安装到模型目录，源码版使用 .venv-ocr 与已有模型（只检查，不修改）。"""
    if is_installed():
        root = install_root()
        return PaddleInstallPaths(
            engine_dir=paths.engines_dir / "paddle",
            model_cache_dir=paths.ocr_models_dir,
            uv=root / "tools" / "uv.exe",
            python=root / "runtime" / "python.exe",
            requirements_dir=root / "app",
        )
    return DevPaddleEnvironment(PROGRAM_DIR / ".venv-ocr", (paths.ocr_models_dir, LEGACY_OCR_MODELS))


def rapid_layout(paths: AppPaths) -> RapidInstallPaths | DevRapidEnvironment:
    """RapidOCR 引擎的目录：安装版按需安装到模型目录，源码版使用 .venv-ocr（只检查，不修改）。

    另下载的模型两种情况都放在模型目录的 ocr/rapid。
    """
    model_dir = paths.ocr_models_dir / "rapid"
    if is_installed():
        root = install_root()
        return RapidInstallPaths(
            engine_dir=paths.engines_dir / "rapid",
            model_dir=model_dir,
            uv=root / "tools" / "uv.exe",
            python=root / "runtime" / "python.exe",
            requirements_dir=root / "app",
        )
    return DevRapidEnvironment(PROGRAM_DIR / ".venv-ocr", model_dir)


def installed_worker_prefix(kind: Literal["nmt", "ocr"]) -> tuple[str, ...]:
    r"""安装版推理进程启动命令的公共前缀：安装目录里的解释器以隔离模式（-I）和 UTF-8 模式运行
    launcher\worker.py，第一个参数是进程种类。各进程的参数接在后面。
    """
    root = install_root()
    return (
        str(root / "runtime" / "python.exe"),
        "-I",
        "-X",
        "utf8",
        str(root / "launcher" / "worker.py"),
        kind,
    )


def ocr_worker_command(engine: OcrEngineKind, paths: AppPaths) -> tuple[str, ...]:
    r"""OCR 进程的启动命令（不含 --address）。调用前组合根已确认引擎可用。

    - Windows 自带 OCR：主环境解释器加 Windows OCR 后端；
    - RapidOCR：安装版经启动器使用模型目录下的 engines\rapid\site（--ocr-site）加 RapidOCR 后端，
      源码版用 .venv-ocr；
    - PaddleOCR：安装版经启动器使用模型目录下的 engines\paddle\site，源码版用 .venv-ocr，
      后端是默认的 PaddleOCR 后端。

    各引擎的不同运行环境版本（CPU、DirectML、CUDA）装在同一个 site 目录里（换版本即重装），命令不随版本变化；
    组合根把已装版本算进进程决策的依据，换版本后会用同一条命令重启进程。
    """
    tail = ("--log-level", "WARNING")
    if is_installed():
        base = installed_worker_prefix("ocr")
        if engine is OcrEngineKind.WINDOWS:
            return (*base, "--ocr-site", "main", "--backend", WINOCR_BACKEND, *tail)
        if engine is OcrEngineKind.RAPID:
            rapid = rapid_layout(paths)
            assert isinstance(rapid, RapidInstallPaths)
            return (*base, "--ocr-site", str(rapid.site_dir), "--backend", RAPID_BACKEND, *tail)
        layout = paddle_layout(paths)
        assert isinstance(layout, PaddleInstallPaths)
        return (*base, "--ocr-site", str(layout.site_dir), *tail)
    if engine is OcrEngineKind.WINDOWS:
        return (sys.executable, "-m", "lst.workers.ocr_worker", "--backend", WINOCR_BACKEND, *tail)
    ocr_python = PROGRAM_DIR / ".venv-ocr" / "Scripts" / "python.exe"
    if engine is OcrEngineKind.RAPID:
        return (str(ocr_python), "-m", "lst.workers.ocr_worker", "--backend", RAPID_BACKEND, *tail)
    return (str(ocr_python), "-m", "lst.workers.ocr_worker", *tail)


OCR_FAILURE_CODES: Final = {
    "DEPENDENCY_ERROR": OcrEngineErrorCode.RUNTIME_MISMATCH,
    "CONFIG_ERROR": OcrEngineErrorCode.BAD_CONFIG,
}
"""监管器给出的确定性故障名 → 引擎目录里的错误码。界面按错误码显示原因与处理建议。"""


def ocr_failure_code(detail: str) -> OcrEngineErrorCode:
    """OCR 进程进入 FAILED 时的错误码：确定性故障按故障名区分，其余（反复崩溃）记为 WORKER_FAILED。"""
    return next(
        (code for name, code in OCR_FAILURE_CODES.items() if name in detail), OcrEngineErrorCode.WORKER_FAILED
    )


def worker_command(kind: Literal["nmt"]) -> tuple[str, ...]:
    r"""NMT 进程的启动命令（不含 --address，监管器会追加）。

    - 安装版：用安装目录里的解释器经 launcher\worker.py 启动，它使用 site\main；
    - 源码版：用当前解释器（主进程的 .venv）。
    """
    if is_installed():
        return (*installed_worker_prefix(kind), "--log-level", "WARNING")
    return (sys.executable, "-m", "lst.workers.nmt_worker", "--log-level", "WARNING")


def _section[M: BaseModel](config: AppConfig, name: str, model: type[M]) -> M:
    """解析一节配置。数据已经由配置服务校验过，解析失败只可能是节缺失，这时用默认值。"""
    raw = config.section(name)
    try:
        return model.model_validate(raw if isinstance(raw, dict) else {})
    except ValueError:
        logger.warning("配置节 %s 无法解析，使用默认值", name)
        return model()


@dataclass
class AppOptions:
    """组合根的可注入选项，测试用来替换外部依赖。"""

    paths: AppPaths
    secrets: SecretStore | None = None
    nmt_command: Sequence[str] = field(default_factory=lambda: worker_command("nmt"))
    ocr_command: Callable[[OcrEngineKind], Sequence[str]] | Literal["auto"] | None = "auto"
    """OCR 进程的启动命令：按引擎给出命令的函数；"auto" 用 ocr_worker_command；
    None 不启动 OCR 进程（测试用）。
    """
    updates_startup_delay: float = 30.0
    """启动后多久第一次检查更新（秒）。"""
    tray_icon: Path | None = PROGRAM_DIR / "assets" / "icons" / "lsttranslate.ico"
    extra_modules: Sequence[Module] = field(default_factory=lambda: ())
    http_transport: httpx.AsyncBaseTransport | None = None
    """HTTP 客户端的传输层。测试注入 httpx.MockTransport，保证不发出真实网络请求。"""
    models_index_url: str = DEFAULT_INDEX_URL
    dict_index_url: str = DEFAULT_DICT_INDEX_URL
    data: DataDirChoice | None = None
    """数据目录的来源与位置文件（入口按 choose_data_dir 给出）。None 表示数据目录由调用方固定（测试），
    设置里不能更改数据目录。"""
    flavor: BuildFlavor = BuildFlavor.NORMAL
    """构建类型（lst/app/flavor.py）：决定 [automation].enabled 的默认值与应用内更新下载哪一种安装包。"""
    automation_pipe: str | None = None
    """自动化接口的管道名；None 时按当前用户生成（或取环境变量 LST_AUTOMATION_PIPE）。
    测试用它避开正在运行的实例。"""


class Application:
    """一次程序运行。start() 启动全部模块，wait_closed() 等待退出请求，stop() 停止全部模块。"""

    def __init__(self, options: AppOptions, qt_app: QApplication | None = None) -> None:
        self._options = options
        self._paths = options.paths
        self._qt_app = qt_app
        self._quit = asyncio.Event()
        self._started: list[tuple[Module, ModuleContext]] = []
        self._worker_statuses: dict[str, WorkerStatus] = {}
        self._nmt_settings: dict[str, JsonValue] | None = None
        self._nmt_enabled = False
        self._ui_config: UiConfig | None = None
        self._models_dir: Path | None = None
        self._nmt_reload: asyncio.Task[None] | None = None
        self._nmt_reload_again = False
        self._ocr_key: tuple[object, ...] | None = None
        self._ocr_engine: OcrEngineKind | None = None
        self._ocr_substituted: OcrEngineKind | None = None
        """配置的引擎没有装好、OCR 进程改跑 Windows OCR 时为配置的引擎，用来只在变化时记一次警告。"""
        self._engines_fingerprint: tuple[object, ...] | None = None
        # 进程决策的锁与停止标志，见模块说明。
        self._decide = asyncio.Lock()
        self._stopping = False

        self.bus: EventBus | None = None
        self.redactor = RedactingFilter()
        self.secrets: SecretStore = options.secrets if options.secrets is not None else KeyringSecretStore()
        self.client: httpx.AsyncClient | None = None
        self.cache: SqliteTranslationCache | None = None
        self.nmt: WorkerSupervisor | None = None
        self.ocr: WorkerSupervisor | None = None
        self.ocr_engines: OcrEnginesModule | None = None
        self.config_service: ConfigService | None = None
        self.models: ModelsModule | None = None
        self.engines: EnginesModule | None = None
        self.nmt_runtime: NmtRuntimeModule | None = None
        self.storage: StorageModule | None = None
        self.onboarding: OnboardingModule | None = None
        self.automation: AutomationModule | None = None
        self._suspended = False
        """模型目录正在更换：进程与用到模型目录的模块已经停下，进程决策暂停。"""
        self._refresh_index = False
        self._restart = False

    @property
    def quit_requested(self) -> asyncio.Event:
        return self._quit

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop)
        self.bus = bus
        self._paths.ensure()
        self._apply_window_icon()
        self.client = httpx.AsyncClient(transport=self._options.http_transport)
        self.nmt = WorkerSupervisor(
            "nmt",
            self._options.nmt_command,
            settings={},
            cwd=install_root(),
            on_state=self._on_worker_state("nmt"),
            logger=logging.getLogger("lst.app.supervisor.nmt"),
        )
        if self._options.ocr_command is not None:
            # 启动命令随引擎在 _apply_ocr 第一次 reload 时给出。
            self.ocr = WorkerSupervisor(
                "ocr",
                None,
                settings={},
                cwd=install_root(),
                on_state=self._on_worker_state("ocr"),
                logger=logging.getLogger("lst.app.supervisor.ocr"),
            )
        self.cache = SqliteTranslationCache(self._paths.cache_db)
        await self.cache.open()

        engine_schema = EngineListSchema()
        self.config_service = ConfigService(
            self._paths.config_file,
            sections=all_config_sections(),
            list_sections={"engines": engine_schema},
            secrets=self.secrets,
            redactor=self.redactor,
        )
        self._subscribe(bus)

        # 配置服务发布 AppConfig 时总线立即记下最新值，之后的模块据此决定构造参数。
        await self._start_module(bus, self.config_service)
        storage = self._latest_section("storage", StorageConfig)
        self._paths = self._paths.with_models_dir(storage.models_dir)
        await asyncio.to_thread(migrate_legacy_layout, self._paths.data_dir, self._paths.models_root)
        await asyncio.to_thread(self._paths.ensure_models)
        self._models_dir = self._resolve_models_dir()
        self.models = self._new_models_module(self._models_dir)
        self.onboarding = OnboardingModule(self._paths.ui_state_file)
        self.ocr_engines = self._new_ocr_engines()
        self.nmt_runtime = self._new_nmt_runtime()
        self.engines = EnginesModule(
            EngineDeps(
                secrets=self.secrets,
                client=self.client,
                nmt=self.nmt,
                dict_dir=self._paths.dict_models_dir,
            )
        )
        self.storage = StorageModule(lambda: self._paths, self._data_choice(), self)
        version = running_version(INSTALL_MARKER)
        capture = CaptureModule()
        overlay = OverlayModule()
        settings = SettingsModule(
            self._paths.ui_state_file,
            sections=all_config_sections(),
            engine_kinds={kind: spec.settings_model for kind, spec in ENGINE_KINDS.items()},
            flavor=self._options.flavor,
        )
        self.automation = AutomationModule(
            AutomationDeps(
                flavor=self._options.flavor,
                version=version,
                installed=is_installed(),
                paths=lambda: self._paths,
                capture=capture,
                overlay=overlay,
                settings=settings,
            ),
            pipe_name=self._options.automation_pipe,
        )
        modules: list[Module] = [
            StatsModule(SqliteStatsStore(self._paths.stats_db)),
            CacheModule(self.cache),
            self.models,
            self.engines,
            RaceModule(cache=self.cache),
            self.nmt_runtime,
            self.ocr_engines,
            *([OcrModule(self.ocr)] if self.ocr is not None else []),
            UpdatesModule(
                self.client,
                current_version=version,
                can_install=is_installed(),
                flavor=self._options.flavor,
                record_file=self._paths.data_dir / "updates.json",
                download_dir=self._paths.tmp_dir / "updates",
                startup_delay=self._options.updates_startup_delay,
            ),
            self.storage,
            # 界面模块从这里开始。截屏与覆盖窗口在全部业务模块之后启动：CaptureCompleted 的订阅顺序是
            # OCR、竞速在前，覆盖窗口在后，识别请求先提交，再创建窗口。
            capture,
            overlay,
            TrayModule(APP_NAME, self._options.tray_icon, self._paths.logs_dir),
            HotkeysModule(),
            BilingualModule(self._paths.ui_state_file),
            settings,
            self.automation,
            *self._options.extra_modules,
            self.onboarding,
        ]
        for module in modules:
            await self._start_module(bus, module)
        # 全部模块都已订阅，补发配置服务在启动时发现的问题（它们是 Event，不会回放）。
        for rejection in self.config_service.startup_rejections:
            bus.publish(rejection)
        fallback = self._options.data.fallback if self._options.data is not None else None
        if fallback is not None:
            bus.publish(
                NotificationRequested(
                    "app.data_dir_fallback.title",
                    "app.data_dir_fallback.message",
                    NoticeLevel.WARNING,
                    {"path": str(fallback)},
                )
            )
        self._publish_worker_statuses()
        logger.info("全部模块已启动")

    def _data_choice(self) -> DataDirChoice:
        """入口给出的数据目录来源；测试直接给 AppPaths 时按命令行参数指定处理，设置里不能更改数据目录。"""
        if self._options.data is not None:
            return self._options.data
        return DataDirChoice(
            self._paths.data_dir, DataDirSource.ARGUMENT, self._paths.data_dir / "location.json"
        )

    def _apply_window_icon(self) -> None:
        """程序图标作为全部窗口的默认图标：设置窗口、双语窗口、首次运行引导和各种对话框都用它。"""
        icon = self._options.tray_icon
        if self._qt_app is not None and icon is not None and icon.is_file():
            self._qt_app.setWindowIcon(QIcon(str(icon)))

    def _subscribe(self, bus: EventBus) -> None:
        """组合根自己的订阅。

        配置最先订阅：NMT 进程按配置启动，之后的每次变化由这里处理界面语言、主题与进程重载。

        引擎目录每次发布都检查一次：首次发布、装好、卸载、Windows OCR 语言变化都会改变 OCR 进程的启动条件；
        只看安装状态（_on_ocr_catalog），组合根自己合并进去的进程状态不触发判断。
        """
        bus.subscribe(AppConfig, self._on_config)
        bus.subscribe(QuitRequested, lambda _: self._quit.set())
        bus.subscribe(ModelsInstalled, self._on_models_installed)
        bus.subscribe(NmtRuntimeChanged, self._on_nmt_runtime_changed)
        bus.subscribe(OcrEngineCatalog, self._on_ocr_catalog)
        bus.subscribe(RestartWorker, self._on_restart_worker)

    async def _start_module(self, bus: EventBus, module: Module) -> None:
        ctx = ModuleContext(bus, logging.getLogger(f"lst.{module.name}"))
        await module.start(ctx)
        self._started.append((module, ctx))

    async def wait_closed(self) -> None:
        await self._quit.wait()

    def request_quit(self) -> None:
        self._quit.set()

    def notify_second_instance(self) -> None:
        if self.bus is not None:
            notice = NotificationRequested("app.second_instance.title", "app.second_instance.message")
            self.bus.publish(notice)

    async def stop(self) -> None:
        # 先置停止标志再等决策锁：进行中的决策做完当前一步，之后的决策见到标志直接返回，
        # 不会在停掉推理进程之后又把它们启动起来。
        self._stopping = True
        if self._nmt_reload is not None:
            self._nmt_reload.cancel()
            await asyncio.gather(self._nmt_reload, return_exceptions=True)
        async with self._decide:
            await self._stop_all()
        logger.info("全部模块已停止")

    async def _stop_all(self) -> None:
        for module, ctx in reversed(self._started):
            try:
                await module.stop()
            except Exception:
                logger.exception("停止模块 %s 时出错", module.name)
            ctx.close()
        self._started.clear()
        if self.nmt is not None:
            await self.nmt.stop()
        if self.ocr is not None:
            await self.ocr.stop()
        if self.cache is not None:
            await self.cache.close()
        if self.client is not None:
            await self.client.aclose()
        if self.bus is not None:
            await self.bus.aclose()

    # ---- 配置

    def _latest_config(self) -> AppConfig | None:
        return self.bus.latest(AppConfig) if self.bus is not None else None

    def _latest_section[M: BaseModel](self, name: str, model: type[M]) -> M:
        config = self._latest_config()
        return _section(config, name, model) if config is not None else model()

    async def _on_config(self, _config: AppConfig) -> None:
        """应用配置变化。收到的那一份可能已经过时，各步都读取总线上最新的 AppConfig（见模块说明）。"""
        async with self._decide:
            if self._stopping:
                return
            ui = self._latest_section("ui", UiConfig)
            if ui != self._ui_config:
                self._ui_config = ui
                i18n.set_language(ui.language)
                if self._qt_app is not None:
                    apply_app_style(self._qt_app, ui.theme)
            if self._suspended:
                return  # 模型目录正在更换，恢复时按最新配置决定进程
            await self._apply_storage()
            await self._apply_nmt()
            await self._apply_ocr()
            await self._apply_models_dir()

    async def _apply_nmt(self) -> None:
        """按最新的 [nmt] 节启动、重载或停止 NMT 进程。调用方持有决策锁。"""
        if self.nmt is None or self._stopping or self._suspended:
            return
        config = self._latest_section("nmt", NmtConfig)
        settings = nmt_worker_settings(config, self._paths, installed_bin_dir(self._paths.nmt_runtime_dir))
        if config.enabled == self._nmt_enabled and settings == self._nmt_settings:
            return
        self._nmt_settings, self._nmt_enabled = settings, config.enabled
        if config.enabled:
            # 未运行时 reload 直接用新配置启动，运行中则结束旧进程再启动（架构文档 8.2.1 节）。
            await self.nmt.reload(settings)
        else:
            await self.nmt.stop()

    def _ocr_command(self, engine: OcrEngineKind) -> tuple[str, ...]:
        option = self._options.ocr_command
        if option == "auto" or option is None:
            return ocr_worker_command(engine, self._paths)
        return tuple(option(engine))

    def _windows_languages(self) -> tuple[str, ...]:
        engines = self.ocr_engines
        info = None if engines is None else engines.catalog().engine(OcrEngineKind.WINDOWS)
        return () if info is None else info.languages

    async def _apply_ocr(self, config: OcrConfig | None = None) -> None:
        """按 [ocr] 节与引擎安装状态启动、重载或停止 OCR 进程（需求 OCRE-04）。调用方持有决策锁。

        config 为 None 时取总线上最新的 [ocr] 节，当前引擎的流水线配置节也取最新的。决定进程的输入（去重键）
        是：
        是否启用、当前引擎、引擎是否可用、process_key（引擎、识别模式、设备、语言与流水线配置的进程级字段）、
        PaddleOCR 的模型目录、Windows 已装的 OCR 语言（新装了语言，原来因缺语言失败的进程就可能起来）、
        已装的运行环境版本，以及选用的模型是否就位（模型下载完成后，原来因缺模型失败的进程就可能起来）。
        只有去重键变化时才重载，子能力开关与请求级参数随请求发送。OCR 进程进入 FAILED 后去重键保留，
        与 OCR 无关的配置变化不会重启它（见模块说明）；RestartWorker 与卸载前的停止会清除去重键。
        """
        if self.ocr is None or self._stopping or self._suspended:
            return
        if self.bus is None or self.bus.latest(OcrEngineCatalog) is None:
            # 引擎模块还没启动、没有发布过目录：引擎是否可用还不知道，这时不启动也不停止，
            # 等目录第一次发布时（_on_ocr_catalog）再决定。不跳过的话，配置服务启动时的第一次调用
            # 会把 Windows OCR 判为不可用，记一条不准确的警告（安装版实测）。
            return
        if config is None:
            config = self._latest_section("ocr", OcrConfig)
        engine = config.engine
        if self.ocr_engines is not None:
            # 记下这次决定所依据的安装状态，_on_ocr_catalog 只在它变化时重新决定。
            self._engines_fingerprint = self._install_fingerprint(self.ocr_engines.catalog())
        available = self.ocr_engines is not None and self.ocr_engines.can_run(engine)
        substituted: OcrEngineKind | None = None
        if (
            not available
            and engine is not OcrEngineKind.WINDOWS
            and self.ocr_engines is not None
            and self.ocr_engines.can_run(OcrEngineKind.WINDOWS)
        ):
            # 选用的引擎没有装好（还没下载、已卸载、换版本失败）时先用 Windows OCR 识别，
            # 截屏翻译不因此失效；选用的引擎装好后，目录变化会让这里重新决定并换回去（OCRE-04）。
            substituted = engine
            engine, available = OcrEngineKind.WINDOWS, True
            config = config.model_copy(update={"engine": engine})
        pipeline = self._latest_pipeline(engine)
        cache = ocr_model_cache(self._paths)
        enabled = config.enabled and available
        engines = self.ocr_engines
        key = (
            enabled,
            engine,
            process_key(config, pipeline),
            str(cache) if engine is OcrEngineKind.PADDLE else "",
            self._windows_languages() if engine is OcrEngineKind.WINDOWS else (),
            engines.installed_variant(engine) if engines is not None else "",
            engines.selected_models_ready(engine) if engines is not None else (),
        )
        if substituted is not self._ocr_substituted:
            self._ocr_substituted = substituted
            if substituted is not None and enabled:
                logger.warning("当前 OCR 引擎 %s 没有装好，先用 Windows OCR 识别", substituted.value)
        if key == self._ocr_key:
            return
        self._ocr_key, self._ocr_engine = key, engine
        if engines is not None:
            engines.set_running(engine if enabled else None)
        if enabled:
            settings = worker_settings(config, pipeline)
            if engine is not OcrEngineKind.WINDOWS:
                models = engines.worker_model_settings(engine) if engines is not None else {}
                settings = ocr_process_settings(config, cache, pipeline, models)
            # 未运行时 reload 直接启动；运行中则结束旧进程再用新命令启动（架构文档 8.2.1 节）。
            await self.ocr.reload(settings, command=self._ocr_command(engine))
            return
        if config.enabled and not available:
            logger.warning(
                "当前 OCR 引擎 %s 不可用（未安装或系统缺少 OCR 语言），截屏识别暂不可用", engine.value
            )
        await self.ocr.stop()
        if self.ocr_engines is not None:
            self.ocr_engines.set_process_state(engine, None)

    async def _on_ocr_catalog(self, catalog: OcrEngineCatalog) -> None:
        """引擎的安装状态可能变了（模块刚启动、引擎装好或卸载、Windows OCR 语言变化），重新决定 OCR 进程。

        只比较安装状态（can_run 与语言列表），与上一次决定时的安装状态相同就不动。组合根合并进程状态时
        引擎目录也会重新发布，这些发布不触发判断：
        否则进程进入 FAILED、去重键被清除之后，合并 ERROR 状态的那次发布会立即重启进程，
        确定性故障因此无限循环。
        """
        async with self._decide:
            if self._stopping or self._suspended:
                return
            if self._install_fingerprint(catalog) == self._engines_fingerprint:
                return
            await self._apply_ocr()

    def _latest_pipeline(self, engine: OcrEngineKind) -> PipelineConfig:
        """当前引擎的流水线配置节（[ocr_windows]、[ocr_rapid]、[ocr_paddle]），缺失或无法解析时用默认值。"""
        config = self._latest_config()
        raw = config.section(PIPELINE_SECTIONS[engine][0]) if config is not None else None
        return pipeline_or_default(engine, raw)

    def _install_fingerprint(self, catalog: OcrEngineCatalog) -> tuple[object, ...] | None:
        """引擎目录里决定 OCR 进程能否启动、加载什么的部分：各引擎的 can_run、语言列表、已装版本与选用模型
        是否就位，不含进程状态。"""
        engines = self.ocr_engines
        if engines is None:
            return None
        return tuple(
            (
                info.kind,
                engines.can_run(info.kind),
                info.languages,
                engines.installed_variant(info.kind),
                engines.selected_models_ready(info.kind),
            )
            for info in catalog.engines
        )

    async def _before_nmt_runtime_delete(self) -> None:
        """删除显卡运行库前停掉 NMT 进程：它加载着运行库里的 DLL，不停的话删除会失败。

        停止后清掉上次的设置，运行库删除后 _on_nmt_runtime_changed 按配置重新启动进程。
        """
        async with self._decide:
            if self.nmt is None or self._stopping or self._suspended:
                return
            await self.nmt.stop()
            self._nmt_settings = None

    async def _on_nmt_runtime_changed(self, _event: NmtRuntimeChanged) -> None:
        """显卡运行库装好或删除后，按最新配置重新决定 NMT 进程（设置里多了或少了 cuda_dll_dir）。"""
        async with self._decide:
            if not self._stopping:
                await self._apply_nmt()

    async def _before_ocr_uninstall(self, kind: OcrEngineKind) -> None:
        """卸载引擎前先停掉正在使用它的 OCR 进程，否则运行环境里的 DLL 被占用，删除会失败。"""
        async with self._decide:
            if self.ocr is not None and self._ocr_engine is kind:
                await self.ocr.stop()
                self._ocr_key = None
                if self.ocr_engines is not None:
                    self.ocr_engines.set_process_state(kind, None)

    async def _on_restart_worker(self, command: RestartWorker) -> None:
        """重新启动一个推理进程（托盘或设置窗口在进程 FAILED 后发出）：按当前配置重新决定，清零崩溃计数。"""
        async with self._decide:
            if self._stopping or self._suspended:
                return
            if command.worker == "ocr":
                # 清除去重键：配置没变也重新启动；当前引擎不可用或 OCR 关闭时照常停止并记日志。
                self._ocr_key = None
                await self._apply_ocr()
            elif command.worker == "nmt":
                if self.nmt is None or not self._nmt_enabled or self._nmt_settings is None:
                    logger.info("NMT 进程已在配置中关闭，不重新启动")
                    return
                await self.nmt.reload(self._nmt_settings)
            else:
                logger.warning("RestartWorker 的进程名未知：%s", command.worker)

    # ---- 模型管理

    def _new_ocr_engines(self) -> OcrEnginesModule:
        """OCR 引擎模块：运行环境与模型都在当前的模型目录下。"""
        assert self.client is not None
        return OcrEnginesModule(
            paddle_layout(self._paths),
            self.client,
            rapid=rapid_layout(self._paths),
            before_uninstall=self._before_ocr_uninstall,
            # 版本清单变化后重装 PaddleOCR 运行环境：替换旧 site 之前同样要停掉正在使用它的 OCR 进程。
            before_runtime_update=self._before_ocr_uninstall,
        )

    def _new_nmt_runtime(self) -> NmtRuntimeModule:
        assert self.client is not None
        return NmtRuntimeModule(
            self._paths.nmt_runtime_dir, self.client, before_delete=self._before_nmt_runtime_delete
        )

    def _new_models_module(self, models_dir: Path) -> ModelsModule:
        """模型管理模块：NMT 模型在 models_dir，词典包在模型目录的 dict（与字典翻译引擎同一目录）。"""
        assert self.client is not None
        return ModelsModule(
            models_dir,
            self.client,
            self._options.models_index_url,
            dict_dir=self._paths.dict_models_dir,
            dict_index_url=self._options.dict_index_url,
        )

    def _resolve_models_dir(self) -> Path:
        """模型管理模块的目录：与 NMT 进程实际扫描的目录相同（含源码版回退到旧模型目录的情况）。"""
        nmt = self._latest_section("nmt", NmtConfig)
        return Path(str(nmt_worker_settings(nmt, self._paths)["model_dir"]))

    async def _apply_models_dir(self) -> None:
        """[nmt].model_dir 在运行中改变时，换一个指向新目录的模型管理模块（需求 MOD-04）。

        调用方持有决策锁。
        """
        old = self.models
        if old is None or self.bus is None or self.client is None or self._stopping or self._suspended:
            return
        models_dir = self._resolve_models_dir()
        if models_dir == self._models_dir:
            return
        await self._stop_module(old)
        self.models = await self._swap_module(old, self._new_models_module(models_dir))
        self._models_dir = models_dir
        logger.info("本地翻译模型目录已改为 %s", models_dir)

    async def _stop_module(self, module: Module | None) -> None:
        """停止一个模块并取消它的订阅。它在 _started 里的位置保留，由 _swap_module 换成新实例。"""
        if module is None:
            return
        try:
            await module.stop()
        except Exception:
            logger.exception("停止模块 %s 时出错", module.name)
        for started, ctx in self._started:
            if started is module:
                ctx.close()

    async def _swap_module[M: Module](self, old: Module | None, new: M) -> M:
        """启动 new，替换 _started 里 old 的位置（停止时仍按启动的逆序）；old 不在其中时排到最后。"""
        assert self.bus is not None
        ctx = ModuleContext(self.bus, logging.getLogger(f"lst.{new.name}"))
        index = next((i for i, (module, _) in enumerate(self._started) if module is old), None)
        if index is None:
            self._started.append((new, ctx))
        else:
            self._started[index] = (new, ctx)
        await new.start(ctx)
        return new

    # ---- 模型目录与重启（存储位置模块调用，见 storage_module.StorageHost）

    @property
    def paths(self) -> AppPaths:
        """当前的路径；模型目录在运行中可能变化。"""
        return self._paths

    @property
    def restart_requested(self) -> bool:
        return self._restart

    def request_restart(self) -> None:
        """退出；入口在释放单实例锁之后以同样的命令行启动新进程。"""
        logger.info("收到重新启动的请求")
        self._restart = True
        self._quit.set()

    async def save_models_dir(self, value: str) -> str | None:
        """把 [storage].models_dir 写进配置文件，等它落盘。成功返回 None，失败返回原因。"""
        service = self.config_service
        if service is None:
            return "配置服务没有运行"
        rejected = await service.update({"storage": {"models_dir": value}})
        return "；".join(event.reason for event in rejected) or None

    async def _apply_storage(self) -> None:
        """配置里的模型目录与当前的不同（手工改了配置文件）时，按新目录重建模块、重启进程，不移动文件。

        调用方持有决策锁。设置里的更改先由存储位置模块移动文件、再写配置，那时这里看到的目录已经相同。
        """
        if self._stopping or self._suspended or self.bus is None or self.client is None:
            return
        storage = self._latest_section("storage", StorageConfig)
        wanted = Path(storage.models_dir) if storage.models_dir else None
        if wanted == self._paths.models_dir:
            return
        logger.info("配置里的模型目录改为 %s，按新目录重新加载", wanted or self._paths.default_models_dir)
        await self._suspend_locked()
        await self._resume_locked(wanted)

    async def suspend_models(self) -> None:
        """停下用到模型目录的进程与模块：NMT、OCR 进程，模型管理、OCR 引擎、显卡运行库模块。"""
        async with self._decide:
            if not self._stopping:
                await self._suspend_locked()

    async def resume_models(self, models_dir: Path | None) -> None:
        """按 models_dir（None 为默认位置）重建三个模块，换字典引擎的词典目录，重新决定 NMT 与 OCR 进程。"""
        async with self._decide:
            if not self._stopping:
                await self._resume_locked(models_dir)
        if self.storage is not None:
            self.storage.publish()

    async def _suspend_locked(self) -> None:
        self._suspended = True
        catalog = self.bus.latest(ModelCatalog) if self.bus is not None else None
        self._refresh_index = catalog is not None and (catalog.index_loaded or catalog.dict_index_loaded)
        if self.nmt is not None:
            await self.nmt.stop()
            self._nmt_settings = None
        if self.ocr is not None:
            await self.ocr.stop()
            self._ocr_key = None
        if self.ocr_engines is not None and self._ocr_engine is not None:
            self.ocr_engines.set_process_state(self._ocr_engine, None)
        for module in (self.models, self.ocr_engines, self.nmt_runtime):
            await self._stop_module(module)
        logger.info("已停下本地翻译、文字识别与模型管理，准备更换模型目录")

    async def _resume_locked(self, models_dir: Path | None) -> None:
        try:
            self._paths = self._paths.with_models_dir(models_dir)
            try:
                await asyncio.to_thread(self._paths.ensure_models)
            except OSError as exc:
                logger.error("无法创建模型目录 %s：%s", self._paths.models_root, exc)
            self._models_dir = self._resolve_models_dir()
            if self.models is not None:
                self.models = await self._restart_module(
                    self.models, self._new_models_module(self._models_dir)
                )
            if self.ocr_engines is not None:
                self.ocr_engines = await self._restart_module(self.ocr_engines, self._new_ocr_engines())
            if self.nmt_runtime is not None:
                self.nmt_runtime = await self._restart_module(self.nmt_runtime, self._new_nmt_runtime())
            if self.engines is not None:
                await self.engines.set_dict_dir(self._paths.dict_models_dir)
        finally:
            self._suspended = False
            self._engines_fingerprint = None
        await self._apply_nmt()
        await self._apply_ocr()
        if self._refresh_index and self.bus is not None:
            # 新的模型管理模块还没有索引：之前已经获取过（用户打开过模型页），换目录后再取一次。
            self.bus.send(RefreshModelCatalog())
        logger.info("模型目录：%s", self._paths.models_root)

    async def _restart_module[M: Module](self, old: Module, new: M) -> M:
        """_swap_module 的容错版本：新模块启动失败时记错误，返回已经替换进去的新实例。"""
        try:
            return await self._swap_module(old, new)
        except Exception:
            logger.exception("按新模型目录启动模块 %s 失败", new.name)
            return new

    def _on_models_installed(self, event: ModelsInstalled) -> None:
        """模型装好或删除后重启 NMT 进程，让它重新扫描模型目录（架构文档 8.5 节）。

        连续装好几个模型时合并重载：重载进行中又有模型变化，就在这次结束后再重载一次。
        """
        if self.nmt is None or not self._nmt_enabled or self._nmt_settings is None or self._stopping:
            return
        if ModelKind.NMT not in event.kinds():
            return  # 只涉及词典包：字典引擎自己发现词典目录的变化
        if self._nmt_reload is not None and not self._nmt_reload.done():
            self._nmt_reload_again = True
            return
        self._nmt_reload = asyncio.create_task(self._reload_nmt_for_models())

    async def _reload_nmt_for_models(self) -> None:
        while True:
            self._nmt_reload_again = False
            # 在决策锁里检查并重载：配置在这期间关闭了 NMT 时，不会在 stop 之后又把进程启动起来。
            async with self._decide:
                if self.nmt is None or not self._nmt_enabled or self._nmt_settings is None or self._stopping:
                    return
                if self._suspended:
                    return  # 恢复时会按新目录重新启动 NMT 进程
                try:
                    await self.nmt.reload(self._nmt_settings)
                except Exception:
                    logger.exception("模型变化后重载 NMT 进程失败")
            if not self._nmt_reload_again:
                return

    # ---- 推理进程状态

    def _on_worker_state(self, name: str) -> Callable[[WorkerState, str], None]:
        def on_state(state: WorkerState, detail: str) -> None:
            previous = self._worker_statuses.get(name)
            supervisor = self.nmt if name == "nmt" else self.ocr
            versions = dict(supervisor.versions) if supervisor is not None else {}
            self._worker_statuses[name] = WorkerStatus(state, detail, versions)
            self._publish_worker_statuses()
            if name == "ocr":
                # FAILED 时去重键保留：只有决定进程的依据变化（装好语言、改了 OCR 配置、换了运行环境版本）
                # 或 RestartWorker 才重新启动，无关的配置变化不会把确定性故障的进程反复拉起（见模块说明）。
                self._merge_ocr_process_state(state, detail)
            if previous is not None and previous.state == state:
                return
            worker = i18n.tr(f"worker.{name}")
            if state is WorkerState.CRASHED:
                self._notify("app.worker_crashed", NoticeLevel.WARNING, worker)
            elif state is WorkerState.FAILED:
                self._notify("app.worker_failed", NoticeLevel.ERROR, worker)

        return on_state

    def _merge_ocr_process_state(self, state: WorkerState, detail: str) -> None:
        """把 OCR 进程的状态合并进 OcrEngineCatalog，设置窗口据此显示加载中、就绪或出错（OCRE-03）。"""
        engines, engine = self.ocr_engines, self._ocr_engine
        if engines is None or engine is None:
            return
        if state in (WorkerState.STARTING, WorkerState.LOADING, WorkerState.CRASHED):
            engines.set_process_state(engine, OcrEngineState.LOADING)
        elif state in (WorkerState.READY, WorkerState.BUSY):
            engines.set_process_state(engine, OcrEngineState.READY)
        elif state is WorkerState.FAILED:
            engines.set_process_state(engine, OcrEngineState.ERROR, ocr_failure_code(detail), detail)
        else:
            engines.set_process_state(engine, None)

    def _publish_worker_statuses(self) -> None:
        if self.bus is not None:
            statuses: Mapping[str, WorkerStatus] = dict(self._worker_statuses)
            self.bus.publish(WorkerStatuses(statuses))

    def _notify(self, key: str, level: NoticeLevel, worker: str) -> None:
        if self.bus is not None:
            notice = NotificationRequested(f"{key}.title", f"{key}.message", level, {"worker": worker})
            self.bus.publish(notice)


def single_instance_name(paths: AppPaths) -> str:
    """单实例服务名。按数据目录区分，两个数据目录可以各运行一个实例（开发与测试时有用）。"""
    digest = hashlib.sha256(str(paths.data_dir).lower().encode("utf-8")).hexdigest()[:12]
    return f"{APP_NAME}-{digest}"

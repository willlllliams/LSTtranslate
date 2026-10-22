"""组合根：创建全部模块、按固定顺序启动与停止，并把它们需要的端口注入进去（架构文档 4.3、4.4 节）。

启动顺序：配置 → 统计 → 缓存 → 模型 → 引擎 → 竞速 → OCR 引擎 → OCR → 应用更新 → 托盘 → 热键 → 双语窗口
→ 设置窗口 →（AppOptions.extra_modules）→ 首次运行引导。
- 托盘先于热键启动：热键在收到配置时注册，注册失败的通知要有托盘接收；
- 配置服务最先启动，它在启动时发现的配置问题（ConfigRejected，是 Event，不回放）早于其他模块订阅，
  全部模块启动之后由这里补发一次，托盘与设置窗口都能收到；
- NMT 进程在配置就绪之后按 [nmt] 节启动，[nmt] 节变化时重载；
- OCR 进程按 [ocr] 节启动（需求 OCR-07 的后台预加载），启动命令随当前 OCR 引擎而定（OCRE-01）：
  Windows 自带 OCR 用主环境，PaddleOCR 用它自己的运行环境。当前引擎不可用（Windows 无 OCR 语言、
  PaddleOCR 未安装）时不启动。引擎、识别模式、设备、识别语言变化，或引擎目录变化（OcrEngineCatalog）
  时重新决定；子能力开关随每个请求发送，不重载（架构文档 8.4 节）。OCR 进程的状态合并进 OcrEngineCatalog；
- 推理进程进入 FAILED 后，托盘或设置窗口发出 RestartWorker，这里按当前配置重新启动该进程；OCR 进程进入
  FAILED 时还清除去重键，下一次配置变化或引擎安装状态变化（含 Windows OCR 语言列表）也会重新启动它；
- 配置、引擎目录、RestartWorker、模型变化引起的进程决策都在同一把锁里串行执行，每次读取总线上最新的
  AppConfig：总线把每个协程处理函数包成独立的任务，不串行化时先到的旧配置可能在后到的新配置之后才应用；
- 应用更新服务检查 GitHub Releases（UPD-01 至 UPD-06）；
- 模型管理模块的模型目录由 [nmt] 节解析，与 NMT 进程用同一个目录，所以在配置服务之后才创建；
  目录在运行中改变时换一个新的模型管理模块。模型装好或删除后（ModelsInstalled）重载 NMT 进程；
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
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field
from PySide6.QtWidgets import QApplication

from lst.app.paths import INSTALL_MARKER, PROGRAM_DIR, AppPaths, install_root, is_installed
from lst.app.supervisor import WorkerSupervisor
from lst.contracts.config import AppConfig
from lst.contracts.events import (
    NoticeLevel,
    NotificationRequested,
    QuitRequested,
    RestartWorker,
    WorkerStatus,
    WorkerStatuses,
)
from lst.contracts.models import ModelsInstalled
from lst.contracts.ocr_engines import OcrEngineCatalog, OcrEngineErrorCode, OcrEngineKind, OcrEngineState
from lst.contracts.ports import WorkerState
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, Module, ModuleContext
from lst.services.cache import CacheModule, SqliteTranslationCache
from lst.services.config import ConfigService, KeyringSecretStore, RedactingFilter, SecretStore
from lst.services.engines import ENGINE_KINDS, EngineDeps, EngineListSchema, EnginesModule
from lst.services.models import DEFAULT_INDEX_URL, ModelsModule
from lst.services.ocr import CONFIG_SECTIONS as OCR_SECTIONS
from lst.services.ocr import OcrConfig, OcrModule, process_key, worker_settings
from lst.services.ocr_engines import DevPaddleEnvironment, OcrEnginesModule, PaddleInstallPaths
from lst.services.race import CONFIG_SECTIONS as RACE_SECTIONS
from lst.services.race import RaceModule
from lst.services.stats import SqliteStatsStore, StatsModule
from lst.services.updates import CONFIG_SECTIONS as UPDATE_SECTIONS
from lst.services.updates import UpdatesModule, running_version
from lst.ui import i18n
from lst.ui.bilingual import BilingualModule
from lst.ui.hotkeys import CONFIG_SECTIONS as HOTKEY_SECTIONS
from lst.ui.hotkeys import HotkeysModule
from lst.ui.onboarding import OnboardingModule
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
    model_dir: str = Field(default="", description="存放 .argosmodel 的目录；留空时用 data/models/nmt")
    device: Literal["auto", "cpu", "cuda"] = Field(default="auto", description="推理设备")
    compute_type: str = Field(default="auto", description="CTranslate2 计算类型")
    beam_size: int = Field(default=2, ge=1, le=8, description="束搜索宽度")


APP_SECTIONS: dict[str, type[BaseModel]] = {"nmt": NmtConfig}

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
    }
)


def all_config_sections() -> dict[str, type[BaseModel]]:
    """全部普通配置节。节名重复时抛 ValueError（配置服务也会检查）。"""
    sections: dict[str, type[BaseModel]] = {}
    for group in (APP_SECTIONS, UI_SECTIONS, HOTKEY_SECTIONS, RACE_SECTIONS, OCR_SECTIONS, UPDATE_SECTIONS):
        overlap = sections.keys() & group.keys()
        if overlap:
            raise ValueError(f"配置节名重复：{sorted(overlap)}")
        sections.update(group)
    return sections


def nmt_worker_settings(config: NmtConfig, paths: AppPaths) -> dict[str, JsonValue]:
    """由 [nmt] 节生成 NMT 进程的 Configure 设置。

    model_dir 留空时用 data/models/nmt；那里没有模型而旧项目的 assets/argosmodel 里有时，用旧目录，
    开发时不必复制模型就能直接运行。解压目录与 CUDA 编译缓存始终放在数据目录下。
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
        "device": config.device,
        "compute_type": config.compute_type,
        "beam_size": config.beam_size,
    }


def ocr_model_cache(paths: AppPaths) -> Path:
    """OCR 模型缓存目录（PaddleX 的 PADDLE_PDX_CACHE_HOME，模型在其中的 official_models/ 下）。

    data/models/ocr 里没有模型而旧项目的 assets/PaddleOCR_and_PP-Structure 里有时用旧目录，
    与 NMT 模型的回退相同。“有模型”指 official_models 下至少有一个子目录。
    """
    if not _has_models(paths.ocr_models_dir) and _has_models(LEGACY_OCR_MODELS):
        return LEGACY_OCR_MODELS
    return paths.ocr_models_dir


def _has_models(cache: Path) -> bool:
    """official_models 下至少有一个模型子目录，与 OCR 引擎模块判断源码版环境可用的条件相同。"""
    models = cache / "official_models"
    return models.is_dir() and any(entry.is_dir() for entry in models.iterdir())


def ocr_process_settings(config: OcrConfig, cache: Path) -> dict[str, JsonValue]:
    """OCR 进程的 Configure 内容：services.ocr 给出的字段加上模型缓存目录。"""
    return {**worker_settings(config), "model_cache_dir": str(cache)}


WINOCR_BACKEND = "lst.workers.ocr_worker.winocr_backend:WinOcrBackend"
PADDLE_REQUIREMENTS = "ocr-paddle-requirements.txt"


def paddle_layout(paths: AppPaths) -> PaddleInstallPaths | DevPaddleEnvironment:
    """PaddleOCR 引擎的目录：安装版按需安装到数据目录，源码版使用 .venv-ocr 与已有模型（只检查，不修改）。"""
    if is_installed():
        root = install_root()
        return PaddleInstallPaths(
            engine_dir=paths.data_dir / "engines" / "paddle",
            model_cache_dir=paths.ocr_models_dir,
            uv=root / "tools" / "uv.exe",
            python=root / "runtime" / "python.exe",
            requirements=root / "app" / PADDLE_REQUIREMENTS,
        )
    return DevPaddleEnvironment(PROGRAM_DIR / ".venv-ocr", (paths.ocr_models_dir, LEGACY_OCR_MODELS))


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
    - PaddleOCR：安装版经启动器使用数据目录里的运行环境（--ocr-site），源码版使用 .venv-ocr。
    """
    tail = ("--log-level", "WARNING")
    if is_installed():
        base = installed_worker_prefix("ocr")
        if engine is OcrEngineKind.WINDOWS:
            return (*base, "--ocr-site", "main", "--backend", WINOCR_BACKEND, *tail)
        layout = paddle_layout(paths)
        assert isinstance(layout, PaddleInstallPaths)
        return (*base, "--ocr-site", str(layout.site_dir), *tail)
    if engine is OcrEngineKind.WINDOWS:
        return (sys.executable, "-m", "lst.workers.ocr_worker", "--backend", WINOCR_BACKEND, *tail)
    ocr_python = PROGRAM_DIR / ".venv-ocr" / "Scripts" / "python.exe"
    return (str(ocr_python), "-m", "lst.workers.ocr_worker", *tail)


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
        self.onboarding: OnboardingModule | None = None

    @property
    def quit_requested(self) -> asyncio.Event:
        return self._quit

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        bus = EventBus(loop)
        self.bus = bus
        self._paths.ensure()
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
        self._models_dir = self._resolve_models_dir()
        self.models = ModelsModule(self._models_dir, self.client, self._options.models_index_url)
        self.onboarding = OnboardingModule(self._paths.ui_state_file)
        self.ocr_engines = OcrEnginesModule(
            paddle_layout(self._paths),
            self.client,
            before_uninstall=self._before_ocr_uninstall,
            # 版本清单变化后重装 PaddleOCR 运行环境：替换旧 site 之前同样要停掉正在使用它的 OCR 进程。
            before_runtime_update=self._before_ocr_uninstall,
        )
        modules: list[Module] = [
            StatsModule(SqliteStatsStore(self._paths.stats_db)),
            CacheModule(self.cache),
            self.models,
            EnginesModule(EngineDeps(secrets=self.secrets, client=self.client, nmt=self.nmt)),
            RaceModule(cache=self.cache),
            self.ocr_engines,
            *([OcrModule(self.ocr)] if self.ocr is not None else []),
            UpdatesModule(
                self.client,
                current_version=running_version(INSTALL_MARKER),
                can_install=is_installed(),
                record_file=self._paths.data_dir / "updates.json",
                download_dir=self._paths.tmp_dir / "updates",
                startup_delay=self._options.updates_startup_delay,
            ),
            TrayModule(APP_NAME, self._options.tray_icon, self._paths.logs_dir),
            HotkeysModule(),
            BilingualModule(self._paths.ui_state_file),
            SettingsModule(
                self._paths.ui_state_file,
                sections=all_config_sections(),
                engine_kinds={kind: spec.settings_model for kind, spec in ENGINE_KINDS.items()},
            ),
            *self._options.extra_modules,
            self.onboarding,
        ]
        for module in modules:
            await self._start_module(bus, module)
        # 全部模块都已订阅，补发配置服务在启动时发现的问题（它们是 Event，不会回放）。
        for rejection in self.config_service.startup_rejections:
            bus.publish(rejection)
        self._publish_worker_statuses()
        logger.info("全部模块已启动")

    def _subscribe(self, bus: EventBus) -> None:
        """组合根自己的订阅。

        配置最先订阅：NMT 进程按配置启动，之后的每次变化由这里处理界面语言、主题与进程重载。

        引擎目录每次发布都检查一次：首次发布、装好、卸载、Windows OCR 语言变化都会改变 OCR 进程的启动条件；
        只看安装状态（_on_ocr_catalog），组合根自己合并进去的进程状态不触发判断。
        """
        bus.subscribe(AppConfig, self._on_config)
        bus.subscribe(QuitRequested, lambda _: self._quit.set())
        bus.subscribe(ModelsInstalled, self._on_models_installed)
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
            await self._apply_nmt()
            await self._apply_ocr()
            await self._apply_models_dir()

    async def _apply_nmt(self) -> None:
        """按最新的 [nmt] 节启动、重载或停止 NMT 进程。调用方持有决策锁。"""
        if self.nmt is None or self._stopping:
            return
        config = self._latest_section("nmt", NmtConfig)
        settings = nmt_worker_settings(config, self._paths)
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

        config 为 None 时取总线上最新的 [ocr] 节。决定进程的输入是：是否启用、当前引擎、引擎是否可用、
        process_key（Windows 看语言，PaddleOCR 再看识别模式与设备）、PaddleOCR 的模型目录，以及 Windows
        已装的 OCR 语言（新装了语言，原来因缺语言失败的进程就可能起来）。只有这些变化时才重载，
        子能力开关随请求发送。OCR 进程进入 FAILED 时去重键被清除，下一次调用总会重新决定。
        """
        if self.ocr is None or self._stopping:
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
        cache = ocr_model_cache(self._paths)
        enabled = config.enabled and available
        key = (
            enabled,
            engine,
            process_key(config),
            str(cache) if engine is OcrEngineKind.PADDLE else "",
            self._windows_languages() if engine is OcrEngineKind.WINDOWS else (),
        )
        if key == self._ocr_key:
            return
        self._ocr_key, self._ocr_engine = key, engine
        if enabled:
            settings = worker_settings(config)
            if engine is OcrEngineKind.PADDLE:
                settings = ocr_process_settings(config, cache)
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
            if self._stopping or self._install_fingerprint(catalog) == self._engines_fingerprint:
                return
            await self._apply_ocr()

    def _install_fingerprint(self, catalog: OcrEngineCatalog) -> tuple[object, ...] | None:
        """引擎目录里决定 OCR 进程能否启动的部分：各引擎的 can_run 与语言列表，不含进程状态。"""
        engines = self.ocr_engines
        if engines is None:
            return None
        return tuple((info.kind, engines.can_run(info.kind), info.languages) for info in catalog.engines)

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
            if self._stopping:
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

    def _resolve_models_dir(self) -> Path:
        """模型管理模块的目录：与 NMT 进程实际扫描的目录相同（含源码版回退到旧模型目录的情况）。"""
        nmt = self._latest_section("nmt", NmtConfig)
        return Path(str(nmt_worker_settings(nmt, self._paths)["model_dir"]))

    async def _apply_models_dir(self) -> None:
        """[nmt].model_dir 在运行中改变时，换一个指向新目录的模型管理模块（需求 MOD-04）。

        调用方持有决策锁。
        """
        old = self.models
        if old is None or self.bus is None or self.client is None or self._stopping:
            return
        models_dir = self._resolve_models_dir()
        if models_dir == self._models_dir:
            return
        index = next(i for i, (module, _) in enumerate(self._started) if module is old)
        await old.stop()
        self._started[index][1].close()
        new = ModelsModule(models_dir, self.client, self._options.models_index_url)
        ctx = ModuleContext(self.bus, logging.getLogger(f"lst.{new.name}"))
        await new.start(ctx)
        self._started[index] = (new, ctx)
        self.models, self._models_dir = new, models_dir
        logger.info("模型目录已改为 %s", models_dir)

    def _on_models_installed(self, _event: ModelsInstalled) -> None:
        """模型装好或删除后重启 NMT 进程，让它重新扫描模型目录（架构文档 8.5 节）。

        连续装好几个模型时合并重载：重载进行中又有模型变化，就在这次结束后再重载一次。
        """
        if self.nmt is None or not self._nmt_enabled or self._nmt_settings is None or self._stopping:
            return
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
            self._worker_statuses[name] = WorkerStatus(state, detail)
            self._publish_worker_statuses()
            if name == "ocr":
                if state is WorkerState.FAILED:
                    # 清除去重键：外部条件修好之后（装好语言、改了配置），下一次判断会重新启动进程。
                    self._ocr_key = None
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
            engines.set_process_state(engine, OcrEngineState.ERROR, OcrEngineErrorCode.WORKER_FAILED, detail)
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

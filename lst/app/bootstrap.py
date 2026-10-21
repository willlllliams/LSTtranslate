"""组合根：创建全部模块、按固定顺序启动与停止，并把它们需要的端口注入进去（架构文档 4.3、4.4 节）。

启动顺序：配置 → 缓存 → 统计 → 模型 → 引擎 → 竞速 → OCR → 托盘 → 热键 → 双语窗口 → 设置窗口 → 首次运行引导。
- 托盘先于热键启动：热键在收到配置时注册，注册失败的通知要有托盘接收；
- 配置服务最先启动，它在启动时发现的配置问题早于托盘订阅，托盘启动之后由这里补发一次；
- NMT 进程在配置就绪之后按 [nmt] 节启动，[nmt] 节变化时重载；
- OCR 进程按 [ocr] 节启动（需求 OCR-07 的后台预加载）；识别模式、设备、识别语言或模型目录变化时重载，
  子能力开关随每个请求发送，不重载（架构文档 8.4 节）。模型目录里没有 OCR 模型时不启动；
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

from lst.app.paths import PROGRAM_DIR, AppPaths, install_root, is_installed
from lst.app.supervisor import WorkerSupervisor
from lst.contracts.config import AppConfig
from lst.contracts.events import (
    NoticeLevel,
    NotificationRequested,
    QuitRequested,
    WorkerStatus,
    WorkerStatuses,
)
from lst.contracts.models import ModelsInstalled
from lst.contracts.ports import WorkerState
from lst.contracts.worker_protocol import JsonValue
from lst.kernel import EventBus, Module, ModuleContext
from lst.services.cache import CacheModule, SqliteTranslationCache
from lst.services.config import ConfigService, KeyringSecretStore, RedactingFilter, SecretStore
from lst.services.engines import ENGINE_KINDS, EngineDeps, EngineListSchema, EnginesModule
from lst.services.models import DEFAULT_INDEX_URL, ModelsModule
from lst.services.ocr import CONFIG_SECTIONS as OCR_SECTIONS
from lst.services.ocr import OcrConfig, OcrModule, worker_settings
from lst.services.race import CONFIG_SECTIONS as RACE_SECTIONS
from lst.services.race import RaceModule
from lst.services.stats import SqliteStatsStore, StatsModule
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
    for group in (APP_SECTIONS, UI_SECTIONS, HOTKEY_SECTIONS, RACE_SECTIONS, OCR_SECTIONS):
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
    与 NMT 模型的回退相同。
    """
    if (
        not (paths.ocr_models_dir / "official_models").is_dir()
        and (LEGACY_OCR_MODELS / "official_models").is_dir()
    ):
        return LEGACY_OCR_MODELS
    return paths.ocr_models_dir


def ocr_process_settings(config: OcrConfig, cache: Path) -> dict[str, JsonValue]:
    """OCR 进程的 Configure 内容：services.ocr 给出的字段加上模型缓存目录。"""
    return {**worker_settings(config), "model_cache_dir": str(cache)}


def worker_command(kind: Literal["nmt", "ocr"]) -> tuple[str, ...]:
    r"""推理进程的启动命令（不含 --address，监管器会追加）。

    - 安装版：用安装目录里的解释器经 launcher\worker.py 启动，它按进程类型选用 site\main 或 site\ocr；
    - 源码版：NMT 用当前解释器（主进程的 .venv），OCR 用独立环境 .venv-ocr（架构文档 2.4 节）。
    """
    if is_installed():
        root = install_root()
        python = str(root / "runtime" / "python.exe")
        return (
            python,
            "-I",
            "-X",
            "utf8",
            str(root / "launcher" / "worker.py"),
            kind,
            "--log-level",
            "WARNING",
        )
    if kind == "nmt":
        return (sys.executable, "-m", "lst.workers.nmt_worker", "--log-level", "WARNING")
    ocr_python = PROGRAM_DIR / ".venv-ocr" / "Scripts" / "python.exe"
    return (str(ocr_python), "-m", "lst.workers.ocr_worker", "--log-level", "WARNING")


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
    ocr_command: Sequence[str] | None = field(default_factory=lambda: worker_command("ocr"))
    """OCR 进程的启动命令；None 表示不启动 OCR 进程（测试用）。"""
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

        self.bus: EventBus | None = None
        self.redactor = RedactingFilter()
        self.secrets: SecretStore = options.secrets if options.secrets is not None else KeyringSecretStore()
        self.client: httpx.AsyncClient | None = None
        self.cache: SqliteTranslationCache | None = None
        self.nmt: WorkerSupervisor | None = None
        self.ocr: WorkerSupervisor | None = None
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
            self.ocr = WorkerSupervisor(
                "ocr",
                self._options.ocr_command,
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
        # 先订阅配置，NMT 进程按配置启动；之后的每次变化由这里处理界面语言、主题与 NMT 重载。
        bus.subscribe(AppConfig, self._on_config)
        bus.subscribe(QuitRequested, lambda _: self._quit.set())
        bus.subscribe(ModelsInstalled, self._on_models_installed)

        # 配置服务发布 AppConfig 时总线立即记下最新值，之后的模块据此决定构造参数。
        await self._start_module(bus, self.config_service)
        self._models_dir = self._resolve_models_dir()
        self.models = ModelsModule(self._models_dir, self.client, self._options.models_index_url)
        self.onboarding = OnboardingModule(self._paths.ui_state_file)
        modules: list[Module] = [
            StatsModule(SqliteStatsStore(self._paths.stats_db)),
            CacheModule(self.cache),
            self.models,
            EnginesModule(EngineDeps(secrets=self.secrets, client=self.client, nmt=self.nmt)),
            RaceModule(cache=self.cache),
            *([OcrModule(self.ocr)] if self.ocr is not None else []),
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
            if isinstance(module, TrayModule):
                # 托盘已经订阅，补发配置服务在启动时发现的问题（它们是 Event，不会回放）。
                for rejection in self.config_service.startup_rejections:
                    bus.publish(rejection)
        self._publish_worker_statuses()
        logger.info("全部模块已启动")

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
        if self._nmt_reload is not None:
            self._nmt_reload.cancel()
            await asyncio.gather(self._nmt_reload, return_exceptions=True)
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
        logger.info("全部模块已停止")

    # ---- 配置

    async def _on_config(self, config: AppConfig) -> None:
        ui = _section(config, "ui", UiConfig)
        if ui != self._ui_config:
            self._ui_config = ui
            i18n.set_language(ui.language)
            if self._qt_app is not None:
                apply_app_style(self._qt_app, ui.theme)
        await self._apply_nmt(_section(config, "nmt", NmtConfig))
        await self._apply_ocr(_section(config, "ocr", OcrConfig))
        await self._apply_models_dir()

    async def _apply_nmt(self, config: NmtConfig) -> None:
        if self.nmt is None:
            return
        settings = nmt_worker_settings(config, self._paths)
        if config.enabled == self._nmt_enabled and settings == self._nmt_settings:
            return
        self._nmt_settings, self._nmt_enabled = settings, config.enabled
        if config.enabled:
            # 未运行时 reload 直接用新配置启动，运行中则结束旧进程再启动（架构文档 8.2.1 节）。
            await self.nmt.reload(settings)
        else:
            await self.nmt.stop()

    async def _apply_ocr(self, config: OcrConfig) -> None:
        """按 [ocr] 节启动、重载或停止 OCR 进程。只有决定加载哪套模型的字段变化时才重载。"""
        if self.ocr is None:
            return
        cache = ocr_model_cache(self._paths)
        has_models = (cache / "official_models").is_dir()
        enabled = config.enabled and has_models
        key = (enabled, config.mode, config.device, config.language, str(cache))
        if key == self._ocr_key:
            return
        first = self._ocr_key is None
        self._ocr_key = key
        if enabled:
            # 未运行时 reload 直接启动；运行中则结束旧进程再启动（架构文档 8.2.1 节）。
            await self.ocr.reload(ocr_process_settings(config, cache))
            return
        if config.enabled and first:
            # 模型由 OCR 进程预加载时下载（需求 MOD-02）还没有实现，这里不启动进程，避免它以配置错误反复退出。
            logger.warning("OCR 模型目录 %s 里没有模型，截屏翻译不可用", cache)
        await self.ocr.stop()

    # ---- 模型管理

    def _resolve_models_dir(self) -> Path:
        """模型管理模块的目录：与 NMT 进程实际扫描的目录相同（含源码版回退到旧模型目录的情况）。"""
        config = self.bus.latest(AppConfig) if self.bus is not None else None
        nmt = _section(config, "nmt", NmtConfig) if config is not None else NmtConfig()
        return Path(str(nmt_worker_settings(nmt, self._paths)["model_dir"]))

    async def _apply_models_dir(self) -> None:
        """[nmt].model_dir 在运行中改变时，换一个指向新目录的模型管理模块（需求 MOD-04）。"""
        old = self.models
        if old is None or self.bus is None or self.client is None:
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
        if self.nmt is None or not self._nmt_enabled or self._nmt_settings is None:
            return
        if self._nmt_reload is not None and not self._nmt_reload.done():
            self._nmt_reload_again = True
            return
        self._nmt_reload = asyncio.create_task(self._reload_nmt_for_models())

    async def _reload_nmt_for_models(self) -> None:
        while True:
            self._nmt_reload_again = False
            if self.nmt is None or not self._nmt_enabled or self._nmt_settings is None:
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
            if previous is not None and previous.state == state:
                return
            worker = i18n.tr(f"worker.{name}")
            if state is WorkerState.CRASHED:
                self._notify("app.worker_crashed", NoticeLevel.WARNING, worker)
            elif state is WorkerState.FAILED:
                self._notify("app.worker_failed", NoticeLevel.ERROR, worker)

        return on_state

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

"""设置窗口测试的公共夹具。

平台：用 QT_QPA_PLATFORM=windows 在原生平台上运行时，窗口带上 WA_DontShowOnScreen，
设置窗口弹出的对话框也带同一个属性。

事件循环、QApplication、事件总线与界面语言的夹具在 tests/ui/conftest.py 里，四类界面测试共用。
文本框的 500 毫秒防抖是 Qt 计时器，测试用 ``await asyncio.sleep`` 让时间流逝；用 ``await bus.drain()``
等总线把排队的消息分发完。

两种装配：
- ``start_settings``：只启动设置窗口模块，配置由测试直接发布，设置窗口发出的命令由记录器接收；
- ``start_full``：设置窗口加真实的配置服务（临时目录里的配置文件、内存密钥存储）、引擎模块、
  统计模块和缓存模块，
  用来检查修改经过真实校验后的往返。
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from PySide6.QtCore import Qt

from lst.contracts.config import AppConfig, UpdateConfig, UpdateSecret
from lst.contracts.events import (
    CheckEngineConnection,
    ClearCache,
    EngineStatuses,
    OpenSettings,
    ResetStats,
    RestartWorker,
    WorkerStatuses,
)
from lst.contracts.messages import Command, Message
from lst.contracts.models import (
    CancelModelDownload,
    DeleteModel,
    DownloadModels,
    ModelCatalog,
    RefreshModelCatalog,
)
from lst.contracts.nmt_runtime import CancelNmtRuntimeInstall, DeleteNmtRuntime, InstallNmtRuntime
from lst.contracts.ocr_engines import (
    CancelOcrEngineInstall,
    CancelOcrModelDownload,
    DeleteOcrModel,
    DownloadOcrModel,
    InstallOcrEngine,
    UninstallOcrEngine,
)
from lst.contracts.storage import ChangeDataDir, ChangeModelsDir, RestartApp
from lst.contracts.updates import CheckForUpdates, DownloadUpdate, InstallUpdate
from lst.kernel import EventBus, ModuleContext
from lst.services.cache import CacheModule, SqliteTranslationCache
from lst.services.config import ConfigService, MemorySecretStore
from lst.services.engines import EngineDeps, EngineListSchema, EnginesModule
from lst.services.stats import SqliteStatsStore, StatsModule
from lst.ui.settings import SettingsModule
from lst.ui.settings.presenter import SettingsPresenter
from lst.ui.settings.window import SettingsWindow
from tests.ui.conftest import on_native_platform
from tests.ui.settings.support import (
    FakeDialogs,
    all_models,
    default_config,
    engine_kinds,
)

TEST_DEBOUNCE_MS = 40
"""多数测试使用的文本框防抖时间，比默认的 500 毫秒短，测试跑得快；默认值本身由单独的测试检查。"""

COMMAND_TYPES: tuple[type[Command], ...] = (
    UpdateConfig,
    UpdateSecret,
    ResetStats,
    ClearCache,
    CheckEngineConnection,
    RefreshModelCatalog,
    DownloadModels,
    CancelModelDownload,
    DeleteModel,
    InstallOcrEngine,
    CancelOcrEngineInstall,
    UninstallOcrEngine,
    DownloadOcrModel,
    CancelOcrModelDownload,
    DeleteOcrModel,
    InstallNmtRuntime,
    CancelNmtRuntimeInstall,
    DeleteNmtRuntime,
    CheckForUpdates,
    DownloadUpdate,
    InstallUpdate,
    RestartWorker,
    ChangeDataDir,
    ChangeModelsDir,
    RestartApp,
)


@dataclass
class Recorded:
    """设置窗口发出的命令，按到达顺序记录。"""

    commands: list[Command] = field(default_factory=list[Command])

    def of[C: Command](self, kind: type[C]) -> list[C]:
        return [command for command in self.commands if isinstance(command, kind)]

    @property
    def updates(self) -> list[UpdateConfig]:
        return self.of(UpdateConfig)

    @property
    def secrets(self) -> list[UpdateSecret]:
        return self.of(UpdateSecret)

    def clear(self) -> None:
        self.commands.clear()


@dataclass
class Harness:
    """一个运行中的设置窗口模块。"""

    bus: EventBus
    module: SettingsModule
    ctx: ModuleContext
    recorded: Recorded
    dialogs: FakeDialogs
    settings_path: Path
    stopped: bool = False

    @property
    def window(self) -> SettingsWindow:
        window = self.module.window
        assert window is not None
        return window

    @property
    def presenter(self) -> SettingsPresenter:
        presenter = self.module.presenter
        assert presenter is not None
        return presenter

    async def flush(self) -> None:
        await self.bus.drain()

    async def publish(
        self, *messages: AppConfig | EngineStatuses | WorkerStatuses | ModelCatalog | Message
    ) -> None:
        for message in messages:
            self.bus.publish(message)  # pyright: ignore[reportArgumentType]
        await self.bus.drain()

    async def open(self) -> SettingsWindow:
        self.bus.send(OpenSettings())
        await self.bus.drain()
        return self.window

    async def stop(self) -> None:
        if not self.stopped:
            self.stopped = True
            await self.module.stop()
            self.ctx.close()


type StartSettings = Callable[..., Awaitable[Harness]]


@pytest.fixture
def settings_path(tmp_path: Path) -> Path:
    return tmp_path / "data" / "ui_state.ini"


@pytest.fixture
async def start_settings(bus: EventBus, settings_path: Path) -> AsyncIterator[StartSettings]:
    """返回启动函数；可以多次调用（模拟重启），共用同一条总线和同一个命令记录。"""
    recorded = Recorded()
    for kind in COMMAND_TYPES:
        bus.subscribe(kind, recorded.commands.append)
    harnesses: list[Harness] = []

    async def start(
        *,
        config: AppConfig | None = None,
        sections: Sequence[str] | None = None,
        path: Path | None = None,
        debounce_ms: int = TEST_DEBOUNCE_MS,
        dialogs: FakeDialogs | None = None,
        **module_args: Any,
    ) -> Harness:
        models = all_models()
        if sections is not None:
            models = {name: model for name, model in models.items() if name in sections}
        chosen = dialogs or FakeDialogs()
        module = SettingsModule(
            path or settings_path,
            sections=models,
            engine_kinds=module_args.pop("engine_kinds", engine_kinds()),
            text_debounce_ms=debounce_ms,
            dialogs=chosen,
            app_version=module_args.pop("app_version", "9.9.9"),
            **module_args,
        )
        ctx = ModuleContext(bus, logging.getLogger("lst.ui.settings"))
        if config is not None:
            bus.publish(config)
        else:
            bus.publish(default_config())
        await module.start(ctx)
        harness = Harness(bus, module, ctx, recorded, chosen, path or settings_path)
        harnesses.append(harness)
        if on_native_platform():
            harness.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        await bus.drain()
        return harness

    yield start
    for harness in reversed(harnesses):
        await harness.stop()


@pytest.fixture
async def harness(start_settings: StartSettings) -> Harness:
    return await start_settings()


# ---------------------------------------------------------------------- 完整装配


@dataclass
class Full(Harness):
    """设置窗口加真实的服务模块。命令由真实的处理者接收，没有记录器。"""

    config_path: Path = field(default_factory=Path)
    secrets: MemorySecretStore = field(default_factory=MemorySecretStore)
    stats: SqliteStatsStore | None = None
    cache: SqliteTranslationCache | None = None
    services: list[tuple[Any, ModuleContext]] = field(default_factory=list[tuple[Any, ModuleContext]])
    extra_stops: list[Callable[[], Awaitable[None]]] = field(
        default_factory=list[Callable[[], Awaitable[None]]]
    )

    async def settle(self, rounds: int = 3) -> None:
        """让配置服务、引擎模块等异步处理者把消息处理完（它们在线程池里写文件）。"""
        for _ in range(rounds):
            await self.bus.drain()
            await asyncio.sleep(0.05)
        await self.bus.drain()

    async def stop(self) -> None:
        if self.stopped:
            return
        await super().stop()
        for module, ctx in reversed(self.services):
            await module.stop()
            ctx.close()
        for stop in self.extra_stops:
            await stop()


type StartFull = Callable[..., Awaitable[Full]]


@pytest.fixture
async def start_full(bus: EventBus, tmp_path: Path) -> AsyncIterator[StartFull]:
    fulls: list[Full] = []

    async def start(
        *,
        debounce_ms: int = TEST_DEBOUNCE_MS,
        config_text: str | None = None,
        handler: Callable[[httpx.Request], httpx.Response] | None = None,
    ) -> Full:
        config_path = tmp_path / "config" / "settings.toml"
        if config_text is not None:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            config_path.write_text(config_text, encoding="utf-8")
        secrets = MemorySecretStore()
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler or _refuse))
        stats = SqliteStatsStore(tmp_path / "stats.sqlite3")
        cache = SqliteTranslationCache(tmp_path / "cache.sqlite3")
        await cache.open()
        services: list[tuple[Any, ModuleContext]] = []
        for module in (
            ConfigService(
                config_path,
                sections=all_models(),
                list_sections={"engines": EngineListSchema()},
                secrets=secrets,
            ),
            CacheModule(cache),
            StatsModule(stats, snapshot_interval_s=0.02),
            EnginesModule(EngineDeps(secrets=secrets, client=client, nmt=None)),
        ):
            ctx = ModuleContext(bus, logging.getLogger(f"lst.{module.name}"))
            await module.start(ctx)
            services.append((module, ctx))
        # 完整装配里没有模型管理模块：模型命令由占位的处理者接收（总线要求命令恰好有一个处理者）。
        for kind in (RefreshModelCatalog, DownloadModels, CancelModelDownload, DeleteModel):
            bus.subscribe(kind, lambda _command: None)
        settings_module = SettingsModule(
            tmp_path / "ui_state.ini",
            sections=all_models(),
            engine_kinds=engine_kinds(),
            text_debounce_ms=debounce_ms,
            dialogs=FakeDialogs(),
            app_version="9.9.9",
        )
        ctx = ModuleContext(bus, logging.getLogger("lst.ui.settings"))
        await settings_module.start(ctx)
        full = Full(
            bus,
            settings_module,
            ctx,
            Recorded(),
            cast(FakeDialogs, settings_module._dialogs),
            tmp_path / "ui_state.ini",
            config_path=config_path,
            secrets=secrets,
            stats=stats,
            cache=cache,
            services=services,
        )
        full.extra_stops.extend((cache.close, client.aclose))
        if on_native_platform():
            full.window.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        fulls.append(full)
        await full.settle()
        return full

    yield start
    for full in reversed(fulls):
        await full.stop()


def _refuse(request: httpx.Request) -> httpx.Response:
    raise AssertionError("设置窗口测试不应发出 HTTP 请求")

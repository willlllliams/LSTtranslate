"""设置窗口模块（需求 UI-05、UI-06，架构文档 4.3、11.7 节）。"""

import os
from collections.abc import Collection, Mapping
from importlib import metadata
from pathlib import Path

from pydantic import BaseModel
from PySide6.QtCore import QSettings

from lst.contracts.config import AppConfig, ConfigRejected
from lst.contracts.events import (
    CacheCleared,
    EngineConnectionChecked,
    EngineStatuses,
    OpenSettings,
    StatsSnapshot,
    WorkerStatuses,
)
from lst.contracts.models import ModelCatalog
from lst.kernel import ModuleContext
from lst.ui.settings.controls import DEFAULT_DEBOUNCE_MS
from lst.ui.settings.dialogs import Dialogs, QtDialogs
from lst.ui.settings.entries import KEYLESS_KINDS
from lst.ui.settings.presenter import SettingsPresenter
from lst.ui.settings.state import WindowStateStore
from lst.ui.settings.window import SettingsWindow

__all__ = ["SettingsModule", "package_version"]


def package_version() -> str:
    """已安装的 lsttranslate 包的版本号；源码直接运行、没有安装元数据时为空串。"""
    try:
        return metadata.version("lsttranslate")
    except metadata.PackageNotFoundError:
        return ""


class SettingsModule:
    """设置窗口（需求 UI-05）。

    构造参数：
        settings_path：保存窗口位置、大小的 INI 文件，组合根传入与双语窗口、引导相同的 ui_state.ini，
            本模块用自己的 settings_window 组；
        sections：配置节名 → 该节的 pydantic 模型（[ui]、[hotkeys]、[ocr]、[screenshot]、[race]、
            [bilingual]、[nmt] 等）。界面模块不能 import services，模型由组合根注入，设置项的类型、
            取值范围和默认值都从它们读出；某一节没有注入时，依赖它的设置项不显示；
        engine_kinds：引擎类别（kind）→ 该类引擎一条 ``[[engines]]`` 的 pydantic 模型；
        keyless_kinds：不需要 API key 的引擎类别，默认只有 local_nmt；
        app_version：关于页显示的版本号，缺省时读已安装包的版本；
        text_debounce_ms：文本框停止输入后提交的等待时间，默认 500 毫秒（需求 UI-06）；
        dialogs：确认与选择目录的对话框，缺省用 Qt 的模态对话框，测试时替换。

    start 时创建窗口并保持隐藏；收到 OpenSettings 命令时显示窗口，窗口已经显示时只是带到前面。
    """

    name = "ui.settings"

    def __init__(
        self,
        settings_path: str | os.PathLike[str],
        *,
        sections: Mapping[str, type[BaseModel]],
        engine_kinds: Mapping[str, type[BaseModel]],
        keyless_kinds: Collection[str] = KEYLESS_KINDS,
        app_version: str | None = None,
        text_debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        dialogs: Dialogs | None = None,
    ) -> None:
        self._settings_path = Path(settings_path)
        self._sections = dict(sections)
        self._engine_kinds = dict(engine_kinds)
        self._keyless_kinds = frozenset(keyless_kinds)
        self._app_version = package_version() if app_version is None else app_version
        self._debounce_ms = text_debounce_ms
        self._dialogs = dialogs
        self._window: SettingsWindow | None = None
        self._presenter: SettingsPresenter | None = None

    @property
    def window(self) -> SettingsWindow | None:
        return self._window

    @property
    def presenter(self) -> SettingsPresenter | None:
        return self._presenter

    async def start(self, ctx: ModuleContext) -> None:
        if self._window is not None:
            raise RuntimeError("设置窗口模块已经启动")
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = QSettings(str(self._settings_path), QSettings.Format.IniFormat)
        presenter = SettingsPresenter(
            bus=ctx.bus,
            logger=ctx.logger,
            sections=self._sections,
            engine_kinds=self._engine_kinds,
            keyless_kinds=self._keyless_kinds,
            app_version=self._app_version,
        )
        presenter.start()  # 窗口创建时页面要读到已有的配置，所以先初始化展示器
        window = SettingsWindow(
            presenter,
            self._dialogs if self._dialogs is not None else QtDialogs(),
            WindowStateStore(settings),
            debounce_ms=self._debounce_ms,
        )
        self._window, self._presenter = window, presenter
        ctx.subscribe(AppConfig, presenter.on_config)
        ctx.subscribe(ConfigRejected, presenter.on_rejected)
        ctx.subscribe(EngineStatuses, presenter.on_engine_statuses)
        ctx.subscribe(WorkerStatuses, presenter.on_workers)
        ctx.subscribe(ModelCatalog, presenter.on_catalog)
        ctx.subscribe(StatsSnapshot, presenter.on_stats)
        ctx.subscribe(EngineConnectionChecked, presenter.on_connection_checked)
        ctx.subscribe(CacheCleared, presenter.on_cache_cleared)
        ctx.subscribe(OpenSettings, lambda _: presenter.on_open_settings())

    async def stop(self) -> None:
        """提交还没发出的输入，保存窗口状态并关闭窗口。订阅由 ModuleContext.close 统一取消。"""
        presenter, window = self._presenter, self._window
        self._presenter = self._window = None
        if window is not None:
            window.flush()
        if presenter is not None:
            presenter.close()
        if window is not None:
            window.close_for_good()
            window.deleteLater()

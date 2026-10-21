"""双语窗口模块（需求 BI-01 至 BI-07，架构文档 4.3、11.6 节）。"""

import os
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QGuiApplication

from lst.contracts.config import AppConfig
from lst.contracts.events import EngineStatuses, SegmentsUpdated, ToggleBilingualWindow
from lst.kernel import ModuleContext
from lst.ui.bilingual.presenter import BilingualPresenter
from lst.ui.bilingual.state import WindowStateStore
from lst.ui.bilingual.window import BilingualWindow


def system_clipboard(text: str) -> None:
    """把文字写进系统剪贴板。"""
    QGuiApplication.clipboard().setText(text)


class BilingualModule:
    """双语窗口模块。

    settings_path 是保存窗口状态的 INI 文件（组合根传入数据目录下的 ui_state.ini 之类的路径），
    clipboard 用于替换剪贴板写入，缺省时写系统剪贴板。
    start 时创建展示器和窗口，窗口创建好但保持隐藏，收到 ToggleBilingualWindow 时只做显示
    （架构文档 11.2 节“窗口首次创建”）。
    """

    name = "ui.bilingual"

    def __init__(
        self, settings_path: str | os.PathLike[str], *, clipboard: Callable[[str], None] | None = None
    ) -> None:
        self._settings_path = Path(settings_path)
        self._clipboard = clipboard or system_clipboard
        self._window: BilingualWindow | None = None
        self._presenter: BilingualPresenter | None = None

    @property
    def window(self) -> BilingualWindow | None:
        return self._window

    @property
    def presenter(self) -> BilingualPresenter | None:
        return self._presenter

    async def start(self, ctx: ModuleContext) -> None:
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = QSettings(str(self._settings_path), QSettings.Format.IniFormat)
        window = BilingualWindow()
        presenter = BilingualPresenter(
            window,
            bus=ctx.bus,
            store=WindowStateStore(settings),
            logger=ctx.logger,
            clipboard=self._clipboard,
        )
        self._window, self._presenter = window, presenter
        ctx.subscribe(ToggleBilingualWindow, presenter.on_toggle)
        ctx.subscribe(SegmentsUpdated, presenter.on_segments_updated)
        ctx.subscribe(AppConfig, presenter.on_config)
        ctx.subscribe(EngineStatuses, presenter.on_engine_statuses)

    async def stop(self) -> None:
        """保存窗口状态并关闭窗口。订阅由 ModuleContext.close 统一取消。"""
        presenter, window = self._presenter, self._window
        self._presenter = self._window = None
        if presenter is not None:
            presenter.shutdown()
        if window is not None:
            window.close_for_good()
            window.deleteLater()

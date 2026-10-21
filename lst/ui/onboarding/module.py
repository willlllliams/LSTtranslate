"""首次运行引导模块（需求 UI-08、INS-03，架构文档 4.3、11.7 节）。"""

import os
from pathlib import Path

from PySide6.QtCore import QSettings

from lst.contracts.config import AppConfig
from lst.contracts.events import ShowModelDownloads
from lst.contracts.models import ModelCatalog
from lst.kernel import ModuleContext
from lst.ui.onboarding.presenter import OnboardingPresenter
from lst.ui.onboarding.store import SettingsCompletionStore
from lst.ui.onboarding.window import OnboardingWindow


class OnboardingModule:
    """首次运行引导。

    settings_path 是记录“引导已完成”的 INI 文件，组合根可以传入与双语窗口相同的 ui_state.ini。
    start 时创建窗口；引导没有完成过时立即显示，否则保持隐藏。托盘菜单的“下载翻译模型”发送
    ShowModelDownloads 命令，这里收到后重新显示窗口；组合根也可以直接调用 ``show()``。
    """

    name = "ui.onboarding"

    def __init__(self, settings_path: str | os.PathLike[str]) -> None:
        self._settings_path = Path(settings_path)
        self._window: OnboardingWindow | None = None
        self._presenter: OnboardingPresenter | None = None

    @property
    def window(self) -> OnboardingWindow | None:
        return self._window

    @property
    def presenter(self) -> OnboardingPresenter | None:
        return self._presenter

    async def start(self, ctx: ModuleContext) -> None:
        if self._window is not None:
            raise RuntimeError("首次运行引导模块已经启动")
        self._settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings = QSettings(str(self._settings_path), QSettings.Format.IniFormat)
        window = OnboardingWindow()
        presenter = OnboardingPresenter(
            window, bus=ctx.bus, store=SettingsCompletionStore(settings), logger=ctx.logger
        )
        window.bind(presenter)
        self._window, self._presenter = window, presenter
        presenter.start()
        ctx.subscribe(ModelCatalog, presenter.on_catalog)
        ctx.subscribe(AppConfig, presenter.on_config)
        ctx.subscribe(ShowModelDownloads, lambda _: presenter.show())
        if presenter.show_if_needed():
            ctx.logger.info("首次运行：显示引导窗口")

    def show(self) -> None:
        """显示引导窗口（托盘、设置窗口重新打开引导时调用）。模块未启动时抛出 RuntimeError。"""
        if self._presenter is None:
            raise RuntimeError("首次运行引导模块没有启动")
        self._presenter.show()

    async def stop(self) -> None:
        """关闭窗口。订阅由 ModuleContext.close 统一取消。重复调用没有效果。"""
        presenter, window = self._presenter, self._window
        self._presenter = self._window = None
        if presenter is not None:
            presenter.close()
        if window is not None:
            window.close_for_good()
            window.deleteLater()

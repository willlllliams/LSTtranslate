"""托盘模块的入口：实现 kernel 的 Module 协议，装配视图与展示器（架构文档 4.3、11.1 节）。"""

from pathlib import Path

from PySide6.QtWidgets import QSystemTrayIcon

from lst.kernel import ModuleContext
from lst.ui.tray.presenter import CONFIG_REJECT_WINDOW_MS, TrayPresenter
from lst.ui.tray.view import QtOneShotTimer, QtTrayView, load_icon


class TrayModule:
    """系统托盘（需求 TR-01 至 TR-05）。

    构造参数：
        app_name：应用名，显示在托盘图标的悬停提示里。
        icon_path：托盘图标文件；为 None、不存在或无法解码时改用程序绘制的图标。
        log_dir：“打开日志目录”打开的目录。
        tray_available：系统托盘是否可用，None 表示询问 Qt。测试用它模拟两种情况。
        reject_window_ms：ConfigRejected 的聚合窗口，默认 1 秒。
    """

    name = "ui.tray"

    def __init__(
        self,
        app_name: str,
        icon_path: Path | str | None,
        log_dir: Path | str,
        *,
        tray_available: bool | None = None,
        reject_window_ms: int = CONFIG_REJECT_WINDOW_MS,
    ) -> None:
        self._app_name = app_name
        self._icon_path = Path(icon_path) if icon_path is not None else None
        self._log_dir = Path(log_dir)
        self._tray_available = tray_available
        self._reject_window_ms = reject_window_ms
        self._view: QtTrayView | None = None
        self._presenter: TrayPresenter | None = None

    @property
    def view(self) -> QtTrayView | None:
        """运行期间的视图；未启动或已停止时为 None。"""
        return self._view

    @property
    def presenter(self) -> TrayPresenter | None:
        """运行期间的展示器；未启动或已停止时为 None。"""
        return self._presenter

    async def start(self, ctx: ModuleContext) -> None:
        """创建托盘图标与菜单并订阅消息。系统托盘不可用时记一条警告，菜单逻辑照常工作。"""
        if self._view is not None:
            raise RuntimeError("托盘模块已经启动")
        available = self._tray_available
        if available is None:
            available = QSystemTrayIcon.isSystemTrayAvailable()
        if not available:
            ctx.logger.warning("系统托盘不可用：托盘图标和通知不会显示，菜单照常构建")
        view = QtTrayView(
            self._app_name,
            load_icon(self._icon_path, ctx.logger),
            tray_available=available,
            logger=ctx.logger,
        )
        presenter = TrayPresenter(
            ctx,
            view,
            app_name=self._app_name,
            log_dir=self._log_dir,
            timer_factory=QtOneShotTimer,
            reject_window_ms=self._reject_window_ms,
        )
        view.bind(presenter)
        presenter.start()
        view.show()
        self._view = view
        self._presenter = presenter

    async def stop(self) -> None:
        """取消订阅与待发的通知，隐藏并销毁托盘图标。重复调用没有效果。"""
        presenter, self._presenter = self._presenter, None
        view, self._view = self._view, None
        if presenter is not None:
            presenter.close()
        if view is not None:
            view.close()

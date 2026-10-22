"""覆盖窗口模块（需求 OV-01 至 OV-07，架构文档 4.3、5.3、11.5 节）。"""

from collections.abc import Callable

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QWidget

from lst.contracts.config import AppConfig
from lst.contracts.events import EngineStatuses, SegmentsUpdated, SessionProgress, WorkerStatuses
from lst.contracts.ocr import CaptureCompleted, OcrCompleted, OcrFailed
from lst.kernel import ModuleContext
from lst.ui.overlay.presenter import OverlayPresenter
from lst.ui.overlay.window import OverlayWindow


def system_clipboard(text: str) -> None:
    """把文字写进系统剪贴板。"""
    QGuiApplication.clipboard().setText(text)


class OverlayModule:
    """覆盖窗口模块。

    clipboard 替换剪贴板写入（测试用记录列表），缺省写系统剪贴板；
    prepare_window 在每个覆盖窗口显示之前调用（测试用来加上 WA_DontShowOnScreen）。
    覆盖窗口在每次截屏完成时新建：窗口的尺寸、位置和底图都随截屏而定。
    """

    name = "ui.overlay"

    def __init__(
        self,
        *,
        clipboard: Callable[[str], None] | None = None,
        prepare_window: Callable[[QWidget], None] | None = None,
    ) -> None:
        self._clipboard = clipboard or system_clipboard
        self._prepare = prepare_window
        self._presenter: OverlayPresenter | None = None

    @property
    def presenter(self) -> OverlayPresenter | None:
        return self._presenter

    @property
    def window(self) -> OverlayWindow | None:
        return self._presenter.window if self._presenter is not None else None

    async def start(self, ctx: ModuleContext) -> None:
        if self._presenter is not None:
            raise RuntimeError("覆盖窗口模块已经启动过")
        prepare = self._prepare

        def factory(event: CaptureCompleted, presenter: OverlayPresenter) -> OverlayWindow:
            window = OverlayWindow(event.image, event.geometry, presenter)
            if prepare is not None:
                prepare(window)
            return window

        presenter = OverlayPresenter(
            ctx.bus, clipboard=self._clipboard, window_factory=factory, logger=ctx.logger
        )
        self._presenter = presenter
        ctx.subscribe(AppConfig, presenter.on_config)
        ctx.subscribe(WorkerStatuses, presenter.on_worker_statuses)
        ctx.subscribe(EngineStatuses, presenter.on_engine_statuses)
        ctx.subscribe(CaptureCompleted, presenter.on_capture)
        ctx.subscribe(OcrCompleted, presenter.on_ocr_completed)
        ctx.subscribe(OcrFailed, presenter.on_ocr_failed)
        ctx.subscribe(SegmentsUpdated, presenter.on_segments_updated)
        ctx.subscribe(SessionProgress, presenter.on_progress)

    async def stop(self) -> None:
        """关闭覆盖窗口。订阅由 ModuleContext.close 统一取消。"""
        if self._presenter is not None:
            self._presenter.shutdown()

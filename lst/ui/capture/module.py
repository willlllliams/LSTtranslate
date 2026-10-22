"""截屏模块（需求 CAP-01 至 CAP-07，架构文档 4.3、5.3、11.4 节）。"""

from lst.contracts.config import AppConfig
from lst.contracts.events import StartCapture
from lst.kernel import ModuleContext
from lst.ui.capture.controller import CaptureController, Freezer, WindowHook
from lst.ui.common.bindings import bindings_from
from lst.ui.i18n import apply_config_language


class CaptureModule:
    """截屏模块：StartCapture 的唯一处理者，框选完成后发布 CaptureCompleted。

    freezer 替换冻结屏幕的方式（测试注入虚构的屏幕布局），缺省读真实屏幕；
    prepare_window 在每个蒙版窗口显示之前调用（测试用来加上 WA_DontShowOnScreen）。
    蒙版窗口在每次截屏时新建：屏幕的数量与布局每次都可能不同。
    """

    name = "ui.capture"

    def __init__(self, *, freezer: Freezer | None = None, prepare_window: WindowHook | None = None) -> None:
        self._freezer = freezer
        self._prepare = prepare_window
        self._controller: CaptureController | None = None

    @property
    def controller(self) -> CaptureController | None:
        return self._controller

    async def start(self, ctx: ModuleContext) -> None:
        if self._controller is not None:
            raise RuntimeError("截屏模块已经启动过")
        controller = CaptureController(
            ctx.bus, freezer=self._freezer, prepare_window=self._prepare, logger=ctx.logger
        )
        self._controller = controller
        ctx.subscribe(StartCapture, lambda _command: controller.start())
        ctx.subscribe(AppConfig, lambda config: _apply_config(controller, config))

    async def stop(self) -> None:
        """取消进行中的截屏。订阅由 ModuleContext.close 统一取消。"""
        if self._controller is not None:
            self._controller.cancel()


def _apply_config(controller: CaptureController, config: AppConfig) -> None:
    # 蒙版上的提示文字随界面语言变化（CFG-05）；键位（UI-09）对正在显示的蒙版与之后的截屏都生效。
    apply_config_language(config)
    controller.set_bindings(bindings_from(config.section("bindings")))

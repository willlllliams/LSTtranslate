"""自动化接口模块：按配置开启或关闭命名管道服务端，发布 AutomationStatus
（需求 DEV-01 至 DEV-08，架构文档第 16 章）。

- 开关是 ``[automation].enabled``，没有写时按构建类型取默认值（config.effective_enabled）。每次 AppConfig 变化
  都重新判断，运行中打开或关闭立即建立或撤掉管道，不需要重启；
- 管道名缺省按当前用户的 SID 生成，环境变量 LST_AUTOMATION_PIPE 或组合根的 pipe_name 可以覆盖；
- 本次运行第一次开启时发一条通知，提醒用户本机的程序现在可以控制 LSTtranslate；托盘的悬停提示在开启期间
  写“自动化接口已开启”（托盘订阅 AutomationStatus）；
- 开启期间记录截屏会话的结果（recorder），关闭时清空。日志只记方法名、结果与耗时。
"""

import asyncio
import logging

from lst.app.automation.config import automation_config, effective_enabled
from lst.app.automation.methods import AutomationDeps, AutomationMethods
from lst.app.automation.recorder import SessionRecorder
from lst.app.automation.server import Deferred, PipeServer
from lst.automation import winsec
from lst.automation.protocol import Json, Request
from lst.contracts.automation import AutomationStatus
from lst.contracts.config import AppConfig
from lst.contracts.events import NoticeLevel, NotificationRequested, SegmentsUpdated, SessionProgress
from lst.contracts.ocr import CaptureCompleted, OcrCompleted, OcrFailed, OcrStarted
from lst.kernel import ModuleContext
from lst.ui import i18n

i18n.register_catalog(
    {
        "automation.notice.title": {"zh-Hans": "自动化接口已开启", "en": "Automation interface is on"},
        "automation.notice.message": {
            "zh-Hans": "这台电脑上以你的账户运行的程序现在可以控制 LSTtranslate。"
            "不需要时在设置的“关于与诊断”里关闭",
            "en": "Programs running under your account on this PC can now control LSTtranslate. "
            "Turn it off under About and diagnostics in the settings when you do not need it",
        },
    }
)


class AutomationModule:
    """自动化接口，实现 kernel 的 Module 协议。

    deps 给出构建类型、版本号、路径与各界面模块；pipe_name 为 None 时用 winsec.default_pipe_name()。
    """

    name = "automation"

    def __init__(self, deps: AutomationDeps, *, pipe_name: str | None = None) -> None:
        self._deps = deps
        self._pipe_override = pipe_name
        self._pipe = ""
        self._ctx: ModuleContext | None = None
        self._logger = logging.getLogger(__name__)
        self._recorder = SessionRecorder()
        self._methods: AutomationMethods | None = None
        self._server: PipeServer | None = None
        self._status: AutomationStatus | None = None
        self._enabled = False
        self._explicit = False
        self._error = ""
        self._notified = False
        self._closing: set[asyncio.Task[None]] = set()

    @property
    def server(self) -> PipeServer | None:
        return self._server

    @property
    def pipe(self) -> str:
        return self._pipe

    @property
    def status(self) -> AutomationStatus | None:
        return self._status

    async def start(self, ctx: ModuleContext) -> None:
        if self._ctx is not None:
            raise RuntimeError("自动化接口模块已经启动")
        self._ctx = ctx
        self._logger = ctx.logger
        try:
            self._pipe = winsec.normalize_pipe_name(self._pipe_override) if self._pipe_override else ""
            self._pipe = self._pipe or winsec.default_pipe_name()
        except (OSError, ValueError) as exc:
            self._error = f"无法确定管道名：{exc}"
            self._logger.warning("自动化接口不可用：%s", self._error)
        self._methods = AutomationMethods(ctx.bus, self._deps, self._recorder, lambda: self._status)
        recorder = self._recorder
        ctx.subscribe(CaptureCompleted, recorder.on_capture)
        ctx.subscribe(OcrStarted, recorder.on_ocr_started)
        ctx.subscribe(OcrCompleted, recorder.on_ocr_completed)
        ctx.subscribe(OcrFailed, recorder.on_ocr_failed)
        ctx.subscribe(SegmentsUpdated, recorder.on_segments)
        ctx.subscribe(SessionProgress, recorder.on_progress)
        ctx.subscribe(AppConfig, self._on_config)
        self._publish()

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            await server.aclose()
            self._logger.info("自动化接口已关闭")
        if self._closing:
            await asyncio.gather(*self._closing, return_exceptions=True)
        self._recorder.enabled = False
        self._recorder.clear()
        self._ctx = None

    # ------------------------------------------------------------------ 开关

    def _on_config(self, config: AppConfig) -> None:
        section = automation_config(config)
        self._explicit = section.enabled is not None
        self._apply(effective_enabled(section, self._deps.flavor))

    def _apply(self, enabled: bool) -> None:
        self._enabled = enabled
        if enabled and self._server is None and self._pipe:
            self._open()
        elif not enabled and self._server is not None:
            server, self._server = self._server, None
            # 正在处理的请求（例如关掉开关的这一次 settings.set）写完回复后断开，之后释放安全描述符。
            task = asyncio.get_running_loop().create_task(server.aclose(), name="automation:close")
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
            self._recorder.enabled = False
            self._recorder.clear()
            self._error = ""
            self._logger.info("自动化接口已关闭")
        self._publish()

    def _open(self) -> None:
        methods = self._methods
        assert methods is not None
        server = PipeServer(self._pipe, self._dispatch, logger=self._logger)
        try:
            server.start()
        except (OSError, RuntimeError) as exc:
            self._error = str(exc)
            self._logger.warning("自动化接口无法开启（管道 %s）：%s", self._pipe, exc)
            return
        self._server = server
        self._error = ""
        self._recorder.enabled = True
        self._logger.info("自动化接口已开启：%s（构建类型 %s）", self._pipe, self._deps.flavor.value)
        if not self._notified and self._ctx is not None:
            self._notified = True
            self._ctx.bus.publish(
                NotificationRequested(
                    "automation.notice.title", "automation.notice.message", NoticeLevel.WARNING
                )
            )

    async def _dispatch(self, request: Request) -> Json | Deferred:
        methods = self._methods
        assert methods is not None
        return await methods.dispatch(request)

    def _publish(self) -> None:
        status = AutomationStatus(
            flavor=self._deps.flavor,
            enabled=self._enabled,
            explicit=self._explicit,
            listening=self._server is not None,
            pipe=self._pipe,
            error=self._error if self._enabled else "",
        )
        if status != self._status:
            self._status = status
            if self._ctx is not None:
                self._ctx.bus.publish(status)


__all__ = ["AutomationModule"]

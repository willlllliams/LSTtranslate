"""OCR 模块：把截屏、OCR 进程和竞速接到事件总线上（架构文档第 7 章第 4、5 步）。

模块订阅四类消息：

- CaptureCompleted：把截图交给 OCR 进程。请求号用 UUID4，options 来自当前配置（config.request_options），
  像素帧是截图的 BGRA 字节，处理时限按模式和子能力分档（config.processing_timeout）。
- RetryOcr：重新识别最近一次截屏（需求 OCR-08）。只有最近一次截屏的会话、并且上一次识别以失败结束时才
  重新提交；其他情况忽略并记日志。
- AppConfig：解析 [ocr] 节，之后提交的请求使用新配置。识别模式、推理设备或识别语言变化时调用构造参数
  ``on_reload_required``，由组合根让监管器重载 OCR 进程（见下文“重载”）。
- WorkerStatuses：只记录 OCR 进程的状态变化。“识别模型加载中”由覆盖窗口订阅同一状态显示（需求 OCR-07）；
  加载期间提交的请求在监管器里排队，就绪后自动发出。

识别结果的处理：OcrResult 依次经过 decode_blocks、blocks_to_segments，发布 OcrCompleted；
RequestFailed 发布 OcrFailed，错误码取自 RequestFailed.code（未知取值按 engine.internal）。

同一时刻只有一个识别任务（需求 OCR-06、CAP-06）：新的截屏到达时，模块取消上一个请求并停止读取它的回复，
上一个会话迟到的结果因此直接丢弃；监管器把取消转发给 OCR 进程，进程按到达顺序处理请求，正在执行的旧请求
跑完后才开始新请求。

重载与取消：监管器重载或停止 OCR 进程时，已发给进程的请求以 cancelled 结束（架构文档 8.2.1 节）。
被取消的如果是当前会话的请求，模块自动重新提交一次，新请求在监管器里排队等新进程就绪；同一会话再次被取消
时发布 OcrFailed（engine.worker_crashed），覆盖窗口显示失败原因和重试按钮，而不是一直停在“识别中”。
模块自己取消的旧请求不会走到这一步，因为它的回复已经不再读取。

日志只写会话号、请求号、尺寸、块数、段数、耗时和错误码，不写识别出的文本（需求 PRV-02）。
"""

import asyncio
import contextlib
import logging
import time
import traceback
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Final

from pydantic import ValidationError

from lst.contracts.config import AppConfig
from lst.contracts.errors import ErrorCode
from lst.contracts.events import WorkerStatuses
from lst.contracts.ids import SessionId
from lst.contracts.ocr import CaptureCompleted, OcrCompleted, OcrFailed, RawImage, RetryOcr
from lst.contracts.ports import WorkerChannel, WorkerRequestHandle, WorkerState
from lst.contracts.worker_protocol import (
    CANCELLED,
    JsonValue,
    OcrRequest,
    OcrResult,
    RequestFailed,
    Started,
    WorkerReply,
)
from lst.kernel import EventBus, ModuleContext
from lst.services.ocr.blocks import decode_blocks
from lst.services.ocr.config import (
    OcrConfig,
    needs_reload,
    processing_timeout,
    request_options,
    worker_settings,
)
from lst.services.ocr.paragraphs import GeometricMerger, ParagraphMerger
from lst.services.ocr.segments import blocks_to_segments

OCR_WORKER: Final = "ocr"
"""WorkerStatuses 里 OCR 进程的键。"""

MAX_AUTO_RESUBMITS: Final = 1
"""当前会话的请求被监管器取消（重载、停止）后自动重新提交的次数上限。"""

ERROR_SUBMIT: Final = "提交识别请求失败"
ERROR_STREAM: Final = "读取 OCR 进程的回复出错"
ERROR_NO_RESULT: Final = "OCR 进程的回复在给出结果之前就结束了"
ERROR_CONVERT: Final = "转换识别结果出错"
ERROR_CANCELLED: Final = "识别请求被取消，OCR 进程可能正在重启"

type ReloadCallback = Callable[[dict[str, JsonValue]], None]
"""请组合根重载 OCR 进程；参数是新的 Configure 内容（config.worker_settings）。"""


@dataclass(slots=True)
class _Capture:
    """最近一次截屏。识别成功后释放图像：只有失败的识别才需要重试（需求 OCR-08）。"""

    session_id: SessionId
    image: RawImage | None


@dataclass(slots=True, eq=False)
class _Attempt:
    """一次已提交的识别请求。config 是提交时的配置，结果按其中的模式转换。"""

    session_id: SessionId
    request: OcrRequest
    config: OcrConfig
    handle: WorkerRequestHandle
    resubmits: int
    submitted_at: float
    started_at: float | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


class OcrModule:
    """OCR 模块（实现 kernel 的 Module 协议）。

    channel 是 OCR 进程的 WorkerChannel（由组合根注入监管器）；merger 是文本模式的段落合并器，缺省时用默认
    参数的 GeometricMerger；clock 只用于计算日志里的耗时，缺省为 time.monotonic。

    on_reload_required 在识别模式、推理设备或识别语言变化时调用，参数是新的 Configure 内容。重载由组合根执行，
    通常是 ``lambda s: spawn(supervisor.reload(s))``；模块只判断“需要重载”这件事。模块启动时的配置是基准，
    不触发回调：组合根启动监管器时用同一份 AppConfig 计算初始设置（config.worker_settings）。
    回调为 None 时只记日志。
    """

    name = "ocr"

    def __init__(
        self,
        channel: WorkerChannel,
        *,
        merger: ParagraphMerger | None = None,
        clock: Callable[[], float] | None = None,
        on_reload_required: ReloadCallback | None = None,
    ) -> None:
        self._channel = channel
        self._merger: ParagraphMerger = merger if merger is not None else GeometricMerger()
        self._clock = clock if clock is not None else time.monotonic
        self._on_reload_required = on_reload_required
        self._logger = logging.getLogger(__name__)
        self._bus: EventBus | None = None
        self._running = False
        self._config = OcrConfig()
        self._capture: _Capture | None = None
        self._attempt: _Attempt | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._worker_state: WorkerState | None = None

    # ---- 查询 ----

    @property
    def config(self) -> OcrConfig:
        """当前生效的 [ocr] 配置。"""
        return self._config

    @property
    def worker_state(self) -> WorkerState | None:
        """最近一次 WorkerStatuses 里 OCR 进程的状态；没有收到过时为 None。"""
        return self._worker_state

    @property
    def busy(self) -> bool:
        """有一个识别请求正在进行。"""
        return self._attempt is not None

    # ---- 生命周期 ----

    async def start(self, ctx: ModuleContext) -> None:
        """订阅消息。总线上已有的 AppConfig 先同步应用一次，作为判断是否需要重载的基准。"""
        if self._bus is not None:
            raise RuntimeError("OCR 模块已经启动过")
        self._bus = ctx.bus
        self._logger = ctx.logger
        self._running = True
        config = ctx.bus.latest(AppConfig)
        if config is not None:
            self._config = self._parse(config)
        ctx.subscribe(AppConfig, self._on_config)
        ctx.subscribe(WorkerStatuses, self._on_worker_statuses)
        ctx.subscribe(CaptureCompleted, self._on_capture)
        ctx.subscribe(RetryOcr, self._on_retry)

    async def stop(self) -> None:
        """取消正在进行的识别并停止读取回复。之后到达的消息都被忽略。可以重复调用。"""
        if not self._running:
            return
        self._running = False
        attempt, self._attempt = self._attempt, None
        if attempt is not None:
            self._cancel(attempt)
        self._capture = None
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---- 消息处理 ----

    def _on_capture(self, event: CaptureCompleted) -> None:
        if not self._running:
            return
        previous, self._attempt = self._attempt, None
        if previous is not None:
            self._logger.info(
                "会话 %s 的新截屏到达，放弃会话 %s 的识别请求 %s",
                event.session_id,
                previous.session_id,
                previous.request.request_id,
            )
            self._cancel(previous)
        capture = _Capture(event.session_id, event.image)
        self._capture = capture
        self._submit(capture, resubmits=0)

    def _on_retry(self, command: RetryOcr) -> None:
        if not self._running:
            return
        capture = self._capture
        if capture is None or capture.session_id != command.session_id:
            self._logger.warning("会话 %s 不是最近一次截屏，忽略重试请求", command.session_id)
            return
        if self._attempt is not None:
            self._logger.info("会话 %s 的识别仍在进行，忽略重试请求", command.session_id)
            return
        if capture.image is None:
            self._logger.info("会话 %s 已经识别成功，忽略重试请求", command.session_id)
            return
        self._logger.info("重新识别会话 %s", command.session_id)
        self._submit(capture, resubmits=0)

    def _on_config(self, message: AppConfig) -> None:
        if not self._running:
            return
        old, new = self._config, self._parse(message)
        if new == old:
            return
        self._config = new
        self._logger.info(
            "OCR 配置已更新：模式 %s，设备 %s，语言 %s，处理时限 %g 秒",
            new.mode.value,
            new.device.value,
            new.language,
            processing_timeout(new),
        )
        if not needs_reload(old, new):
            return
        callback = self._on_reload_required
        if callback is None:
            self._logger.warning("识别模式、设备或语言已变化，但没有提供重载 OCR 进程的回调")
            return
        try:
            callback(worker_settings(new))
        except Exception:
            self._logger.exception("请求重载 OCR 进程时出错")

    def _on_worker_statuses(self, message: WorkerStatuses) -> None:
        status = message.statuses.get(OCR_WORKER)
        state = None if status is None else status.state
        if state == self._worker_state:
            return
        previous, self._worker_state = self._worker_state, state
        # 就绪与忙碌每个请求都会来回切换一次，记 DEBUG；其余变化记 INFO。
        routine = {previous, state} <= {WorkerState.READY, WorkerState.BUSY}
        detail = f"（{status.detail}）" if status is not None and status.detail else ""
        self._logger.log(
            logging.DEBUG if routine else logging.INFO,
            "OCR 进程状态：%s → %s%s",
            None if previous is None else previous.value,
            None if state is None else state.value,
            detail,
        )

    # ---- 提交与回复 ----

    def _submit(self, capture: _Capture, *, resubmits: int) -> None:
        """为 capture 提交一个识别请求，并开始读取回复。"""
        image = capture.image
        assert image is not None, "只有带图像的截屏才能提交"
        config = self._config
        request = OcrRequest(
            request_id=uuid.uuid4().hex,
            session_id=capture.session_id,
            width=image.width,
            height=image.height,
            stride=image.stride,
            options=request_options(config),
        )
        timeout_s = processing_timeout(config)
        try:
            handle = self._channel.submit(request, payload=_payload(image), timeout_s=timeout_s)
        except Exception as exc:
            # 异常信息里可能带着请求对象，日志只记异常类型。
            self._logger.warning("会话 %s 提交识别请求失败（%s）", capture.session_id, type(exc).__name__)
            self._publish(
                OcrFailed(capture.session_id, ErrorCode.INTERNAL, f"{ERROR_SUBMIT}（{type(exc).__name__}）")
            )
            return
        attempt = _Attempt(capture.session_id, request, config, handle, resubmits, self._clock())
        self._attempt = attempt
        state = self._channel.state
        self._logger.info(
            "会话 %s 提交识别请求 %s：%d×%d，%s 模式，处理时限 %g 秒%s",
            capture.session_id,
            request.request_id,
            image.width,
            image.height,
            config.mode.value,
            timeout_s,
            ""
            if state in (WorkerState.READY, WorkerState.BUSY)
            else f"；OCR 进程处于 {state.value}，请求排队等待",
        )
        task = asyncio.get_running_loop().create_task(
            self._consume(attempt), name=f"ocr:{request.request_id}"
        )
        attempt.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _consume(self, attempt: _Attempt) -> None:
        """读取一个请求的回复，直到结果、失败或回复流结束。请求不再是当前请求时立即退出。"""
        request_id = attempt.request.request_id
        replies: AsyncIterator[WorkerReply] | None = None
        try:
            replies = attempt.handle.replies()
            async for reply in replies:
                if self._attempt is not attempt:
                    return
                if not isinstance(reply, Started | OcrResult | RequestFailed):
                    self._logger.warning(
                        "识别请求 %s 收到不属于 OCR 请求的回复 %s，已忽略", request_id, type(reply).__name__
                    )
                    continue
                if reply.request_id != request_id:
                    self._logger.warning(
                        "识别请求 %s 收到属于请求 %s 的回复，已忽略", request_id, reply.request_id
                    )
                    continue
                if isinstance(reply, Started):
                    attempt.started_at = self._clock()
                    self._logger.debug("OCR 进程开始处理请求 %s", request_id)
                    continue
                self._attempt = None
                if isinstance(reply, OcrResult):
                    self._complete(attempt, reply)
                else:
                    self._failed(attempt, reply)
                return
        except Exception as exc:
            if self._attempt is attempt:
                self._attempt = None
                self._logger.warning("读取识别请求 %s 的回复出错（%s）", request_id, type(exc).__name__)
                self._publish(
                    OcrFailed(
                        attempt.session_id, ErrorCode.INTERNAL, f"{ERROR_STREAM}（{type(exc).__name__}）"
                    )
                )
            return
        finally:
            if replies is not None:
                await _close(replies)
        if self._attempt is attempt:
            self._attempt = None
            self._logger.warning("识别请求 %s 的回复流在给出结果之前结束", request_id)
            self._publish(OcrFailed(attempt.session_id, ErrorCode.INTERNAL, ERROR_NO_RESULT))

    def _complete(self, attempt: _Attempt, result: OcrResult) -> None:
        """识别成功：转换成版面块与段，发布 OcrCompleted。"""
        received = self._clock()
        request = attempt.request
        try:
            blocks = decode_blocks(result.blocks, width=request.width, height=request.height)
            segments = blocks_to_segments(blocks, attempt.config.mode, self._merger)
        except Exception as exc:
            # 只记异常类型和调用栈，不记异常信息：自定义合并器的异常信息里可能带着文本。
            self._logger.error(
                "会话 %s 的识别结果转换出错（%s）：\n%s",
                attempt.session_id,
                type(exc).__name__,
                "".join(traceback.format_tb(exc.__traceback__)),
            )
            self._publish(
                OcrFailed(attempt.session_id, ErrorCode.INTERNAL, f"{ERROR_CONVERT}（{type(exc).__name__}）")
            )
            return
        converted = self._clock()
        self._publish(OcrCompleted(attempt.session_id, blocks, segments))
        capture = self._capture
        if capture is not None and capture.session_id == attempt.session_id:
            capture.image = None
        started = attempt.started_at if attempt.started_at is not None else attempt.submitted_at
        self._logger.info(
            "会话 %s 识别完成：%d 个版面块（收到 %d 个），%d 个段；"
            "排队 %.0f 毫秒，识别 %.0f 毫秒，转换 %.1f 毫秒",
            attempt.session_id,
            len(blocks),
            len(result.blocks),
            len(segments),
            (started - attempt.submitted_at) * 1000,
            (received - started) * 1000,
            (converted - received) * 1000,
        )

    def _failed(self, attempt: _Attempt, failed: RequestFailed) -> None:
        """识别失败：发布 OcrFailed。监管器取消的当前请求先自动重新提交一次。"""
        capture = self._capture
        if failed.code == CANCELLED:
            if (
                attempt.resubmits < MAX_AUTO_RESUBMITS
                and capture is not None
                and capture.session_id == attempt.session_id
                and capture.image is not None
            ):
                self._logger.info(
                    "会话 %s 的识别请求 %s 被监管器取消（%s），重新提交",
                    attempt.session_id,
                    failed.request_id,
                    failed.error,
                )
                self._submit(capture, resubmits=attempt.resubmits + 1)
                return
            code, detail = ErrorCode.WORKER_CRASHED, f"{ERROR_CANCELLED}（{failed.error}）"
        else:
            code, detail = self._error_code(failed.code, failed.error)
        self._logger.warning("会话 %s 识别失败（%s）：%s", attempt.session_id, code.value, failed.error)
        self._publish(OcrFailed(attempt.session_id, code, detail))

    def _error_code(self, raw: str, detail: str) -> tuple[ErrorCode, str]:
        try:
            return ErrorCode(raw), detail
        except ValueError:
            self._logger.warning("OCR 进程返回了未知错误码 %r，按内部错误处理", raw)
            return ErrorCode.INTERNAL, f"{detail}（未知错误码 {raw}）"

    # ---- 内部 ----

    def _cancel(self, attempt: _Attempt) -> None:
        """取消请求并停止读取它的回复。监管器抛出的异常只记日志。"""
        try:
            attempt.handle.cancel()
        except Exception as exc:
            self._logger.warning(
                "取消识别请求 %s 时出错（%s）", attempt.request.request_id, type(exc).__name__
            )
        task = attempt.task
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _publish(self, message: OcrCompleted | OcrFailed) -> None:
        if self._bus is not None and self._running:
            self._bus.publish(message)

    def _parse(self, config: AppConfig) -> OcrConfig:
        """解析 [ocr] 节：缺失时用默认值。配置服务已经校验过，解析失败说明组合根没有注册这一节的模型，
        这时记录错误并沿用当前值。"""
        raw = config.section("ocr")
        if raw is None:
            return OcrConfig()
        try:
            return OcrConfig.model_validate(raw)
        except ValidationError:
            self._logger.exception("配置节 [ocr] 无法解析，沿用当前值")
            return self._config


def _payload(image: RawImage) -> bytes:
    """像素帧：恰好 stride × height 字节。截图数据通常正好这么长，这时不复制。"""
    size = image.stride * image.height
    return image.data if len(image.data) == size else image.data[:size]


async def _close(replies: AsyncIterator[WorkerReply]) -> None:
    """关闭回复流，让它的 finally 及时执行。普通异步迭代器没有 aclose，跳过；关闭时的异常不影响识别结果。"""
    if isinstance(replies, AsyncGenerator):
        with contextlib.suppress(Exception):
            await replies.aclose()

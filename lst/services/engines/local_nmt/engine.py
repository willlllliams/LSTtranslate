"""本地 NMT 翻译引擎（架构文档 2.5、2.7.3、6.5、8.3 节）。

引擎本身不做推理。它把调度器交来的一个任务转成一条 TranslateRequest，经 WorkerChannel 端口交给
监管器，由监管器转发给 NMT 进程；NMT 进程每译完一段发回一条 SegmentResult，引擎把它转成
SegmentOutcome 立即产出。语言对是否经英语中转由 NMT 进程决定，引擎只按能力清单回答 supports。

日志里只出现实例号、请求号、会话号、段号、段数、字符数和错误码，不出现原文和译文（PRV-02）。
"""

import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterator, Mapping, Sequence

from pydantic import Field

from lst.contracts.errors import ErrorCode
from lst.contracts.ids import Lang, SegmentId
from lst.contracts.ports import WorkerChannel, WorkerRequestHandle
from lst.contracts.translation import Engine, EngineInfo, EngineKind, Job, Segment, SegmentOutcome
from lst.contracts.worker_protocol import (
    CANCELLED,
    JsonValue,
    RequestDone,
    RequestFailed,
    SegmentResult,
    Started,
    TranslateRequest,
    TranslateSegment,
    WorkerReply,
)
from lst.services.engines.common import EngineSettings, build_info
from lst.services.engines.local_nmt.capabilities import CapabilityError, parse_capabilities, read_fingerprint

logger = logging.getLogger(__name__)

# 引擎侧影响译文的处理方式变化时加一，让旧译文不再命中缓存。
_FINGERPRINT_VERSION = 1

ERROR_CANCELLED = "请求被取消"
ERROR_MALFORMED = "NMT 进程返回的段结果格式不对"
ERROR_SUBMIT = "提交 NMT 请求失败"


class LocalNmtSettings(EngineSettings):
    """本地 NMT 实例的配置。

    两个时限的计时范围不同：

    - timeout_s 是调度器对整个任务的总时限（EngineInfo.timeout_s），从任务开始执行算起，
      包括请求在监管器和 NMT 进程里排队的时间。NMT 进程只有一个推理线程，被取消的批次仍会跑完
      （架构文档 6.5 节第 4 条），新请求最坏要先等它跑完，再算自己的处理时间，所以默认取处理时限的两倍。
    - processing_timeout_s 是交给 WorkerChannel.submit 的处理时限，从 NMT 进程发出 Started 时计时，
      超时后监管器结束并重启 NMT 进程（架构文档 8.2 节第 3 条）。

    max_retries 固定为 0（架构文档 2.7.3 节）：失败只有两种来源，模型缺失这类确定性错误重试结果相同，
    进程崩溃由监管器负责重启。限定取值范围，配置文件里就不会出现“重试 1 次”却不生效的设置。

    max_batch_segments 与 max_batch_chars 是一个请求的段数与字符数上限。批越大处理时间越长，
    调大时要保证一批能在 processing_timeout_s 内跑完，经英语中转的语言对要跑两跳。
    默认 800 字依据实测：CPU int8 下中文约 0.75 毫秒/字，800 字直连约 0.6 秒、中转约 1.3 秒，
    在 5 秒处理时限内留有约 4 倍余量；批小一些，第一批译文也更早出现。
    """

    timeout_s: float = Field(default=10.0, gt=0, le=600)
    processing_timeout_s: float = Field(default=5.0, gt=0, le=600)
    max_concurrency: int = Field(default=1, ge=1, le=32)
    max_batch_segments: int = Field(default=16, ge=1, le=200)
    max_batch_chars: int = Field(default=800, ge=1, le=100_000)
    max_retries: int = Field(default=0, ge=0, le=0)


class _Pending:
    """一个任务中还没有结果的段，按任务中的顺序排列。"""

    def __init__(self, segments: Sequence[Segment]) -> None:
        self._waiting: dict[str, SegmentId] = {segment.id: segment.id for segment in segments}
        self._done: set[str] = set()

    def __len__(self) -> int:
        return len(self._waiting)

    def take(self, segment_id: str) -> SegmentId | None:
        """取出一个等待中的段；不属于本任务或已经有结果时返回 None。"""
        found = self._waiting.pop(segment_id, None)
        if found is not None:
            self._done.add(segment_id)
        return found

    def is_done(self, segment_id: str) -> bool:
        return segment_id in self._done

    def fail_rest(self, code: ErrorCode, detail: str) -> list[SegmentOutcome]:
        """把剩下的段全部记为失败。"""
        outcomes = [
            SegmentOutcome(segment_id, error=detail, code=code) for segment_id in self._waiting.values()
        ]
        self._done.update(self._waiting)
        self._waiting.clear()
        return outcomes


class LocalNmtEngine:
    """本地 NMT 引擎实例。

    supports 每次都读取通道上能力清单的当前值，NMT 进程重载模型后结果随之改变。模型指纹只在创建时
    读取一次：指纹变化后由装配方重建实例，EngineInfo 随之改变，调度器据此替换实例（架构文档 6.5 节）。
    """

    def __init__(self, settings: LocalNmtSettings, channel: WorkerChannel) -> None:
        self._channel = channel
        self._processing_timeout_s = settings.processing_timeout_s
        # 最近一次警告过的格式不对的能力清单；同一份清单只警告一次。
        self._warned_capabilities: dict[str, JsonValue] | None = None
        worker_fingerprint = _worker_fingerprint(channel.capabilities, settings.id)
        self._info = build_info(
            settings,
            kind=EngineKind.LOCAL_NMT,
            billing=False,
            max_batch_segments=settings.max_batch_segments,
            max_batch_chars=settings.max_batch_chars,
            fingerprint_parts=("local_nmt", _FINGERPRINT_VERSION, worker_fingerprint),
            secret_value=None,
        )

    @property
    def info(self) -> EngineInfo:
        return self._info

    def supports(self, source: Lang, target: Lang) -> bool:
        """按 NMT 进程当前的能力清单判断，规则见 NmtCapabilities.supports。

        NMT 进程从未就绪（能力清单为 None）或能力清单格式不对时返回 False。
        """
        if source == target:
            return False
        raw = self._channel.capabilities
        if raw is None:
            return False
        try:
            capabilities = parse_capabilities(raw)
        except CapabilityError as exc:
            self._warn_capabilities(raw, exc)
            return False
        return capabilities.supports(source, target)

    async def translate(self, job: Job) -> AsyncGenerator[SegmentOutcome]:
        """把任务作为一个请求交给 NMT 进程，每收到一段的结果就产出一次。

        - 回复流以 RequestDone 正常结束；RequestFailed 让还没有结果的段全部失败。
        - 回复流结束时仍有段没有结果，只记警告，调度器会把这些段记为“引擎未返回该段”（架构文档 6.4 节）。
        - 调度器取消任务（包括总时限到达）或提前关闭生成器时，先取消请求，再让 CancelledError 照常上抛
          （架构文档 6.5 节第 4 条）。请求已经结束时不取消。
        """
        if not job.segments:
            return
        request = TranslateRequest(
            request_id=uuid.uuid4().hex,
            session_id=job.session_id,
            source=job.source_lang,
            target=job.target_lang,
            segments=tuple(TranslateSegment(segment.id, segment.text) for segment in job.segments),
        )
        pending = _Pending(job.segments)
        logger.debug(
            "本地 NMT 实例 %s 提交请求 %s：会话 %s，%s→%s，%d 段，%d 字符",
            self._info.id,
            request.request_id,
            job.session_id,
            job.source_lang,
            job.target_lang,
            len(job.segments),
            sum(len(segment.text) for segment in job.segments),
        )
        handle = self._submit(request)
        if handle is None:
            for outcome in pending.fail_rest(ErrorCode.INTERNAL, ERROR_SUBMIT):
                yield outcome
            return

        # settled 为真表示请求在 NMT 进程一侧已经结束，或者已经主动放弃；为假时退出要取消请求。
        settled = False
        replies: AsyncIterator[WorkerReply] | None = None
        try:
            replies = handle.replies()
            while not settled:
                try:
                    reply = await anext(replies)
                except StopAsyncIteration:
                    settled = True
                    if pending:
                        logger.warning(
                            "本地 NMT 实例 %s 的请求 %s 回复流提前结束，%d 段没有结果",
                            self._info.id,
                            request.request_id,
                            len(pending),
                        )
                    break
                except Exception as exc:
                    # 回复流本身出错：放弃这个请求，剩下的段记为失败。
                    settled = True
                    self._cancel(handle, request)
                    logger.warning(
                        "本地 NMT 实例 %s 读取请求 %s 的回复出错（%s），%d 段记为失败",
                        self._info.id,
                        request.request_id,
                        type(exc).__name__,
                        len(pending),
                    )
                    detail = f"读取 NMT 进程回复出错（{type(exc).__name__}）"
                    outcomes = pending.fail_rest(ErrorCode.INTERNAL, detail)
                else:
                    outcomes, settled = self._accept(reply, request, pending)
                for outcome in outcomes:
                    yield outcome
        finally:
            if not settled:
                self._cancel(handle, request)
            if replies is not None:
                await _close_replies(replies)

    # ---- 请求与回复 ----

    def _submit(self, request: TranslateRequest) -> WorkerRequestHandle | None:
        """提交请求；监管器抛出异常时返回 None。

        异常信息可能带有请求的内容（TranslateRequest 的 repr 含原文），日志只记异常类型。
        """
        try:
            return self._channel.submit(request, timeout_s=self._processing_timeout_s)
        except Exception as exc:
            logger.warning(
                "本地 NMT 实例 %s 提交请求 %s 失败（%s）",
                self._info.id,
                request.request_id,
                type(exc).__name__,
            )
            return None

    def _accept(
        self, reply: WorkerReply, request: TranslateRequest, pending: _Pending
    ) -> tuple[list[SegmentOutcome], bool]:
        """处理一条回复，返回要产出的结果，以及请求是否已经结束。"""
        if reply.request_id != request.request_id:
            logger.warning(
                "本地 NMT 实例 %s 的请求 %s 收到属于请求 %s 的回复，已忽略",
                self._info.id,
                request.request_id,
                reply.request_id,
            )
            return [], False
        if isinstance(reply, Started):
            logger.debug("NMT 进程开始处理请求 %s", request.request_id)
            return [], False
        if isinstance(reply, SegmentResult):
            outcome = self._convert(reply, pending)
            return ([] if outcome is None else [outcome]), False
        if isinstance(reply, RequestDone):
            if pending:
                logger.warning(
                    "本地 NMT 实例 %s 的请求 %s 已结束，%d 段没有结果",
                    self._info.id,
                    request.request_id,
                    len(pending),
                )
            else:
                logger.debug("请求 %s 完成", request.request_id)
            return [], True
        if isinstance(reply, RequestFailed):
            return self._fail_request(reply, pending), True
        logger.warning(
            "本地 NMT 实例 %s 的请求 %s 收到不属于翻译请求的回复 %s，已忽略",
            self._info.id,
            request.request_id,
            reply.TYPE,
        )
        return [], False

    def _convert(self, result: SegmentResult, pending: _Pending) -> SegmentOutcome | None:
        """把一段的结果转成 SegmentOutcome；段号不属于本任务或重复时返回 None。"""
        segment_id = pending.take(result.segment_id)
        if segment_id is None:
            what = "重复的" if pending.is_done(result.segment_id) else "不属于本任务的"
            logger.warning(
                "本地 NMT 实例 %s 的请求 %s 返回了%s段 %s，已忽略",
                self._info.id,
                result.request_id,
                what,
                result.segment_id,
            )
            return None
        text, error = result.text, result.error
        if (text is None) == (error is None):
            logger.warning(
                "本地 NMT 实例 %s 的请求 %s 中段 %s 的结果%s，按内部错误处理",
                self._info.id,
                result.request_id,
                segment_id,
                "既没有译文也没有错误" if text is None else "同时带有译文和错误",
            )
            return SegmentOutcome(segment_id, error=ERROR_MALFORMED, code=ErrorCode.INTERNAL)
        if text is not None:
            return SegmentOutcome(segment_id, text=text)
        code, detail = self._error_code(result.code, error or "NMT 进程没有给出错误信息")
        logger.debug("请求 %s 中段 %s 失败（%s）", result.request_id, segment_id, code.value)
        return SegmentOutcome(segment_id, error=detail, code=code)

    def _fail_request(self, failed: RequestFailed, pending: _Pending) -> list[SegmentOutcome]:
        """请求整体失败：还没有结果的段全部失败。被取消的请求按内部错误处理，并在错误信息里写明。"""
        if failed.code == CANCELLED:
            code = ErrorCode.INTERNAL
            detail = f"{ERROR_CANCELLED}（{failed.error}）" if failed.error else ERROR_CANCELLED
        else:
            code, detail = self._error_code(failed.code, failed.error or "NMT 请求失败")
        logger.info(
            "本地 NMT 实例 %s 的请求 %s 失败（%s：%s），%d 段记为失败",
            self._info.id,
            failed.request_id,
            failed.code,
            failed.error,
            len(pending),
        )
        return pending.fail_rest(code, detail)

    def _error_code(self, raw: str | None, detail: str) -> tuple[ErrorCode, str]:
        """把协议里的错误码字符串转成 ErrorCode。缺失或未知的取值按 INTERNAL 处理，未知取值写进错误信息。"""
        if raw is None:
            return ErrorCode.INTERNAL, detail
        try:
            return ErrorCode(raw), detail
        except ValueError:
            logger.warning("本地 NMT 实例 %s 收到未知错误码 %r，按内部错误处理", self._info.id, raw)
            return ErrorCode.INTERNAL, f"{detail}（未知错误码 {raw}）"

    def _cancel(self, handle: WorkerRequestHandle, request: TranslateRequest) -> None:
        """取消请求。监管器抛出的异常只记日志，不能盖过正在上抛的 CancelledError。

        与提交失败一样，日志只记异常类型，不记异常信息。
        """
        try:
            handle.cancel()
        except Exception as exc:
            logger.warning(
                "本地 NMT 实例 %s 取消请求 %s 时出错（%s）",
                self._info.id,
                request.request_id,
                type(exc).__name__,
            )
        else:
            logger.debug("本地 NMT 实例 %s 已取消请求 %s", self._info.id, request.request_id)

    # ---- 能力清单 ----

    def _warn_capabilities(self, raw: Mapping[str, JsonValue], exc: CapabilityError) -> None:
        snapshot = dict(raw)
        if snapshot == self._warned_capabilities:
            return
        self._warned_capabilities = snapshot
        logger.warning("本地 NMT 实例 %s：NMT 进程的能力清单格式不对（%s），不参加竞速", self._info.id, exc)


def _worker_fingerprint(capabilities: Mapping[str, JsonValue] | None, engine_id: str) -> str:
    """NMT 进程报告的模型指纹。从未就绪时为空串；格式不对时记警告，按空串处理。"""
    if capabilities is None:
        return ""
    try:
        return read_fingerprint(capabilities)
    except CapabilityError as exc:
        logger.warning(
            "本地 NMT 实例 %s：NMT 进程的能力清单格式不对（%s），模型指纹按空串处理", engine_id, exc
        )
        return ""


async def _close_replies(replies: AsyncIterator[WorkerReply]) -> None:
    """关闭回复流，让它的 finally 及时执行。普通异步迭代器没有 aclose，跳过。"""
    if isinstance(replies, AsyncGenerator):
        try:
            await replies.aclose()
        except Exception as exc:
            logger.warning("关闭 NMT 回复流时出错（%s）", type(exc).__name__)


def create_local_nmt_engine(settings: LocalNmtSettings, channel: WorkerChannel) -> Engine:
    """创建本地 NMT 引擎实例。channel 是 NMT 进程的监管器，由组合根注入（架构文档 4.4 节）。"""
    return LocalNmtEngine(settings, channel)

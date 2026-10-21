"""把 NMT 后端接到推理进程运行时上：每段发一条 SegmentResult，最后发 RequestDone。"""

import logging
from collections.abc import Mapping

from lst.contracts.errors import ErrorCode
from lst.contracts.worker_protocol import JsonValue, RequestDone, SegmentResult, TranslateRequest, WorkerReply
from lst.workers._runtime import Emit, LoadResult, ProgressReporter
from lst.workers.nmt_worker.backend import NmtBackend

log = logging.getLogger(__name__)


class TranslateHandler:
    """TranslateRequest 的处理器，实现 _runtime.RequestHandler[TranslateRequest]。

    它保证协议要求的不变式，不依赖后端是否守规矩：请求里的每个段恰好收到一条 SegmentResult。
    后端漏掉的段、后端中途抛出异常后剩下的段，都补发 engine.internal 错误；未知段号和重复段号丢弃。
    """

    def __init__(self, backend: NmtBackend) -> None:
        self._backend = backend

    @property
    def request_type(self) -> type[TranslateRequest]:
        return TranslateRequest

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult:
        return self._backend.load(settings, progress)

    def handle(self, request: TranslateRequest, payload: bytes | None, emit: Emit) -> WorkerReply:
        rid = request.request_id
        pending = {segment.id for segment in request.segments}
        missing_reason = "后端没有返回该段"
        log.debug("请求 %s：%s→%s，%d 段", rid, request.source, request.target, len(request.segments))
        try:
            for outcome in self._backend.translate(request.source, request.target, request.segments):
                if outcome.segment_id not in pending:
                    log.warning("请求 %s：后端返回了未知或重复的段 %s，已丢弃", rid, outcome.segment_id)
                    continue
                pending.discard(outcome.segment_id)
                if outcome.text is not None and outcome.error is None:
                    emit(SegmentResult(rid, outcome.segment_id, text=outcome.text))
                else:
                    code = outcome.code or ErrorCode.INTERNAL.value
                    emit(SegmentResult(rid, outcome.segment_id, error=outcome.error or "未知错误", code=code))
        except Exception as exc:
            log.exception("请求 %s：后端抛出异常，剩余 %d 段记为失败", rid, len(pending))
            missing_reason = f"{type(exc).__name__}: {exc}"
        for segment in request.segments:
            if segment.id in pending:
                pending.discard(segment.id)
                emit(SegmentResult(rid, segment.id, error=missing_reason, code=ErrorCode.INTERNAL.value))
        return RequestDone(rid)

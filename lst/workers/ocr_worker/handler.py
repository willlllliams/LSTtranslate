"""把 OCR 后端接到推理进程运行时上：每个 OcrRequest 以 OcrResult 或 RequestFailed 结束。

错误码：

- 像素帧与宽、高、跨度不符，或 options 取值无效 → engine.bad_request；
- 请求用到的子能力缺模型 → engine.model_missing；
- 推理过程中的其他异常 → engine.internal。

日志只记请求号、图像尺寸、块数和耗时，不记识别出的文字（需求 PRV-02）。
"""

import logging
import time
from collections.abc import Mapping

from lst.contracts.errors import ErrorCode
from lst.contracts.worker_protocol import JsonValue, OcrRequest, OcrResult, RequestFailed, WorkerReply
from lst.workers._runtime import Emit, LoadResult, ProgressReporter
from lst.workers.ocr_worker.backend import ModelMissing, OcrBackend
from lst.workers.ocr_worker.image import BadImage, bgra_to_bgr
from lst.workers.ocr_worker.settings import BadOptions

log = logging.getLogger(__name__)


class OcrHandler:
    """OcrRequest 的处理器，实现 _runtime.RequestHandler[OcrRequest]。"""

    def __init__(self, backend: OcrBackend) -> None:
        self._backend = backend

    @property
    def request_type(self) -> type[OcrRequest]:
        return OcrRequest

    def load(self, settings: Mapping[str, JsonValue], progress: ProgressReporter) -> LoadResult:
        return self._backend.load(settings, progress)

    def handle(self, request: OcrRequest, payload: bytes | None, emit: Emit) -> WorkerReply:
        rid = request.request_id
        if payload is None:
            return RequestFailed(rid, ErrorCode.BAD_REQUEST, "OCR 请求缺少像素帧")
        try:
            image = bgra_to_bgr(payload, request.width, request.height, request.stride)
        except BadImage as exc:
            log.warning("请求 %s：像素帧无效：%s", rid, exc)
            return RequestFailed(rid, ErrorCode.BAD_REQUEST, str(exc))
        started = time.perf_counter()
        try:
            blocks = self._backend.recognize(image, request.options)
        except BadOptions as exc:
            log.warning("请求 %s：options 无效：%s", rid, exc)
            return RequestFailed(rid, ErrorCode.BAD_REQUEST, str(exc))
        except ModelMissing as exc:
            log.error("请求 %s：%s", rid, exc)
            return RequestFailed(rid, ErrorCode.MODEL_MISSING, str(exc))
        except Exception as exc:
            log.exception("请求 %s：识别失败", rid)
            return RequestFailed(rid, ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}")
        log.info(
            "请求 %s：%d×%d，%d 个版面块，耗时 %.2f 秒",
            rid,
            request.width,
            request.height,
            len(blocks),
            time.perf_counter() - started,
        )
        return OcrResult(rid, blocks)

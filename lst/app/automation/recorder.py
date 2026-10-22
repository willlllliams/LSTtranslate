"""截屏会话的记录：订阅总线上的截屏、识别与竞速消息，供 wait_result 返回（架构文档 16.4 节）。

记录只在自动化接口开启期间进行，只保留最近 MAX_SESSIONS 个会话，程序退出即丢弃，不写盘。
识别与翻译结果从总线消息里取（CaptureCompleted、OcrCompleted、OcrFailed、SegmentsUpdated、SessionProgress），
不读 OCR 模块与竞速模块的内部状态。

一个会话“已定”的条件：

- 识别失败（OcrFailed）；
- 识别完成且没有段；
- 识别完成，并且每个段都收到过 finished 为真的快照，或者 SessionProgress 报告全部段已定。
"""

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Final

from lst.automation.protocol import Json
from lst.contracts.events import SegmentsUpdated, SessionProgress
from lst.contracts.ids import SegmentId, SessionId
from lst.contracts.ocr import BBox, CaptureCompleted, OcrCompleted, OcrFailed, OcrStarted
from lst.contracts.translation import AttemptView, SegmentView

MAX_SESSIONS: Final = 8


@dataclass(eq=False, slots=True)
class SessionRecord:
    session_id: SessionId
    started: float
    capture: CaptureCompleted | None = None
    ocr: OcrCompleted | None = None
    failure: OcrFailed | None = None
    ocr_at: float | None = None
    views: dict[SegmentId, SegmentView] = field(default_factory=lambda: dict[SegmentId, SegmentView]())
    progress: tuple[int, int] | None = None
    settled_at: float | None = None
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def settled(self) -> bool:
        if self.failure is not None:
            return True
        if self.ocr is None:
            return False
        segments = self.ocr.segments
        if not segments:
            return True
        if all((view := self.views.get(s.segment.id)) is not None and view.finished for s in segments):
            return True
        return (
            self.progress is not None
            and self.progress[1] == len(segments)
            and self.progress[0] >= len(segments)
        )

    @property
    def phase(self) -> str:
        if self.failure is not None:
            return "failed"
        if self.ocr is None:
            return "recognizing"
        return "done" if self.settled else "translating"

    def touch(self) -> None:
        if self.settled_at is None and self.settled:
            self.settled_at = time.monotonic()
        self.changed.set()

    def begin(self) -> None:
        """同一会话重新识别，保留截图，清除上一次尝试的结果。"""
        self.started = time.monotonic()
        self.ocr = None
        self.failure = None
        self.ocr_at = None
        self.views.clear()
        self.progress = None
        self.settled_at = None
        self.changed.set()


class SessionRecorder:
    """按会话号保存最近几次截屏的结果。enabled 为假时不记录。"""

    def __init__(self) -> None:
        self.enabled = False
        self._sessions: OrderedDict[SessionId, SessionRecord] = OrderedDict()

    def clear(self) -> None:
        self._sessions.clear()

    def get(self, session_id: SessionId) -> SessionRecord | None:
        return self._sessions.get(session_id)

    def ensure(self, session_id: SessionId) -> SessionRecord:
        """取会话的记录；还没有时新建一条（自动化接口发起截屏时先建好，免得漏掉早到的消息）。"""
        record = self._sessions.get(session_id)
        if record is None:
            record = SessionRecord(session_id, time.monotonic())
            self._sessions[session_id] = record
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
        return record

    def _record(self, session_id: SessionId) -> SessionRecord | None:
        return self.ensure(session_id) if self.enabled else None

    def on_capture(self, event: CaptureCompleted) -> None:
        record = self._record(event.session_id)
        if record is not None:
            record.capture = event
            record.touch()

    def on_ocr_completed(self, event: OcrCompleted) -> None:
        record = self._record(event.session_id)
        if record is not None:
            if record.failure is not None:
                record.begin()
            record.ocr, record.ocr_at = event, time.monotonic()
            record.touch()

    def on_ocr_started(self, event: OcrStarted) -> None:
        record = self._record(event.session_id)
        if record is not None:
            record.begin()

    def on_ocr_failed(self, event: OcrFailed) -> None:
        record = self._record(event.session_id)
        if record is not None:
            record.failure, record.ocr_at = event, time.monotonic()
            record.touch()

    def on_segments(self, event: SegmentsUpdated) -> None:
        record = self._record(event.session_id)
        if record is not None:
            for view in event.views:
                record.views[view.segment_id] = view
            record.touch()

    def on_progress(self, event: SessionProgress) -> None:
        record = self._record(event.session_id)
        if record is not None:
            record.progress = (event.finished, event.total)
            record.touch()


def _bbox(box: BBox) -> list[Json]:
    return [box.x0, box.y0, box.x1, box.y1]


def _attempt(attempt: AttemptView) -> dict[str, Json]:
    return {
        "engine": attempt.engine_id,
        "state": attempt.state.name.lower(),
        "text": attempt.text,
        "error": attempt.error,
        "error_code": attempt.error_code.value if attempt.error_code is not None else None,
        "latency_ms": None if attempt.latency_ms is None else round(attempt.latency_ms, 1),
        "cached": attempt.cached,
        "started": attempt.started,
    }


def _ms(start: float, end: float | None) -> Json:
    return None if end is None else round((end - start) * 1000, 1)


def describe_record(record: SessionRecord) -> dict[str, Json]:
    """一个会话的识别与翻译结果，写成 JSON。"""
    capture = record.capture
    data: dict[str, Json] = {
        "session_id": record.session_id,
        "phase": record.phase,
        "settled": record.settled,
        "ocr_ms": _ms(record.started, record.ocr_at),
        "settled_ms": _ms(record.started, record.settled_at),
        "capture": None
        if capture is None
        else {
            "width": capture.image.width,
            "height": capture.image.height,
            "screen": capture.geometry.screen_name,
            "selection": list(capture.geometry.selection),
            "dpr": capture.geometry.dpr,
        },
        "progress": None if record.progress is None else list(record.progress),
        "error": None
        if record.failure is None
        else {"code": record.failure.code.value, "message": record.failure.error},
    }
    ocr = record.ocr
    if ocr is None:
        data["blocks"] = []
        data["segments"] = []
        return data
    data["blocks"] = [
        {
            "id": block.id,
            "category": block.category.value,
            "bbox": _bbox(block.bbox),
            "order": block.order,
            "score": round(block.score, 3),
            "text": block.text,
            "lines": len(block.lines),
        }
        for block in ocr.blocks
    ]
    segments: list[Json] = []
    for index, item in enumerate(ocr.segments):
        view = record.views.get(item.segment.id)
        segments.append(
            {
                "index": index,
                "id": item.segment.id,
                "block_id": item.block_id,
                "bbox": _bbox(item.bbox),
                "cell": None if item.cell is None else list(item.cell),
                "source": item.segment.text,
                "display": view.display_text if view is not None else item.segment.text,
                "shown_engine": view.shown_engine if view is not None else None,
                "finished": view.finished if view is not None else False,
                "skip_reason": view.skip_reason.value if view is not None and view.skip_reason else None,
                "attempts": [_attempt(a) for a in view.attempts] if view is not None else [],
            }
        )
    data["segments"] = segments
    return data


__all__ = ["MAX_SESSIONS", "SessionRecord", "SessionRecorder", "describe_record"]

"""覆盖窗口的展示器（需求 OV-01 至 OV-07、CAP-06、OCR-07、OCR-08，架构文档第 7 章与 11.1 节）。

展示器订阅总线消息，维护“当前会话”，把消息转换成对覆盖窗口的调用；窗口里的“重试”和复制由它转换成
RetryOcr 命令和剪贴板写入。

会话规则（CAP-06）：最近一次 CaptureCompleted 的会话号是当前会话。新截屏到达时关闭上一个窗口，
之后属于其他会话的 OcrCompleted、OcrFailed、SegmentsUpdated、SessionProgress 一律丢弃。
用户关掉窗口后，当前会话的后续消息也没有窗口可以显示，同样丢弃。

窗口的阶段：

- 识别中：CaptureCompleted 之后。OCR 进程处于启动或加载状态（WorkerStatuses 中 ocr 为 starting、loading）时
  显示“识别模型加载中”，就绪后改回“识别中”（OCR-07）；
- 识别失败：OcrFailed，显示原因、建议与“重试”；点“重试”发 RetryOcr，回到识别中（OCR-08）；
- 结果：OcrCompleted，先按段显示原文；之后的 SegmentsUpdated 按段增量更新，SessionProgress 更新进度。
  竞速模块的第一次刷新与 OcrCompleted 在同一轮之后才可能到达，但为了不依赖这个顺序，
  在 OcrCompleted 之前到达的同一会话的快照先暂存，显示结果时一并应用。

日志只写会话号与段数，不写原文与译文（需求 PRV-02）。
"""

import logging
from collections.abc import Callable
from typing import Final

from lst.contracts.config import AppConfig
from lst.contracts.events import SegmentsUpdated, SessionProgress, WorkerStatuses
from lst.contracts.ids import SessionId
from lst.contracts.ocr import CaptureCompleted, OcrCompleted, OcrFailed, RetryOcr
from lst.contracts.ports import WorkerState
from lst.contracts.translation import SegmentView
from lst.kernel import EventBus
from lst.ui.common.bindings import Keymap, Scope, bindings_from
from lst.ui.i18n import apply_config_language, error_text
from lst.ui.overlay.window import OverlayWindow, Phase

OCR_WORKER: Final = "ocr"
_LOADING_STATES: Final = frozenset({WorkerState.STARTING, WorkerState.LOADING})

type WindowFactory = Callable[[CaptureCompleted, "OverlayPresenter"], OverlayWindow]
"""创建覆盖窗口的函数。缺省直接构造 OverlayWindow；模块用它在显示前加上测试需要的窗口属性。"""


class OverlayPresenter:
    """覆盖窗口的展示器，同时实现 OverlayActions。"""

    def __init__(
        self,
        bus: EventBus,
        *,
        clipboard: Callable[[str], None],
        window_factory: WindowFactory | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._bus = bus
        self._clipboard = clipboard
        self._factory: WindowFactory = window_factory or (
            lambda event, actions: OverlayWindow(event.image, event.geometry, actions)
        )
        self._logger = logger or logging.getLogger(__name__)
        self._session: SessionId | None = None
        self._window: OverlayWindow | None = None
        self._pending: dict[str, SegmentView] = {}
        self._pending_progress: tuple[int, int] | None = None
        self._ocr_state: WorkerState | None = None
        self._keymap = Keymap.defaults(Scope.OVERLAY)

    # ---- 查询 ----

    @property
    def session_id(self) -> SessionId | None:
        return self._session

    @property
    def window(self) -> OverlayWindow | None:
        return self._window

    @property
    def keymap(self) -> Keymap:
        return self._keymap

    # ---- 总线消息 ----

    def on_capture(self, event: CaptureCompleted) -> None:
        self._close_window()
        self._session = event.session_id
        self._pending.clear()
        self._pending_progress = None
        window = self._factory(event, self)
        self._window = window
        window.set_keymap(self._keymap)
        window.show_recognizing(self._loading())
        window.present()
        self._logger.debug("会话 %s 的覆盖窗口已弹出", event.session_id)

    def on_ocr_completed(self, event: OcrCompleted) -> None:
        window = self._current(event.session_id)
        if window is None:
            return
        window.show_result(event.blocks, event.segments)
        self._logger.debug("会话 %s 识别完成：%d 个段", event.session_id, len(event.segments))
        if self._pending:
            window.apply_views(tuple(self._pending.values()))
            self._pending.clear()
        if self._pending_progress is not None:
            window.set_progress(*self._pending_progress)
            self._pending_progress = None

    def on_ocr_failed(self, event: OcrFailed) -> None:
        window = self._current(event.session_id)
        if window is None:
            return
        reason, suggestion = error_text(event.code)
        window.show_failed(reason, suggestion)
        self._logger.info("会话 %s 识别失败（%s）", event.session_id, event.code)

    def on_segments_updated(self, event: SegmentsUpdated) -> None:
        window = self._current(event.session_id)
        if window is None:
            return
        if window.phase is not Phase.RESULT:
            for view in event.views:
                self._pending[view.segment_id] = view
            return
        window.apply_views(event.views)

    def on_progress(self, event: SessionProgress) -> None:
        window = self._current(event.session_id)
        if window is None:
            return
        if window.phase is not Phase.RESULT:
            self._pending_progress = (event.finished, event.total)
            return
        window.set_progress(event.finished, event.total)

    def on_worker_statuses(self, message: WorkerStatuses) -> None:
        status = message.statuses.get(OCR_WORKER)
        self._ocr_state = None if status is None else status.state
        window = self._window
        if window is not None and window.phase is Phase.RECOGNIZING:
            window.show_recognizing(self._loading())

    def on_config(self, config: AppConfig) -> None:
        apply_config_language(config)
        # 窗口内的键位（UI-09）：已经打开的窗口立即换用新键位，之后新开的窗口也用它。
        self._keymap = Keymap.for_scope(bindings_from(config.section("bindings")), Scope.OVERLAY)
        if self._window is not None:
            self._window.set_keymap(self._keymap)

    # ---- OverlayActions ----

    def retry(self) -> None:
        window, session = self._window, self._session
        if window is None or session is None or window.phase is not Phase.FAILED:
            return
        self._logger.info("重新识别会话 %s", session)
        window.show_recognizing(self._loading())
        self._bus.send(RetryOcr(session))

    def copy(self, text: str) -> None:
        self._clipboard(text)

    def window_closed(self, window: OverlayWindow) -> None:
        if window is self._window:
            self._window = None
            self._pending.clear()
            self._pending_progress = None
        window.deleteLater()

    # ---- 生命周期 ----

    def shutdown(self) -> None:
        """模块停止：关闭窗口。"""
        self._close_window()
        self._session = None

    # ---- 内部 ----

    def _current(self, session_id: SessionId) -> OverlayWindow | None:
        """属于当前会话且窗口还开着时返回窗口，否则返回 None（消息丢弃，CAP-06）。"""
        if session_id != self._session:
            return None
        return self._window

    def _loading(self) -> bool:
        return self._ocr_state in _LOADING_STATES

    def _close_window(self) -> None:
        window, self._window = self._window, None
        if window is not None:
            window.close()

"""双语窗口的展示器（架构文档 11.1、11.6 节）。

展示器订阅总线消息并转换成对视图的调用，把用户操作转换成 TranslateText 命令，本身不含绘制代码。
它和视图之间只通过下面两个协议交互，测试可以换上假视图。

与事件循环的配合方式：展示器不 await 任何东西。输入停顿用 Qt 的单次计时器判断；发命令用
``EventBus.send``，它只把分发排进事件循环就返回；订阅的处理函数都是普通函数。qasync 让 asyncio
运行在 Qt 主线程上，总线处理函数、Qt 信号槽和计时器回调因此都在主线程上依次执行，展示器的状态
只在主线程读写，不需要加锁。

日志只记录会话号、字符数和计数，不记录原文与译文（需求 PRV-02）。
"""

import logging
import uuid
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from PySide6.QtCore import QPoint, QSize, QTimer
from PySide6.QtGui import QColor

from lst.contracts.config import AppConfig
from lst.contracts.events import EngineStatuses, SegmentsUpdated, ToggleBilingualWindow, TranslateText
from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId
from lst.contracts.translation import AttemptState, AttemptView, SegmentView
from lst.kernel import EventBus
from lst.ui.bilingual.options import BilingualOptions, parse_options
from lst.ui.bilingual.state import WindowStateStore
from lst.ui.i18n import current_language, error_text, tr
from lst.ui.theme import engine_color

_STATE_KEYS: Mapping[AttemptState, str] = {
    AttemptState.QUEUED: "bilingual.state.queued",
    AttemptState.RUNNING: "bilingual.state.running",
    AttemptState.DONE: "bilingual.state.done",
    AttemptState.FAILED: "bilingual.state.failed",
    AttemptState.CANCELLED: "bilingual.state.cancelled",
}

# 一个实例在多个段上的尝试合成一行时，状态按这个顺序取第一个出现的：还有段在翻译就显示“翻译中”，
# 都结束后只要有一段成功就显示“完成”。
_STATE_PRECEDENCE = (
    AttemptState.RUNNING,
    AttemptState.QUEUED,
    AttemptState.DONE,
    AttemptState.FAILED,
    AttemptState.CANCELLED,
)


@dataclass(frozen=True, slots=True)
class SegmentItem:
    """译文区里一段的显示内容。color 为 None 时这一段旁边不画来源标记。"""

    text: str
    color: QColor | None


@dataclass(frozen=True, slots=True)
class EngineRow:
    """引擎结果列表的一行（需求 BI-05、RACE-06）。文字字段都已经本地化。

    status：状态；latency：耗时，缓存命中时为“缓存”，还没有结束的为空串；
    text：该实例各段译文按段落顺序拼接；error：失败原因与处理建议，没有失败的段时为空串。
    """

    engine_id: EngineId
    name: str
    color: QColor
    state: AttemptState
    status: str
    latency: str
    text: str
    error: str


class BilingualActions(Protocol):
    """视图把用户操作交给展示器的接口。"""

    def input_changed(self, text: str) -> None: ...
    def translate_now(self) -> None: ...
    def languages_changed(self, source: Lang | None, target: Lang | None) -> None: ...
    def swap_languages(self) -> None: ...
    def clear(self) -> None: ...
    def copy_translation(self) -> None: ...
    def copy_engine(self, engine_id: EngineId) -> None: ...
    def pin_toggled(self, pinned: bool) -> None: ...
    def hide_requested(self) -> None: ...


class BilingualView(Protocol):
    """展示器驱动视图的接口。段用序号定位，序号就是段在会话里的顺序。"""

    def bind(self, actions: BilingualActions) -> None: ...
    def retranslate(self) -> None: ...
    def input_text(self) -> str: ...
    def set_input_text(self, text: str) -> None:
        """替换输入框内容，不触发 input_changed。"""
        ...

    def focus_input(self) -> None: ...
    def set_languages(self, source: Lang | None, target: Lang | None) -> None:
        """设置两个语言下拉框，不触发 languages_changed。"""
        ...

    def set_pinned(self, pinned: bool) -> None: ...
    def set_horizontal(self, horizontal: bool) -> None: ...
    def set_markers_visible(self, visible: bool) -> None: ...
    def show_segments(self, items: Sequence[SegmentItem], fade: Collection[int]) -> None:
        """整体替换译文区；fade 中的段播放淡入。"""
        ...

    def update_segments(self, changes: Sequence[tuple[int, SegmentItem]], fade: Collection[int]) -> None:
        """只改动给出的段；序号等于当前段数时追加到末尾。"""
        ...

    def set_segment_colors(self, colors: Mapping[int, QColor | None]) -> None: ...
    def show_engine_rows(self, order: Sequence[EngineId], changed: Sequence[EngineRow]) -> None:
        """order 是全部行的显示顺序，changed 是内容有变化的行；不在 order 里的行被移除。"""
        ...

    def clear_engine_rows(self) -> None: ...
    def is_shown(self) -> bool: ...
    def restore_geometry(self, position: QPoint | None, size: QSize | None) -> None: ...
    def window_geometry(self) -> tuple[QPoint, QSize]:
        """外框左上角与客户区尺寸。"""
        ...

    def present(self) -> None:
        """显示并激活窗口，焦点放到输入框；位置不在任何屏幕上时先移到主屏中央。"""
        ...

    def conceal(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _Request:
    """最近一次发出的 TranslateText。"""

    session_id: SessionId
    text: str
    source: Lang | None
    target: Lang | None

    @property
    def key(self) -> tuple[str, Lang | None, Lang | None]:
        return (self.text, self.source, self.target)


@dataclass(slots=True)
class _Segment:
    """译文区一段的状态。shown_* 是当前显示的内容，可能与 view 不同，见 _next_shown。"""

    view: SegmentView
    shown_text: str
    shown_winner: EngineId | None
    placeholder: bool = False


@dataclass(slots=True)
class _Engine:
    """一个参赛实例在当前会话各段上的尝试。rank 用于胜出次数相同时排序，按首次出现的顺序分配。"""

    rank: int
    attempts: dict[int, AttemptView] = field(default_factory=dict[int, AttemptView])
    wins: int = 0


def _next_shown(segment: _Segment, view: SegmentView) -> tuple[str, EngineId | None, bool]:
    """一段收到新快照后应当显示什么：(文字, 来源实例, 是否仍是占位)。

    新会话开始时，原文没变的段先沿用上一个会话的译文作为占位（见 _show_new_session），
    直到这一段有了胜出者，或者竞速结束仍没有胜出者（这时按 RACE-04 显示原文）。
    """
    if view.winner is not None:
        return view.display_text, view.winner, False
    if segment.placeholder and not view.finished:
        return segment.shown_text, segment.shown_winner, True
    return view.display_text, None, False


class BilingualPresenter:
    """双语窗口的展示器。构造时读取上次保存的界面状态，并把自己绑定到视图上。"""

    def __init__(
        self,
        view: BilingualView,
        *,
        bus: EventBus,
        store: WindowStateStore,
        logger: logging.Logger,
        clipboard: Callable[[str], None],
    ) -> None:
        self._view = view
        self._bus = bus
        self._store = store
        self._logger = logger
        self._clipboard = clipboard
        self._options = BilingualOptions()
        self._timer = QTimer()
        self._timer.setSingleShot(True)
        self._timer.setInterval(self._options.debounce_ms)
        self._timer.timeout.connect(self._on_pause)
        self._last: _Request | None = None
        self._statuses: EngineStatuses | None = None
        self._language = current_language()

        # 译文区：当前显示的是哪个会话、它的语言，以及各段状态。
        self._shown_session: SessionId | None = None
        self._shown_langs: tuple[Lang | None, Lang | None] = (None, None)
        self._segments: list[_Segment] = []
        self._index: dict[SegmentId, int] = {}

        # 引擎结果列表。
        self._engines: dict[EngineId, _Engine] = {}
        self._rows: dict[EngineId, EngineRow] = {}
        self._order: list[EngineId] = []

        self._saved = store.load()
        self._geometry_restored = False
        self._closed = False
        self._source = self._saved.source
        self._target = self._saved.target
        self._pinned = self._saved.on_top
        view.set_languages(self._source, self._target)
        view.set_pinned(self._pinned)
        view.bind(self)

    # ------------------------------------------------------------------ 供组合根和测试读取的状态

    @property
    def options(self) -> BilingualOptions:
        return self._options

    @property
    def debounce_timer(self) -> QTimer:
        return self._timer

    @property
    def session_id(self) -> SessionId | None:
        """最近一次发出的会话号。"""
        return None if self._last is None else self._last.session_id

    @property
    def languages(self) -> tuple[Lang | None, Lang | None]:
        return (self._source, self._target)

    def translation_text(self) -> str:
        """译文区当前显示的全部文字，段与段之间用换行分隔。"""
        return "\n".join(segment.shown_text for segment in self._segments)

    def engine_rows(self) -> list[EngineRow]:
        """引擎结果列表当前的各行，按显示顺序。"""
        return [self._rows[engine_id] for engine_id in self._order if engine_id in self._rows]

    # ------------------------------------------------------------------ 总线消息

    def on_toggle(self, _: ToggleBilingualWindow) -> None:
        if self._closed:
            # 模块已停止、订阅还没取消的间隙里到达的命令，不能把正在关闭的窗口重新显示出来。
            return
        if self._view.is_shown():
            self._hide()
        else:
            self._show()

    def on_config(self, config: AppConfig) -> None:
        options = parse_options(config)
        self._options = options
        # 正在计时的停顿保持原来的时长，新时长从下一次输入开始生效。
        self._timer.setInterval(options.debounce_ms)
        self._view.set_horizontal(options.layout == "horizontal")
        self._view.set_markers_visible(options.show_source_marker)
        self._refresh_language()

    def on_engine_statuses(self, statuses: EngineStatuses) -> None:
        self._statuses = statuses
        self._push_rows(self._engines)
        colors = {
            index: self._color(segment.shown_winner)
            for index, segment in enumerate(self._segments)
            if segment.shown_winner is not None
        }
        if colors:
            self._view.set_segment_colors(colors)

    def on_segments_updated(self, event: SegmentsUpdated) -> None:
        if self._last is None or event.session_id != self._last.session_id:
            # 截屏会话、已被取代的双语会话都在这里丢弃。
            self._logger.debug("丢弃不属于当前会话的 SegmentsUpdated：会话 %s", event.session_id)
            return
        if event.session_id != self._shown_session:
            self._show_new_session(event.session_id, event.views)
        else:
            self._apply(event.views)

    # ------------------------------------------------------------------ 用户操作（BilingualActions）

    def input_changed(self, text: str) -> None:
        if not text.strip():
            # 清空立即生效，不等停顿：竞速模块收到空文本后取消当前会话。
            self._timer.stop()
            self._clear_display()
            if self._last is not None and self._last.text:
                self._send(self._new_session(), "", billing=False)
            return
        self._timer.start(self._options.debounce_ms)

    def translate_now(self) -> None:
        """Ctrl+Enter：立即翻译，并让计费实例加入（需求 BI-04，架构文档 11.6 节）。"""
        self._timer.stop()
        text = self._input()
        if not text:
            return
        # 文本与语言都没变时沿用当前会话号，竞速模块据此让计费实例加入当前会话（6.3 节“晚加入”）。
        same = self._last is not None and self._last.key == (text, self._source, self._target)
        session_id = self._last.session_id if same and self._last is not None else self._new_session()
        self._send(session_id, text, billing=True)

    def languages_changed(self, source: Lang | None, target: Lang | None) -> None:
        if (source, target) == (self._source, self._target):
            return
        self._source, self._target = source, target
        self._store.save_languages(source, target)
        self._resend_now()

    def swap_languages(self) -> None:
        """交换源语言与目标语言。两边都是“自动”时没有可交换的内容，什么也不做。"""
        if self._source is None and self._target is None:
            return
        source, target = self._target, self._source
        self._view.set_languages(source, target)
        self.languages_changed(source, target)

    def clear(self) -> None:
        self._view.set_input_text("")
        self.input_changed("")
        self._view.focus_input()

    def copy_translation(self) -> None:
        text = self.translation_text()
        if text:
            self._clipboard(text)

    def copy_engine(self, engine_id: EngineId) -> None:
        row = self._rows.get(engine_id)
        if row is not None and row.text:
            self._clipboard(row.text)

    def pin_toggled(self, pinned: bool) -> None:
        self._pinned = pinned
        self._view.set_pinned(pinned)
        self._store.save_on_top(pinned)

    def hide_requested(self) -> None:
        if self._view.is_shown():
            self._hide()

    # ------------------------------------------------------------------ 生命周期

    def shutdown(self) -> None:
        """模块停止时调用：停止计时器，保存窗口位置。本次运行从未显示过窗口时不覆盖上次保存的位置。"""
        self._closed = True
        self._timer.stop()
        if self._geometry_restored:
            self._store.save_geometry(*self._view.window_geometry())

    # ------------------------------------------------------------------ 发送

    def _input(self) -> str:
        """输入框的文字，去掉首尾空白。首尾空白不影响译文，也不算作文本变化。"""
        return self._view.input_text().strip()

    @staticmethod
    def _new_session() -> SessionId:
        # 统计库按会话号记录胜出者，会话号必须跨重启唯一，所以用 UUID4（架构文档第 7 章）。
        return SessionId(uuid.uuid4().hex)

    def _on_pause(self) -> None:
        text = self._input()
        if not text:
            return
        if self._last is not None and self._last.key == (text, self._source, self._target):
            return
        self._send(self._new_session(), text, billing=False)

    def _resend_now(self) -> None:
        """语言变化后不等停顿，按新语言立即重发当前文本。"""
        self._timer.stop()
        self._on_pause()

    def _send(self, session_id: SessionId, text: str, *, billing: bool) -> None:
        self._last = _Request(session_id, text, self._source, self._target)
        self._logger.debug(
            "发送 TranslateText：会话 %s，%d 个字符，计费实例=%s", session_id, len(text), billing
        )
        self._bus.send(
            TranslateText(
                session_id=session_id,
                text=text,
                source=self._source,
                target=self._target,
                include_billing=billing,
            )
        )

    # ------------------------------------------------------------------ 译文区与引擎列表

    def _color(self, engine_id: EngineId) -> QColor:
        configured = ""
        if self._statuses is not None and self._statuses.colors is not None:
            configured = self._statuses.colors.get(engine_id, "")
        return engine_color(engine_id, configured)

    def _item(self, segment: _Segment) -> SegmentItem:
        winner = segment.shown_winner
        return SegmentItem(segment.shown_text, None if winner is None else self._color(winner))

    def _clear_display(self) -> None:
        self._shown_session = None
        self._segments = []
        self._index = {}
        self._engines = {}
        self._rows = {}
        self._order = []
        self._view.show_segments((), ())
        self._view.clear_engine_rows()

    def _show_new_session(self, session_id: SessionId, views: Sequence[SegmentView]) -> None:
        """当前会话的第一帧：整体替换译文区和引擎列表。

        第一帧包含会话里的全部段，顺序就是段的顺序（架构文档 6.6 节）。这时多数段还没有译文，
        只按 display_text 显示的话，输入停顿一次，所有没改动的段都会先跳回原文再换成缓存译文。
        所以语言没变时，原文与上一个会话某段相同、还没有胜出者的段先沿用那一段正在显示的译文，
        这一段有了结果后再按正常规则替换；结果与占位相同时不播放淡入。
        """
        assert self._last is not None
        langs = (self._last.source, self._last.target)
        carried: dict[str, _Segment] = {}
        if langs == self._shown_langs:
            for segment in self._segments:
                if segment.shown_winner is not None:
                    carried.setdefault(segment.view.source_text, segment)

        self._shown_session = session_id
        self._shown_langs = langs
        self._segments = []
        self._index = {}
        self._engines = {}
        self._rows = {}
        self._order = []
        self._view.clear_engine_rows()

        fade: list[int] = []
        dirty: set[EngineId] = set()
        for view in views:
            if view.segment_id in self._index:
                continue
            index = len(self._segments)
            previous = carried.get(view.source_text)
            if view.winner is None and not view.finished and previous is not None:
                segment = _Segment(view, previous.shown_text, previous.shown_winner, placeholder=True)
            else:
                segment = _Segment(view, view.display_text, view.winner)
                if view.winner is not None and (
                    previous is None
                    or (previous.shown_text, previous.shown_winner) != (view.display_text, view.winner)
                ):
                    fade.append(index)
            self._segments.append(segment)
            self._index[view.segment_id] = index
            self._track(index, None, view, dirty)
        self._view.show_segments([self._item(segment) for segment in self._segments], fade)
        self._push_rows(dirty)

    def _apply(self, views: Iterable[SegmentView]) -> None:
        """同一会话的后续刷新：只改动变化的段和变化的行（需求 REL-04）。"""
        changes: list[tuple[int, SegmentItem]] = []
        fade: list[int] = []
        dirty: set[EngineId] = set()
        for view in views:
            index = self._index.get(view.segment_id)
            if index is None:
                # 第一帧之后才出现的段，追加到末尾。
                index = len(self._segments)
                segment = _Segment(view, view.display_text, view.winner)
                self._segments.append(segment)
                self._index[view.segment_id] = index
                self._track(index, None, view, dirty)
                changes.append((index, self._item(segment)))
                if view.winner is not None:
                    fade.append(index)
                continue
            segment = self._segments[index]
            previous = segment.view
            segment.view = view
            self._track(index, previous, view, dirty)
            text, winner, placeholder = _next_shown(segment, view)
            segment.placeholder = placeholder
            if (text, winner) == (segment.shown_text, segment.shown_winner):
                continue
            segment.shown_text, segment.shown_winner = text, winner
            changes.append((index, self._item(segment)))
            # 显示的译文换了来源或内容就播放淡入（需求 RACE-06）；没有胜出者时显示的是原文，不播放。
            if winner is not None:
                fade.append(index)
        if changes:
            self._view.update_segments(changes, fade)
        self._push_rows(dirty)

    def _track(
        self, index: int, previous: SegmentView | None, view: SegmentView, dirty: set[EngineId]
    ) -> None:
        """把一段的新快照记入各实例的尝试表与胜出次数，变化的实例加入 dirty。"""
        stale = (
            {attempt.engine_id for attempt in previous.attempts} if previous is not None else set[EngineId]()
        )
        # 快照里胜出者排第一、其余按优先级排列；分配 rank 时把胜出者放到最后，rank 因此接近优先级顺序。
        ordered = sorted(view.attempts, key=lambda attempt: attempt.engine_id == view.winner)
        for attempt in ordered:
            engine = self._engines.get(attempt.engine_id)
            if engine is None:
                engine = self._engines[attempt.engine_id] = _Engine(rank=len(self._engines))
            if engine.attempts.get(index) != attempt:
                engine.attempts[index] = attempt
                dirty.add(attempt.engine_id)
            stale.discard(attempt.engine_id)
        for engine_id in stale:
            engine = self._engines.get(engine_id)
            if engine is not None and engine.attempts.pop(index, None) is not None:
                dirty.add(engine_id)
        old_winner = previous.winner if previous is not None else None
        if old_winner != view.winner:
            if old_winner is not None and old_winner in self._engines:
                self._engines[old_winner].wins -= 1
                dirty.add(old_winner)
            if view.winner is not None and view.winner in self._engines:
                self._engines[view.winner].wins += 1
                dirty.add(view.winner)

    def _row(self, engine_id: EngineId, engine: _Engine) -> EngineRow:
        attempts = [engine.attempts[index] for index in sorted(engine.attempts)]
        states = {attempt.state for attempt in attempts}
        state = next((s for s in _STATE_PRECEDENCE if s in states), AttemptState.CANCELLED)
        fresh = [
            attempt.latency_ms
            for attempt in attempts
            if attempt.state in (AttemptState.DONE, AttemptState.FAILED)
            and not attempt.cached
            and attempt.latency_ms is not None
        ]
        if fresh:
            latency = tr("bilingual.latency.ms", ms=round(max(fresh)))
        elif any(attempt.state is AttemptState.DONE and attempt.cached for attempt in attempts):
            latency = tr("bilingual.latency.cached")
        else:
            latency = ""
        text = "\n".join(
            attempt.text for attempt in attempts if attempt.state is AttemptState.DONE and attempt.text
        )
        failed = next((attempt for attempt in attempts if attempt.state is AttemptState.FAILED), None)
        error = ""
        if failed is not None:
            reason, suggestion = error_text(failed.error_code)
            error = tr("bilingual.row.error", reason=reason, suggestion=suggestion)
        name = engine_id
        if self._statuses is not None:
            name = self._statuses.names.get(engine_id) or engine_id
        return EngineRow(
            engine_id=engine_id,
            name=name,
            color=self._color(engine_id),
            state=state,
            status=tr(_STATE_KEYS[state]),
            latency=latency,
            text=text,
            error=error,
        )

    def _push_rows(self, engine_ids: Iterable[EngineId]) -> None:
        """重算给出的实例的行，只把内容变化的行和新的顺序交给视图。

        排序：胜出段数多的在前，单段输入时胜出者因此排第一；胜出段数相同的按 rank。
        """
        changed: list[EngineRow] = []
        for engine_id in engine_ids:
            engine = self._engines.get(engine_id)
            if engine is None:
                continue
            row = self._row(engine_id, engine)
            if self._rows.get(engine_id) != row:
                self._rows[engine_id] = row
                changed.append(row)
        engines = self._engines
        order = sorted(engines, key=lambda engine_id: (-engines[engine_id].wins, engines[engine_id].rank))
        if changed or order != self._order:
            self._order = order
            self._view.show_engine_rows(order, changed)

    # ------------------------------------------------------------------ 窗口

    def _refresh_language(self) -> None:
        """界面语言变了就刷新视图上的文案和列表里的状态文字（需求 CFG-05）。"""
        language = current_language()
        if language == self._language:
            return
        self._language = language
        self._view.retranslate()
        self._rows = {}
        self._push_rows(self._engines)

    def _show(self) -> None:
        self._refresh_language()
        if not self._geometry_restored:
            self._view.restore_geometry(self._saved.position, self._saved.size)
            self._geometry_restored = True
        self._view.present()
        self._logger.debug("显示双语窗口")

    def _hide(self) -> None:
        self._store.save_geometry(*self._view.window_geometry())
        self._view.conceal()
        self._logger.debug("隐藏双语窗口")

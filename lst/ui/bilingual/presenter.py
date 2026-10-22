"""双语窗口的展示器（架构文档 11.1、11.6 节）。

展示器订阅总线消息并转换成对视图的调用，把用户操作转换成 TranslateText 命令，本身不含绘制代码。
它和视图之间只通过下面两个协议交互，测试可以换上假视图。

与事件循环的配合方式：展示器不 await 任何东西。输入停顿用 Qt 的单次计时器判断；发命令用
``EventBus.send``，它只把分发排进事件循环就返回；订阅的处理函数都是普通函数。qasync 让 asyncio
运行在 Qt 主线程上，总线处理函数、Qt 信号槽和计时器回调因此都在主线程上依次执行，展示器的状态
只在主线程读写，不需要加锁。

翻译服务（需求 BI-04）：窗口顶部的下拉框列出启用的引擎实例，按优先级排列，计费的实例带“计费”标注，
出错的实例列出但不能选。每次请求只交给选中的那一个。用户的选择记在窗口状态里；记住的服务被停用、删除或出错时，
窗口在状态行说明，并改用列表里第一个能选的服务，记住的选择不变，它恢复可用后自动换回。

日志只记录会话号、字符数和计数，不记录原文与译文（需求 PRV-02）。
"""

import logging
import uuid
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import QPoint, QSize, QTimer

from lst.contracts.config import AppConfig
from lst.contracts.events import (
    EngineHealth,
    EngineStatuses,
    SegmentsUpdated,
    ToggleBilingualWindow,
    TranslateText,
)
from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId
from lst.contracts.translation import AttemptState, SegmentView, SkipReason
from lst.kernel import EventBus
from lst.ui.bilingual.options import BilingualOptions, parse_options
from lst.ui.bilingual.state import WindowStateStore
from lst.ui.common.bindings import BindingsConfig, bindings_from
from lst.ui.i18n import apply_config_language, current_language, error_text, tr


@dataclass(frozen=True, slots=True)
class EngineChoice:
    """服务下拉框里的一项。label 已经本地化，计费的服务带“计费”标注；available 为假的项不能选（实例出错）。"""

    engine_id: EngineId
    label: str
    available: bool


class BilingualActions(Protocol):
    """视图把用户操作交给展示器的接口。"""

    def input_changed(self, text: str) -> None: ...
    def translate_now(self) -> None: ...
    def languages_changed(self, source: Lang | None, target: Lang | None) -> None: ...
    def engine_chosen(self, engine_id: EngineId) -> None: ...
    def swap_languages(self) -> None: ...
    def clear(self) -> None: ...
    def copy_translation(self) -> None: ...
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

    def set_engines(self, choices: Sequence[EngineChoice], current: EngineId | None) -> None:
        """重建服务下拉框并选中 current，不触发 engine_chosen。"""
        ...

    def set_status(self, text: str) -> None:
        """译文区下方的状态行；空串表示没有要说明的情况。"""
        ...

    def set_pinned(self, pinned: bool) -> None: ...
    def set_horizontal(self, horizontal: bool) -> None: ...
    def set_bindings(self, bindings: BindingsConfig) -> None:
        """换用窗口内的键位（UI-09）。"""
        ...

    def show_segments(self, texts: Sequence[str], fade: Collection[int]) -> None:
        """整体替换译文区；fade 中的段播放淡入。"""
        ...

    def update_segments(self, changes: Sequence[tuple[int, str]], fade: Collection[int]) -> None:
        """只改动给出的段；序号等于当前段数时追加到末尾。"""
        ...

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
    engine_id: EngineId | None

    @property
    def key(self) -> tuple[str, Lang | None, Lang | None, EngineId | None]:
        return (self.text, self.source, self.target, self.engine_id)


@dataclass(slots=True)
class _Segment:
    """译文区一段的状态。shown_* 是当前显示的内容，可能与 view 不同，见 _next_shown。"""

    view: SegmentView
    shown_text: str
    shown_engine: EngineId | None
    placeholder: bool = False


def _next_shown(segment: _Segment, view: SegmentView) -> tuple[str, EngineId | None, bool]:
    """一段收到新快照后应当显示什么：(文字, 来源实例, 是否仍是占位)。

    新会话开始时，原文没变的段先沿用上一个会话的译文作为占位（见 _show_new_session），
    直到这一段有了译文，或者显示已定仍没有译文（这时按 RACE-04 显示原文）。
    """
    if view.shown_engine is not None:
        return view.display_text, view.shown_engine, False
    if segment.placeholder and not view.finished:
        return segment.shown_text, segment.shown_engine, True
    return view.display_text, None, False


def engine_choices(statuses: EngineStatuses | None) -> list[EngineChoice]:
    """服务下拉框的内容：启用的实例按优先级排列（优先级相同时按 statuses 里的顺序）。"""
    if statuses is None:
        return []
    priorities = statuses.priorities or {}
    billing = statuses.billing or {}
    enabled = [
        (priorities.get(engine_id, 0), index, engine_id, status.health)
        for index, (engine_id, status) in enumerate(statuses.statuses.items())
        if status.health is not EngineHealth.DISABLED
    ]
    choices: list[EngineChoice] = []
    for _, _, engine_id, health in sorted(enabled):
        name = statuses.names.get(engine_id) or engine_id
        if health is EngineHealth.ERROR:
            label = tr("bilingual.engine.error", name=name)
        elif billing.get(engine_id, False):
            label = tr("bilingual.engine.billing", name=name)
        else:
            label = name
        choices.append(EngineChoice(engine_id, label, available=health is not EngineHealth.ERROR))
    return choices


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

        # 译文区：当前显示的是哪个会话、它的语言与服务，以及各段状态。
        self._shown_session: SessionId | None = None
        self._shown_key: tuple[Lang | None, Lang | None, EngineId | None] = (None, None, None)
        self._segments: list[_Segment] = []
        self._index: dict[SegmentId, int] = {}

        self._saved = store.load()
        self._geometry_restored = False
        self._closed = False
        self._source = self._saved.source
        self._target = self._saved.target
        self._pinned = self._saved.on_top
        # 翻译服务：preferred 是用户选过并记住的，engine 是实际使用的（preferred 不可用时回退）。
        self._preferred: EngineId | None = self._saved.engine
        self._engine: EngineId | None = None
        self._choices: list[EngineChoice] = []
        self._notice = ""
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

    @property
    def engine(self) -> EngineId | None:
        """当前使用的翻译服务；没有可用的服务时为 None。"""
        return self._engine

    @property
    def preferred_engine(self) -> EngineId | None:
        """用户选过并记住的翻译服务。"""
        return self._preferred

    @property
    def choices(self) -> list[EngineChoice]:
        return list(self._choices)

    def translation_text(self) -> str:
        """译文区当前显示的全部文字，段与段之间用换行分隔。"""
        return "\n".join(segment.shown_text for segment in self._segments)

    def status_text(self) -> str:
        """状态行当前的文字。"""
        return self._status()

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
        if self._closed:
            return
        # 组合根与各模块处理 AppConfig 的先后没有保证（组合根的处理函数是协程，下一轮循环才执行），
        # 所以这里先按配置设置界面语言，再判断文案要不要刷新。
        apply_config_language(config)
        options = parse_options(config)
        self._options = options
        # 正在计时的停顿保持原来的时长，新时长从下一次输入开始生效。
        self._timer.setInterval(options.debounce_ms)
        self._view.set_horizontal(options.layout == "horizontal")
        self._view.set_bindings(bindings_from(config.section("bindings")))
        self._refresh_language()

    def on_engine_statuses(self, statuses: EngineStatuses) -> None:
        if self._closed:
            return
        self._statuses = statuses
        self._refresh_engines()

    def on_segments_updated(self, event: SegmentsUpdated) -> None:
        if self._closed:
            return
        if self._last is None or event.session_id != self._last.session_id:
            # 截屏会话、已被取代的双语会话都在这里丢弃。
            self._logger.debug("丢弃不属于当前会话的 SegmentsUpdated：会话 %s", event.session_id)
            return
        if event.session_id != self._shown_session:
            self._show_new_session(event.session_id, event.views)
        else:
            self._apply(event.views)
        self._view.set_status(self._status())

    # ------------------------------------------------------------------ 用户操作（BilingualActions）

    def input_changed(self, text: str) -> None:
        if not text.strip():
            # 清空立即生效，不等停顿：竞速模块收到空文本后取消当前会话。
            self._timer.stop()
            self._clear_display()
            if self._last is not None and self._last.text:
                self._send(self._new_session(), "")
            return
        self._timer.start(self._options.debounce_ms)

    def translate_now(self) -> None:
        """立即翻译（默认 Ctrl+Return）：不等停顿，总是开始新会话，上次失败时也可以借此重试。"""
        self._timer.stop()
        text = self._input()
        if text:
            self._send(self._new_session(), text)

    def languages_changed(self, source: Lang | None, target: Lang | None) -> None:
        if (source, target) == (self._source, self._target):
            return
        self._source, self._target = source, target
        self._store.save_languages(source, target)
        self._resend_now()

    def engine_chosen(self, engine_id: EngineId) -> None:
        """用户在下拉框里选了一个服务：记住它，并按它立即重发当前文本。"""
        if not any(choice.engine_id == engine_id and choice.available for choice in self._choices):
            return
        self._preferred = engine_id
        self._store.save_engine(engine_id)
        self._refresh_engines()

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

    # ------------------------------------------------------------------ 翻译服务

    def _refresh_engines(self) -> None:
        """按最新的实例状态重建下拉框，决定实际使用的服务；服务变了且有文本时立即重发。"""
        choices = engine_choices(self._statuses)
        usable = [choice.engine_id for choice in choices if choice.available]
        engine = self._preferred if self._preferred in usable else next(iter(usable), None)
        changed = engine != self._engine
        self._choices = choices
        self._engine = engine
        self._notice = self._engine_notice()
        self._view.set_engines(choices, engine)
        self._view.set_status(self._status())
        if changed:
            self._logger.debug("双语窗口改用翻译服务 %s", engine)
            self._resend_now()

    def _engine_notice(self) -> str:
        """服务方面要在状态行说明的情况：没有能用的服务，或者记住的服务不能用、改用了别的。"""
        if self._statuses is None:
            return ""
        engine, preferred = self._engine, self._preferred
        if engine is None:
            return tr("bilingual.engine.none")
        if preferred is not None and preferred != engine:
            return tr("bilingual.engine.fallback", old=self._name(preferred), new=self._name(engine))
        return ""

    def _name(self, engine_id: EngineId) -> str:
        if self._statuses is not None:
            return self._statuses.names.get(engine_id) or engine_id
        return engine_id

    def _status(self) -> str:
        """状态行：服务的回退说明优先，其次是当前会话里的问题（失败原因、不支持的语言对）。"""
        if self._notice:
            return self._notice
        views = [segment.view for segment in self._segments]
        for view in views:
            failed = next((a for a in view.attempts if a.state is AttemptState.FAILED), None)
            if failed is not None and view.shown_engine is None:
                reason, suggestion = error_text(failed.error_code)
                return tr("bilingual.status.failed", reason=reason, suggestion=suggestion)
        if any(view.skip_reason is SkipReason.NO_ENGINE for view in views) and self._engine is not None:
            return tr("bilingual.status.unsupported", name=self._name(self._engine))
        return ""

    # ------------------------------------------------------------------ 发送

    def _input(self) -> str:
        """输入框的文字，去掉首尾空白。首尾空白不影响译文，也不算作文本变化。"""
        return self._view.input_text().strip()

    @staticmethod
    def _new_session() -> SessionId:
        # 会话号也是统计库记录尝试的键，必须跨重启唯一，所以用 UUID4（架构文档第 7 章）。
        return SessionId(uuid.uuid4().hex)

    def _on_pause(self) -> None:
        text = self._input()
        if not text:
            return
        if self._last is not None and self._last.key == (text, self._source, self._target, self._engine):
            return
        self._send(self._new_session(), text)

    def _resend_now(self) -> None:
        """语言或服务变化后不等停顿，按新设置立即重发当前文本。"""
        self._timer.stop()
        self._on_pause()

    def _send(self, session_id: SessionId, text: str) -> None:
        self._last = _Request(session_id, text, self._source, self._target, self._engine)
        self._logger.debug(
            "发送 TranslateText：会话 %s，%d 个字符，服务 %s", session_id, len(text), self._engine
        )
        self._bus.send(
            TranslateText(
                session_id=session_id,
                text=text,
                source=self._source,
                target=self._target,
                engine_id=self._engine,
            )
        )

    # ------------------------------------------------------------------ 译文区

    def _clear_display(self) -> None:
        self._shown_session = None
        self._segments = []
        self._index = {}
        self._view.show_segments((), ())
        self._view.set_status(self._status())

    def _show_new_session(self, session_id: SessionId, views: Sequence[SegmentView]) -> None:
        """当前会话的第一帧：整体替换译文区。

        第一帧包含会话里的全部段，顺序就是段的顺序（架构文档 6.6 节）。这时多数段还没有译文，
        只按 display_text 显示的话，输入停顿一次，所有没改动的段都会先跳回原文再换成缓存译文。
        所以语言与服务都没变时，原文与上一个会话某段相同、还没有译文的段先沿用那一段正在显示的译文，
        这一段有了结果后再按正常规则替换；结果与占位相同时不播放淡入。
        """
        assert self._last is not None
        key = (self._last.source, self._last.target, self._last.engine_id)
        carried: dict[str, _Segment] = {}
        if key == self._shown_key:
            for segment in self._segments:
                if segment.shown_engine is not None:
                    carried.setdefault(segment.view.source_text, segment)

        self._shown_session = session_id
        self._shown_key = key
        self._segments = []
        self._index = {}

        fade: list[int] = []
        for view in views:
            if view.segment_id in self._index:
                continue
            index = len(self._segments)
            previous = carried.get(view.source_text)
            if view.shown_engine is None and not view.finished and previous is not None:
                segment = _Segment(view, previous.shown_text, previous.shown_engine, placeholder=True)
            else:
                segment = _Segment(view, view.display_text, view.shown_engine)
                if view.shown_engine is not None and (
                    previous is None
                    or (previous.shown_text, previous.shown_engine) != (view.display_text, view.shown_engine)
                ):
                    fade.append(index)
            self._segments.append(segment)
            self._index[view.segment_id] = index
        self._view.show_segments([segment.shown_text for segment in self._segments], fade)

    def _apply(self, views: Iterable[SegmentView]) -> None:
        """同一会话的后续刷新：只改动变化的段（需求 REL-04）。"""
        changes: list[tuple[int, str]] = []
        fade: list[int] = []
        for view in views:
            index = self._index.get(view.segment_id)
            if index is None:
                # 第一帧之后才出现的段，追加到末尾。
                index = len(self._segments)
                segment = _Segment(view, view.display_text, view.shown_engine)
                self._segments.append(segment)
                self._index[view.segment_id] = index
                changes.append((index, segment.shown_text))
                if view.shown_engine is not None:
                    fade.append(index)
                continue
            segment = self._segments[index]
            segment.view = view
            text, engine, placeholder = _next_shown(segment, view)
            segment.placeholder = placeholder
            if (text, engine) == (segment.shown_text, segment.shown_engine):
                continue
            segment.shown_text, segment.shown_engine = text, engine
            changes.append((index, text))
            # 显示的译文换了内容就播放淡入（需求 RACE-06）；没有译文时显示的是原文，不播放。
            if engine is not None:
                fade.append(index)
        if changes:
            self._view.update_segments(changes, fade)

    # ------------------------------------------------------------------ 窗口

    def _refresh_language(self) -> None:
        """界面语言变了就刷新视图上的文案、服务列表与状态行（需求 CFG-05）。"""
        language = current_language()
        if language == self._language:
            return
        self._language = language
        self._view.retranslate()
        self._choices = engine_choices(self._statuses)
        self._notice = self._engine_notice()
        self._view.set_engines(self._choices, self._engine)
        self._view.set_status(self._status())

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

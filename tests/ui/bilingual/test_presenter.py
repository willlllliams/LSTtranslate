"""展示器脱离真实窗口的测试：换上记录调用的假视图，确认展示器只通过 BilingualView 协议驱动视图
（架构文档 11.1 节“展示器不包含绘制代码，可以单独测试”）。"""

import logging
from collections.abc import Collection, Sequence
from pathlib import Path

from PySide6.QtCore import QPoint, QSettings, QSize

from lst.contracts.events import SegmentsUpdated, ToggleBilingualWindow, TranslateText
from lst.contracts.ids import EngineId, Lang, SessionId
from lst.contracts.translation import AttemptState
from lst.kernel import EventBus
from lst.ui.bilingual import BilingualPresenter, EngineChoice
from lst.ui.bilingual.presenter import BilingualActions, BilingualView
from lst.ui.bilingual.state import WindowStateStore
from lst.ui.common.bindings import BindingsConfig
from tests.ui.bilingual.conftest import attempt, segment, statuses

D, R = AttemptState.DONE, AttemptState.RUNNING


class FakeView:
    """记录展示器调用的假视图。"""

    def __init__(self) -> None:
        self.actions: BilingualActions | None = None
        self.text = ""
        self.langs: tuple[Lang | None, Lang | None] = (None, None)
        self.pinned = False
        self.horizontal = False
        self.bindings = BindingsConfig()
        self.items: list[str] = []
        self.fades: list[set[int]] = []
        self.choices: list[EngineChoice] = []
        self.current: EngineId | None = None
        self.status = ""
        self.shown = False
        self.calls: list[str] = []

    def bind(self, actions: BilingualActions) -> None:
        self.actions = actions

    def retranslate(self) -> None:
        self.calls.append("retranslate")

    def input_text(self) -> str:
        return self.text

    def set_input_text(self, text: str) -> None:
        self.text = text

    def focus_input(self) -> None:
        self.calls.append("focus")

    def set_languages(self, source: Lang | None, target: Lang | None) -> None:
        self.langs = (source, target)

    def set_pinned(self, pinned: bool) -> None:
        self.pinned = pinned

    def set_horizontal(self, horizontal: bool) -> None:
        self.horizontal = horizontal

    def set_engines(self, choices: Sequence[EngineChoice], current: EngineId | None) -> None:
        self.choices = list(choices)
        self.current = current

    def set_status(self, text: str) -> None:
        self.status = text

    def set_bindings(self, bindings: BindingsConfig) -> None:
        self.bindings = bindings

    def show_segments(self, texts: Sequence[str], fade: Collection[int]) -> None:
        self.items = list(texts)
        self.fades.append(set(fade))
        self.calls.append("show_segments")

    def update_segments(self, changes: Sequence[tuple[int, str]], fade: Collection[int]) -> None:
        for index, item in changes:
            if index == len(self.items):
                self.items.append(item)
            else:
                self.items[index] = item
        self.fades.append(set(fade))
        self.calls.append("update_segments")

    def is_shown(self) -> bool:
        return self.shown

    def restore_geometry(self, position: QPoint | None, size: QSize | None) -> None:
        self.calls.append("restore_geometry")

    def window_geometry(self) -> tuple[QPoint, QSize]:
        return (QPoint(10, 20), QSize(640, 600))

    def present(self) -> None:
        self.shown = True

    def conceal(self) -> None:
        self.shown = False


def _presenter(bus: EventBus, tmp_path: Path, view: FakeView) -> BilingualPresenter:
    protocol_view: BilingualView = view  # 静态检查：假视图满足视图协议
    settings = QSettings(str(tmp_path / "state.ini"), QSettings.Format.IniFormat)
    return BilingualPresenter(
        protocol_view,
        bus=bus,
        store=WindowStateStore(settings),
        logger=logging.getLogger("test.bilingual"),
        clipboard=lambda _text: None,
    )


async def test_presenter_drives_a_fake_view(bus: EventBus, tmp_path: Path) -> None:
    sent: list[TranslateText] = []
    bus.subscribe(TranslateText, sent.append)
    view = FakeView()
    presenter = _presenter(bus, tmp_path, view)
    assert view.actions is presenter

    presenter.on_engine_statuses(statuses({"deepl": "DeepL", "argos": "Argos"}, billing={"deepl"}))
    assert [(c.engine_id, c.label, c.available) for c in view.choices] == [
        ("deepl", "DeepL（计费）", True),
        ("argos", "Argos", True),
    ]
    assert view.current == "deepl"
    presenter.engine_chosen(EngineId("argos"))
    assert view.current == "argos"

    view.text = "one\ntwo"
    presenter.translate_now()
    await bus.drain()
    session = sent[-1].session_id
    assert sent[-1].engine_id == "argos"

    first = (
        segment("a", "one", attempt("argos", D, "一", latency=5), shown_engine="argos"),
        segment("b", "two", attempt("argos", R)),
    )
    presenter.on_segments_updated(SegmentsUpdated(session, first))
    assert view.items == ["一", "two"]
    assert view.fades[-1] == {0}

    # 只有第二段变化：只更新这一段。
    view.calls.clear()
    presenter.on_segments_updated(
        SegmentsUpdated(
            session,
            (segment("b", "two", attempt("argos", D, "贰", latency=90), shown_engine="argos"),),
        )
    )
    assert view.calls == ["update_segments"]
    assert view.items == ["一", "贰"]

    # 其他会话的刷新不触达视图。
    view.calls.clear()
    presenter.on_segments_updated(SegmentsUpdated(SessionId("old"), first))
    assert view.calls == []

    presenter.on_toggle(ToggleBilingualWindow())
    presenter.on_toggle(ToggleBilingualWindow())
    presenter.on_toggle(ToggleBilingualWindow())
    assert view.shown
    assert view.calls.count("restore_geometry") == 1, "只在第一次显示时恢复保存的位置"

    presenter.shutdown()
    presenter.on_toggle(ToggleBilingualWindow())
    assert view.shown, "停止后到达的切换命令被忽略"

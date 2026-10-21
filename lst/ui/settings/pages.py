"""设置窗口的页面基类，以及由声明生成的四个页面：常规、热键、截屏与识别、竞速（需求 UI-05）。

页面是一个可滚动的区域，里面从上到下排列分组标题、说明文字和设置卡片。页面不保存状态：
展示器通过 ``Topic`` 通知哪类数据变了，页面据此刷新自己的控件。
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from lst.contracts.events import EngineStatsRow
from lst.ui.i18n import tr
from lst.ui.settings.cards import Binding, CardBuilder, FieldDef
from lst.ui.settings.controls import SettingCard, ThemedLabel
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.presenter import Location, SettingsPresenter, Topic
from lst.ui.settings.strings import format_duration, format_percent
from lst.ui.theme import TOKENS, Glyph, fluent_icon

PAGE_WIDTH: Final = 1000
"""页面内容的最大宽度。窗口很宽时内容不继续拉伸，两边留白。"""


@dataclass(frozen=True, slots=True)
class Title:
    """分组标题，key 是文案键。"""

    key: str


@dataclass(frozen=True, slots=True)
class Intro:
    """一段说明文字，key 是文案键。"""

    key: str


type PageItem = Title | Intro | FieldDef


class SettingsPage(QScrollArea):
    """页面基类：标题、可滚动的内容区，以及与展示器的连接。"""

    page_id = ""
    icon = Glyph.SETTINGS

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__()
        self.presenter = presenter
        self.builder = builder
        self.dialogs = dialogs
        self.bindings: list[Binding] = []
        self._titles: list[tuple[QLabel, str]] = []
        self._intros: list[tuple[QLabel, str]] = []
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.viewport().setAutoFillBackground(False)

        self.content = QWidget()
        self.content.setAutoFillBackground(False)
        self.content.setMaximumWidth(PAGE_WIDTH)
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(
            TOKENS.page_margin, TOKENS.spacing_xl, TOKENS.page_margin, TOKENS.spacing_xl
        )
        self.body.setSpacing(TOKENS.card_gap)
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.content, 1)
        row.addStretch(0)
        self.setWidget(wrapper)
        # QScrollArea.setWidget 会把内容控件设成自动填充背景，这会盖住 Mica，要在它之后关掉。
        wrapper.setAutoFillBackground(False)

        self.heading = ThemedLabel("primary", TOKENS.page_title_px, semibold=True, display=True)
        self.body.addWidget(self.heading)
        self.body.addSpacing(TOKENS.spacing_s)

    # ------------------------------------------------------------------ 构建

    def add_title(self, key: str) -> QLabel:
        """分组标题：与上一组之间隔 group_gap，与下面的卡片之间隔一点。"""
        label = ThemedLabel("primary", TOKENS.title_px, semibold=True)
        self.body.addSpacing(TOKENS.group_gap - TOKENS.card_gap)
        self.body.addWidget(label)
        self.body.addSpacing(TOKENS.card_gap)
        self._titles.append((label, key))
        return label

    def add_intro(self, key: str) -> QLabel:
        label = ThemedLabel("secondary", TOKENS.caption_px)
        label.setWordWrap(True)
        self.body.addWidget(label)
        self.body.addSpacing(TOKENS.spacing_s)
        self._intros.append((label, key))
        return label

    def add_field(self, definition: FieldDef) -> Binding | None:
        binding = self.builder.build(definition)
        if binding is not None:
            self.bindings.append(binding)
            self.body.addWidget(binding.card)
        return binding

    def add_card(self, card: QWidget) -> None:
        self.body.addWidget(card)

    def build(self, items: Sequence[PageItem]) -> None:
        for item in items:
            match item:
                case Title(key):
                    self.add_title(key)
                case Intro(key):
                    self.add_intro(key)
                case FieldDef():
                    self.add_field(item)

    def finish(self) -> None:
        """内容构建完成：底部留出弹性空白，设置文案并显示配置里的值。"""
        self.body.addStretch(1)
        self.retranslate()
        self.refresh_all()

    # ------------------------------------------------------------------ 刷新

    def retranslate(self) -> None:
        self.heading.setText(tr(f"settings.nav.{self.page_id}"))
        for label, key in self._titles:
            label.setText(tr(key))
        for label, key in self._intros:
            label.setText(tr(key))
        for binding in self.bindings:
            binding.retranslate()

    def refresh_all(self) -> None:
        for binding in self.bindings:
            binding.refresh(self.presenter)
            binding.refresh_error(self.presenter)

    def refresh(self, topic: Topic) -> None:
        """展示器通知 topic 的数据变了。"""
        match topic:
            case Topic.CONFIG:
                for binding in self.bindings:
                    binding.refresh(self.presenter)
            case Topic.REJECTIONS:
                for binding in self.bindings:
                    binding.refresh_error(self.presenter)
            case Topic.LANGUAGE:
                self.retranslate()
            case _:
                pass

    def all_bindings(self) -> list[Binding]:
        return list(self.bindings)

    def claims(self, location: Location) -> bool:
        """页面上有卡片负责显示这个位置的 ConfigRejected。"""
        return any(binding.matches(location) for binding in self.all_bindings())

    def flush(self) -> None:
        """把还没提交的输入提交出去（窗口隐藏时调用）。"""
        for binding in self.all_bindings():
            binding.editor.flush()

    def on_shown(self) -> None:
        """页面被切换到前台时调用。"""


class GeneralPage(SettingsPage):
    """常规：界面语言、主题。日志级别没有对应的配置节，暂不提供。"""

    page_id = "general"
    icon = Glyph.SETTINGS

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self.build(
            [
                Title("settings.section.language"),
                FieldDef("ui", ("language",)),
                Title("settings.section.appearance"),
                FieldDef("ui", ("theme",)),
            ]
        )
        self.finish()


class HotkeysPage(SettingsPage):
    """热键：截屏翻译与双语窗口的全局热键。"""

    page_id = "hotkeys"
    icon = Glyph.KEYBOARD

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self.build(
            [
                Intro("settings.hotkeys.intro"),
                FieldDef("hotkeys", ("capture",), widget="hotkey"),
                FieldDef("hotkeys", ("bilingual",), widget="hotkey"),
            ]
        )
        self.finish()


class CapturePage(SettingsPage):
    """截屏与识别：[ocr] 节与 [screenshot] 节的语言。"""

    page_id = "capture"
    icon = Glyph.SCREEN_CAPTURE

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self.build(
            [
                Title("settings.section.ocr"),
                FieldDef("ocr", ("enabled",)),
                FieldDef("ocr", ("mode",)),
                FieldDef("ocr", ("device",)),
                FieldDef("ocr", ("language",), widget="language_auto"),
                Title("settings.section.ocr_features"),
                FieldDef("ocr", ("table",)),
                FieldDef("ocr", ("formula",)),
                FieldDef("ocr", ("chart",)),
                FieldDef("ocr", ("doc_orientation",)),
                FieldDef("ocr", ("doc_unwarping",)),
                FieldDef("ocr", ("textline_orientation",)),
                Title("settings.section.preprocess"),
                FieldDef("ocr", ("grayscale",)),
                FieldDef(
                    "ocr",
                    ("resize_max_side",),
                    suffix="settings.suffix.pixels",
                    zero="settings.special.no_scaling",
                ),
                Title("settings.section.translation"),
                FieldDef("screenshot", ("target_lang",), widget="language"),
            ]
        )
        self.finish()


# ---------------------------------------------------------------- 竞速统计

_STAT_COLUMNS: Final = (
    "settings.stats.col.engine",
    "settings.stats.col.executed",
    "settings.stats.col.wins",
    "settings.stats.col.win_rate",
    "settings.stats.col.p50",
    "settings.stats.col.p95",
    "settings.stats.col.failure",
)


class StatsPanel(QWidget):
    """竞速统计表与“重置统计”“清空译文缓存”两个按钮（需求 RACE-07）。

    表里每个引擎实例一行：配置里有的实例按优先级排列在前，即使还没有统计数据也显示（数字为 0 或“—”）；
    统计库里有、配置里已经没有的实例（已删除）显示实例号，排在后面。
    """

    def __init__(self, presenter: SettingsPresenter, dialogs: Dialogs) -> None:
        super().__init__()
        self._presenter = presenter
        self._dialogs = dialogs
        self.intro = ThemedLabel("secondary", TOKENS.caption_px)
        self.intro.setWordWrap(True)
        self.table = QTableWidget(0, len(_STAT_COLUMNS))
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setMinimumHeight(190)
        self.empty_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.empty_label.setWordWrap(True)
        self.reset_button = QPushButton()
        self.reset_button.setIcon(fluent_icon(Glyph.REFRESH))
        self.clear_cache_button = QPushButton()
        self.clear_cache_button.setIcon(fluent_icon(Glyph.DELETE))
        self.cache_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.cache_label.setWordWrap(True)
        self.cache_label.setAccessibleName(tr("settings.cache.clear"))
        self.cache_label.hide()

        buttons = QHBoxLayout()
        buttons.setSpacing(TOKENS.spacing_s)
        buttons.addWidget(self.reset_button)
        buttons.addWidget(self.clear_cache_button)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOKENS.spacing_s)
        layout.addWidget(self.intro)
        layout.addWidget(self.table)
        layout.addWidget(self.empty_label)
        layout.addLayout(buttons)
        layout.addWidget(self.cache_label)

        self.reset_button.clicked.connect(self._reset)
        self.clear_cache_button.clicked.connect(self._clear_cache)
        self.retranslate()
        self.refresh()

    def retranslate(self) -> None:
        self.intro.setText(tr("settings.stats.intro"))
        self.table.setAccessibleName(tr("settings.stats.table"))
        self.table.setHorizontalHeaderLabels([tr(key) for key in _STAT_COLUMNS])
        self.empty_label.setText(tr("settings.stats.empty"))
        self.reset_button.setText(tr("settings.stats.reset"))
        self.clear_cache_button.setText(tr("settings.cache.clear"))
        self.refresh()

    def rows(self) -> list[tuple[str, EngineStatsRow | None]]:
        """表的内容：(实例名, 统计行)。没有统计数据的实例统计行为 None。"""
        stats = {str(row.engine_id): row for row in self._presenter.stats}
        rows: list[tuple[str, EngineStatsRow | None]] = []
        for entry in self._presenter.engines():
            rows.append((self._presenter.engine_name(entry.id), stats.pop(entry.id, None)))
        rows.extend((engine_id, row) for engine_id, row in stats.items())
        return rows

    def refresh(self) -> None:
        rows = self.rows()
        self.table.setRowCount(len(rows))
        right = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        for index, (name, row) in enumerate(rows):
            executed = row.executed if row is not None else 0
            wins = row.wins if row is not None else 0
            win_rate = wins / executed if executed else None
            cells = (
                name,
                str(executed),
                str(wins),
                format_percent(win_rate),
                format_duration(row.p50_ms if row is not None else None),
                format_duration(row.p95_ms if row is not None else None),
                format_percent(row.failure_rate if row is not None else None),
            )
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                if column > 0:
                    item.setTextAlignment(right)
                self.table.setItem(index, column, item)
        has_data = any(row is not None and row.executed > 0 for _, row in rows)
        self.empty_label.setVisible(not has_data)

    def refresh_cache(self) -> None:
        count = self._presenter.last_cache_cleared
        self.cache_label.setVisible(count is not None)
        if count is not None:
            self.cache_label.setText(tr("settings.cache.cleared", count=count))

    def _reset(self) -> None:
        if self._dialogs.confirm(tr("settings.confirm.title"), tr("settings.stats.reset.confirm")):
            self._presenter.reset_stats()

    def _clear_cache(self) -> None:
        if self._dialogs.confirm(tr("settings.confirm.title"), tr("settings.cache.clear.confirm")):
            self._presenter.clear_cache()


class RacePage(SettingsPage):
    """竞速：结束方式、来源标记、双语窗口的设置，以及竞速统计。"""

    page_id = "race"
    icon = Glyph.LIGHTNING

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self.build(
            [
                Title("settings.section.race_mode"),
                FieldDef("race", ("mode",)),
                FieldDef("race", ("show_source_marker", "overlay")),
                FieldDef("race", ("show_source_marker", "bilingual")),
                Title("settings.section.bilingual"),
                FieldDef("bilingual", ("pair",), widget="pair"),
                FieldDef("bilingual", ("debounce_ms",), suffix="settings.suffix.ms"),
                FieldDef("bilingual", ("auto_billing",)),
                FieldDef("bilingual", ("layout",)),
                Title("settings.section.stats"),
            ]
        )
        self.stats = StatsPanel(presenter, dialogs)
        self.stats_card = SettingCard(self.stats, stacked=True)
        self.stats_card.hide_header()
        self.add_card(self.stats_card)
        self.finish()

    def retranslate(self) -> None:
        super().retranslate()
        if hasattr(self, "stats"):
            self.stats.retranslate()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        if topic in (Topic.STATS, Topic.ENGINES):
            self.stats.refresh()
        elif topic is Topic.CACHE:
            self.stats.refresh_cache()

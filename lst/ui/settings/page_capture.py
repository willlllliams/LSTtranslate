"""截屏与识别页：OCR 引擎、[ocr] 节与 [screenshot] 节的语言（需求 OCRE-01 至 OCRE-06、UI-05）。

页面顶部是“OCR 引擎”卡片组，每个引擎一张卡片（``OcrEngineCard``），内容由 ``ocr_engine_rows.build_view``
按 ``OcrEngineCatalog`` 给出；下面是 [ocr] 节的设置项。

当前引擎是 Windows 自带 OCR 时：

- 只对 PaddleOCR 生效的设置项（识别模式、推理设备、表格、公式、图表、方向校正、去畸变、文本行方向）
  整张卡片不可用，说明后面注明“仅 PaddleOCR 可用”。配置里的值保留，切回 PaddleOCR 时照常生效；
- 识别语言的下拉框只列出 Windows 已安装 OCR 功能的语言（OcrEngineInfo.languages），前面加“自动”。
  配置里的语言不在其中时照实显示，并注明当前引擎不支持。

引擎卡片在构造时为每种引擎各建一张，引擎目录里没有的引擎隐藏。控件不随状态重建，焦点与 Tab 顺序不变。
"""

from typing import Final

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout, QWidget

from lst.contracts.ocr_engines import OcrEngineKind
from lst.ui.common.text import PROGRESS_STEPS, language_name, progress_steps
from lst.ui.i18n import tr
from lst.ui.settings.cards import Binding, CardBuilder, FieldDef, language_options
from lst.ui.settings.controls import ChoiceEditor, SettingCard, ThemedLabel
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.ocr_engine_rows import EngineView, Tone, build_view, engine_title
from lst.ui.settings.pages import SettingsPage
from lst.ui.settings.presenter import Location, SettingsPresenter, Topic
from lst.ui.theme import TOKENS, Glyph, fluent_icon, fluent_icon_font, style_primary_button, theme_colors

PADDLE_ONLY: Final = frozenset(
    {
        "mode",
        "device",
        "table",
        "formula",
        "chart",
        "doc_orientation",
        "doc_unwarping",
        "textline_orientation",
    }
)
"""[ocr] 节里只对 PaddleOCR 生效的字段。"""

ENGINE_LOCATION: Final[Location] = ("ocr", "engine")
LANGUAGE_LOCATION: Final[Location] = ("ocr", "language")

ENGINE_GLYPHS: Final = {OcrEngineKind.WINDOWS: Glyph.SYSTEM, OcrEngineKind.PADDLE: Glyph.DOCUMENT}


class OcrEngineCard(SettingCard):
    """一个 OCR 引擎的卡片：图标、名称、简介与能力，右侧是按钮或“当前引擎”标识，下方是状态。

    状态区与标题文字左对齐：状态图标加状态文字，下面依次是进度条、处理建议、添加语言的说明与可识别的语言。
    当前引擎的卡片边框用强调色。
    """

    def __init__(self, kind: OcrEngineKind, presenter: SettingsPresenter, dialogs: Dialogs) -> None:
        actions = QWidget()
        self.download_button = QPushButton()
        style_primary_button(self.download_button, Glyph.DOWNLOAD)
        self.cancel_button = QPushButton()
        self.cancel_button.setIcon(fluent_icon(Glyph.CANCEL))
        self.retry_button = QPushButton()
        self.retry_button.setIcon(fluent_icon(Glyph.REFRESH))
        self.uninstall_button = QPushButton()
        self.uninstall_button.setIcon(fluent_icon(Glyph.DELETE))
        self.use_button = QPushButton()
        self.current_badge = QWidget()
        self.badge_icon = ThemedLabel("accent")
        self.badge_icon.setFont(fluent_icon_font(TOKENS.caption_px + 2))
        self.badge_icon.setText(Glyph.CHECK)
        self.badge_label = ThemedLabel("accent", TOKENS.title_px, semibold=True)
        badge = QHBoxLayout(self.current_badge)
        badge.setContentsMargins(0, 0, 0, 0)
        badge.setSpacing(TOKENS.spacing_xs + 2)
        badge.addWidget(self.badge_icon)
        badge.addWidget(self.badge_label)
        row = QHBoxLayout(actions)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(TOKENS.spacing_s)
        for widget in (
            self.download_button,
            self.cancel_button,
            self.retry_button,
            self.uninstall_button,
            self.use_button,
            self.current_badge,
        ):
            row.addWidget(widget)
        super().__init__(actions, location=ENGINE_LOCATION, icon=ENGINE_GLYPHS.get(kind, Glyph.FONT))
        self.kind = kind
        self._presenter = presenter
        self._dialogs = dialogs
        self._current = False
        self.view: EngineView | None = None

        self.status_icon = ThemedLabel("secondary")
        self.status_icon.setFont(fluent_icon_font(TOKENS.caption_px + 2))
        self.status_icon.setFixedWidth(TOKENS.caption_px + 4)
        self.status_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(TOKENS.spacing_xs + 2)
        self.progress_bar.setRange(0, PROGRESS_STEPS)
        self.suggestion_label = self._detail_label()
        self.help_label = self._detail_label()
        self.languages_label = self._detail_label()
        status_row = QHBoxLayout()
        status_row.setContentsMargins(0, 0, 0, 0)
        status_row.setSpacing(TOKENS.spacing_s)
        status_row.addWidget(self.status_icon, 0, Qt.AlignmentFlag.AlignTop)
        status_row.addWidget(self.status_label, 1)
        self.details = QWidget()
        details = QVBoxLayout(self.details)
        details.setContentsMargins(self.icon_label.width() + TOKENS.spacing_l, 0, 0, 0)
        details.setSpacing(TOKENS.spacing_xs)
        details.addLayout(status_row)
        details.addWidget(self.progress_bar)
        for label in (self.suggestion_label, self.help_label, self.languages_label):
            details.addWidget(label)
        root = self.layout()
        if isinstance(root, QVBoxLayout):
            root.insertWidget(1, self.details)

        self.download_button.clicked.connect(lambda: presenter.install_ocr_engine(kind))
        self.retry_button.clicked.connect(lambda: presenter.install_ocr_engine(kind))
        self.cancel_button.clicked.connect(lambda: presenter.cancel_ocr_engine_install(kind))
        self.uninstall_button.clicked.connect(self._uninstall)
        self.use_button.clicked.connect(lambda: presenter.set_ocr_engine(kind))
        self.retranslate()

    @staticmethod
    def _detail_label() -> ThemedLabel:
        label = ThemedLabel("secondary", TOKENS.caption_px)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        return label

    @property
    def current(self) -> bool:
        return self._current

    def retranslate(self) -> None:
        self.download_button.setText(tr("settings.ocr_engine.download"))
        self.cancel_button.setText(tr("settings.ocr_engine.cancel"))
        self.retry_button.setText(tr("settings.ocr_engine.retry"))
        self.uninstall_button.setText(tr("settings.ocr_engine.uninstall"))
        self.use_button.setText(tr("settings.ocr_engine.use"))
        self.badge_label.setText(tr("settings.ocr_engine.current"))
        self.progress_bar.setAccessibleName(tr("settings.ocr_engine.progress", name=engine_title(self.kind)))
        self.refresh()

    def refresh(self) -> None:
        """按引擎目录与配置里的当前引擎刷新。目录里没有这个引擎时隐藏整张卡片。"""
        info = self._presenter.ocr_engine(self.kind)
        if info is None:
            self.view = None
            self.hide()
            return
        view = build_view(info, current=self._presenter.current_ocr_engine is self.kind)
        self.view = view
        self._current = view.current
        title = view.title
        self.set_texts(title, view.description)
        self.title_label.setAccessibleName(
            tr("settings.ocr_engine.current.name", name=title) if view.current else title
        )
        self.status_icon.setText(view.glyph)
        self.status_icon.set_role(view.tone.value)
        self.status_label.setText(view.status)
        self.status_label.set_role("critical" if view.tone is Tone.ERROR else "primary")
        self._set_progress(view.progress)
        for label, text in (
            (self.suggestion_label, view.suggestion),
            (self.help_label, view.help),
            (self.languages_label, view.languages),
        ):
            label.setText(text)
            label.setVisible(bool(text))
        self.download_button.setVisible(view.can_download)
        self.cancel_button.setVisible(view.can_cancel)
        self.cancel_button.setEnabled(view.cancel_enabled)
        self.retry_button.setVisible(view.can_retry)
        self.uninstall_button.setVisible(view.can_uninstall)
        self.use_button.setVisible(view.can_use)
        self.current_badge.setVisible(view.current)
        for button in (self.download_button, self.retry_button, self.uninstall_button, self.use_button):
            button.setAccessibleDescription(title)
        self.cancel_button.setAccessibleDescription(title)
        self.show()
        self.update()

    def _set_progress(self, progress: float | None) -> None:
        if progress is None:
            self.progress_bar.hide()
            return
        if progress < 0:
            self.progress_bar.setRange(0, 0)  # 进度未知：忙碌动画
        else:
            self.progress_bar.setRange(0, PROGRESS_STEPS)
            self.progress_bar.setValue(progress_steps(progress))
        self.progress_bar.show()

    def _uninstall(self) -> None:
        name = engine_title(self.kind)
        key = (
            "settings.ocr_engine.uninstall.confirm_current"
            if self._current
            else "settings.ocr_engine.uninstall.confirm"
        )
        if self._dialogs.confirm(tr("settings.confirm.title"), tr(key, name=name)):
            self._presenter.uninstall_ocr_engine(self.kind)

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        if not self._current:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(theme_colors().accent_strong)
        pen.setWidthF(1.5)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        rect = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        painter.drawRoundedRect(rect, TOKENS.card_radius, TOKENS.card_radius)


class CapturePage(SettingsPage):
    """截屏与识别：OCR 引擎、[ocr] 节与 [screenshot] 节的语言。"""

    page_id = "capture"
    icon = Glyph.SCREEN_CAPTURE

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self._language_options: list[tuple[str, str]] | None = None
        self.add_title("settings.section.ocr_engines")
        self.add_intro("settings.ocr_engines.intro")
        self.engines_missing = ThemedLabel("secondary", TOKENS.caption_px)
        self.engines_missing.setWordWrap(True)
        self.add_card(self.engines_missing)
        self.engine_cards: dict[OcrEngineKind, OcrEngineCard] = {}
        for kind in OcrEngineKind:
            card = OcrEngineCard(kind, presenter, dialogs)
            self.engine_cards[kind] = card
            self.add_card(card)
        self.engine_error = ThemedLabel("critical", TOKENS.caption_px)
        self.engine_error.setWordWrap(True)
        self.engine_error.hide()
        self.add_card(self.engine_error)
        self.add_title("settings.section.ocr")
        self.build(
            [
                FieldDef("ocr", ("enabled",)),
                FieldDef("ocr", ("mode",)),
                FieldDef("ocr", ("device",)),
                FieldDef("ocr", ("language",), widget="language_auto"),
            ]
        )
        self.add_title("settings.section.ocr_features")
        self.build(
            [
                FieldDef("ocr", ("table",)),
                FieldDef("ocr", ("formula",)),
                FieldDef("ocr", ("chart",)),
                FieldDef("ocr", ("doc_orientation",)),
                FieldDef("ocr", ("doc_unwarping",)),
                FieldDef("ocr", ("textline_orientation",)),
            ]
        )
        self.add_title("settings.section.preprocess")
        self.build(
            [
                FieldDef("ocr", ("grayscale",)),
                FieldDef(
                    "ocr",
                    ("resize_max_side",),
                    suffix="settings.suffix.pixels",
                    zero="settings.special.no_scaling",
                ),
            ]
        )
        self.add_title("settings.section.translation")
        self.build([FieldDef("screenshot", ("target_lang",), widget="language")])

    # ------------------------------------------------------------------ 查询

    def binding(self, location: Location) -> Binding | None:
        return next((binding for binding in self.bindings if binding.location == location), None)

    def paddle_only_bindings(self) -> list[Binding]:
        return [
            binding
            for binding in self.bindings
            if binding.location[0] == "ocr" and binding.location[-1] in PADDLE_ONLY
        ]

    @property
    def windows_current(self) -> bool:
        return self.presenter.current_ocr_engine is OcrEngineKind.WINDOWS

    # ------------------------------------------------------------------ 文案与刷新

    def retranslate(self) -> None:
        super().retranslate()
        self.engines_missing.setText(tr("settings.ocr_engines.missing"))
        for card in self.engine_cards.values():
            card.retranslate()
        self._language_options = None  # 语言切换后，基类已经把语言下拉框换回完整列表
        self._sync_engine_dependent()
        self._refresh_engine_error()

    def refresh_all(self) -> None:
        super().refresh_all()
        self._sync_engines()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        match topic:
            case Topic.CONFIG | Topic.OCR_ENGINES:
                self._sync_engines()
            case Topic.REJECTIONS:
                self._refresh_engine_error()
            case _:
                pass

    def claims(self, location: Location) -> bool:
        return location[: len(ENGINE_LOCATION)] == ENGINE_LOCATION or super().claims(location)

    def _sync_engines(self) -> None:
        catalog = self.presenter.ocr_engines
        self.engines_missing.setVisible(catalog is None)
        for card in self.engine_cards.values():
            card.refresh()
        self._sync_engine_dependent()

    def _refresh_engine_error(self) -> None:
        reason = self.presenter.rejection_for(ENGINE_LOCATION)
        self.engine_error.setText(tr("settings.error.prefix", reason=reason) if reason else "")
        self.engine_error.setVisible(bool(reason))

    def _sync_engine_dependent(self) -> None:
        """当前引擎是 Windows 自带 OCR 时，PaddleOCR 专属的卡片不可用；识别语言只列出 Windows 支持的语言。"""
        windows = self.windows_current
        for binding in self.paddle_only_bindings():
            binding.card.setEnabled(not windows)
            binding.set_note("settings.ocr.paddle_only" if windows else "")
        self._sync_language()

    def _sync_language(self) -> None:
        binding = self.binding(LANGUAGE_LOCATION)
        if binding is None or not isinstance(binding.editor, ChoiceEditor):
            return
        info = self.presenter.ocr_engine(OcrEngineKind.WINDOWS)
        if self.windows_current and info is not None:
            options = [("auto", tr("settings.choice.ocr.language.auto"))]
            options.extend((code, language_name(code)) for code in info.languages)
            value = self.presenter.value("ocr", ("language",))
            if isinstance(value, str) and value not in {code for code, _ in options}:
                options.append((value, tr("settings.ocr.language.unsupported", name=language_name(value))))
            note = "settings.ocr.language.windows"
        else:
            options = language_options(with_auto=True)
            note = ""
        binding.set_note(note, replace=True)  # 原说明讲的是 PaddleOCR 的识别模型，Windows 下换成自己的说明
        if options != self._language_options:
            self._language_options = options
            binding.editor.set_options(options)
            binding.refresh(self.presenter)

    def language_codes(self) -> list[str]:
        """识别语言下拉框里的取值，按显示顺序。"""
        binding = self.binding(LANGUAGE_LOCATION)
        if binding is None or not isinstance(binding.editor, ChoiceEditor):
            return []
        combo = binding.editor.combo
        return [str(combo.itemData(index)) for index in range(combo.count())]


__all__ = ["PADDLE_ONLY", "CapturePage", "OcrEngineCard"]

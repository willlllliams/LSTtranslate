"""截屏与识别的两个页面：OCR 引擎（capture.engines）与截图与识别选项（capture.options）。

截屏与识别分六个子页：OCR 引擎、Windows OCR、RapidOCR、PaddleOCR、结构归一化、截图与识别选项。
本模块是其中的 OCR 引擎页与选项页；三个引擎各自的流水线页与结构归一化页在 ``page_ocr_engine``。

OCR 引擎页从上到下是“OCR 引擎”卡片组，每个引擎一张卡片（``OcrEngineCard``），内容由
``ocr_engine_rows.build_view`` 按 ``OcrEngineCatalog`` 给出。需要下载的引擎有多个运行环境版本时，
卡片上有版本下拉框与推荐理由；已安装的引擎显示已装版本，可以“更换版本”（确认后重装运行环境，模型保留）。
已安装的引擎卡片上有“管理”，点它直接跳到该引擎的子页。引擎卡片在构造时为每种引擎各建一张，
引擎目录里没有的引擎隐藏，控件不随状态重建，焦点与 Tab 顺序不变。

选项页是 [ocr] 节的设置项（启用、识别模式、推理设备、识别语言、截图预处理）与 [screenshot] 节的目标语言。
各环节的开关（表格、公式、方向校正等）在引擎子页的流水线里，这里不重复。

当前引擎是只有文本模式的引擎（Windows 自带 OCR、RapidOCR）时：

- 只对 PaddleOCR 生效的设置项（识别模式、推理设备）整张卡片不可用，说明后面注明“仅 PaddleOCR 可用”。
  配置里的值保留，切回 PaddleOCR 时照常生效；
- 识别语言的下拉框只列出引擎报告的可识别语言（OcrEngineInfo.languages），前面加“自动”：Windows 是系统
  已安装 OCR 功能的语言，RapidOCR 是它的识别模型覆盖的语言。配置里的语言不在其中时照实显示，并注明
  当前引擎不支持。PaddleOCR 按识别语言选择识别模型，下拉框列出全部语言。
"""

from collections.abc import Callable
from typing import Final

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout, QWidget

from lst.contracts.ocr_engines import OcrEngineInfo, OcrEngineKind, OcrRuntimeVariant
from lst.ui.common.text import PROGRESS_STEPS, language_name, progress_steps
from lst.ui.i18n import tr
from lst.ui.settings import (
    strings_layout,  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
    strings_ocr,  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
)
from lst.ui.settings.cards import Binding, CardBuilder, FieldDef, language_options
from lst.ui.settings.controls import ChoiceEditor, SettingCard, ThemedLabel
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.ocr_engine_rows import (
    EngineView,
    Tone,
    VariantView,
    build_variant_view,
    build_view,
    engine_title,
    variant_name,
)
from lst.ui.settings.page_ocr_pipeline import OcrActions, ParamsGroup, section_model
from lst.ui.settings.pages import SettingsPage
from lst.ui.settings.presenter import Location, SettingsPresenter, Topic
from lst.ui.theme import (
    TOKENS,
    Glyph,
    fluent_icon,
    fluent_icon_font,
    fluent_icon_text,
    style_primary_button,
    theme_colors,
)

PADDLE_ONLY: Final = frozenset({"mode", "device"})
"""选项页上只对 PaddleOCR 生效的 [ocr] 节字段。表格、公式、方向校正等环节开关在 PaddleOCR 子页。"""

ENGINE_PAGES: Final = {
    OcrEngineKind.WINDOWS: "capture.windows",
    OcrEngineKind.RAPID: "capture.rapid",
    OcrEngineKind.PADDLE: "capture.paddle",
}
"""引擎的子页标识。"""

ENGINE_LOCATION: Final[Location] = ("ocr", "engine")
LANGUAGE_LOCATION: Final[Location] = ("ocr", "language")

ENGINE_GLYPHS: Final = {
    OcrEngineKind.WINDOWS: Glyph.SYSTEM,
    OcrEngineKind.RAPID: Glyph.LIGHTNING,
    OcrEngineKind.PADDLE: Glyph.DOCUMENT,
}

LANGUAGE_NOTES: Final = {
    OcrEngineKind.WINDOWS: "settings.ocr.language.windows",
    OcrEngineKind.RAPID: "settings.ocr.language.rapid",
}
"""只列出引擎报告的可识别语言的引擎，及识别语言卡片换上的说明。"""

TEXT_ONLY_ENGINES: Final = frozenset({OcrEngineKind.WINDOWS, OcrEngineKind.RAPID})
"""只有文本模式的引擎：PaddleOCR 专属的设置项对它们不生效。"""


class OcrEngineCard(SettingCard):
    """一个 OCR 引擎的卡片：图标、名称、简介与能力，右侧是按钮或“当前引擎”标识，下方是状态。

    状态区与标题文字左对齐：状态图标加状态文字，下面依次是进度条、处理建议、添加语言的说明、可识别的语言，
    以及运行环境版本一行（版本下拉框、“更换版本”）和推荐理由。当前引擎的卡片边框用强调色。

    commands 发送带版本的安装命令；on_manage 是点“管理”时的回调（页面用它跳到引擎的子页）。
    """

    def __init__(
        self,
        kind: OcrEngineKind,
        presenter: SettingsPresenter,
        dialogs: Dialogs,
        commands: OcrActions | None = None,
        on_manage: Callable[[OcrEngineKind], None] | None = None,
    ) -> None:
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
        self.manage_button = QPushButton()
        self.manage_button.setIcon(fluent_icon(Glyph.SLIDERS))
        self.current_badge = QWidget()
        self.badge_icon = ThemedLabel("accent")
        self.badge_icon.setFont(fluent_icon_font(TOKENS.caption_px + 2))
        self.badge_icon.setText(fluent_icon_text(Glyph.CHECK))
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
            self.manage_button,
            self.current_badge,
        ):
            row.addWidget(widget)
        super().__init__(actions, location=ENGINE_LOCATION, icon=ENGINE_GLYPHS.get(kind, Glyph.FONT))
        self.kind = kind
        self._presenter = presenter
        self._dialogs = dialogs
        self._actions = commands if commands is not None else OcrActions(presenter)
        self._on_manage = on_manage
        self._current = False
        self.view: EngineView | None = None
        self.variant_view: VariantView | None = None
        self._chosen_variant: OcrRuntimeVariant | None = None
        self._seen_installed: OcrRuntimeVariant | None = None

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
        self.variant_label = ThemedLabel("primary", TOKENS.caption_px)
        self.variant_combo = QComboBox()
        self.change_button = QPushButton()
        self.change_button.setIcon(fluent_icon(Glyph.SWITCH))
        self.variant_row = QWidget()
        variant_row = QHBoxLayout(self.variant_row)
        variant_row.setContentsMargins(0, TOKENS.spacing_xs, 0, 0)
        variant_row.setSpacing(TOKENS.spacing_s)
        variant_row.addWidget(self.variant_label)
        variant_row.addWidget(self.variant_combo)
        variant_row.addWidget(self.change_button)
        variant_row.addStretch(1)
        self.recommend_label = self._detail_label()
        self.variant_options: list[OcrRuntimeVariant] = []
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
        details.addWidget(self.variant_row)
        details.addWidget(self.recommend_label)
        root = self.layout()
        if isinstance(root, QVBoxLayout):
            root.insertWidget(1, self.details)

        self.download_button.clicked.connect(self._install)
        self.retry_button.clicked.connect(self._install)
        self.cancel_button.clicked.connect(lambda: presenter.cancel_ocr_engine_install(kind))
        self.uninstall_button.clicked.connect(self._uninstall)
        self.use_button.clicked.connect(lambda: presenter.set_ocr_engine(kind))
        self.manage_button.clicked.connect(self._manage)
        self.variant_combo.activated.connect(self._variant_chosen)
        self.change_button.clicked.connect(self._change_variant)
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
        self.manage_button.setText(tr("settings.ocr_engine.manage"))
        self.change_button.setText(tr("settings.ocr_variant.change"))
        self.variant_label.setText(tr("settings.ocr_variant.label"))
        self.badge_label.setText(tr("settings.ocr_engine.current"))
        self.progress_bar.setAccessibleName(tr("settings.ocr_engine.progress", name=engine_title(self.kind)))
        self.variant_options = []  # 选项的显示名随界面语言变化，下次刷新时重填
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
        self.status_icon.setText(fluent_icon_text(view.glyph))
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
        self.manage_button.setVisible(view.can_manage)
        self.manage_button.setAccessibleName(tr("settings.ocr_engine.manage.name", name=title))
        self.current_badge.setVisible(view.current)
        for button in (self.download_button, self.retry_button, self.uninstall_button, self.use_button):
            button.setAccessibleDescription(title)
        self.cancel_button.setAccessibleDescription(title)
        self._refresh_variants(info)
        self.show()
        self.update()

    def _refresh_variants(self, info: OcrEngineInfo) -> None:
        """运行环境版本一行。已装版本变了（装好、换了版本、卸载）时，丢掉用户在下拉框里的选择。"""
        if info.installed_variant is not self._seen_installed:
            self._seen_installed = info.installed_variant
            self._chosen_variant = None
        variant = build_variant_view(info, self._chosen_variant)
        self.variant_view = variant
        self.variant_row.setVisible(variant.visible)
        self.recommend_label.setVisible(variant.visible)
        if not variant.visible:
            return
        options = [value for value, _ in variant.options]
        texts = [text for _, text in variant.options]
        shown = [self.variant_combo.itemText(index) for index in range(self.variant_combo.count())]
        if options != self.variant_options or texts != shown:
            self.variant_combo.clear()
            for value, text in variant.options:
                self.variant_combo.addItem(text, value.value)
            self.variant_options = options
        if variant.chosen is not None:
            self.variant_combo.setCurrentIndex(options.index(variant.chosen))
        self.variant_combo.setEnabled(variant.enabled)
        self.variant_combo.setAccessibleName(tr("settings.ocr_variant.combo", name=engine_title(self.kind)))
        self.change_button.setVisible(variant.show_change)
        self.change_button.setEnabled(variant.can_change)
        self.change_button.setAccessibleDescription(engine_title(self.kind))
        help_text = tr("settings.ocr_variant.help")
        if variant.recommendation:
            help_text = tr(
                "settings.ocr_variant.recommend_help", recommendation=variant.recommendation, help=help_text
            )
        self.recommend_label.setText(help_text)

    def _chosen(self) -> OcrRuntimeVariant | None:
        """安装时用的版本：显示版本一行时是下拉框选中的版本，否则交给模块用推荐版本。"""
        variant = self.variant_view
        return variant.chosen if variant is not None and variant.visible else None

    def _install(self) -> None:
        self._actions.install(self.kind, self._chosen())

    def _variant_chosen(self, index: int) -> None:
        if 0 <= index < len(self.variant_options):
            self._chosen_variant = self.variant_options[index]
            self.refresh()

    def _change_variant(self) -> None:
        info = self._presenter.ocr_engine(self.kind)
        variant = self.variant_view
        if info is None or variant is None or not variant.can_change:
            return
        old, new = info.installed_variant, variant.chosen
        if old is None or new is None:
            return
        key = (
            "settings.ocr_variant.change.confirm_current"
            if self._current
            else "settings.ocr_variant.change.confirm"
        )
        text = tr(key, name=engine_title(self.kind), old=variant_name(old), new=variant_name(new))
        if self._dialogs.confirm(tr("settings.confirm.title"), text):
            self._actions.install(self.kind, new)

    def _manage(self) -> None:
        if self._on_manage is not None:
            self._on_manage(self.kind)

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


class CaptureEnginesPage(SettingsPage):
    """OCR 引擎：三个引擎的卡片，下载、卸载、更换版本、设为当前引擎，以及跳到引擎子页。"""

    page_id = "capture.engines"

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self.ocr_actions = OcrActions(presenter)
        self.add_title("settings.section.ocr_engines")
        self.add_intro("settings.ocr_engines.intro")
        self.engines_missing = ThemedLabel("secondary", TOKENS.caption_px)
        self.engines_missing.setWordWrap(True)
        self.add_card(self.engines_missing)
        self.engine_cards: dict[OcrEngineKind, OcrEngineCard] = {}
        for kind in OcrEngineKind:
            card = OcrEngineCard(kind, presenter, dialogs, self.ocr_actions, self.manage)
            self.engine_cards[kind] = card
            self.add_card(card)
        self.engine_error = ThemedLabel("critical", TOKENS.caption_px)
        self.engine_error.setWordWrap(True)
        self.engine_error.hide()
        self.add_card(self.engine_error)

    def manage(self, kind: OcrEngineKind) -> None:
        """点引擎卡片上的“管理”：切到该引擎的子页。"""
        self.navigate(ENGINE_PAGES[kind])

    def retranslate(self) -> None:
        super().retranslate()
        self.engines_missing.setText(tr("settings.ocr_engines.missing"))
        for card in self.engine_cards.values():
            card.retranslate()
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
        self.engines_missing.setVisible(self.presenter.ocr_engines is None)
        for card in self.engine_cards.values():
            card.refresh()

    def _refresh_engine_error(self) -> None:
        reason = self.presenter.rejection_for(ENGINE_LOCATION)
        self.engine_error.setText(tr("settings.error.prefix", reason=reason) if reason else "")
        self.engine_error.setVisible(bool(reason))


class CaptureOptionsPage(SettingsPage):
    """截图与识别选项：[ocr] 节的启用、模式、设备、语言、预处理，与 [screenshot] 节的目标语言。"""

    page_id = "capture.options"

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self._language_options: list[tuple[str, str]] | None = None
        self.add_title("settings.section.ocr")
        self.build(
            [
                FieldDef("ocr", ("enabled",)),
                FieldDef("ocr", ("mode",)),
                FieldDef("ocr", ("device",)),
                FieldDef("ocr", ("language",), widget="language_auto"),
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
        self.box_layout: ParamsGroup | None = None
        model = section_model(presenter, "overlay_layout")
        if model is not None:
            self.box_layout = ParamsGroup(
                presenter,
                builder,
                dialogs,
                "overlay_layout",
                model,
                title_key="settings.overlay_layout.title",
                intro_key="settings.overlay_layout.intro",
                owner=lambda: tr("settings.overlay_layout.title"),
                top_margin=TOKENS.spacing_l,
                text_key_prefix="settings.f.overlay_layout",
            )
            self.add_card(self.box_layout)

    # ------------------------------------------------------------------ 查询

    def binding(self, location: Location) -> Binding | None:
        return next((binding for binding in self.all_bindings() if binding.location == location), None)

    def paddle_only_bindings(self) -> list[Binding]:
        return [
            binding
            for binding in self.bindings
            if binding.location[0] == "ocr" and binding.location[-1] in PADDLE_ONLY
        ]

    @property
    def text_only_current(self) -> bool:
        """当前引擎只有文本模式（Windows 自带 OCR 或 RapidOCR）。"""
        return self.presenter.current_ocr_engine in TEXT_ONLY_ENGINES

    # ------------------------------------------------------------------ 文案与刷新

    def retranslate(self) -> None:
        super().retranslate()
        if self.box_layout is not None:
            self.box_layout.retranslate()
            for binding in self.box_layout.bindings:
                binding.retranslate()
        self._language_options = None  # 语言切换后，基类已经把语言下拉框换回完整列表
        self._sync_engine_dependent()

    def refresh_all(self) -> None:
        super().refresh_all()
        if self.box_layout is not None:
            for binding in self.box_layout.bindings:
                binding.refresh(self.presenter)
                binding.refresh_error(self.presenter)
        self._sync_engine_dependent()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        if self.box_layout is not None:
            for binding in self.box_layout.bindings:
                if topic is Topic.CONFIG:
                    binding.refresh(self.presenter)
                elif topic is Topic.REJECTIONS:
                    binding.refresh_error(self.presenter)
        if topic in (Topic.CONFIG, Topic.OCR_ENGINES):
            self._sync_engine_dependent()

    def all_bindings(self) -> list[Binding]:
        return [*self.bindings, *(self.box_layout.bindings if self.box_layout is not None else [])]

    def _sync_engine_dependent(self) -> None:
        """当前引擎只有文本模式时，PaddleOCR 专属的卡片不可用；识别语言只列出当前引擎支持的语言。"""
        text_only = self.text_only_current
        for binding in self.paddle_only_bindings():
            binding.card.setEnabled(not text_only)
            binding.set_note("settings.ocr.paddle_only" if text_only else "")
        self._sync_language()

    def _sync_language(self) -> None:
        binding = self.binding(LANGUAGE_LOCATION)
        if binding is None or not isinstance(binding.editor, ChoiceEditor):
            return
        engine = self.presenter.current_ocr_engine
        note = LANGUAGE_NOTES.get(engine) if engine is not None else None
        info = self.presenter.ocr_engine(engine) if engine is not None and note is not None else None
        if note is not None and info is not None:
            options = [("auto", tr("settings.choice.ocr.language.auto"))]
            options.extend((code, language_name(code)) for code in info.languages)
            value = self.presenter.value("ocr", ("language",))
            if isinstance(value, str) and value not in {code for code, _ in options}:
                options.append((value, tr("settings.ocr.language.unsupported", name=language_name(value))))
        else:
            options = language_options(with_auto=True)
            note = ""
        # 原说明讲的是 PaddleOCR 的识别模型；只列出引擎语言时换成该引擎自己的说明。
        binding.set_note(note, replace=True)
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


__all__ = ["ENGINE_PAGES", "PADDLE_ONLY", "CaptureEnginesPage", "CaptureOptionsPage", "OcrEngineCard"]

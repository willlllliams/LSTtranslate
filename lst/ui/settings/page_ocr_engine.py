"""截屏与识别的引擎子页与结构归一化页（需求 OCRE-07 至 OCRE-09、OCR-11、UI-05）。

``OcrEnginePage`` 是 Windows OCR（capture.windows）、RapidOCR（capture.rapid）、PaddleOCR（capture.paddle）
三个子页的共同实现，每个子页固定显示自己引擎的内容，从上到下是：

1. 运行环境卡片：引擎名、已装的运行环境版本与状态，以及可识别的语言（Windows 与 RapidOCR 由引擎报告）；
2. 引擎没有安装时，一张说明卡片和“转到 OCR 引擎页”按钮：引擎要先在“OCR 引擎”页下载；
3. 识别流水线（page_ocr_flow 的 ``EnginePipeline``）：流程图按实际执行顺序画出这个引擎的每一步，
   点开节点在下方选模型、下载与删除模型、调整归属这一步的参数；可单独开关的步骤在节点上有开关；
4. 不属于任何环节的识别参数：常用参数和“高级参数”折叠区。

引擎没有可选模型也没有参数时（Windows 自带 OCR），流程图下方另有一句说明。

``NormalizePage`` 是结构归一化页（capture.normalize），显示 ``ocr_normalize`` 节的步骤开关与参数。
"""

from PySide6.QtWidgets import QPushButton

from lst.contracts.ocr_engines import OcrEngineKind
from lst.ui.i18n import tr
from lst.ui.settings import strings_ocr  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
from lst.ui.settings.cards import Binding, CardBuilder
from lst.ui.settings.controls import SettingCard, ThemedLabel
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.nav import nav_name
from lst.ui.settings.ocr_engine_rows import build_view, engine_title
from lst.ui.settings.page_capture import ENGINE_PAGES
from lst.ui.settings.page_ocr_flow import EnginePipeline
from lst.ui.settings.page_ocr_pipeline import (
    INSTALLED_STATES,
    OcrActions,
    ParamsGroup,
    build_normalize_group,
)
from lst.ui.settings.pages import SettingsPage
from lst.ui.settings.presenter import Location, SettingsPresenter, Topic
from lst.ui.theme import TOKENS, Glyph, fluent_icon

ENGINES_PAGE = "capture.engines"
ENGINE_GLYPHS = {
    OcrEngineKind.WINDOWS: Glyph.SYSTEM,
    OcrEngineKind.RAPID: Glyph.LIGHTNING,
    OcrEngineKind.PADDLE: Glyph.DOCUMENT,
}


class OcrEnginePage(SettingsPage):
    """一个 OCR 引擎的子页：运行环境、识别流水线与识别参数。"""

    def __init__(
        self,
        kind: OcrEngineKind,
        presenter: SettingsPresenter,
        builder: CardBuilder,
        dialogs: Dialogs,
    ) -> None:
        self.kind = kind
        self.page_id = ENGINE_PAGES[kind]
        super().__init__(presenter, builder, dialogs)
        self.add_title("settings.section.ocr_runtime")
        self.runtime_card = SettingCard(None, icon=ENGINE_GLYPHS.get(kind, Glyph.FONT))
        self.add_card(self.runtime_card)
        self.goto_button = QPushButton()
        self.goto_button.setIcon(fluent_icon(Glyph.DOWNLOAD))
        self.goto_button.clicked.connect(lambda: self.navigate(ENGINES_PAGE))
        self.install_card = SettingCard(self.goto_button, icon=Glyph.WARNING)
        self.add_card(self.install_card)
        self.note_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.note_label.setWordWrap(True)
        self.add_card(self.note_label)
        self.add_title("settings.section.ocr_pipeline")
        self.flow_intro = self.add_intro("settings.flow.ocr.intro")
        self.intro_label = self.add_intro("settings.ocr_pipeline.intro")
        self.empty_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.empty_label.setWordWrap(True)
        self.add_card(self.empty_label)
        self.pipeline = EnginePipeline(
            kind, presenter, builder, dialogs, OcrActions(presenter), lambda page_id: self.navigate(page_id)
        )
        self.add_card(self.pipeline)

    # ------------------------------------------------------------------ 查询

    def all_bindings(self) -> list[Binding]:
        return [*self.bindings, *self.pipeline.bindings]

    def claims(self, location: Location) -> bool:
        return self.pipeline.claims(location) or super().claims(location)

    @property
    def installed(self) -> bool:
        """引擎的运行环境可用（不需要下载，或已经装好）。"""
        info = self.presenter.ocr_engine(self.kind)
        return info is not None and (not info.needs_download or info.state in INSTALLED_STATES)

    # ------------------------------------------------------------------ 文案与刷新

    def retranslate(self) -> None:
        super().retranslate()
        self.pipeline.retranslate()
        self.goto_button.setText(tr("settings.ocr_engine_page.goto"))
        self.empty_label.setText(tr("settings.ocr_engine_page.empty", name=engine_title(self.kind)))
        self._sync()

    def refresh_all(self) -> None:
        super().refresh_all()
        self.pipeline.refresh_values(self.presenter)
        self.pipeline.refresh_errors()
        self._sync()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        match topic:
            case Topic.CONFIG:
                self.pipeline.refresh_values(self.presenter)
                self._sync()
            case Topic.OCR_ENGINES | Topic.WORKERS:
                self.pipeline.refresh()
                self._sync()
            case Topic.REJECTIONS:
                self.pipeline.refresh_errors()
            case _:
                pass

    def _sync(self) -> None:
        name = engine_title(self.kind)
        info = self.presenter.ocr_engine(self.kind)
        self.runtime_card.set_texts(name, self._runtime_text())
        installed = self.installed
        self.install_card.setVisible(not installed)
        if not installed:
            key = (
                "settings.ocr_engine_page.not_installed"
                if info is not None
                else "settings.ocr_engines.missing"
            )
            self.install_card.set_texts(
                tr("settings.ocr_engine_page.not_installed.title", name=name), tr(key, name=name)
            )
        notes: list[str] = []
        if installed and self.presenter.current_ocr_engine is not self.kind:
            notes.append(tr("settings.ocr_pipeline.not_current", name=name))
        self.note_label.setText("\n".join(notes))
        self.note_label.setVisible(bool(notes))
        self.empty_label.setVisible(not self.pipeline.has_content)
        self.intro_label.setVisible(bool(self.pipeline.visible_stages()))

    def _runtime_text(self) -> str:
        info = self.presenter.ocr_engine(self.kind)
        if info is None:
            return tr("settings.ocr_engines.missing")
        view = build_view(info, current=self.presenter.current_ocr_engine is self.kind)
        return "\n".join(part for part in (view.status, view.languages) if part)


class NormalizePage(SettingsPage):
    """结构归一化（需求 OCR-11）：``ocr_normalize`` 节的步骤开关与参数。"""

    page_id = "capture.normalize"

    def __init__(self, presenter: SettingsPresenter, builder: CardBuilder, dialogs: Dialogs) -> None:
        super().__init__(presenter, builder, dialogs)
        self.normalize: ParamsGroup | None = build_normalize_group(presenter, builder, dialogs, top_margin=0)
        if self.normalize is not None:
            self.add_card(self.normalize)
        self.missing_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.missing_label.setWordWrap(True)
        self.missing_label.setVisible(self.normalize is None)
        self.add_card(self.missing_label)

    def all_bindings(self) -> list[Binding]:
        return [*self.bindings, *(self.normalize.bindings if self.normalize is not None else [])]

    def retranslate(self) -> None:
        super().retranslate()
        self.missing_label.setText(tr("settings.ocr_normalize.missing", name=nav_name(self.page_id)))
        for binding in self.all_bindings():
            binding.retranslate()
        if self.normalize is not None:
            self.normalize.retranslate()

    def refresh_all(self) -> None:
        super().refresh_all()
        for binding in self.all_bindings():
            binding.refresh(self.presenter)
            binding.refresh_error(self.presenter)
        if self.normalize is not None:
            self.normalize.sync()

    def refresh(self, topic: Topic) -> None:
        super().refresh(topic)
        match topic:
            case Topic.CONFIG:
                for binding in self.all_bindings():
                    binding.refresh(self.presenter)
                if self.normalize is not None:
                    self.normalize.sync()
            case Topic.REJECTIONS:
                for binding in self.all_bindings():
                    binding.refresh_error(self.presenter)
            case _:
                pass


__all__ = ["NormalizePage", "OcrEnginePage"]

"""OCR 引擎子页的识别流水线：流程图加节点面板（需求 OCRE-08、OCRE-09、OCR-11、UI-05）。

``EnginePipeline`` 是一个引擎的流水线。上面是流程图（flow.FlowView），节点与顺序取自 flow_specs：
PaddleOCR 按 [ocr].mode 在版面模式与文本模式两套流程之间切换，RapidOCR 与 Windows OCR 各一套。
流程图下方是被点开的节点的面板，面板里放的是已有的设置组件：

- 环节节点（key 是 ``OcrStage`` 的值）：``StageSection``（选模型、下载与删除），以及归属这个环节的参数
  （``ParamsGroup``，参数按 ``param_stage`` 归属）；
- 截图预处理：说明与“转到截图与识别选项”按钮（缩放上限与灰度在那一页）；
- 结构归一化的清洗、拆分、合并段落、合并相邻正文块：说明与“转到结构归一化”按钮；
- 其余节点（系统 OCR、拼成文本行、转成版面块、解码校验、切分成段）：只有说明。

节点上的开关：可单独开关的环节绑定 [ocr] 节的同名字段，切换时点一下环节卡片标题行里的开关
（``StageSection.toggle``），修改经同一个 Binding 提交；结构归一化节点绑定 [ocr_normalize] 节的步骤开关，
经展示器提交。配置里没有对应字段时节点上不显示开关。关闭的节点画成被跳过。
PaddleOCR 的图表识别只在显卡上运行（paddle_backend.GPU_ONLY_FEATURES）：OCR 进程报告的设备是 CPU 时，
图表节点即使开着也画成被跳过，状态写“CPU 上跳过”。

不属于任何环节的参数（线程数等）在流程图下方单独成组，标题是“识别参数”。
"""

from collections.abc import Callable
from typing import Final

from pydantic import BaseModel
from PySide6.QtWidgets import QAbstractButton, QPushButton, QVBoxLayout, QWidget

from lst.contracts.ocr_engines import OcrEngineKind, OcrStage
from lst.ui.i18n import tr
from lst.ui.settings import strings_flow  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
from lst.ui.settings.cards import Binding, CardBuilder, FieldDef
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.flow import FlowView, NodeNote, NodeState
from lst.ui.settings.flow_specs import (
    AGGREGATE,
    ASSEMBLE,
    CLEAN,
    CONVERT,
    DECODE,
    MERGE_BLOCKS,
    NORMALIZE_SWITCHES,
    OCR_FLOW_KEYS,
    PREPROCESS,
    SEGMENTS,
    SPLIT,
    SYSTEM_OCR,
    ocr_flow,
)
from lst.ui.settings.ocr_engine_rows import engine_title
from lst.ui.settings.ocr_pipeline_rows import (
    NORMALIZE_SECTION,
    STAGE_GLYPHS,
    StageView,
    build_stage_views,
    section_of,
    stage_title,
)
from lst.ui.settings.page_ocr_pipeline import (
    INSTALLED_STATES,
    STAGE_TOGGLES,
    TOGGLE_SECTION,
    OcrActions,
    ParamsGroup,
    StageSection,
    param_stage,
    section_model,
    stage_fields,
)
from lst.ui.settings.presenter import Location, SettingsPresenter
from lst.ui.theme import TOKENS, Glyph, fluent_icon

NOTE_GLYPHS: Final[dict[str, str]] = {
    PREPROCESS: Glyph.PICTURE,
    SYSTEM_OCR: Glyph.SYSTEM,
    ASSEMBLE: Glyph.LINES,
    CONVERT: Glyph.CODE,
    DECODE: Glyph.CHECK,
    CLEAN: Glyph.FILTER,
    SPLIT: Glyph.CUT,
    AGGREGATE: Glyph.MERGE,
    MERGE_BLOCKS: Glyph.LAYOUT,
    SEGMENTS: Glyph.LIST,
}
"""非环节节点的图标；环节节点用 ocr_pipeline_rows.STAGE_GLYPHS。"""

OCR_PROCESS_NODES: Final = frozenset({SYSTEM_OCR, ASSEMBLE, CONVERT})
MAIN_PROCESS_NODES: Final = frozenset({DECODE, SEGMENTS})
GPU_ONLY_STAGES: Final = frozenset({OcrStage.CHART})
"""只在显卡上运行的环节，对应 paddle_backend.GPU_ONLY_FEATURES。"""

_STAGES: Final = {stage.value: stage for stage in OcrStage}


def _goto_button(glyph: str) -> QPushButton:
    button = QPushButton()
    button.setIcon(fluent_icon(glyph))
    return button


class EnginePipeline(QWidget):
    """一个引擎的流程图、节点面板，以及不属于任何环节的参数。

    构造顺序就是焦点链的顺序：先流程图的节点，再按 OCR_FLOW_KEYS 的顺序建各节点的面板，最后是通用参数。
    navigate 用来跳到另一个页面（页面标识），由所在页面转交窗口的跳转函数。
    """

    def __init__(
        self,
        kind: OcrEngineKind,
        presenter: SettingsPresenter,
        builder: CardBuilder,
        dialogs: Dialogs,
        actions: OcrActions,
        navigate: Callable[[str], None] = lambda _page: None,
    ) -> None:
        super().__init__()
        self.kind = kind
        self._presenter = presenter
        self._navigate = navigate
        self.section = section_of(kind)
        model = section_model(presenter, self.section)
        self.fields = stage_fields(model)
        self.flow = FlowView(OCR_FLOW_KEYS)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(TOKENS.card_gap)
        layout.addWidget(self.flow)

        self.stage_cards: dict[OcrStage, StageSection] = {}
        self.toggles: dict[OcrStage, Binding] = {}
        self.stage_params: dict[OcrStage, ParamsGroup] = {}
        self.notes: dict[str, NodeNote] = {}
        self._views: dict[OcrStage, StageView] = {}
        self._pending: dict[str, bool] = {}
        switchable = STAGE_TOGGLES.get(kind, frozenset())
        for key in OCR_FLOW_KEYS:
            panel = QWidget()
            box = QVBoxLayout(panel)
            box.setContentsMargins(0, 0, 0, 0)
            box.setSpacing(TOKENS.card_gap)
            stage = _STAGES.get(key)
            if stage is not None:
                toggle = (
                    builder.build(FieldDef(TOGGLE_SECTION, (stage.value,))) if stage in switchable else None
                )
                if toggle is not None:
                    self.toggles[stage] = toggle
                card = StageSection(kind, stage, presenter, actions, dialogs, toggle)
                card.hide()
                self.stage_cards[stage] = card
                box.addWidget(card)
                if model is not None:
                    group = self._stage_group(stage, presenter, builder, dialogs, model)
                    if group is not None:
                        self.stage_params[stage] = group
                        box.addWidget(group)
            else:
                note = self._note(key)
                self.notes[key] = note
                box.addWidget(note)
            self.flow.add_panel(key, panel)

        self.params: ParamsGroup | None = None
        if model is not None:
            params = ParamsGroup(
                presenter,
                builder,
                dialogs,
                self.section,
                model,
                title_key="settings.ocr_params.title",
                owner=lambda: engine_title(kind),
                top_margin=TOKENS.spacing_l,
                include=lambda param: param_stage(param) is None,
            )
            if params.has_params:
                self.params = params
                layout.addWidget(params)
            else:
                params.deleteLater()
        self.flow.chart.toggled.connect(self._on_toggled)
        self._sync_items()

    # ---- 构建

    def _stage_group(
        self,
        stage: OcrStage,
        presenter: SettingsPresenter,
        builder: CardBuilder,
        dialogs: Dialogs,
        model: type[BaseModel],
    ) -> ParamsGroup | None:
        """归属 stage 的参数；没有时返回 None。"""
        group = ParamsGroup(
            presenter,
            builder,
            dialogs,
            self.section,
            model,
            title_key="settings.flow.stage_params",
            title=lambda: tr("settings.flow.stage_params", stage=stage_title(stage)),
            owner=lambda: tr(
                "settings.flow.stage_params.owner", name=engine_title(self.kind), stage=stage_title(stage)
            ),
            top_margin=TOKENS.spacing_s,
            include=lambda param: param_stage(param) is stage,
            button_keys="settings.flow.params",
        )
        if group.has_params:
            return group
        group.deleteLater()
        return None

    def _note(self, key: str) -> NodeNote:
        button: QPushButton | None = None
        if key == PREPROCESS:
            button = _goto_button(Glyph.SETTINGS)
            button.clicked.connect(lambda: self._navigate("capture.options"))
        elif key in NORMALIZE_SWITCHES:
            button = _goto_button(Glyph.SETTINGS)
            button.clicked.connect(lambda: self._navigate("capture.normalize"))
        return NodeNote(NOTE_GLYPHS[key], button)

    # ---- 查询

    @property
    def bindings(self) -> list[Binding]:
        """参数卡片的绑定（各环节的参数与通用参数）。环节开关在各环节的标题行里，见 ``toggles``。"""
        groups = [*self.stage_params.values(), *([self.params] if self.params is not None else [])]
        return [binding for group in groups for binding in group.bindings]

    @property
    def param_groups(self) -> list[ParamsGroup]:
        return [*self.stage_params.values(), *([self.params] if self.params is not None else [])]

    def param_binding(self, path: tuple[str, ...]) -> Binding | None:
        """任意一组参数里路径为 path 的绑定。"""
        return next((b for group in self.param_groups if (b := group.binding(path)) is not None), None)

    @property
    def structure(self) -> bool:
        """PaddleOCR 按版面模式识别。其他引擎总是文本模式。"""
        return self.kind is OcrEngineKind.PADDLE and self._presenter.value("ocr", ("mode",)) != "text"

    def views(self) -> list[StageView]:
        info = self._presenter.ocr_engine(self.kind)
        configured = {
            stage: self._presenter.value(self.section, (field,)) for stage, field in self.fields.items()
        }
        installed = info is not None and (not info.needs_download or info.state in INSTALLED_STATES)
        return build_stage_views(info, fields=self.fields, configured=configured, installed=installed)

    @property
    def has_content(self) -> bool:
        """有可选的模型或参数。流程图总是显示，这一项决定页面是否显示“没有可选的模型和参数”。"""
        return self.params is not None or bool(self.stage_params) or bool(self.visible_stages())

    def visible_stages(self) -> list[OcrStage]:
        """引擎有的环节，按 OcrStage 的定义顺序。"""
        cards = self.stage_cards
        return [stage for stage in OcrStage if stage in cards and cards[stage].view is not None]

    # ---- 刷新

    def refresh(self) -> None:
        views = {view.stage: view for view in self.views()}
        self._views = views
        for stage, card in self.stage_cards.items():
            card.set_view(views.get(stage))
            card.refresh_error()
        for group in self.param_groups:
            group.sync()
        self._sync_items()
        self._sync_nodes()

    def retranslate(self) -> None:
        for binding in [*self.toggles.values(), *self.bindings]:
            binding.retranslate()
        for group in self.param_groups:
            group.retranslate()
        self.flow.chart.setAccessibleName(tr("settings.flow.ocr.name", name=engine_title(self.kind)))
        for key, note in self.notes.items():
            note.set_texts(tr(f"settings.flow.node.{key}.title"), tr(f"settings.flow.node.{key}.desc"))
            if note.button is not None:
                text = "settings.flow.goto_options" if key == PREPROCESS else "settings.flow.goto_normalize"
                note.button.setText(tr(text))
        self.refresh()

    def refresh_values(self, presenter: SettingsPresenter) -> None:
        """配置变了：开关与参数显示新值，环节按开关变灰或恢复，流程图按识别模式与开关重画。"""
        self._pending.clear()
        for binding in [*self.toggles.values(), *self.bindings]:
            binding.refresh(presenter)
        self.refresh()

    def refresh_errors(self) -> None:
        self._pending.clear()
        for binding in self.bindings:
            binding.refresh_error(self._presenter)
        for card in self.stage_cards.values():
            card.refresh_error()
        self._sync_nodes()

    def claims(self, location: Location) -> bool:
        if len(location) < 2:
            return False
        if location[0] == self.section:
            return location[1] in self.fields.values() or any(b.matches(location) for b in self.bindings)
        return any(binding.matches(location) for binding in self.toggles.values())

    def first_focus(self) -> QWidget | None:
        current = self.flow.chart.current
        return self.flow.chart.nodes[current] if current is not None else None

    # ---- 流程图

    def _sync_items(self) -> None:
        fallback = OcrStage.RECOGNITION.value if self.kind is not OcrEngineKind.WINDOWS else SYSTEM_OCR
        self.flow.set_items(ocr_flow(self.kind, structure=self.structure), fallback=fallback)

    def _switch_value(self, key: str) -> bool | None:
        """节点上开关的值；节点没有开关时为 None。用户刚切换、配置还没确认的值优先。"""
        if key in self._pending:
            return self._pending[key]
        stage = _STAGES.get(key)
        if stage is not None:
            toggle = self.toggles.get(stage)
            return None if toggle is None else toggle.editor.json_value() is True
        field = NORMALIZE_SWITCHES.get(key)
        if field is None or self._presenter.spec(NORMALIZE_SECTION, (field,)) is None:
            return None
        return self._presenter.value(NORMALIZE_SECTION, (field,)) is True

    def _ocr_device(self) -> str | None:
        statuses = self._presenter.worker_statuses
        status = statuses.statuses.get("ocr") if statuses is not None else None
        if status is None or self._presenter.current_ocr_engine is not self.kind:
            return None
        return status.versions.get("device") or None

    def node_state(self, key: str) -> NodeState:
        """节点的显示内容。"""
        switch = self._switch_value(key)
        off = switch is False
        separator = tr("settings.flow.status.separator")
        stage = _STAGES.get(key)
        if stage is not None:
            view = self._views.get(stage)
            chosen = view.selected if view is not None else None
            name = chosen.name if chosen is not None else (view.default_name if view is not None else "")
            status = name or tr("settings.flow.status.no_model")
            skipped = off
            if off:
                status = tr("settings.flow.status.off")
            elif stage in GPU_ONLY_STAGES and self._ocr_device() == "cpu":
                status, skipped = tr("settings.flow.status.gpu_only"), True
            return NodeState(stage_title(stage), status, STAGE_GLYPHS[stage], switch, skipped)
        title = tr(f"settings.flow.node.{key}.title")
        if key == PREPROCESS:
            side = self._presenter.value("ocr", ("resize_max_side",))
            resize = (
                tr("settings.flow.status.resize", size=side)
                if isinstance(side, int) and side > 0
                else tr("settings.flow.status.no_resize")
            )
            gray = self._presenter.value("ocr", ("grayscale",)) is True
            status = separator.join(
                (resize, tr("settings.flow.status.gray" if gray else "settings.flow.status.color"))
            )
        elif key == SYSTEM_OCR:
            status = tr("settings.flow.status.system_ocr")
        elif key in OCR_PROCESS_NODES:
            status = tr("settings.flow.status.ocr_process")
        elif key in MAIN_PROCESS_NODES:
            status = tr("settings.flow.status.main_process")
        elif switch is not None:
            status = tr("settings.flow.status.on" if switch else "settings.flow.status.off")
        else:
            status = tr("settings.flow.status.main_process")
        return NodeState(title, status, NOTE_GLYPHS[key], switch, off)

    def _sync_nodes(self) -> None:
        for key in self.flow.chart.order:
            self.flow.chart.set_state(key, self.node_state(key))

    def _on_toggled(self, key: str, value: bool) -> None:
        """节点上的开关被切换：环节开关点一下环节卡片里的开关，结构归一化开关直接提交。"""
        stage = _STAGES.get(key)
        if stage is not None:
            toggle = self.toggles.get(stage)
            switch = toggle.editor.focus_widget() if toggle is not None else None
            if not isinstance(switch, QAbstractButton):
                return
            if switch.isChecked() != value:
                switch.click()
        else:
            field = NORMALIZE_SWITCHES.get(key)
            if field is None:
                return
            self._pending[key] = value
            self._presenter.set_value(NORMALIZE_SECTION, (field,), value)
        self._sync_nodes()


__all__ = ["GPU_ONLY_STAGES", "NOTE_GLYPHS", "EnginePipeline"]

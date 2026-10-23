"""OCR 引擎子页的组件：环节卡片、参数分组与结构归一化（OCRE-07 至 OCRE-09、OCR-11）。

每个引擎有自己的子页（Windows OCR、RapidOCR、PaddleOCR），子页用流程图展示识别流水线（page_ocr_flow 的
``EnginePipeline``），点开流程图上的节点，下方显示这里的组件：

1. 各环节一个 ``StageSection``，列出这个环节的可选模型（``ModelRow``）：
   单选按钮选用，旁边是状态、大小、语言与“默认”标记，右边是下载、取消、删除按钮。选用写
   ``UpdateConfig {"ocr_<引擎>": {"<环节>_model": 模型 id}}``；未下载的模型不能选用。
   删除选用中的模型时先确认，再把该环节改回默认模型，然后发 ``DeleteOcrModel``；
2. 可以单独打开关闭的环节（文档方向校正、文档去畸变、文本行方向、表格、公式、图表、版面区域检测），
   标题行右侧有开关，绑定 ``[ocr]`` 节里与环节同名的字段。关闭后模型选择与下载变灰，说明里注明
   “已关闭，识别时跳过这一步”；配置节里没有对应字段时不显示开关；
3. 该引擎配置节的参数（``ParamsGroup``）：常用参数直接显示，json_schema_extra 带 advanced 的参数放在
   “高级参数”折叠区里；每项右边有“恢复默认值”，标题行右边的“恢复默认”把这一组参数恢复为默认值（选用的模型不变）。
   参数按 ``param_stage`` 归到环节，挂在对应节点的面板里；不属于任何环节的参数在流程图下方单独成组。

“结构归一化”页（``ocr_normalize`` 节）用同一个 ``ParamsGroup``：步骤开关与参数，同样分常用与高级。
步骤的参数在步骤关闭时不可用（字段名以步骤开关的字段名加下划线开头，或嵌套模型里 enabled 之外的字段）。

环节与参数卡片在构造时全部建好，只切换显示，参数卡片的焦点与 Tab 顺序不随状态变化。
模型行随引擎目录里的模型列表重建；重建后把新行接回焦点链里环节卡片的位置，Tab 顺序与屏幕上的顺序一致。

设置窗口的展示器还没有发送模型命令与带版本的安装命令的方法，``OcrActions`` 直接通过展示器的总线发送，
与展示器其他命令一样先检查窗口是否已关闭并记日志。
"""

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from itertools import pairwise
from typing import Final, cast

from pydantic import BaseModel
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QHBoxLayout,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from lst.contracts.messages import Command
from lst.contracts.ocr_engines import (
    CancelOcrModelDownload,
    DeleteOcrModel,
    DownloadOcrModel,
    InstallOcrEngine,
    OcrEngineKind,
    OcrEngineState,
    OcrRuntimeVariant,
    OcrStage,
)
from lst.contracts.worker_protocol import JsonValue
from lst.ui.common.text import PROGRESS_STEPS, progress_steps
from lst.ui.i18n import current_language, tr
from lst.ui.settings.cards import Binding, CardBuilder, FieldDef
from lst.ui.settings.controls import SettingCard, ThemedLabel
from lst.ui.settings.dialogs import Dialogs
from lst.ui.settings.flow_specs import PARAM_STAGE_PREFIXES
from lst.ui.settings.ocr_engine_rows import Tone
from lst.ui.settings.ocr_pipeline_rows import (
    MODEL_FIELD_SUFFIX,
    NORMALIZE_SECTION,
    STAGE_GLYPHS,
    ModelView,
    StageView,
    section_of,
)
from lst.ui.settings.presenter import Location, SettingsPresenter
from lst.ui.settings.schema import FieldSpec, Kind, model_field_specs, nested_model
from lst.ui.theme import TOKENS, Glyph, fluent_icon, fluent_icon_font, theme_colors

CHEVRON_RIGHT: Final = "\ue76c"
"""Fluent 图标里向右的箭头：折叠区收起。"""
CHEVRON_DOWN: Final = "\ue70d"
"""向下的箭头：折叠区展开。"""

PARAM_KEY_PREFIX: Final = "settings.ocrf"
"""引擎配置节参数的文案键前缀：settings.ocrf.<字段>.title 与 .desc。"""

INSTALLED_STATES: Final = frozenset(
    {OcrEngineState.INSTALLED, OcrEngineState.LOADING, OcrEngineState.READY, OcrEngineState.ERROR}
)
"""可以下载模型的引擎状态。出错的引擎也算：运行环境还在，出错的多半是识别进程。"""

PROGRESS_WIDTH: Final = 120

TOGGLE_SECTION: Final = "ocr"
"""环节开关所在的配置节；开关的字段名与环节的取值相同。"""

STAGE_TOGGLES: Final[Mapping[OcrEngineKind, frozenset[OcrStage]]] = {
    OcrEngineKind.RAPID: frozenset({OcrStage.TEXTLINE_ORIENTATION}),
    OcrEngineKind.PADDLE: frozenset(
        {
            OcrStage.DOC_ORIENTATION,
            OcrStage.DOC_UNWARPING,
            OcrStage.TEXTLINE_ORIENTATION,
            OcrStage.REGION,
            OcrStage.TABLE,
            OcrStage.FORMULA,
            OcrStage.CHART,
        }
    ),
}
"""各引擎里可以单独打开关闭的环节。Windows 自带 OCR 没有。"""


# ---------------------------------------------------------------- 命令


class OcrActions:
    """OCR 引擎管理的命令：带版本的安装与四个模型命令。都只在用户点按钮时发出（需求 PRV-01）。"""

    def __init__(self, presenter: SettingsPresenter) -> None:
        self._presenter = presenter

    def install(self, kind: OcrEngineKind, variant: OcrRuntimeVariant | None) -> None:
        """安装（也用于重试和更换版本）。variant 为 None 时由模块用推荐版本。"""
        if variant is None:
            self._presenter.install_ocr_engine(kind)
            return
        self._send(InstallOcrEngine(kind, variant), "安装 OCR 引擎 %s（%s 版）", kind.value, variant.value)

    def download_model(self, kind: OcrEngineKind, model_id: str) -> None:
        self._send(DownloadOcrModel(kind, model_id), "下载 OCR 模型 %s/%s", kind.value, model_id)

    def cancel_model(self, kind: OcrEngineKind, model_id: str) -> None:
        self._send(CancelOcrModelDownload(kind, model_id), "取消下载 OCR 模型 %s/%s", kind.value, model_id)

    def delete_model(self, kind: OcrEngineKind, model_id: str) -> None:
        self._send(DeleteOcrModel(kind, model_id), "删除 OCR 模型 %s/%s", kind.value, model_id)

    def _send(self, command: Command, message: str, *args: object) -> None:
        presenter = self._presenter
        if presenter._closed:
            return
        presenter._logger.info("设置窗口：" + message, *args)
        presenter._bus.send(command)


def section_model(presenter: SettingsPresenter, section: str) -> type[BaseModel] | None:
    """组合根注入的一节配置的模型；没有注入时为 None。"""
    return presenter._sections.get(section)


def stage_fields(model: type[BaseModel] | None) -> dict[OcrStage, str]:
    """配置节里各环节的选模型字段：json_schema_extra 带 ocr_stage 的字段，或名为 <环节>_model 的字段。"""
    fields: dict[OcrStage, str] = {}
    if model is None:
        return fields
    stages = {stage.value: stage for stage in OcrStage}
    for name, spec in model_field_specs(model).items():
        if not _is_model_field(name, spec):
            continue
        stage = stages.get(spec.ocr_stage or name.removesuffix(MODEL_FIELD_SUFFIX))
        if stage is not None:
            fields.setdefault(stage, name)
    return fields


# ---------------------------------------------------------------- 参数


@dataclass(frozen=True, slots=True)
class Param:
    """配置节里的一个参数。switch 是控制它的步骤开关的路径，没有时为 None。"""

    path: tuple[str, ...]
    spec: FieldSpec
    advanced: bool
    switch: tuple[str, ...] | None = None


_MODEL_KINDS: Final = frozenset({Kind.TEXT, Kind.OPTIONAL_TEXT})


def _is_model_field(name: str, spec: FieldSpec) -> bool:
    """选模型的字段：名为 <环节>_model，或带 ocr_stage 的文字字段。

    带 ocr_stage 的数值、开关不是选模型字段，而是归属该环节的参数（见 param_stage）。
    """
    return name.endswith(MODEL_FIELD_SUFFIX) or (bool(spec.ocr_stage) and spec.kind in _MODEL_KINDS)


def param_stage(param: "Param") -> OcrStage | None:
    """参数归属的环节：json_schema_extra 的 ocr_stage 优先，
    否则按字段名前缀（flow_specs.PARAM_STAGE_PREFIXES）；
    嵌套模型里的字段按外层字段名判断。不属于任何环节时为 None。"""
    stages = {stage.value: stage for stage in OcrStage}
    if param.spec.ocr_stage in stages:
        return stages[param.spec.ocr_stage]
    name = param.path[0]
    return next((stage for prefix, stage in PARAM_STAGE_PREFIXES if name.startswith(prefix)), None)


def collect_params(model: type[BaseModel], *, steps: bool = False) -> list[Param]:
    """配置节里界面能编辑的参数，按声明顺序；选模型字段与没有通用控件的字段除外。

    步骤开关决定哪些参数在开关关闭时不可用：

    - 嵌套的模型展开成它的字段，其中名为 enabled 的开关控制同一模型里的其他字段；
    - steps 为真（结构归一化这类“开关加参数”依次排列的节）：顶层的每个开关控制它后面、下一个开关之前的参数；
    - 否则顶层的开关 X 只控制名字以 X_ 开头的字段（引擎参数里的开关多半是独立的选项）。

    嵌套模型本身带 advanced 时，它的字段都算高级参数。
    """
    specs = model_field_specs(model)
    last_switch: str | None = None
    switches = [name for name, spec in specs.items() if spec.kind is Kind.BOOL]
    params: list[Param] = []
    for name, spec in specs.items():
        if _is_model_field(name, spec):
            continue
        inner = nested_model(model, name)
        if inner is not None:
            inner_specs = model_field_specs(inner)
            enabled = inner_specs.get("enabled")
            has_switch = enabled is not None and enabled.kind is Kind.BOOL
            for sub, sub_spec in inner_specs.items():
                if _is_model_field(sub, sub_spec) or sub_spec.kind is Kind.OTHER:
                    continue
                switch = (name, "enabled") if has_switch and sub != "enabled" else None
                params.append(Param((name, sub), sub_spec, spec.advanced or sub_spec.advanced, switch))
            continue
        if spec.kind is Kind.OTHER:
            continue
        if spec.kind is Kind.BOOL:
            last_switch = name
            owner = None
        elif steps:
            owner = last_switch
        else:
            owner = next((s for s in switches if s != name and name.startswith(f"{s}_")), None)
        params.append(Param((name,), spec, spec.advanced, (owner,) if owner is not None else None))
    return params


def _param_icon(spec: FieldSpec) -> str:
    match spec.kind:
        case Kind.BOOL:
            return Glyph.POWER
        case Kind.CHOICE:
            return Glyph.LIST
        case _:
            return Glyph.SLIDERS


def _defaults_patch(params: list[Param]) -> dict[str, JsonValue]:
    """全部参数的默认值，按路径嵌套：{"a": 1, "step": {"enabled": False}}。"""
    patch: dict[str, JsonValue] = {}
    for param in params:
        node = patch
        for key in param.path[:-1]:
            child = node.setdefault(key, {})
            node = cast(dict[str, JsonValue], child)
        node[param.path[-1]] = param.spec.default
    return patch


def _title_label() -> ThemedLabel:
    return ThemedLabel("primary", TOKENS.title_px, semibold=True)


def _caption(role: str = "secondary") -> ThemedLabel:
    label = ThemedLabel(role, TOKENS.caption_px)
    label.setWordWrap(True)
    return label


class ParamsGroup(QWidget):
    """一节配置的参数：标题行（标题与“恢复默认”）、说明、常用参数的卡片，以及“高级参数”折叠区。

    top_margin 是与上一组之间的距离；整组隐藏时这段距离也一起消失。
    include 只保留一部分参数（例如归属某个环节的）；title 给出标题时代替 title_key；
    button_keys 是“恢复默认”“展开”“收起”按钮文案键的前缀，同一页上有几组参数时各组用不同的助记键。
    """

    def __init__(
        self,
        presenter: SettingsPresenter,
        builder: CardBuilder,
        dialogs: Dialogs,
        section: str,
        model: type[BaseModel],
        *,
        title_key: str,
        intro_key: str = "",
        owner: Callable[[], str],
        top_margin: int = 0,
        steps: bool = False,
        include: Callable[[Param], bool] | None = None,
        title: Callable[[], str] | None = None,
        button_keys: str = "settings.ocr_params",
        text_key_prefix: str = PARAM_KEY_PREFIX,
    ) -> None:
        super().__init__()
        self.section = section
        self._presenter = presenter
        self._dialogs = dialogs
        self._title_key = title_key
        self._title = title
        self._button_keys = button_keys
        self._text_key_prefix = text_key_prefix
        self._intro_key = intro_key
        self._owner = owner
        params = collect_params(model, steps=steps)
        self.params = [param for param in params if include is None or include(param)]
        self.bindings: list[Binding] = []
        self._switches: list[tuple[Binding, tuple[str, ...]]] = []

        self.title_label = _title_label()
        self.reset_button = QPushButton()
        self.reset_button.setIcon(fluent_icon(Glyph.REFRESH))
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(TOKENS.spacing_s)
        header.addWidget(self.title_label, 1, Qt.AlignmentFlag.AlignBottom)
        header.addWidget(self.reset_button, 0, Qt.AlignmentFlag.AlignBottom)
        self.intro_label = _caption()
        self.intro_label.setVisible(bool(intro_key))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, top_margin, 0, 0)
        layout.setSpacing(TOKENS.card_gap)
        layout.addLayout(header)
        layout.addSpacing(TOKENS.card_gap)
        layout.addWidget(self.intro_label)

        common = [param for param in self.params if not param.advanced]
        advanced = [param for param in self.params if param.advanced]
        for param in common:
            self._add(builder, param, layout)

        # 不用可勾选按钮：勾选状态的按钮画成强调色填充，看起来像主要操作。展开与否记在 advanced_open 里。
        self.advanced_button = QPushButton()
        self.advanced_open = False
        self.advanced_card = SettingCard(self.advanced_button, icon=Glyph.SETTINGS)
        self.advanced_box = QWidget()
        box = QVBoxLayout(self.advanced_box)
        # 折叠区里的卡片向右缩进，看得出它们属于上面的“高级参数”。
        box.setContentsMargins(TOKENS.spacing_xl, 0, 0, 0)
        box.setSpacing(TOKENS.card_gap)
        layout.addWidget(self.advanced_card)
        layout.addWidget(self.advanced_box)
        for param in advanced:
            self._add(builder, param, box)
        self.advanced_count = len(advanced)
        self.advanced_card.setVisible(bool(advanced))
        self.advanced_box.hide()

        self.reset_button.clicked.connect(self._reset_all)
        self.advanced_button.clicked.connect(lambda: self.set_advanced_open(not self.advanced_open))

    def _add(self, builder: CardBuilder, param: Param, layout: QVBoxLayout) -> None:
        definition = FieldDef(
            self.section,
            param.path,
            text_key=".".join((self._text_key_prefix, *param.path)),
            schema_texts=True,
            details=True,
            resettable=True,
            icon=_param_icon(param.spec),
        )
        binding = builder.build(definition)
        if binding is None:
            return
        self.bindings.append(binding)
        layout.addWidget(binding.card)
        if param.switch is not None:
            self._switches.append((binding, param.switch))

    # ---- 查询

    def binding(self, path: tuple[str, ...]) -> Binding | None:
        location: Location = (self.section, *path)
        return next((binding for binding in self.bindings if binding.location == location), None)

    @property
    def has_params(self) -> bool:
        return bool(self.bindings)

    # ---- 文案与刷新

    def retranslate(self) -> None:
        self.title_label.setText(self._title() if self._title is not None else tr(self._title_key))
        if self._intro_key:
            self.intro_label.setText(tr(self._intro_key))
        self.reset_button.setText(tr(f"{self._button_keys}.reset"))
        self.reset_button.setAccessibleName(tr("settings.ocr_params.reset.name", name=self._owner()))
        self.advanced_card.set_texts(
            tr("settings.ocr_params.advanced.title"),
            tr("settings.ocr_params.advanced.count", count=self.advanced_count),
        )
        self.advanced_button.setAccessibleName(tr("settings.ocr_params.advanced.title"))
        self.set_advanced_open(self.advanced_open)

    def sync(self) -> None:
        """步骤开关关闭时，它的参数不可用，说明后面注明要先打开开关。"""
        for binding, switch in self._switches:
            on = self._presenter.value(self.section, switch) is True
            binding.card.setEnabled(on)
            binding.set_note("" if on else "settings.ocr_normalize.step_off")

    def set_advanced_open(self, shown: bool) -> None:
        """展开或收起“高级参数”折叠区。"""
        self.advanced_open = shown
        self.advanced_box.setVisible(shown)
        state = "settings.ocr_params.expanded" if shown else "settings.ocr_params.collapsed"
        self.advanced_button.setAccessibleDescription(tr(state))
        self.advanced_button.setIcon(fluent_icon(CHEVRON_DOWN if shown else CHEVRON_RIGHT))
        self.advanced_button.setText(
            tr(f"{self._button_keys}.collapse" if shown else f"{self._button_keys}.expand")
        )

    def _reset_all(self) -> None:
        text = tr("settings.ocr_params.reset.confirm", name=self._owner())
        if not self._dialogs.confirm(tr("settings.confirm.title"), text):
            return
        for binding in self.bindings:
            binding.editor.reload(binding.default)
            self._presenter.clear_rejection(binding.location)
        self._presenter.set_value(self.section, (), _defaults_patch(self.params))


# ---------------------------------------------------------------- 模型行与环节卡片


class AccentRadio(QRadioButton):
    """选中时用应用强调色画圆点的单选按钮。

    windows11 样式在运行中切换深浅色后，单选按钮这一类控件的调色板里强调色停留在旧值（深色下变成接近底色的
    #1e1e1e），选中的圆点几乎看不见。这里在创建时与每次调色板、主题变化时，把强调色设成 ui.theme 的强调色。
    """

    def __init__(self) -> None:
        super().__init__()
        self._applying = False
        self._apply_accent()

    def _apply_accent(self) -> None:
        # 每次都显式设置：只有显式设置过的颜色，才不会在控件加入窗口时被这一类控件的调色板盖掉。
        accent = theme_colors().accent
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Accent, accent)
        palette.setColor(QPalette.ColorRole.Highlight, accent)
        self._applying = True
        try:
            self.setPalette(palette)
        finally:
            self._applying = False

    def changeEvent(self, event: QEvent) -> None:
        if not self._applying and event.type() in (
            QEvent.Type.PaletteChange,
            QEvent.Type.ApplicationPaletteChange,
            QEvent.Type.ThemeChange,
        ):
            self._apply_accent()
        super().changeEvent(event)


class ModelRow(QWidget):
    """一个可选模型：单选按钮（模型名），下面是状态与大小、语言、默认标记；右边是进度条与按钮。"""

    def __init__(self, view: ModelView, group: QButtonGroup) -> None:
        super().__init__()
        self.model_id = view.id
        self.view = view
        self.radio = AccentRadio()
        group.addButton(self.radio)
        self.status_icon = ThemedLabel("secondary")
        self.status_icon.setFont(fluent_icon_font(TOKENS.caption_px))
        self.status_label = ThemedLabel("secondary", TOKENS.caption_px)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedSize(PROGRESS_WIDTH, TOKENS.spacing_xs + 2)
        self.progress_bar.setRange(0, PROGRESS_STEPS)
        self.download_button = QPushButton()
        self.download_button.setIcon(fluent_icon(Glyph.DOWNLOAD))
        self.cancel_button = QPushButton()
        self.cancel_button.setIcon(fluent_icon(Glyph.CANCEL))
        self.delete_button = QPushButton()
        self.delete_button.setIcon(fluent_icon(Glyph.DELETE))

        style = self.radio.style()
        indicator = style.pixelMetric(QStyle.PixelMetric.PM_ExclusiveIndicatorWidth, None, self.radio)
        spacing = style.pixelMetric(QStyle.PixelMetric.PM_RadioButtonLabelSpacing, None, self.radio)
        info = QHBoxLayout()
        info.setContentsMargins(indicator + spacing, 0, 0, 0)
        info.setSpacing(TOKENS.spacing_xs)
        info.addWidget(self.status_icon)
        info.addWidget(self.status_label, 1)
        left = QVBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(2)
        left.addWidget(self.radio)
        left.addLayout(info)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, TOKENS.spacing_xs, 0, TOKENS.spacing_xs)
        row.setSpacing(TOKENS.spacing_s)
        row.addLayout(left, 1)
        row.addWidget(self.progress_bar, 0, Qt.AlignmentFlag.AlignVCenter)
        for button in (self.download_button, self.cancel_button, self.delete_button):
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignVCenter)
        self.update_view(view)

    def focus_widgets(self) -> list[QWidget]:
        """这一行接受 Tab 焦点的控件，按屏幕上从左到右的顺序。"""
        return [self.radio, self.download_button, self.cancel_button, self.delete_button]

    def update_view(self, view: ModelView) -> None:
        self.view = view
        self.radio.setText(view.name)
        self.radio.setChecked(view.selected)
        self.radio.setEnabled(view.selectable)
        self.radio.setToolTip("" if view.selectable else tr("settings.ocr_model.needs_download"))
        separator = tr("settings.ocr_model.separator")
        self.status_icon.setText(view.glyph)
        self.status_icon.set_role(view.tone.value)
        self.status_label.setText(f"{view.status}{separator}{view.meta}" if view.meta else view.status)
        self.status_label.set_role("critical" if view.tone is Tone.ERROR else "secondary")
        description = view.status if not view.meta else f"{view.status}{separator}{view.meta}"
        if not view.selectable and not view.selected:
            description = f"{description}{separator}{tr('settings.ocr_model.needs_download')}"
        self.radio.setAccessibleDescription(description)
        if view.progress is None:
            self.progress_bar.hide()
        else:
            if view.progress < 0:
                self.progress_bar.setRange(0, 0)
            else:
                self.progress_bar.setRange(0, PROGRESS_STEPS)
                self.progress_bar.setValue(progress_steps(view.progress))
            self.progress_bar.show()
        self.progress_bar.setAccessibleName(tr("settings.ocr_model.progress", name=view.name))
        self.download_button.setText(
            tr("settings.ocr_model.retry" if view.retry else "settings.ocr_model.download")
        )
        self.download_button.setVisible(view.can_download)
        self.cancel_button.setText(tr("settings.ocr_model.cancel"))
        self.cancel_button.setVisible(view.can_cancel)
        self.delete_button.setText(tr("settings.ocr_model.delete"))
        self.delete_button.setVisible(view.show_delete)
        self.delete_button.setEnabled(not view.delete_blocked)
        self.delete_button.setToolTip(tr("settings.ocr_model.delete.blocked") if view.delete_blocked else "")
        for button in (self.download_button, self.cancel_button, self.delete_button):
            button.setAccessibleName(
                tr("settings.ocr_model.action.name", action=button.text(), name=view.name)
            )


class StageSection(SettingCard):
    """流水线的一个环节：图标、环节名与说明，下面是可选模型的列表；可选环节的标题行右侧有开关。

    一个环节的设置集中在这个组件里（开关、模型选择、下载与删除），不依赖所在页面，可以放到页面的任何位置。

    模型列表随引擎目录变化：模型 id 的列表不变时只更新各行，变了才重建。重建的行在焦点链里接在
    ``anchor`` 之后。anchor 是一个不显示、但接受 Tab 焦点的占位控件（Qt 只能把控件接在接受焦点的控件后面），
    它在构造时就排进了焦点链里环节卡片的位置；不显示的控件不会被 Tab 选中。

    toggle 是环节开关的绑定（``[ocr]`` 节里的同名字段），没有时环节总是启用。
    """

    def __init__(
        self,
        kind: OcrEngineKind,
        stage: OcrStage,
        presenter: SettingsPresenter,
        actions: OcrActions,
        dialogs: Dialogs,
        toggle: Binding | None = None,
    ) -> None:
        content = QWidget()
        super().__init__(content, location=(section_of(kind),), stacked=True, icon=STAGE_GLYPHS[stage])
        self.content = content
        self.kind = kind
        self.stage = stage
        self.toggle = toggle
        self._presenter = presenter
        self._actions = actions
        self._dialogs = dialogs
        self.view: StageView | None = None
        self.rows: list[ModelRow] = []
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.anchor = QWidget()
        self.anchor.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.anchor.hide()
        self._rows_box = QVBoxLayout()
        self._rows_box.setContentsMargins(0, 0, 0, 0)
        self._rows_box.setSpacing(0)
        self.empty_label = _caption()
        layout = QVBoxLayout(self.content)
        layout.setContentsMargins(self.icon_label.width() + TOKENS.spacing_l, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.anchor)
        layout.addLayout(self._rows_box)
        layout.addWidget(self.empty_label)
        if toggle is not None:
            top = self._top_row.layout()
            if isinstance(top, QBoxLayout):
                top.addWidget(toggle.editor, 0, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    @property
    def enabled_stage(self) -> bool:
        """环节是否打开。没有开关的环节总是打开。"""
        toggle = self.toggle
        if toggle is None:
            return True
        section, *path = toggle.location
        return self._presenter.value(str(section), tuple(str(part) for part in path)) is True

    @property
    def section(self) -> str:
        return section_of(self.kind)

    def set_view(self, view: StageView | None) -> None:
        """显示一个环节；None 表示这个引擎没有这个环节，整张卡片隐藏。"""
        self.view = view
        if view is None:
            self.hide()
            return
        self.location = (self.section, view.field) if view.field else (self.section,)
        enabled = self.enabled_stage
        description = view.description
        if not enabled:
            separator = "" if current_language() == "zh-Hans" else " "
            description = f"{description}{separator}{tr('settings.ocr_stage.off')}"
        self.set_texts(view.title, description)
        self.content.setEnabled(enabled)
        if self.toggle is not None:
            self.toggle.editor.focus_widget().setAccessibleName(
                tr("settings.ocr_stage.toggle", stage=view.title)
            )
        self.content.setAccessibleName(tr("settings.ocr_pipeline.models", stage=view.title))
        if tuple(row.model_id for row in self.rows) != view.ids:
            self._rebuild(view)
        else:
            # 先取消独占，才能在没有任何一行选中时把全部单选按钮清掉。
            self.group.setExclusive(False)
            for row, model in zip(self.rows, view.models, strict=True):
                row.update_view(model)
            self.group.setExclusive(True)
        self.empty_label.setText(tr("settings.ocr_pipeline.no_models"))
        self.empty_label.setVisible(not view.models)
        self.show()

    def _rebuild(self, view: StageView) -> None:
        for row in self.rows:
            self.group.removeButton(row.radio)
            self._rows_box.removeWidget(row)
            # 先从窗口里拿掉：deleteLater 要等事件循环才真正删除，在那之前旧行还在焦点链里。
            row.hide()
            row.setParent(None)
            row.deleteLater()
        self.rows = []
        self.group.setExclusive(False)
        for model in view.models:
            row = ModelRow(model, self.group)
            row.radio.clicked.connect(lambda _checked=False, r=row: self._choose(r))
            row.download_button.clicked.connect(lambda _checked=False, r=row: self._download(r))
            row.cancel_button.clicked.connect(lambda _checked=False, r=row: self._cancel(r))
            row.delete_button.clicked.connect(lambda _checked=False, r=row: self._delete(r))
            self._rows_box.addWidget(row)
            self.rows.append(row)
        self.group.setExclusive(True)
        chain: list[QWidget] = [self.anchor]
        for row in self.rows:
            chain.extend(row.focus_widgets())
        for first, second in pairwise(chain):
            QWidget.setTabOrder(first, second)

    def focus_widgets(self) -> Iterator[QWidget]:
        for row in self.rows:
            yield from row.focus_widgets()

    def refresh_error(self) -> None:
        field = self.view.field if self.view is not None else ""
        reason = self._presenter.rejection_for((self.section, field)) if field else None
        if reason is None and self.toggle is not None:
            reason = self._presenter.rejection_for(self.toggle.location)
        self.set_error(reason)

    def _choose(self, row: ModelRow) -> None:
        view = self.view
        if view is None or not view.field or row.view.selected or not row.view.selectable:
            return
        self._presenter.set_value(self.section, (view.field,), row.model_id)

    def _download(self, row: ModelRow) -> None:
        self._actions.download_model(self.kind, row.model_id)

    def _cancel(self, row: ModelRow) -> None:
        self._actions.cancel_model(self.kind, row.model_id)

    def _delete(self, row: ModelRow) -> None:
        view = self.view
        if view is None or row.view.delete_blocked:
            return
        title = tr("settings.confirm.title")
        if row.view.selected:
            text = tr(
                "settings.ocr_model.delete.confirm_selected",
                name=row.view.name,
                stage=view.title,
                default=view.default_name,
            )
            if not self._dialogs.confirm(title, text):
                return
            if view.field and view.default_id is not None:
                self._presenter.set_value(self.section, (view.field,), view.default_id)
        elif not self._dialogs.confirm(title, tr("settings.ocr_model.delete.confirm", name=row.view.name)):
            return
        self._actions.delete_model(self.kind, row.model_id)


def build_normalize_group(
    presenter: SettingsPresenter,
    builder: CardBuilder,
    dialogs: Dialogs,
    *,
    top_margin: int = TOKENS.group_gap - TOKENS.card_gap,
) -> ParamsGroup | None:
    """结构归一化分组（需求 OCR-11）。组合根没有注入 ocr_normalize 节时返回 None。"""
    model = section_model(presenter, NORMALIZE_SECTION)
    if model is None:
        return None
    return ParamsGroup(
        presenter,
        builder,
        dialogs,
        NORMALIZE_SECTION,
        model,
        title_key="settings.ocr_normalize.title",
        intro_key="settings.ocr_normalize.intro",
        owner=lambda: tr("settings.ocr_normalize.title"),
        top_margin=top_margin,
        steps=True,
    )


__all__ = [
    "CHEVRON_DOWN",
    "CHEVRON_RIGHT",
    "INSTALLED_STATES",
    "STAGE_TOGGLES",
    "ModelRow",
    "OcrActions",
    "Param",
    "ParamsGroup",
    "StageSection",
    "build_normalize_group",
    "collect_params",
    "param_stage",
    "section_model",
    "stage_fields",
]

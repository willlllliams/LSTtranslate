"""OCR 流水线的显示内容（需求 OCRE-08）：把引擎的 ``OcrModelInfo`` 按环节整理成模型行。

不依赖 Qt，可以在没有 QApplication 的测试里检查。

一个引擎的配置节是 ``ocr_<引擎>``（例如 ``ocr_paddle``）。节里选模型的字段名为 ``<环节>_model``，
json_schema_extra 带 ``{"ocr_stage": "<环节>"}``；取值是模型 id。环节按 ``OcrStage`` 的定义顺序排列，
只显示引擎目录里有模型或配置节里有选模型字段的环节。

某个环节当前选用的模型：配置里的值是这个环节的某个模型 id 时就是它（配置是唯一的事实来源，选用后立即反映）；
否则取引擎目录标为 selected 的模型；都没有时取默认模型。

模型行的状态与按钮：

| 状态 | 文字 | 按钮 | 能否选用 |
| --- | --- | --- | --- |
| 未下载 | 未下载 | 下载（引擎已安装时） | 否 |
| 排队、下载中 | 等待下载、下载进度 | 取消 | 否 |
| 已下载 | 已下载（自带的写“随引擎自带”） | 删除（自带的没有） | 是 |
| 出错 | 下载失败及原因 | 重试（引擎已安装时） | 否 |

删除选用中的模型时，界面先把该环节改回默认模型再删除；默认模型本身正在选用，或默认模型还没有下载时，
“删除”不可用（``delete_blocked``）。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from lst.contracts.ocr_engines import (
    OcrEngineErrorCode,
    OcrEngineInfo,
    OcrEngineKind,
    OcrModelInfo,
    OcrModelState,
    OcrStage,
)
from lst.ui.common.text import clamp_fraction, format_size, language_name
from lst.ui.i18n import error_text, tr
from lst.ui.settings.ocr_engine_rows import Tone
from lst.ui.theme import Glyph

MODEL_FIELD_SUFFIX: Final = "_model"
"""选模型字段名的后缀：<环节>_model。"""

NORMALIZE_SECTION: Final = "ocr_normalize"
"""结构归一化的配置节（需求 OCR-11）。"""

STAGE_GLYPHS: Final[Mapping[OcrStage, str]] = {
    OcrStage.DETECTION: Glyph.SEARCH,
    OcrStage.RECOGNITION: Glyph.FONT,
    OcrStage.TEXTLINE_ORIENTATION: Glyph.ITALIC,
    OcrStage.DOC_ORIENTATION: Glyph.REFRESH,
    OcrStage.DOC_UNWARPING: Glyph.UNWARP,
    OcrStage.LAYOUT: Glyph.LAYOUT,
    OcrStage.REGION: Glyph.LIBRARY,
    OcrStage.TABLE: Glyph.TABLE,
    OcrStage.FORMULA: Glyph.CALCULATOR,
    OcrStage.CHART: Glyph.CHART,
}

_BUSY: Final = frozenset({OcrModelState.QUEUED, OcrModelState.DOWNLOADING})


def section_of(kind: OcrEngineKind) -> str:
    """引擎的配置节名，例如 ocr_paddle。"""
    return f"ocr_{kind.value}"


def stage_title(stage: OcrStage) -> str:
    return tr(f"settings.ocr_stage.{stage.value}.title")


def stage_description(stage: OcrStage) -> str:
    return tr(f"settings.ocr_stage.{stage.value}.desc")


@dataclass(frozen=True, slots=True)
class ModelView:
    """一行模型的显示内容。文字都已按当前界面语言填好。

    progress：None 不显示进度条，-1 进度未知（忙碌动画），否则在 0 到 1 之间。
    selectable：已下载、而且配置节里有这个环节的选模型字段，才能选用。
    """

    id: str
    name: str
    meta: str
    status: str
    tone: Tone
    glyph: str
    progress: float | None
    selected: bool
    default: bool
    selectable: bool
    can_download: bool
    retry: bool
    can_cancel: bool
    show_delete: bool
    delete_blocked: bool


@dataclass(frozen=True, slots=True)
class StageView:
    """一个环节：名称、说明、选模型字段（配置节里没有时为空串）、默认模型与各模型行。"""

    stage: OcrStage
    title: str
    description: str
    field: str
    default_id: str | None
    default_name: str
    models: tuple[ModelView, ...]

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(model.id for model in self.models)

    def model(self, model_id: str) -> ModelView | None:
        return next((model for model in self.models if model.id == model_id), None)

    @property
    def selected(self) -> ModelView | None:
        return next((model for model in self.models if model.selected), None)


def model_meta(model: OcrModelInfo) -> str:
    """模型的大小、语言与“默认”标记，用“ · ”分隔。“随引擎自带”写在状态里，这里不重复。"""
    parts: list[str] = []
    if model.size_bytes is not None:
        parts.append(format_size(model.size_bytes))
    if model.languages:
        separator = tr("settings.ocr_engine.feature.separator")
        languages = separator.join(language_name(code) for code in model.languages)
        parts.append(tr("settings.ocr_model.languages", languages=languages))
    if model.default:
        parts.append(tr("settings.ocr_model.default"))
    return tr("settings.ocr_model.separator").join(parts)


def model_status(model: OcrModelInfo) -> tuple[str, Tone, str]:
    """状态文字、语气与图标。"""
    match model.state:
        case OcrModelState.NOT_DOWNLOADED:
            return tr("settings.ocr_model.status.not_downloaded"), Tone.NORMAL, Glyph.DOWNLOAD
        case OcrModelState.QUEUED:
            return tr("settings.ocr_model.status.queued"), Tone.NORMAL, Glyph.RECENT
        case OcrModelState.DOWNLOADING:
            if model.progress is None:
                return tr("settings.ocr_model.status.downloading_unknown"), Tone.ACTIVE, Glyph.DOWNLOAD
            percent = int(clamp_fraction(model.progress) * 100)
            return tr("settings.ocr_model.status.downloading", percent=percent), Tone.ACTIVE, Glyph.DOWNLOAD
        case OcrModelState.DOWNLOADED:
            key = "settings.ocr_model.bundled" if model.bundled else "settings.ocr_model.status.downloaded"
            return tr(key), Tone.NORMAL, Glyph.COMPLETED
        case OcrModelState.ERROR:
            reason, _ = error_text(model.error_code, OcrEngineErrorCode.INTERNAL)
            return tr("settings.ocr_model.status.error", reason=reason), Tone.ERROR, Glyph.ERROR


def _progress(model: OcrModelInfo) -> float | None:
    if model.state is not OcrModelState.DOWNLOADING:
        return None
    return -1.0 if model.progress is None else clamp_fraction(model.progress)


def _selected_id(models: list[OcrModelInfo], configured: object) -> str | None:
    ids = {model.id for model in models}
    if isinstance(configured, str) and configured in ids:
        return configured
    marked = next((model.id for model in models if model.selected), None)
    if marked is not None:
        return marked
    return next((model.id for model in models if model.default), None)


def build_stage_views(
    info: OcrEngineInfo | None,
    *,
    fields: Mapping[OcrStage, str],
    configured: Mapping[OcrStage, object],
    installed: bool,
) -> list[StageView]:
    """一个引擎的各环节。

    fields 是“环节 → 选模型字段名”（配置节里有的），configured 是这些字段在配置里的当前值；
    installed 表示引擎已安装，可以下载模型。
    """
    models = info.models if info is not None else ()
    views: list[StageView] = []
    for stage in OcrStage:
        stage_models = [model for model in models if model.stage is stage]
        field = fields.get(stage, "")
        if not stage_models and not field:
            continue
        selected = _selected_id(stage_models, configured.get(stage))
        default = next((model for model in stage_models if model.default), None)
        default_ready = default is not None and default.state is OcrModelState.DOWNLOADED
        rows: list[ModelView] = []
        for model in stage_models:
            status, tone, glyph = model_status(model)
            downloaded = model.state is OcrModelState.DOWNLOADED
            is_selected = model.id == selected
            blocked = is_selected and (default is None or default.id == model.id or not default_ready)
            missing = model.state in (OcrModelState.NOT_DOWNLOADED, OcrModelState.ERROR)
            rows.append(
                ModelView(
                    id=model.id,
                    name=model.name,
                    meta=model_meta(model),
                    status=status,
                    tone=tone,
                    glyph=glyph,
                    progress=_progress(model),
                    selected=is_selected,
                    default=model.default,
                    selectable=downloaded and bool(field),
                    can_download=installed and missing and not model.bundled,
                    retry=model.state is OcrModelState.ERROR,
                    can_cancel=model.state in _BUSY,
                    show_delete=downloaded and not model.bundled,
                    delete_blocked=blocked,
                )
            )
        views.append(
            StageView(
                stage=stage,
                title=stage_title(stage),
                description=stage_description(stage),
                field=field,
                default_id=default.id if default is not None else None,
                default_name=default.name if default is not None else "",
                models=tuple(rows),
            )
        )
    return views


__all__ = [
    "MODEL_FIELD_SUFFIX",
    "NORMALIZE_SECTION",
    "STAGE_GLYPHS",
    "ModelView",
    "StageView",
    "build_stage_views",
    "model_meta",
    "model_status",
    "section_of",
    "stage_description",
    "stage_title",
]
